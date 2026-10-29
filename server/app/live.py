"""Live guided capture: the phone streams frames while it scans, the server works out what is still missing and sends a small map back.
The contract is docs/live-guide.md. Offline stays the default-safe path: capture on the phone never waits for this, and a scan that
lost the connection is completed later with POST /api/scans/{id}/frames (or the whole scan with the ordinary batch upload).

Files of a live scan (the same layout every other consumer already reads): photos/pNNN.jpg + pNNN.depth, photos.json, meta.json,
plus live.json (which frames the server has, session number, finished or not) and, once a second session exists (patching a scan),
sessions.json (the raw frames and transform of every session after the first)."""
import asyncio
import json
import os
import queue
import shutil
import struct
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import anyio.to_thread
import cv2
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from PIL import Image

from . import coverage, frames, registration
from .meshscan import _depth_map
from .photos import _mat

router = APIRouter(tags=["live"])
ctx = SimpleNamespace(data=lambda: None, hub=None, convert=None, loop=None)
SESSIONS: dict[str, "Session"] = {}
SESSIONS_LOCK = threading.Lock()
BUILDING: set[str] = set()  # scans with a build running in this process (a "building" scan not in here was left by a stopped server)
RESUME_TRIES = 1            # a build interrupted by a restart is started again this many times, then marked error (no crash loop)
DELETED: set[str] = set()   # scans deleted while this server runs: nothing may recreate their folders (ids are never reused)
FINISH_LOCK = threading.Lock()   # the socket, the web page and the janitor may all ask to finish the same scan at once
MAP_EVERY = 1.5            # seconds between map messages (only sent when something changed)
IDLE_S = float(os.environ.get("LIVE_IDLE_S", "600"))
MAX_FRAME_BYTES = 12 * 1024 * 1024
ATTEMPT_AT = 6             # a patch session tries to register itself to the old scan after this many frames, then every this many more
LIVE_KINDS = ("room", "sweep")
FLUSH_S = 2.0              # photos.json, sessions.json and live.json are rewritten at most this often while frames stream in (and
FLUSH_SHARE = 4            # every max(16, frames / this) frames), not after every frame: frames.jsonl carries each frame until then
JOURNAL = "frames.jsonl"
STATE_OF = {"capturing": "capturing", "building": "building", "ok": "ready"}   # meta.status -> the `state` message; anything else is "error"


def setup(data_fn, hub, convert) -> None:
    """main.py hands over the data folder (a function, tests move it), the viewers hub and the conversion."""
    ctx.data, ctx.hub, ctx.convert = data_fn, hub, convert
    hub.live_maps = {}


async def startup() -> None:
    ctx.loop = asyncio.get_running_loop()
    try:
        recover_builds()
    except Exception as e:
        print(f"live recover: {type(e).__name__}: {e}", flush=True)
    asyncio.create_task(_janitor())


# ---------------------------------------------------------------- small helpers

def _scan_dir(scan_id: str) -> Path:
    d = ctx.data() / scan_id
    if not scan_id.isalnum() or not d.is_dir() or scan_id in DELETED:
        raise HTTPException(404, "scan not found")
    return d


def _read(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write(p: Path, obj) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


def _meta(d: Path) -> dict:
    return _read(d / "meta.json", {})


def _save_meta(d: Path, meta: dict) -> None:
    _write(d / "meta.json", meta)


def _photos(d: Path) -> list[dict]:
    m = _read(d / "photos.json", {})
    return m.get("photos", []) if isinstance(m, dict) else m


def _save_photos(d: Path, photos: list[dict]) -> None:
    _write(d / "photos.json", {"version": 2, "photos": photos})


def _journal_lines(d: Path) -> list[dict]:
    """frames.jsonl: one line per frame (or bare frame number) stored since photos.json was last rewritten. A line cut off by a
    crash is skipped: its frame was never acknowledged, so the phone sends it again."""
    try:
        text = (d / JOURNAL).read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _sessions(d: Path) -> dict:
    return _read(d / "sessions.json", {"sessions": [{"index": 1, "aligned": True, "transform": None}]})


def _notify(msg: dict) -> None:
    """Tell every open web page (from any thread)."""
    if ctx.loop is not None and ctx.hub is not None:
        asyncio.run_coroutine_threadsafe(ctx.hub.broadcast(msg), ctx.loop)


def _sharpness(jpeg: bytes) -> float | None:
    """Laplacian variance of a small greyscale copy: only compared with the scan's own other frames, so the size just has to be steady."""
    try:
        g = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_4)      # decoded at 1/4 size: much faster
        if g is None:
            return None
        s = 320 / max(g.shape)
        if s < 1:
            g = cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        return float(cv2.Laplacian(g, cv2.CV_64F).var())
    except Exception:
        return None


def _depth_array(photo: dict, data: bytes) -> np.ndarray | None:
    w, h = photo.get("depth_width"), photo.get("depth_height")
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or len(data) != w * h * 4:
        return None
    return np.frombuffer(data, dtype="<f4").reshape(h, w)


class ScanGone(ValueError):
    """The scan was deleted: its frames are refused, never stored (that would recreate the folder)."""


class BadFrame(ValueError):
    """A frame no resend can fix: it is dropped but still counts as received, so the cumulative ack moves past it."""


def _check_photo(photo) -> None:
    """Refuse an entry every consumer would choke on later (it would break the build of the whole scan, not just this frame)."""
    if not isinstance(photo, dict):
        raise BadFrame("photo must be an object")
    try:
        if _mat(photo["transform"]).shape != (4, 4) or np.array(photo["intrinsics"], dtype=float).shape != (3, 3):
            raise ValueError
        if float(photo["width"]) <= 0 or float(photo["height"]) <= 0:
            raise ValueError
        float(photo.get("timestamp") or 0)
    except (KeyError, TypeError, ValueError):
        raise BadFrame("photo needs transform (4x4), intrinsics (3x3), width, height and a numeric timestamp")


