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
from .photos import _mat

router = APIRouter(tags=["live"])
ctx = SimpleNamespace(data=lambda: None, hub=None, convert=None, loop=None)
SESSIONS: dict[str, "Session"] = {}
SESSIONS_LOCK = threading.Lock()
MAP_EVERY = 1.5            # seconds between map messages (only sent when something changed)
IDLE_S = float(os.environ.get("LIVE_IDLE_S", "600"))
MAX_FRAME_BYTES = 12 * 1024 * 1024
ATTEMPT_AT = 6             # a patch session tries to register itself to the old scan after this many frames, then every this many more
LIVE_KINDS = ("room", "sweep")


def setup(data_fn, hub, convert) -> None:
    """main.py hands over the data folder (a function, tests move it), the viewers hub and the conversion."""
    ctx.data, ctx.hub, ctx.convert = data_fn, hub, convert
    hub.live_maps = {}


async def startup() -> None:
    ctx.loop = asyncio.get_running_loop()
    asyncio.create_task(_janitor())


# ---------------------------------------------------------------- small helpers

def _scan_dir(scan_id: str) -> Path:
    d = ctx.data() / scan_id
    if not scan_id.isalnum() or not d.is_dir():
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
    _write(d / "meta.json", {**meta})


def _photos(d: Path) -> list[dict]:
    m = _read(d / "photos.json", {})
    return m.get("photos", []) if isinstance(m, dict) else m


def _save_photos(d: Path, photos: list[dict]) -> None:
    _write(d / "photos.json", {"version": 2, "photos": photos})


def _notify(msg: dict) -> None:
    """Tell every open web page (from any thread)."""
    if ctx.loop is not None and ctx.hub is not None:
        asyncio.run_coroutine_threadsafe(ctx.hub.broadcast(msg), ctx.loop)


def _sharpness(jpeg: bytes) -> float | None:
    try:
        g = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
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
    if not (w and h) or len(data) != int(w) * int(h) * 4:
        return None
    return np.frombuffer(data, dtype="<f4").reshape(int(h), int(w))


# ---------------------------------------------------------------- the session

