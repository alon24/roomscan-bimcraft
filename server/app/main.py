import json
import shutil
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import convert

DATA = Path(__file__).resolve().parent.parent / "data" / "scans"  # /app/data/scans in Docker
STATIC = Path(__file__).parent / "static"
ARTIFACTS = {"scan.json", "scan.usdz", "script.txt", "room.bimcraft", "room.ifc", "plan.png", "meta.json"}

app = FastAPI(title="roomscan-bimcraft")


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
async def upload(json_file: UploadFile = File(..., alias="json"),
                 usdz: UploadFile | None = File(None), name: str = Form("Scan")):
    scan_id = uuid.uuid4().hex[:12]
    d = DATA / scan_id
    d.mkdir(parents=True)
    with open(d / "scan.json", "wb") as f:
        shutil.copyfileobj(json_file.file, f)
    if usdz is not None:
        with open(d / "scan.usdz", "wb") as f:
            shutil.copyfileobj(usdz.file, f)
    (d / "meta.json").write_text(json.dumps({"id": scan_id, "name": name, "created_at": time.time()}))
    meta = _convert(d)
    return {"id": scan_id, "status": meta["status"]}


@app.get("/api/scans")
def list_scans():
    metas = [_meta(d) for d in DATA.glob("*") if (d / "meta.json").exists()] if DATA.exists() else []
    return sorted(metas, key=lambda m: m["created_at"], reverse=True)


@app.get("/api/scans/{scan_id}")
def get_scan(scan_id: str):
    return _meta(_dir(scan_id))


@app.post("/api/scans/{scan_id}/convert")
def reconvert(scan_id: str):
    return _convert(_dir(scan_id))


@app.get("/api/scans/{scan_id}/{file}")
def download(scan_id: str, file: str):
    p = _dir(scan_id) / file
    if file not in ARTIFACTS or not p.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(p, filename=file)


STATIC.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