def _state(d: Path) -> str:
    return STATE_OF.get(_meta(d).get("status"), "error")


# ---------------------------------------------------------------- the session

class Session:
    """One capture session of one scan: stores frames, feeds the coverage engine, registers a patch session to the old scan.
    `index` given = a brand-new session (the offline /add route); else the state is restored from live.json."""

    def __init__(self, d: Path, scan_id: str, index: int | None = None, patch: bool | None = None, kind: str | None = None, engine: bool = True):
        self.d, self.id = d, scan_id
        self.lock = threading.RLock()
        self.reg_lock = threading.Lock()
        st = _read(d / "live.json", {}) if index is None else {}
        self.kind = kind or st.get("kind") or "room"
        self.index = index if index is not None else st.get("session", 1)
        self.patch = patch if patch is not None else bool(st.get("patch", False))
        self.got: set[int] = set(range(1, int(st.get("acked", 0)) + 1)) | set(st.get("got", []))
        self.finished = index is None and (bool(st.get("finish", False)) or _meta(d).get("status") != "capturing")
        self.deleted = False
        self.acked = 0
        self.sess_file = _sessions(d)
        mine = next((s for s in self.sess_file["sessions"] if s["index"] == self.index), None)
        if mine is None:
            mine = {"index": self.index, "aligned": not self.patch, "transform": None, "frames": []}
            self.sess_file["sessions"].append(mine)
        self.mine = mine
        self.T = np.array(mine["transform"], dtype=float).reshape(4, 4) if mine.get("transform") else None
        self.photos: list[dict] = _photos(d)       # photos.json as it is now: written to disk by flush()
        self.journal = d / JOURNAL
        self.dirty = False
        self.since_flush = 0
        self.last_flush = time.time()
        recovered = self._replay()
        self._recount()
        photos = self.photos
        self.stamps: set[float] = set()
        nums = [0]
        for p in photos + [f for s in self.sess_file["sessions"] for f in s.get("frames", [])]:
            nums.append(int("".join(c for c in Path(p["file"]).stem if c.isdigit()) or 0))
            if p.get("timestamp"):
                self.stamps.add(round(float(p["timestamp"]), 4))
        self.next_num = max(nums) + 1
        have = {p["file"] for p in photos}
        self.pending: list[dict] = [f for f in mine.get("frames", []) if f["file"] not in have] if self.patch and not self.aligned else []
        self.out: deque = deque()
        self.ws: WebSocket | None = None
        self.last_seen = time.time()
        self.send_lock = asyncio.Lock()      # one message at a time on the socket
        self.map_lock = asyncio.Lock()       # one map at a time: computed, numbered and sent in that order (see send_map)
        self.jobs: queue.Queue = queue.Queue()
        self.fed = 0               # pending frames the worker has put into pending_engine
        self.seq = 0
        self.last_attempt = 0
        self.engine = coverage.CoverageEngine(self.kind) if engine else None
        self.pending_engine = coverage.CoverageEngine(self.kind) if engine and self.patch and not self.aligned else None
        self.last_meta_count = -1
        self.last_meta_t = 0.0
        if recovered:
            with self.lock:
                self._flush()
        if engine:
            threading.Thread(target=self._work, daemon=True).start()

    # -- state

    @property
    def aligned(self) -> bool:
        return (not self.patch) or self.T is not None

    @property
    def view(self) -> np.ndarray | None:
        return np.linalg.inv(self.T) if self.patch and self.T is not None else None

    def _recount(self) -> None:
        k = 0
        while (k + 1) in self.got:
            k += 1
        self.acked = k

    def _save_state(self, **extra) -> None:
        """live.json and sessions.json. Call with self.lock held: every live.json write of a live session goes through here."""
        keep = {k: v for k, v in _read(self.d / "live.json", {}).items() if k in ("prev_status", "cancelled", "resume_scan")}
        _write(self.d / "live.json", {**keep, **extra, "kind": self.kind, "session": self.index, "patch": self.patch,
                                      "acked": self.acked, "got": sorted(n for n in self.got if n > self.acked), "finish": self.finished})
        self.mine["aligned"] = self.aligned
        self.mine["transform"] = None if self.T is None else self.T.tolist()
        _write(self.d / "sessions.json", self.sess_file)

    def _flush(self, **extra) -> None:
        """Lock held. Rewrite photos.json (atomically: a reader always finds a whole file), live.json and sessions.json from memory,
        then empty the journal. A crash between the two leaves lines that are already in photos.json: _replay skips those."""
        if self.deleted:
            return
        _save_photos(self.d, self.photos)
        self._save_state(**extra)
        self.journal.unlink(missing_ok=True)
        self.dirty, self.since_flush, self.last_flush = False, 0, time.time()

    def flush(self, **extra) -> None:
        with self.lock:
            self._flush(**extra)

    def flush_if_due(self, force_time: bool = True) -> None:
        with self.lock:
            if self.dirty and (self.since_flush >= max(16, len(self.photos) // FLUSH_SHARE)
                               or (force_time and time.time() - self.last_flush >= FLUSH_S)):
                self._flush()

    def _log(self, rec: dict) -> None:
        """Lock held. One line, appended: what makes a frame safe before its ack, at a cost that does not grow with the scan."""
        with open(self.journal, "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.dirty = True
        self.since_flush += 1

    def _replay(self) -> bool:
        """Put the journal's frames (a crash before the last flush) back into photos / sessions / got. True if any was missing."""
        recs = _journal_lines(self.d)
        if not recs:
            return False
        have = {p["file"] for p in self.photos}
        by_index = {s["index"]: s for s in self.sess_file["sessions"]}
        for rec in recs:
            if rec.get("session") == self.index and isinstance(rec.get("n"), int):
                self.got.add(rec["n"])
            raw = rec.get("raw")
            if not isinstance(raw, dict) or "file" not in raw:
                continue
            stored = raw if rec.get("stored") is True else rec.get("stored")
            if isinstance(stored, dict) and stored.get("file") not in have:
                self.photos.append(stored)
                have.add(stored["file"])
            if rec.get("keep_raw"):
                s = by_index.get(rec.get("session"))
                if s is None:
                    s = {"index": rec.get("session"), "aligned": False, "transform": None, "frames": []}
                    self.sess_file["sessions"].append(s)
                    by_index[s["index"]] = s
                if raw["file"] not in {f["file"] for f in s.setdefault("frames", [])}:
                    s["frames"].append(raw)
        return True

    def mark(self, **extra) -> None:
        with self.lock:
            self._flush(**extra)

    def push(self, msg: dict) -> None:
        self.out.append(msg)

    def drain(self) -> list[dict]:
        out = []
        while self.out:
            out.append(self.out.popleft())
        return out

    # -- frames

    def store_frame(self, n: int | None, photo: dict, jpeg: bytes, depth_bytes: bytes) -> int:
        """Write one frame (idempotent by phone frame number and by timestamp). Returns the cumulative ack.
        Raises BadFrame for an entry that cannot be used (counted as received), ValueError once the scan is being built."""
        try:
            _check_photo(photo)
        except BadFrame:
            if n is not None:
                with self.lock:
                    if not self.finished:
                        self.got.add(n); self._recount(); self._log({"session": self.index, "n": n}); self.flush_if_due(False)
            raise
        with self.lock:
            if self.deleted:
                raise ScanGone("this scan was deleted on the server")
            if self.finished:
                raise ValueError("this scan is already being built")
            if n is not None and n in self.got:
                return self.acked
            ts = round(float(photo.get("timestamp") or 0), 4)
            if ts and ts in self.stamps:
                if n is not None:
                    self.got.add(n); self._recount(); self._log({"session": self.index, "n": n}); self.flush_if_due(False)
                return self.acked
            name = f"p{self.next_num:03d}"
            self.next_num += 1
            (self.d / "photos").mkdir(exist_ok=True)
            (self.d / "photos" / f"{name}.jpg").write_bytes(jpeg)
            entry = {k: v for k, v in photo.items() if k not in ("file", "depth_file")}
            entry["file"] = f"{name}.jpg"
            entry["session"] = self.index
            depth = _depth_array(photo, depth_bytes) if depth_bytes else None
            if depth is not None:
                (self.d / "photos" / f"{name}.depth").write_bytes(depth_bytes)
                entry["depth_file"] = f"{name}.depth"
            else:
                for k in ("depth_width", "depth_height"):
                    entry.pop(k, None)
            stored = None
            if self.aligned:
                stored = entry if self.T is None else registration.apply([entry], self.T)[0]
                self.photos.append(stored)
                if self.patch:
                    self.mine.setdefault("frames", []).append(entry)       # the raw one is kept too
            else:
                self.mine.setdefault("frames", []).append(entry)
                self.pending.append(entry)
            # the frame's files are written; this line makes its pose safe too. Only then is it acknowledged
            self._log({"session": self.index, "n": n, "raw": entry, "stored": True if stored is entry else stored, "keep_raw": self.patch})
            if ts:
                self.stamps.add(ts)
            if n is not None:
                self.got.add(n)
                self._recount()
            self.flush_if_due(False)
            ack = self.acked
        if self.engine is not None:
            self.jobs.put((entry, stored, depth, jpeg))
        return ack

    def photo_count(self) -> int:
        return len(self.photos) + len(self.pending)

    def touch_meta(self) -> None:
        """Keep the scan list's photo count about right (not on every frame)."""
        n = self.photo_count()
        if n != self.last_meta_count and time.time() - self.last_meta_t > 2.0:
            meta = _meta(self.d)
            if meta and meta.get("status") == "capturing":
                meta["photo_count"] = n
                _save_meta(self.d, meta)
                self.last_meta_count, self.last_meta_t = n, time.time()

    # -- worker: feeds the engine, registers a patch session

    def _seed(self) -> None:
        """Start from what the scan already covers: the old scan for a patch session, the session's own frames after a restart.
        photos.json only ever holds frames in the scan's frame, so all of it goes in; a patch session's unmatched frames go to pending_engine."""
        with self.lock:
            mine = list(self.photos), list(self.pending)
        for eng, entries in ((self.engine, mine[0]), (self.pending_engine, mine[1])):
            for p in entries if eng is not None else ():
                try:
                    eng.add_frame(p, _depth_map(self.d, p))
                except Exception as e:
                    print(f"live seed {self.id}: {type(e).__name__}: {e}", flush=True)

    def _work(self) -> None:
        self._seed()
        while True:
            try:
                job = self.jobs.get(timeout=FLUSH_S / 2)
            except queue.Empty:          # nothing new: a good moment to bring photos.json up to date
                try:
                    self.flush_if_due()
                except Exception as e:
                    print(f"live flush {self.id}: {type(e).__name__}: {e}", flush=True)
                continue
            if job is None:
                self.jobs.task_done()
                break
            raw, stored, depth, jpeg = job
            if self.deleted:             # the scan is gone: drop what is still queued
                self.jobs.task_done()
                continue
            try:
                sharp = _sharpness(jpeg)
                if stored is not None:
                    self.engine.add_frame(stored, depth, sharpness=sharp)
                else:
                    eng = self.pending_engine
                    if eng is not None:
                        eng.add_frame(raw, depth, sharpness=sharp)
                        self.fed += 1
                        self._maybe_register()
            except Exception as e:
                print(f"live frame {self.id}: {type(e).__name__}: {e}", flush=True)
            finally:
                self.jobs.task_done()
            if self.jobs.empty():
                try:
                    self.flush_if_due()
                except Exception as e:
                    print(f"live flush {self.id}: {type(e).__name__}: {e}", flush=True)

    def _maybe_register(self) -> None:
        """A registration takes seconds: try only when the worker has caught up, so frames do not pile up in memory meanwhile."""
        if self.aligned or not self.jobs.empty() or self.fed < ATTEMPT_AT or self.fed - self.last_attempt < ATTEMPT_AT:
            return
        self.last_attempt = self.fed
        self.try_register()

    def try_register(self) -> dict:
        """Register the new session to the old scan; on success rewrite its transforms into the old frame. Returns the result (without T)."""
        with self.reg_lock:
            with self.lock:
                if self.aligned:
                    return {"ok": True, "message": "already matched", "rms_mm": self.mine.get("rms_mm")}
                old = [p for p in self.photos if p.get("session", 1) != self.index]
                new = list(self.pending)
            try:
                res = registration.register(self.d, old, new)
            except Exception as e:
                res = {"ok": False, "message": f"Could not match yet ({type(e).__name__}).", "rms_mm": None}
            msg = {"type": "aligned", "ok": bool(res.get("ok")), "rms_mm": res.get("rms_mm"), "message": res.get("message", "")}
            if res.get("ok"):
                self._commit_alignment(res["T"], res.get("rms_mm"), msg)
            else:
                self.push(msg)
        return {**res, "T": None}

    def _commit_alignment(self, T: np.ndarray, rms_mm, msg: dict) -> None:
        with self.lock:
            self.T = T
            self.push(msg)               # in the same locked step as T: no map in the old frame can be sent before `aligned`
            moved = registration.apply(self.pending, T)
            have = {p["file"] for p in self.photos}
            self.photos += [p for p in moved if p["file"] not in have]
            raw, self.pending = list(self.pending), []
            self.mine["rms_mm"] = rms_mm
            self._flush()                # at once: the journal's lines for these frames have no `stored` entry
            self.pending_engine = None
        if self.engine is not None:
            for p in raw:
                try:
                    self.engine.add_frame(p, _depth_map(self.d, p), pre=T)
                except Exception as e:
                    print(f"live align {self.id}: {type(e).__name__}: {e}", flush=True)

    # -- maps

    def next_map(self, force: bool = False) -> dict | None:
        if self.engine is None:
            return None
        with self.lock:
            aligned, view, eng = self.aligned, self.view, self.pending_engine
        if aligned:
            msg = self.engine.next_message(view, force)
        else:
            msg = eng.next_message(None, force) if eng is not None else None
            if msg is not None:
                ask = {"id": "align", "kind": "gap", "priority": 3, "stand": None, "look": None, "target": msg["you"] or [0, 0, 0],
                       "text": "Film a wall or corner you already scanned, slowly, so I can find where you are in the room."}
                msg["needs"] = ([ask] + msg["needs"])[:5]
                msg["summary"] = ask["text"]
        return msg                       # without `seq`: send_map numbers it when it goes out

    def set_pose(self, transform) -> None:
        """The phone's pose in its own session frame; an aligned patch session keeps its engine in the old scan's frame."""
        if self.engine is None:
            return
        if not self.aligned:
            eng = self.pending_engine
            if eng is not None:
                eng.set_pose(transform)
        elif self.T is not None:
            self.engine.set_pose((self.T @ _mat(transform)).T.tolist())
        else:
            self.engine.set_pose(transform)

    def stop(self) -> None:
        if self.engine is not None:
            self.jobs.put(None)


# ---------------------------------------------------------------- registry

def settle(d: Path, scan_id: str) -> None:
    """Make photos.json whole before something reads it to build: flush the running session, or, after a crash, put the frames
    the journal still holds back into it (a Session restores itself from the journal when it is made)."""
    with SESSIONS_LOCK:
        s = SESSIONS.get(scan_id)
    if s is not None:
        s.flush()
    elif (d / JOURNAL).exists():
        Session(d, scan_id, engine=False)


def get_session(scan_id: str) -> Session:
    with SESSIONS_LOCK:
        s = SESSIONS.get(scan_id)
        if s is not None:
            return s
    d = _scan_dir(scan_id)
    if not (d / "live.json").exists():
        raise HTTPException(404, "this scan is not a live capture")
    s = Session(d, scan_id, engine=_meta(d).get("status") == "capturing")      # a finished scan needs no coverage maps
    with SESSIONS_LOCK:
        kept = SESSIONS.setdefault(scan_id, s)
    if kept is not s:                    # another thread made one at the same time: keep that one, stop this one's worker
        s.stop()
    return kept


def drop_session(scan_id: str, only: Session | None = None) -> None:
    """Forget the session (stops its worker). With `only`, just if that is still the registered one (a new patch may have replaced it)."""
    with SESSIONS_LOCK:
        s = SESSIONS.get(scan_id)
        if s is None or (only is not None and s is not only):
            return
        del SESSIONS[scan_id]
    s.stop()
    if ctx.hub is not None:
        ctx.hub.live_maps.pop(scan_id, None)


def close_scan(scan_id: str, message: str = "This scan was deleted on the server.") -> None:
    """Call BEFORE removing a scan folder. Refuses its frames from now on, stops its worker, frees the coverage engine, tells the
    phone (`state: error` with `message`) and closes its socket with code 4410. A store in progress finishes first (the flag is set
    under the session lock), so nothing writes into the folder once this returns. A build thread already running notices DELETED."""
    DELETED.add(scan_id)
    with SESSIONS_LOCK:
        s = SESSIONS.pop(scan_id, None)
    if ctx.hub is not None:
        ctx.hub.live_maps.pop(scan_id, None)
    if s is None:
        return
    with s.lock:
        s.deleted = True
        s.finished = True
    s.stop()
    s.engine = s.pending_engine = None
    s.out.clear()
    ws = s.ws
    if ws is not None and ctx.loop is not None and ctx.loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(_close_deleted(ws, s, message), ctx.loop)
        try:
            fut.result(timeout=3)
        except Exception:
            pass                         # the phone is gone already, or slow: the folder is removed anyway


async def _close_deleted(ws: WebSocket, s: "Session", message: str) -> None:
    try:
        await _send(ws, s, {"type": "state", "status": "error", "scan_id": s.id, "message": message})
        await ws.close(code=4410)
    except Exception:
        pass


def _open_for_patch(d: Path, meta: dict) -> None:
    """The checks shared by a live patch session and the offline /add route."""
    if meta.get("kind") != "mesh" or meta.get("ring") or meta.get("turntable"):
        raise HTTPException(400, "only room and sweep scans can be added to")
    if meta.get("status") == "building":
        raise HTTPException(409, "this scan is being built right now")
    if meta.get("status") == "capturing":
        raise HTTPException(409, "this scan is still capturing: finish it first (Build what I have)")
    if not [p for p in _photos(d) if p.get("depth_file")]:
        raise HTTPException(400, "this scan has no depth frames to line up with")


def _old_kind(meta: dict, default: str) -> str:
    live = meta.get("live")
    return live.get("kind", default) if isinstance(live, dict) else default


# ---------------------------------------------------------------- start / finish

def create(body: dict) -> dict:
    """POST /api/live with kind room|sweep: a new live scan, or (resume_scan) a patch session on an existing one."""
    kind = body.get("kind")
    if kind not in LIVE_KINDS:
        raise HTTPException(422, "live capture is for kind room or sweep")
    info = body.get("info") if isinstance(body.get("info"), dict) else {}
    resume = body.get("resume_scan")
    if resume:
        sid = str(resume)
        d = _scan_dir(sid)
        meta = _meta(d)
        _open_for_patch(d, meta)
        index = max(s["index"] for s in _sessions(d)["sessions"]) + 1
        _write(d / "live.json", {"kind": _old_kind(meta, kind), "session": index, "patch": True, "got": [], "finish": False,
                                 "prev_status": meta.get("status"), "resume_scan": sid})
        meta["status"] = "capturing"
        _save_meta(d, meta)
    else:
        sid = uuid.uuid4().hex[:12]
        d = ctx.data() / sid
        (d / "photos").mkdir(parents=True)
        phone = dict(info)
        if kind == "sweep":
            phone.setdefault("scan type", "Sweep from one spot")
            phone["sweep"] = True
        phone["live"] = True
        _save_meta(d, {"id": sid, "name": str(body.get("name") or "Scan"), "kind": "mesh", "scale": 1.0, "photo_count": 0,
                       "project": str(body.get("project") or "").strip(), "created_at": time.time(), "status": "capturing",
                       "live": {"kind": kind}, "phone": phone})
        _write(d / "live.json", {"kind": kind, "session": 1, "patch": False, "got": [], "finish": False})
        index = 1
    drop_session(sid)                    # a session left in memory from an earlier capture of this scan
    get_session(sid)
    _notify({"type": "scan", "id": sid, "status": "capturing"})
    return {"scan_id": sid, "ws": f"/ws/live/{sid}", "session": index, "patch": bool(resume)}


def build_cloud(d: Path, max_points: int = 500_000) -> int:
    """scan.ply from the stored frames: a coloured point cloud of every depth frame (the phone used to send one)."""
    import trimesh
    pts, cols = [], []
    for p in _photos(d):
        dm = _depth_map(d, p)
        if dm is None or not (d / "photos" / p["file"]).exists():
            continue
        P = frames.world_points(p, dm)[::3, ::3]
        dd = dm[::3, ::3]
        ok = np.isfinite(dd) & (dd > 0.15) & (dd < 6.0) & np.isfinite(P).all(axis=2)
        img = np.asarray(Image.open(d / "photos" / p["file"]).convert("RGB"))
        h, w = dm.shape
        ys = np.minimum(((np.arange(0, h, 3) + 0.5) * img.shape[0] / h).astype(int), img.shape[0] - 1)
        xs = np.minimum(((np.arange(0, w, 3) + 0.5) * img.shape[1] / w).astype(int), img.shape[1] - 1)
        C = img[ys][:, xs]
        pts.append(P[ok])
        cols.append(C[ok])
    if not pts:
        raise ValueError("no frame has LiDAR depth, so there is nothing to build")
    P, C = np.concatenate(pts), np.concatenate(cols)
    if len(P) > max_points:
        k = np.random.default_rng(0).choice(len(P), max_points, replace=False)
        P, C = P[k], C[k]
    tmp = d / "scan.ply.tmp"            # a build stopped half way must not leave a cut-off scan.ply behind
    trimesh.PointCloud(P, colors=np.c_[C, np.full(len(C), 255, np.uint8)]).export(tmp, file_type="ply")
    tmp.replace(d / "scan.ply")
    return len(P)


def _finish_now(d: Path, scan_id: str, info: dict | None) -> None:
    """Build the model from the stored frames (runs in its own thread: it survives the socket dropping)."""
    BUILDING.add(scan_id)
    try:
        _build(d, scan_id, info)
    finally:
        BUILDING.discard(scan_id)
        meta = _meta(d)
        if scan_id not in DELETED and meta.get("status") != "building" and "resume_tries" in meta:
            meta.pop("resume_tries")     # the build got to its end (well or not): a later restart starts counting again
            _save_meta(d, meta)


def _build(d: Path, scan_id: str, info: dict | None) -> None:
    sess = SESSIONS.get(scan_id)
    live = _read(d / "live.json", {})
    if sess is None and (live.get("patch") or (d / JOURNAL).exists()):
        sess = Session(d, scan_id, engine=False)        # after a restart: the unmatched frames of a patch still get their chance
    try:
        if scan_id in DELETED:
            return
        if sess is not None:
            sess.jobs.join()
            if not sess.aligned and sess.pending:
                sess.try_register()
            sess.flush()
        meta = _meta(d)
        if info:
            meta["phone"] = {**(meta.get("phone") or {}), **info}
        if live.get("patch") and sess is not None and not sess.aligned:
            meta["status"] = live.get("prev_status") or "ok"       # nothing new could be added: the old model stays
            _save_meta(d, meta)
            _push(sess, {"type": "state", "status": "error", "scan_id": scan_id, "message": "The new frames could not be matched to the first scan, so nothing was added."})
            return
        meta["status"] = "building"
        meta["photo_count"] = len(_photos(d))
        _save_meta(d, meta)
        build_cloud(d)
        meta = ctx.convert(d)
        if meta.pop("interrupted", None) is not None:
            _save_meta(d, meta)
        _push(sess, {"type": "state", "status": "ready" if meta.get("status") == "ok" else "error", "scan_id": scan_id,
                     "message": meta.get("log", "")[:300]})
    except Exception as e:
        if scan_id not in DELETED:
            meta = _meta(d)
            meta["status"] = "error"
            meta["log"] = f"live build failed: {type(e).__name__}: {e}"
            _save_meta(d, meta)
            _push(sess, {"type": "state", "status": "error", "scan_id": scan_id, "message": meta["log"]})
    finally:
        if scan_id in DELETED:           # deleted while it was building: remove what the build wrote after the folder went
            shutil.rmtree(d, ignore_errors=True)
        _notify({"type": "scan", "id": scan_id, "status": _meta(d).get("status")})
        _notify({"type": "live_end_map", "id": scan_id})
        if sess is not None:
            drop_session(scan_id, only=sess)     # an open socket still holds it and delivers the last state message
        if ctx.hub is not None:
            ctx.hub.live_maps.pop(scan_id, None)


def _push(sess: "Session | None", msg: dict) -> None:
    if sess is not None:
        sess.push(msg)


def start_finish(scan_id: str, info: dict | None = None) -> dict:
    d = _scan_dir(scan_id)
    with FINISH_LOCK:
        meta = _meta(d)
        if meta.get("status") == "building":
            return {"status": "building"}
        if meta.get("status") != "capturing" and not (meta.get("status") == "error" and meta.get("interrupted")):
            raise HTTPException(409, f"this scan is {meta.get('status')}, not capturing")
        with SESSIONS_LOCK:
            s = SESSIONS.get(scan_id)
        if s is not None:
            with s.lock:                 # no frame is stored after this, and no later write puts finish: false back
                s.finished = True
                s._flush()
        else:
            live = _read(d / "live.json", {})
            live["finish"] = True
            _write(d / "live.json", live)
        meta["status"] = "building"
        _save_meta(d, meta)
    _notify({"type": "scan", "id": scan_id, "status": "building"})
    threading.Thread(target=_finish_now, args=(d, scan_id, info), daemon=True).start()
    return {"status": "building"}


# ---------------------------------------------------------------- the WebSocket

async def _send(ws: WebSocket, sess: Session, obj: dict) -> None:
    async with sess.send_lock:
        await ws.send_json(obj)


async def _publish_map(sess: Session, msg: dict) -> None:
    if ctx.hub is None:
        return
    ctx.hub.live_maps[sess.id] = {"type": "live_map", "id": sess.id, "name": _meta(sess.d).get("name", ""), "map": msg}
    await ctx.hub.broadcast(ctx.hub.live_maps[sess.id])


async def send_map(ws: WebSocket, sess: Session, want_map: bool = True, force: bool = False) -> None:
    """Compute a map (in a worker thread), then send what is queued and the map. The whole step holds the session's map_lock, so
    two callers (the ticker and a hello, or two sockets of one session) can neither compute at the same time (the engine keeps
    hysteresis state) nor send their maps swapped: `seq` is given in the event loop, in send order, just before the send.
    The lock is an asyncio lock: waiting for it, or for the thread, never blocks the event loop, and acks do not wait for it."""
    async with sess.map_lock:
        msg = None
        if want_map:
            try:
                msg = await anyio.to_thread.run_sync(sess.next_map, force)
            except Exception as e:               # a bug in the map must not stop the other messages for good
                print(f"live map {sess.id}: {type(e).__name__}: {e}", flush=True)
        for m in sess.drain():                   # after computing the map: an `aligned` it already reflects goes out first
            await _send(ws, sess, m)
        if msg is not None:
            sess.seq += 1                        # one counter for the session: a patch switches engines when it is matched
            msg["seq"] = sess.seq
            await _send(ws, sess, msg)
            await _publish_map(sess, msg)


async def _ticker(ws: WebSocket, sess: Session) -> None:
    last = 0.0
    try:
        while True:
            await asyncio.sleep(0.2)
            due = time.time() - last >= MAP_EVERY
            if due:
                last = time.time()
                try:
                    await anyio.to_thread.run_sync(sess.touch_meta)
                except Exception as e:
                    print(f"live meta {sess.id}: {type(e).__name__}: {e}", flush=True)
            await send_map(ws, sess, want_map=due)
    except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
        return


async def _on_frame(ws: WebSocket, sess: Session, data: bytes) -> None:
    try:
        (hlen,) = struct.unpack(">I", data[:4])
        header = json.loads(data[4:4 + hlen])
        jl, dl = int(header.get("jpeg_len", 0)), int(header.get("depth_len", 0))
        body = data[4 + hlen:]
        if jl <= 0 or dl < 0 or len(body) < jl + dl:
            raise ValueError("the frame is shorter than its header says")
        jpeg, depth = body[:jl], body[jl:jl + dl]
        n = int(header["n"])
        photo = header["photo"]
    except Exception as e:
        await _send(ws, sess, {"type": "error", "message": f"bad frame: {type(e).__name__}: {e}"})
        return
    sess.last_seen = time.time()
    try:
        ack = await anyio.to_thread.run_sync(sess.store_frame, n, photo, jpeg, depth)
    except BadFrame as e:
        await _send(ws, sess, {"type": "error", "message": f"frame {n} dropped: {e}"})
        ack = sess.acked
    except ScanGone as e:
        await _send(ws, sess, {"type": "state", "status": "error", "scan_id": sess.id, "message": str(e)})
        return
    except ValueError as e:
        await _send(ws, sess, {"type": "error", "message": str(e)})
        return
    await _send(ws, sess, {"type": "ack", "n": ack})


async def _on_text(ws: WebSocket, sess: Session, m: dict) -> None:
    t = m.get("type")
    sess.last_seen = time.time()
    if t == "hello":
        await _send(ws, sess, {"type": "ack", "n": sess.acked})
        await _send(ws, sess, {"type": "state", "status": _state(sess.d), "scan_id": sess.id})
        await send_map(ws, sess, force=True)
    elif t == "pose":
        try:
            if m.get("transform") is not None:
                sess.set_pose(m["transform"])
        except Exception:
            pass                         # a malformed pose is not worth an answer: the next one comes in 0.2 s
    elif t == "finish":
        info = m.get("info") if isinstance(m.get("info"), dict) else None
        try:
            out = await anyio.to_thread.run_sync(start_finish, sess.id, info)
            await _send(ws, sess, {"type": "state", "status": out["status"], "scan_id": sess.id})
        except HTTPException as e:
            await _send(ws, sess, {"type": "error", "message": str(e.detail)})
    elif t == "cancel":
        await anyio.to_thread.run_sync(lambda: sess.mark(cancelled=True))
        await _send(ws, sess, {"type": "state", "status": "capturing", "scan_id": sess.id, "message": "kept as a draft"})
    else:
        await _send(ws, sess, {"type": "error", "message": f"unknown message {t!r}"})


@router.websocket("/ws/live/{scan_id}")
async def ws_live(ws: WebSocket, scan_id: str):
    try:
        sess = await anyio.to_thread.run_sync(get_session, scan_id)
    except HTTPException:
        await ws.close(code=4404)
        return
    await ws.accept()
    sess.ws = ws
    sess.last_seen = time.time()
    ticker = asyncio.create_task(_ticker(ws, sess))
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                if len(msg["bytes"]) > MAX_FRAME_BYTES:
                    await _send(ws, sess, {"type": "error", "message": "frame too big"})
                    continue
                await _on_frame(ws, sess, msg["bytes"])
            elif msg.get("text") is not None:
                try:
                    m = json.loads(msg["text"])
                except ValueError:
                    m = None
                if not isinstance(m, dict):
                    await _send(ws, sess, {"type": "error", "message": "bad JSON"})
                    continue
                await _on_text(ws, sess, m)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        ticker.cancel()
        if sess.ws is ws:
            sess.ws = None
        sess.last_seen = time.time()
        if sess.finished and sess.ws is None and _state(sess.d) in ("ready", "error"):
            drop_session(scan_id, only=sess)     # a phone that looked at a finished scan: nothing left to keep in memory


# ---------------------------------------------------------------- HTTP: finish, append frames, add a session offline

@router.post("/api/scans/{scan_id}/finish")
def finish_route(scan_id: str):
    """Build what is there ("Build what I have"): for a live scan that was left, or finished from the web page."""
    return start_finish(scan_id)


def _manifest_entries(photos_meta: UploadFile | None) -> list[dict]:
    if photos_meta is None:
        raise HTTPException(422, "photos_meta (the photos.json the phone writes) is required")
    try:
        m = json.loads(photos_meta.file.read())
    except ValueError:
        raise HTTPException(400, "photos_meta is not JSON")
    entries = m.get("photos", []) if isinstance(m, dict) else m
    if not isinstance(entries, list):
        raise HTTPException(400, "photos_meta has no photos list")
    return entries


def _store_batch(sess: Session, entries: list[dict], uploads: list[UploadFile]) -> tuple[int, int]:
    files = {Path(u.filename or "").name: u for u in uploads}
    added = skipped = 0
    for e in entries:
        j = files.get(Path(e.get("file", "")).name) if isinstance(e, dict) else None
        if j is None:
            skipped += 1
            continue
        dep = b""
        if e.get("depth_file") and Path(e["depth_file"]).name in files:
            dep = files[Path(e["depth_file"]).name].file.read()
        before = sess.next_num
        try:
            sess.store_frame(None, e, j.file.read(), dep)
        except ValueError:
            pass
        if sess.next_num > before:
            added += 1
        else:
            skipped += 1
    return added, skipped


@router.post("/api/scans/{scan_id}/frames")
def append_frames(scan_id: str, photos: list[UploadFile] = File(default=[]), photos_meta: UploadFile | None = File(None)):
    """Complete a live scan that lost its connection: store the frames the server does not have yet (matched by timestamp) and do NOT build."""
    d = _scan_dir(scan_id)
    meta = _meta(d)
    if meta.get("status") != "capturing" or not (d / "live.json").exists():
        raise HTTPException(409, "this scan is not an open live capture")
    entries = _manifest_entries(photos_meta)
    sess = get_session(scan_id)
    added, skipped = _store_batch(sess, entries, photos)
    sess.jobs.join()
    sess.flush()
    return {"added": added, "skipped": skipped, "total": sess.photo_count()}


@router.post("/api/scans/{scan_id}/add")
def add_to_scan(scan_id: str, photos: list[UploadFile] = File(default=[]), photos_meta: UploadFile | None = File(None), info: str = Form("")):
    """Patch an existing scan with frames captured offline: store them as a new session, line it up with the old scan, rebuild.
    Returns {aligned, rms_mm, message}; if the new frames cannot be matched the old model is left as it was."""
    d = _scan_dir(scan_id)
    meta = _meta(d)
    _open_for_patch(d, meta)
    entries = _manifest_entries(photos_meta)
    index = max(s["index"] for s in _sessions(d)["sessions"]) + 1
    sess = Session(d, scan_id, index=index, patch=True, kind=_old_kind(meta, "room"), engine=False)
    added, skipped = _store_batch(sess, entries, photos)
    res = sess.try_register()
    sess.flush()                         # matched or not, the frames are kept (sessions.json)
    out = {"aligned": bool(res.get("ok")), "rms_mm": res.get("rms_mm"), "message": res.get("message", ""), "added": added, "skipped": skipped}
    if res.get("ok"):
        meta = _meta(d)
        if info:
            try:
                meta["phone"] = {**(meta.get("phone") or {}), **json.loads(info)}
            except (ValueError, TypeError):
                pass
        meta["status"] = "building"
        _save_meta(d, meta)
        try:
            build_cloud(d)
            meta = ctx.convert(d)
            out["status"] = meta.get("status")
        except Exception as e:
            out["status"] = "error"
            out["message"] += f" Rebuild failed: {type(e).__name__}: {e}"
        _notify({"type": "scan", "id": scan_id, "status": _meta(d).get("status")})
    return out


# ---------------------------------------------------------------- abandoned captures

def recover_builds() -> list[str]:
    """At server start: a scan whose meta says "building" but has no build running here was cut off by a restart (a crash, a
    redeploy). Its frames are all on disk, and a build only reads them and rewrites its own outputs, so it is started again once.
    If that one is cut off too, or there is nothing to build from, the scan is set to "error" with `interrupted: true`, so the
    page's Re-convert and "Build what I have" (POST /api/scans/{id}/finish) work on it. Captures and builds both get the frames
    that were only in the journal (frames.jsonl) put back into photos.json first. Returns the ids it started again."""
    root = ctx.data()
    started = []
    if root is None or not root.is_dir():
        return started
    for d in sorted(root.iterdir()):
        try:
            meta = _meta(d)
            if meta.get("status") not in ("building", "capturing") or d.name in BUILDING or d.name in DELETED:
                continue
            settle(d, d.name)            # frames a crash left only in the journal go back into photos.json first
            if meta.get("status") != "building":
                continue                 # a capture: the phone resumes it, or the janitor builds it
            tries = int(meta.get("resume_tries", 0))
            can = (d / "live.json").exists() and any(p.get("depth_file") for p in _photos(d))
            if can and tries < RESUME_TRIES:
                meta["resume_tries"] = tries + 1
                meta["log"] = "The server restarted during the build: building again from the stored frames."
                _save_meta(d, meta)
                threading.Thread(target=_finish_now, args=(d, d.name, None), daemon=True).start()
                started.append(d.name)
            else:
                meta.pop("resume_tries", None)
                meta["status"], meta["interrupted"] = "error", True
                meta["log"] = ("The server stopped while this scan was being built" + (" (twice)." if can else ".")
                               + " The frames are kept: use Re-convert or Build what I have to try again.")
                _save_meta(d, meta)
            _notify({"type": "scan", "id": d.name, "status": meta["status"]})
        except Exception as e:
            print(f"live recover {d.name}: {type(e).__name__}: {e}", flush=True)
    return started


def abandon(d: Path, scan_id: str) -> None:
    """A capture nobody has touched for IDLE_S: build what there is. A patch goes through the same finish, which tries its
    unmatched frames once more and puts the old status back if nothing could be added."""
    settle(d, scan_id)
    live = _read(d / "live.json", {})
    if not live.get("patch") and not _photos(d):
        meta = _meta(d)
        meta["status"], meta["log"] = "error", "the capture ended without any frame"
        _save_meta(d, meta)
        live["finish"] = True
        _write(d / "live.json", live)
        drop_session(scan_id)
        _notify({"type": "scan", "id": scan_id, "status": meta["status"]})
        _notify({"type": "live_end_map", "id": scan_id})
        return
    start_finish(scan_id)


def janitor_once(now: float | None = None) -> list[str]:
    """Finish every capture with no socket and no news for IDLE_S. Returns the ids it started."""
    now = time.time() if now is None else now
    done = []
    root = ctx.data()
    if root is None or not root.is_dir():
        return done
    for d in root.iterdir():
        if not (d / "live.json").exists() or not (d / "meta.json").exists():
            continue
        try:
            if _meta(d).get("status") != "capturing":
                continue
            s = SESSIONS.get(d.name)
            if s is not None and s.ws is not None:
                continue
            last = max((d / "live.json").stat().st_mtime, s.last_seen if s is not None else 0)
            if now - last >= IDLE_S:
                abandon(d, d.name)
                done.append(d.name)
        except Exception as e:
            print(f"live janitor {d.name}: {type(e).__name__}: {e}", flush=True)
    return done


async def _janitor() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await anyio.to_thread.run_sync(janitor_once)
        except Exception as e:
            print(f"live janitor: {type(e).__name__}: {e}", flush=True)