class Session:
    """One capture session of one scan: stores frames, feeds the coverage engine, registers a patch session to the old scan."""

    def __init__(self, d: Path, scan_id: str, index: int | None = None, patch: bool | None = None, kind: str | None = None, engine: bool = True):
        self.d, self.id = d, scan_id
        self.lock = threading.RLock()
        st = _read(d / "live.json", {})
        self.kind = kind or st.get("kind") or "room"
        self.index = index if index is not None else st.get("session", 1)
        self.patch = patch if patch is not None else bool(st.get("patch", False))
        self.got: set[int] = set(st.get("got", []))
        self.finished = bool(st.get("finish", False))
        self.acked = 0
        self._recount()
        self.sess_file = _read(d / "sessions.json", {"sessions": [{"index": 1, "aligned": True, "transform": None}]})
        mine = next((s for s in self.sess_file["sessions"] if s["index"] == self.index), None)
        if mine is None:
            mine = {"index": self.index, "aligned": not self.patch, "transform": None, "frames": []}
            self.sess_file["sessions"].append(mine)
        self.mine = mine
        self.T = np.array(mine["transform"], dtype=float).reshape(4, 4) if mine.get("transform") else None
        self.stamps: set[float] = set()
        nums = [0]
        for p in _photos(d) + list(mine.get("frames", [])) + [f for s in self.sess_file["sessions"] for f in s.get("frames", [])]:
            nums.append(int("".join(c for c in Path(p["file"]).stem if c.isdigit()) or 0))
            if p.get("timestamp"):
                self.stamps.add(round(float(p["timestamp"]), 4))
        self.next_num = max(nums) + 1
        self.pending: list[dict] = [f for f in mine.get("frames", []) if not self.aligned or f["file"] not in {p["file"] for p in _photos(d)}] if self.patch else []
        self.out: deque = deque()
        self.ws: WebSocket | None = None
        self.last_seen = time.time()
        self.send_lock = asyncio.Lock()
        self.jobs: queue.Queue = queue.Queue()
        self.last_attempt = 0
        self.registering = False
        self.engine = coverage.CoverageEngine(self.kind) if engine else None
        self.pending_engine = coverage.CoverageEngine(self.kind) if engine and self.patch else None
        self.last_meta_count = -1
        self.last_meta_t = 0.0
        self.worker = threading.Thread(target=self._work, daemon=True)
        self._seed_old = engine
        if engine:
            self.worker.start()

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

    def _save_state(self) -> None:
        _write(self.d / "live.json", {"kind": self.kind, "session": self.index, "patch": self.patch, "got": sorted(self.got),
                                      "finish": self.finished, **{k: v for k, v in _read(self.d / "live.json", {}).items()
                                                                  if k in ("prev_status", "cancelled", "resume_scan", "old_count")}})
        self.mine["aligned"] = self.aligned
        self.mine["transform"] = None if self.T is None else self.T.tolist()
        _write(self.d / "sessions.json", self.sess_file)

    def push(self, msg: dict) -> None:
        self.out.append(msg)

    def drain(self) -> list[dict]:
        out = []
        while self.out:
            out.append(self.out.popleft())
        return out

    # -- frames

    def store_frame(self, n: int | None, photo: dict, jpeg: bytes, depth_bytes: bytes) -> int:
        """Write one frame (idempotent by phone frame number and by timestamp). Returns the cumulative ack."""
        with self.lock:
            if n is not None and n in self.got:
                return self.acked
            ts = round(float(photo.get("timestamp") or 0), 4)
            if ts and ts in self.stamps:
                if n is not None:
                    self.got.add(n); self._recount(); self._save_state()
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
                photos = _photos(self.d)
                photos.append(stored)
                _save_photos(self.d, photos)
                if self.patch:
                    self.mine.setdefault("frames", []).append(entry)       # the raw one is kept too
            else:
                self.mine.setdefault("frames", []).append(entry)
                self.pending.append(entry)
            if ts:
                self.stamps.add(ts)
            if n is not None:
                self.got.add(n)
                self._recount()
            self._save_state()
            ack = self.acked
        self.jobs.put((entry, stored, depth, jpeg))
        return ack

    def photo_count(self) -> int:
        return sum(1 for p in _photos(self.d)) + (0 if self.aligned else len(self.pending))

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
        """A patch session starts from what the old scan already covers; a restored session from its own frames."""
        old = [p for p in _photos(self.d) if p.get("session", 1) != self.index or not self.patch]
        for p in old:
            try:
                dm = _depth_array(p, (self.d / "photos" / p["depth_file"]).read_bytes()) if p.get("depth_file") else None
                self.engine.add_frame(p, dm)
            except Exception as e:
                print(f"live seed {self.id}: {type(e).__name__}: {e}", flush=True)
        if self.pending_engine is not None:
            for p in self.pending:
                try:
                    dm = _depth_array(p, (self.d / "photos" / p["depth_file"]).read_bytes()) if p.get("depth_file") else None
                    self.pending_engine.add_frame(p, dm)
                except Exception:
                    pass

    def _work(self) -> None:
        try:
            self._seed()
        except Exception as e:
            print(f"live seed {self.id}: {type(e).__name__}: {e}", flush=True)
        while True:
            job = self.jobs.get()
            if job is None:
                break
            raw, stored, depth, jpeg = job
            try:
                sharp = _sharpness(jpeg)
                if stored is not None:
                    self.engine.add_frame(stored, depth, sharpness=sharp)
                elif self.pending_engine is not None:
                    self.pending_engine.add_frame(raw, depth, sharpness=sharp)
                    self._maybe_register()
            except Exception as e:
                print(f"live frame {self.id}: {type(e).__name__}: {e}", flush=True)
            finally:
                self.jobs.task_done()

    def _maybe_register(self) -> None:
        with self.lock:
            n = len(self.pending)
            if self.aligned or self.registering or n < ATTEMPT_AT or n - self.last_attempt < ATTEMPT_AT:
                return
            self.last_attempt = n
        self.try_register()

    def try_register(self, force: bool = False) -> dict:
        """Register the new session to the old scan; on success rewrite its transforms into the old frame. Returns the result."""
        with self.lock:
            if self.aligned:
                return {"ok": True, "message": "already matched", "rms_mm": self.mine.get("rms_mm")}
            self.registering = True
            old = [p for p in _photos(self.d) if p.get("session", 1) != self.index]
            new = list(self.pending)
        try:
            res = registration.register(self.d, old, new)
        except Exception as e:
            res = {"ok": False, "message": f"Could not match yet ({type(e).__name__}).", "rms_mm": None}
        finally:
            self.registering = False
        if res.get("ok"):
            self._commit_alignment(res["T"], res.get("rms_mm"))
        msg = {"type": "aligned", "ok": bool(res.get("ok")), "rms_mm": res.get("rms_mm"), "message": res.get("message", "")}
        self.push(msg)
        return {**res, "T": None}

    def _commit_alignment(self, T: np.ndarray, rms_mm) -> None:
        with self.lock:
            self.T = T
            moved = registration.apply(self.pending, T)
            photos = _photos(self.d)
            have = {p["file"] for p in photos}
            photos += [p for p in moved if p["file"] not in have]
            _save_photos(self.d, photos)
            raw, self.pending = list(self.pending), []
            self.mine["rms_mm"] = rms_mm
            self._save_state()
        if self.engine is not None:
            for p in raw:
                try:
                    dm = _depth_array(p, (self.d / "photos" / p["depth_file"]).read_bytes()) if p.get("depth_file") else None
                    self.engine.add_frame(p, dm, pre=T)
                except Exception:
                    pass
            self.engine.dirty = True
            self.engine.last_hash = ""
            self.pending_engine = None

    # -- maps

    def next_map(self, force: bool = False) -> dict | None:
        if self.engine is None:
            return None
        if self.aligned:
            return self.engine.next_message(self.view, force)
        if self.pending_engine is None:
            return None
        msg = self.pending_engine.next_message(None, force)
        if msg is not None:
            ask = {"id": "align", "kind": "gap", "priority": 3, "stand": None, "look": None, "target": msg["you"] or [0, 0, 0],
                   "text": "Film a wall or corner you already scanned, slowly, so I can find where you are in the room."}
            msg["needs"] = ([ask] + msg["needs"])[:5]
            msg["summary"] = ask["text"]
        return msg


