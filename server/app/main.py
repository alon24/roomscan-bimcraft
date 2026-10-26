import json
import shutil
import time
import uuid
from pathlib import Path

import anyio.to_thread
from anyio import from_thread
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import convert, objects

DATA = Path(__file__).resolve().parent.parent / "data" / "scans"  # /app/data/scans in Docker
STATIC = Path(__file__).parent / "static"
ARTIFACTS = {"scan.json", "scan.usdz", "script.txt", "room.bimcraft", "room.ifc", "plan.png", "meta.json",
             "model.stl", "model.3mf", "preview.json", "report.json"}

app = FastAPI(title="roomscan-bimcraft")


class Hub:
    """Fan-out of events to every connected web page. Keeps the latest live room frame for late joiners."""

    def __init__(self):
        self.viewers: set[WebSocket] = set()
        self.live: dict | None = None

    async def broadcast(self, msg: dict):
        for ws in list(self.viewers):
            try:
                await ws.send_json(msg)
            except Exception:
                self.viewers.discard(ws)


hub = Hub()


def _dir(scan_id: str) -> Path:
    d = DATA / scan_id
    if not scan_id.isalnum() or not d.is_dir():
        raise HTTPException(404, "scan not found")
    return d


def _meta(d: Path) -> dict:
    return json.loads((d / "meta.json").read_text())


def _convert(d: Path) -> dict:
    meta = _meta(d)
    log: list[str] = []
    if meta.get("kind") == "object":
        ok, out = objects.convert_object(d / "scan.usdz", d, meta.get("scale", 1.0))
        meta["status"] = "ok" if (d / "model.stl").exists() else "error"
        meta["printable"] = ok
        meta["log"] = out
        meta["converted_at"] = time.time()
        (d / "meta.json").write_text(json.dumps(meta, indent=1))
        return meta
    try:
        script, log = convert.convert(json.loads((d / "scan.json").read_text()))
        (d / "script.txt").write_text(script)
        ok, out = convert.run_cli(d / "script.txt", d)
        log.append(out)
        meta["status"] = "ok" if ok else "error"
    except Exception as e:  # bad scan JSON, no walls, ...
        log.append(f"{type(e).__name__}: {e}")
        meta["status"] = "error"
    meta["log"] = "\n".join(l for l in log if l)
    meta["converted_at"] = time.time()
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    return meta


@app.post("/api/scans")
async def upload(json_file: UploadFile | None = File(None, alias="json"),
                 usdz: UploadFile | None = File(None), name: str = Form("Scan"),
                 kind: str = Form("room"), scale: float = Form(1.0)):
    if kind not in ("room", "object"):
        raise HTTPException(422, "kind must be room or object")
    if kind == "room" and json_file is None:
        raise HTTPException(422, "room scans need a json file")
    if kind == "object" and usdz is None:
        raise HTTPException(422, "object scans need a usdz file")
    scan_id = uuid.uuid4().hex[:12]
    d = DATA / scan_id
    d.mkdir(parents=True)
    if json_file is not None:
        with open(d / "scan.json", "wb") as f:
            shutil.copyfileobj(json_file.file, f)
    if usdz is not None:
        with open(d / "scan.usdz", "wb") as f:
            shutil.copyfileobj(usdz.file, f)
    (d / "meta.json").write_text(json.dumps({"id": scan_id, "name": name, "kind": kind, "scale": scale, "created_at": time.time()}))
    meta = await anyio.to_thread.run_sync(_convert, d)
    hub.live = None
    await hub.broadcast({"type": "scan", "id": scan_id, "status": meta["status"]})
    await hub.broadcast({"type": "live_end"})
    return {"id": scan_id, "status": meta["status"]}


@app.get("/api/scans")
def list_scans():
    metas = [_meta(d) for d in DATA.glob("*") if (d / "meta.json").exists()] if DATA.exists() else []
    return sorted(metas, key=lambda m: m["created_at"], reverse=True)


@app.get("/api/scans/{scan_id}")
def get_scan(scan_id: str):
    return _meta(_dir(scan_id))


@app.post("/api/scans/{scan_id}/convert")
def reconvert(scan_id: str, scale: float | None = None):
    d = _dir(scan_id)
    if scale is not None:
        meta = _meta(d)
        meta["scale"] = scale
        (d / "meta.json").write_text(json.dumps(meta))
    meta = _convert(d)
    from_thread.run(hub.broadcast, {"type": "scan", "id": scan_id, "status": meta["status"]})
    return meta


@app.post("/api/live")
async def live(room: dict = Body(...)):
    """Partial CapturedRoom from a scan in progress; shown live in the web UI, never stored."""
    hub.live = room
    await hub.broadcast({"type": "live", "room": room})
    return {"viewers": len(hub.viewers)}


@app.websocket("/ws/events")
async def events(ws: WebSocket):
    await ws.accept()
    hub.viewers.add(ws)
    if hub.live is not None:
        await ws.send_json({"type": "live", "room": hub.live})
    try:
        while True:
            await ws.receive_text()  # viewers send nothing; this notices disconnects
    except WebSocketDisconnect:
        pass
    finally:
        hub.viewers.discard(ws)


@app.get("/api/scans/{scan_id}/{file}")
def download(scan_id: str, file: str):
    p = _dir(scan_id) / file
    if file not in ARTIFACTS or not p.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(p, filename=file)


STATIC.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
