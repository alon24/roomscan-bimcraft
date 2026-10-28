#!/usr/bin/env python3
"""Stream an existing scan's frames to a server over the live-capture WebSocket, as the phone would (docs/live-guide.md).

    python tools/live_replay.py --scan-dir data/scans/<id> --server http://localhost:8010 --kind sweep --speed 5 --finish
    python tools/live_replay.py --scan-dir <dir> --drop-after 12          # cut the connection after 12 frames, reconnect, resume
    python tools/live_replay.py --scan-dir <dir> --resume-scan <old id>   # patch mode: add these frames to an existing scan
    python tools/live_replay.py --scan-dir <dir> --offline-after 12       # stop after 12 frames, send the rest with POST /frames

It creates its own scan on the server (unless --resume-scan) and prints the maps and needs it receives. Uses the `websockets` package."""
import argparse
import json
import struct
import sys
import time
import urllib.request
from pathlib import Path


def manifest(scan_dir: Path) -> list[dict]:
    m = json.loads((scan_dir / "photos.json").read_text())
    return m.get("photos", []) if isinstance(m, dict) else m


def pack_frame(scan_dir: Path, entry: dict, n: int) -> bytes:
    """[u32 big-endian header_len][header JSON][jpeg][depth] as in the contract."""
    jpeg = (scan_dir / "photos" / entry["file"]).read_bytes()
    depth = b""
    if entry.get("depth_file") and (scan_dir / "photos" / entry["depth_file"]).exists():
        depth = (scan_dir / "photos" / entry["depth_file"]).read_bytes()
    header = json.dumps({"type": "frame", "n": n, "jpeg_len": len(jpeg), "depth_len": len(depth), "photo": entry}).encode()
    return struct.pack(">I", len(header)) + header + jpeg + depth