# ---------------------------------------------------------------- registry

def get_session(scan_id: str, create: bool = True, engine: bool = True) -> Session:
    with SESSIONS_LOCK:
        s = SESSIONS.get(scan_id)
        if s is not None:
            return s
    d = _scan_dir(scan_id)
    if not (d / "live.json").exists():
        raise HTTPException(404, "this scan is not a live capture")
    s = Session(d, scan_id, engine=engine)
    with SESSIONS_LOCK:
        return SESSIONS.setdefault(scan_id, s)


def drop_session(scan_id: str) -> None:
    with SESSIONS_LOCK:
        s = SESSIONS.pop(scan_id, None)
    if s is not None:
        s.jobs.put(None)
    if ctx.hub is not None:
        ctx.hub.live_maps.pop(scan_id, None)


# ---------------------------------------------------------------- start / finish

def create(body: dict) -> dict:
    """POST /api/live with kind room|sweep: a new live scan, or (resume_scan) a patch session on an existing one."""
    kind = body.get("kind")
    if kind not in LIVE_KINDS:
        raise HTTPException(422, "live capture is for kind room or sweep")
    info = body.get("info") if isinstance(body.get("info"), dict) else {}
    resume = body.get("resume_scan")
    if resume:
        d = _scan_dir(str(resume))
        meta = _meta(d)
        if meta.get("kind") != "mesh" or meta.get("ring") or meta.get("turntable"):
            raise HTTPException(400, "only room and sweep scans can be added to")
        if meta.get("status") == "building":
            raise HTTPException(409, "this scan is being built right now")
        if not [p for p in _photos(d) if p.get("depth_file")]:
            raise HTTPException(400, "this scan has no depth frames to line up with")
        sessions = _read(d / "sessions.json", {"sessions": [{"index": 1, "aligned": True, "transform": None}]})
        index = max(s["index"] for s in sessions["sessions"]) + 1
        _write(d / "live.json", {"kind": meta.get("live", {}).get("kind", kind) if isinstance(meta.get("live"), dict) else kind, "session": index, "patch": True,
                                 "got": [], "finish": False, "prev_status": meta.get("status"), "resume_scan": str(resume),
                                 "old_count": len(_photos(d))})
        meta["status"] = "capturing"
        _save_meta(d, meta)
        sid = str(resume)
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
    with SESSIONS_LOCK:
        old = SESSIONS.pop(sid, None)
    if old is not None:
        old.jobs.put(None)
    s = get_session(sid)
    s.kind = kind
    _notify({"type": "scan", "id": sid, "status": "capturing"})
    return {"scan_id": sid, "ws": f"/ws/live/{sid}", "session": index, "patch": bool(resume)}


def build_cloud(d: Path, max_points: int = 500_000) -> int:
    """scan.ply from the stored frames: a coloured point cloud of every depth frame (the phone used to send one)."""
    import trimesh
    pts, cols = [], []
    for p in _photos(d):
        df = p.get("depth_file")
        if not df or not (d / "photos" / df).exists() or not (d / "photos" / p["file"]).exists():
            continue
        dm = _depth_array(p, (d / "photos" / df).read_bytes())
        if dm is None:
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
    trimesh.PointCloud(P, colors=np.c_[C, np.full(len(C), 255, np.uint8)]).export(d / "scan.ply")
    return len(P)


def _finish_now(d: Path, scan_id: str, info: dict | None) -> None:
    """Build the model from the stored frames (runs in its own thread: it survives the socket dropping)."""
    sess = SESSIONS.get(scan_id)
    meta = _meta(d)
    try:
        if sess is not None:
            sess.jobs.join()
            if not sess.aligned and sess.pending:
                res = sess.try_register(force=True)
                if not res.get("ok"):
                    meta = _meta(d)
                    meta["log_note"] = "The new frames could not be matched to the first scan, so they were left out."
                    _save_meta(d, meta)
        meta = _meta(d)
        if info:
            meta["phone"] = {**(meta.get("phone") or {}), **info}
        live = _read(d / "live.json", {})
        if live.get("patch") and sess is not None and sess.aligned is False:
            meta["status"] = live.get("prev_status") or "ok"       # nothing new could be added: the old model stays
            _save_meta(d, meta)
            _notify({"type": "scan", "id": scan_id, "status": meta["status"]})
            _push(sess, {"type": "state", "status": "error", "scan_id": scan_id, "message": "The new frames could not be matched to the first scan, so nothing was added."})
            return
        meta["status"] = "building"
        meta["photo_count"] = sum(1 for p in _photos(d))
        _save_meta(d, meta)
        n = build_cloud(d)
        meta = ctx.convert(d)
        note = _meta(d).get("log_note")
        _push(sess, {"type": "state", "status": "ready" if meta.get("status") == "ok" else "error", "scan_id": scan_id,
                     "message": (meta.get("log", "")[:300] + (" " + note if note else "")).strip()})
    except Exception as e:
        meta = _meta(d)
        meta["status"] = "error"
        meta["log"] = f"live build failed: {type(e).__name__}: {e}"
        _save_meta(d, meta)
        _push(sess, {"type": "state", "status": "error", "scan_id": scan_id, "message": meta["log"]})
    finally:
        _notify({"type": "scan", "id": scan_id, "status": _meta(d).get("status")})
        _notify({"type": "live_end_map", "id": scan_id})
        if ctx.hub is not None:
            ctx.hub.live_maps.pop(scan_id, None)


def _push(sess: "Session | None", msg: dict) -> None:
    if sess is not None:
        sess.push(msg)


def start_finish(scan_id: str, info: dict | None = None) -> dict:
    d = _scan_dir(scan_id)
    meta = _meta(d)
    if meta.get("status") == "building":
        return {"status": "building"}
    if meta.get("status") != "capturing":
        raise HTTPException(409, f"this scan is {meta.get('status')}, not capturing")
    live = _read(d / "live.json", {})
    live["finish"] = True
    _write(d / "live.json", live)
    meta["status"] = "building"
    _save_meta(d, meta)
    with SESSIONS_LOCK:
        s = SESSIONS.get(scan_id)
    if s is not None:
        s.finished = True
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


async def _ticker(ws: WebSocket, sess: Session) -> None:
    last = 0.0
    try:
        while True:
            await asyncio.sleep(0.2)
            for m in sess.drain():
                await _send(ws, sess, m)
            if time.time() - last >= MAP_EVERY:
                last = time.time()
                msg = await anyio.to_thread.run_sync(sess.next_map)
                if msg is not None:
                    await _send(ws, sess, msg)
                    await _publish_map(sess, msg)
                await anyio.to_thread.run_sync(sess.touch_meta)
    except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
        return


async def _on_frame(ws: WebSocket, sess: Session, data: bytes) -> None:
    try:
        (hlen,) = struct.unpack(">I", data[:4])
        header = json.loads(data[4:4 + hlen])
        jl, dl = int(header.get("jpeg_len", 0)), int(header.get("depth_len", 0))
        body = data[4 + hlen:]
        if len(body) < jl + dl or jl <= 0:
            raise ValueError("the frame is shorter than its header says")
        jpeg, depth = body[:jl], body[jl:jl + dl]
        n = int(header["n"])
        photo = header["photo"]
    except Exception as e:
        await _send(ws, sess, {"type": "error", "message": f"bad frame: {type(e).__name__}: {e}"})
        return
    if sess.finished:
        await _send(ws, sess, {"type": "error", "message": "this scan is already being built"})
        return
    sess.last_seen = time.time()
    ack = await anyio.to_thread.run_sync(sess.store_frame, n, photo, jpeg, depth)
    await _send(ws, sess, {"type": "ack", "n": ack})