def post_json(url: str, obj: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


class Link:
    """A WebSocket to /ws/live/{id} that remembers the cumulative ack and the last map."""

    def __init__(self, server: str, scan_id: str, verbose: bool = True):
        self.url = server.replace("http", "ws", 1).rstrip("/") + f"/ws/live/{scan_id}"
        self.ws = None
        self.acked = 0
        self.last_map = None
        self.maps = 0
        self.states: list[str] = []
        self.aligned: list[dict] = []
        self.errors: list[str] = []
        self.verbose = verbose

    def open(self):
        from websockets.sync.client import connect
        self._cm = connect(self.url, max_size=None)
        self.ws = self._cm.__enter__()
        self.ws.send(json.dumps({"type": "hello", "resume_after": self.acked, "app": "live_replay"}))

    def drop(self):
        """Close without a goodbye, the way a lost Wi-Fi does."""
        if self.ws is not None:
            try:
                self.ws.socket.close()
            except Exception:
                pass
            self.ws = None

    def pump(self, wait: float = 0.0):
        while self.ws is not None:
            try:
                raw = self.ws.recv(timeout=wait)
            except TimeoutError:
                return
            except Exception:
                self.ws = None
                return
            m = json.loads(raw)
            t = m.get("type")
            if t == "ack":
                self.acked = max(self.acked, m["n"])
            elif t == "map":
                self.last_map = m
                self.maps += 1
                if self.verbose:
                    top = m["needs"][0]["text"] if m["needs"] else "-"
                    print(f"  map #{m['seq']}: {m['percent']}%  needs {len(m['needs'])}  {top[:90]}")
            elif t == "state":
                self.states.append(m["status"])
                if self.verbose:
                    print(f"  state: {m['status']} {m.get('message', '')[:100]}")
            elif t == "aligned":
                self.aligned.append(m)
                if self.verbose:
                    print(f"  aligned: {m['ok']} {m.get('rms_mm')} {m.get('message', '')[:100]}")
            elif t == "error":
                self.errors.append(m["message"])
                print("  error:", m["message"], file=sys.stderr)
            wait = 0.0


def replay(server: str, scan_dir: Path, kind: str = "sweep", name: str = "Replay", speed: float = 5.0, drop_after: int = 0, resume_scan: str | None = None,
           finish: bool = False, limit: int = 0, wait_ready: float = 120.0, verbose: bool = True, entries: list[dict] | None = None) -> dict:
    entries = entries if entries is not None else manifest(scan_dir)
    if limit:
        entries = entries[:limit]
    body = {"kind": kind, "name": name, "info": {"app": "live_replay"}}
    if resume_scan:
        body["resume_scan"] = resume_scan
    started = post_json(server.rstrip("/") + "/api/live", body)
    sid = started["scan_id"]
    if verbose:
        print(f"live scan {sid} ({'patch session ' + str(started['session']) if started['patch'] else 'new'}), {len(entries)} frames")
    link = Link(server, sid, verbose)
    link.open()
    n = 0
    sent_after_drop = 0
    while n < len(entries):
        n += 1
        link.ws.send(pack_frame(scan_dir, entries[n - 1], n)) if link.ws else None
        link.pump(0.0)
        time.sleep(0.5 / max(speed, 0.01))
        if drop_after and n == drop_after:
            link.pump(0.3)
            if verbose:
                print(f"  -- dropping the connection after frame {n} (acked {link.acked})")
            link.drop()
            time.sleep(1.0)
            link.open()
            link.pump(0.5)
            n = max(0, link.acked - 3)         # resend from a bit before the ack: the server must ignore what it already has
            drop_after = 0
            if verbose:
                print(f"  -- reconnected, resuming from frame {n + 1}")
    for _ in range(40):
        link.pump(0.1)
        if link.acked >= len(entries):
            break
    result = {"scan_id": sid, "acked": link.acked, "frames": len(entries), "maps": link.maps, "last_map": link.last_map,
              "aligned": link.aligned, "errors": link.errors, "link": link}
    if finish:
        link.ws.send(json.dumps({"type": "finish", "info": {"scan type": "Sweep from one spot" if kind == "sweep" else "Free scan", "sweep": kind == "sweep"}}))
        t0 = time.time()
        while time.time() - t0 < wait_ready:
            link.pump(0.3)
            st = get_json(server.rstrip("/") + f"/api/scans/{sid}")["status"]
            if st in ("ok", "error"):
                break
        result["status"] = get_json(server.rstrip("/") + f"/api/scans/{sid}")["status"]
        if verbose:
            print("finished:", result["status"])
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan-dir", required=True, type=Path)
    ap.add_argument("--server", default="http://localhost:8010")
    ap.add_argument("--kind", default="sweep", choices=["room", "sweep"])
    ap.add_argument("--name", default="Replay")
    ap.add_argument("--speed", type=float, default=5.0, help="1 = the phone's pace (2 frames a second), 5 = five times faster")
    ap.add_argument("--drop-after", type=int, default=0)
    ap.add_argument("--resume-scan", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--finish", action="store_true")
    ap.add_argument("--offline-after", type=int, default=0, help="stream this many frames, then send the rest in one POST /api/scans/{id}/frames")
    a = ap.parse_args()
    if a.offline_after:
        entries = manifest(a.scan_dir)
        r = replay(a.server, a.scan_dir, a.kind, a.name, a.speed, limit=a.offline_after, finish=False, entries=entries)
        import uuid
        boundary = uuid.uuid4().hex
        parts = []
        rest = entries[a.offline_after - 3:]
        for e in rest:
            for key in ("file", "depth_file"):
                if e.get(key) and (a.scan_dir / "photos" / e[key]).exists():
                    parts.append((e[key], (a.scan_dir / "photos" / e[key]).read_bytes()))
        body = b""
        for fn, data in parts:
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="photos"; filename="{fn}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n"
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="photos_meta"; filename="photos.json"\r\nContent-Type: application/json\r\n\r\n'.encode()
        body += json.dumps({"photos": rest}).encode() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(a.server.rstrip("/") + f"/api/scans/{r['scan_id']}/frames", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        print("append:", urllib.request.urlopen(req, timeout=300).read().decode())
        print("finish:", urllib.request.urlopen(urllib.request.Request(a.server.rstrip("/") + f"/api/scans/{r['scan_id']}/finish", data=b"", method="POST")).read().decode())
        return
    r = replay(a.server, a.scan_dir, a.kind, a.name, a.speed, a.drop_after, a.resume_scan, a.finish, a.limit)
    print({k: v for k, v in r.items() if k not in ("link", "last_map")})


if __name__ == "__main__":
    main()