async def _on_text(ws: WebSocket, sess: Session, m: dict) -> None:
    t = m.get("type")
    sess.last_seen = time.time()
    if t == "hello":
        await _send(ws, sess, {"type": "ack", "n": sess.acked})
        await _send(ws, sess, {"type": "state", "status": "building" if sess.finished else "capturing", "scan_id": sess.id})
        msg = await anyio.to_thread.run_sync(lambda: sess.next_map(force=True))
        if msg is not None:
            await _send(ws, sess, msg)
    elif t == "pose":
        if sess.engine is not None and m.get("transform") is not None:
            try:
                eng = sess.engine if sess.aligned else sess.pending_engine
                if eng is not None:
                    if sess.aligned and sess.T is not None:
                        eng.set_pose((sess.T @ _mat(m["transform"])).T.tolist())
                    else:
                        eng.set_pose(m["transform"])
            except Exception:
                pass
    elif t == "finish":
        info = m.get("info") if isinstance(m.get("info"), dict) else None
        try:
            out = await anyio.to_thread.run_sync(start_finish, sess.id, info)
            await _send(ws, sess, {"type": "state", "status": out["status"], "scan_id": sess.id})
        except HTTPException as e:
            await _send(ws, sess, {"type": "error", "message": str(e.detail)})
    elif t == "cancel":
        live = _read(sess.d / "live.json", {})
        live["cancelled"] = True
        _write(sess.d / "live.json", live)
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
        j = files.get(Path(e.get("file", "")).name)
        if j is None:
            skipped += 1
            continue
        jpeg = j.file.read()
        dep = b""
        if e.get("depth_file") and Path(e["depth_file"]).name in files:
            dep = files[Path(e["depth_file"]).name].file.read()
        before = sess.next_num
        sess.store_frame(None, e, jpeg, dep)
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
    return {"added": added, "skipped": skipped, "total": sess.photo_count()}


@router.post("/api/scans/{scan_id}/add")
def add_to_scan(scan_id: str, photos: list[UploadFile] = File(default=[]), photos_meta: UploadFile | None = File(None), info: str = Form("")):
    """Patch an existing scan with frames captured offline: store them as a new session, line it up with the old scan, rebuild.
    Returns {aligned, rms_mm, message}; if the new frames cannot be matched the old model is left as it was."""
    d = _scan_dir(scan_id)
    meta = _meta(d)
    if meta.get("kind") != "mesh" or meta.get("ring") or meta.get("turntable"):
        raise HTTPException(400, "only room and sweep scans can be added to")
    if meta.get("status") == "building":
        raise HTTPException(409, "this scan is being built right now")
    entries = _manifest_entries(photos_meta)
    if not [p for p in _photos(d) if p.get("depth_file")]:
        raise HTTPException(400, "this scan has no depth frames to line up with")
    sessions = _read(d / "sessions.json", {"sessions": [{"index": 1, "aligned": True, "transform": None}]})
    index = max(s["index"] for s in sessions["sessions"]) + 1
    sess = Session(d, scan_id, index=index, patch=True, kind=(meta.get("live") or {}).get("kind", "room") if isinstance(meta.get("live"), dict) else "room", engine=False)
    added, skipped = _store_batch(sess, entries, photos)
    res = sess.try_register(force=True)
    out = {"aligned": bool(res.get("ok")), "rms_mm": res.get("rms_mm"), "message": res.get("message", ""), "added": added, "skipped": skipped}
    if res.get("ok"):
        live = _read(d / "live.json", {})
        meta = _meta(d)
        if info:
            try:
                meta["phone"] = {**(meta.get("phone") or {}), **json.loads(info)}
            except ValueError:
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

def abandon(d: Path, scan_id: str) -> None:
    """A capture nobody has touched for IDLE_S: build what there is, or for a patch with nothing added put the old status back."""
    live = _read(d / "live.json", {})
    new_frames = len(_photos(d)) - int(live.get("old_count", 0)) if live.get("patch") else len(_photos(d))
    if live.get("patch") and new_frames <= 0:
        meta = _meta(d)
        meta["status"] = live.get("prev_status") or "ok"
        _save_meta(d, meta)
        live["finish"] = True
        _write(d / "live.json", live)
        drop_session(scan_id)
        _notify({"type": "scan", "id": scan_id, "status": meta["status"]})
        return
    if len(_photos(d)) == 0:
        meta = _meta(d)
        meta["status"] = "error"
        meta["log"] = "the capture ended without any frame"
        _save_meta(d, meta)
        live["finish"] = True
        _write(d / "live.json", live)
        drop_session(scan_id)
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
            meta = _meta(d)
            if meta.get("status") != "capturing":
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
