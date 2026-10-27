import os
import json
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import anyio.to_thread
from anyio import from_thread
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import convert, frames as frame_tools, meshscan, ring as ring_scan_mod, turntable, objects, photos as photo_index

DATA = Path(__file__).resolve().parent.parent / "data" / "scans"  # /app/data/scans in Docker
STATIC = Path(__file__).parent / "static"
ARTIFACTS = {"scan.json", "scan.usdz", "scan.ply", "colored.ply", "mesh.bin", "points.bin", "tri_photo.bin", "texturing.json", "footprint.json", "settings.json", "script.txt", "room.bimcraft", "room.ifc", "plan.png", "meta.json",
             "model.stl", "model.3mf", "preview.json", "textured.json", "texture.jpg", "report.json", "photos.json", "photo_index.json"}
MAX_IMPORT_BYTES = 2 * 1024**3  # uncompressed, for the whole zip
PHOTO_NAME = re.compile(r"^[\w-]+\.(jpg|depth)$")
MASK_NAME = re.compile(r"^([\w-]+\.png|edited\.json)$")

app = FastAPI(title="roomscan-bimcraft")


@app.get("/api/version")
def version():
    """Set at image build time by deploy.sh: VERSION file + git commit count, same as the iOS build."""
    return {"version": os.environ.get("APP_VERSION", "dev"), "build": os.environ.get("APP_BUILD", "0")}


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
    if meta.get("kind") == "mesh":
        if meta.get("ring"):  # printed marker ring: every frame knows where the ring is
            ok, out = ring_scan_mod.fuse(d, meta.get("scale", 1.0))
        elif meta.get("turntable"):  # fixed phone, spinning plate: fuse the LiDAR frames in the part's own frame
            ok, out = turntable.fuse(d, meta.get("scale", 1.0))
        else:
            # a free scan with depth frames (a room, a corner): merge the depth into a clean surface; else fit one through the points
            room = not (d / "footprint.json").exists() and frame_tools.depth_frames(d) >= 5
            ok, out = meshscan.convert_mesh(d, meta.get("scale", 1.0), surface=not room)
            if room:
                try:
                    ok2, log2 = frame_tools.tsdf_room(d, meta.get("scale", 1.0))
                except Exception as e:
                    ok2, log2 = False, f"{type(e).__name__}: {e}"
                if ok2:
                    ok, out = True, out + "\n" + log2
                else:
                    ok, out = meshscan.convert_mesh(d, meta.get("scale", 1.0))
                    out += f"\n(merged-depth surface failed: {log2}; fitted through the points instead)"
        try:  # automatic "this is the part" mask for every frame that has LiDAR depth
            n = frame_tools.detect_all(d)
            if n:
                out += f"\nauto-detected the part in {n} frames"
        except Exception as e:
            out += f"\nframe detection failed: {type(e).__name__}: {e}"
        meta["status"] = "ok" if ok else "error"
        meta["log"] = out
        meta["converted_at"] = time.time()
        meta["settings"] = _settings(d, meta)
        (d / "meta.json").write_text(json.dumps(meta, indent=1))
        return meta
    if meta.get("kind") == "object":
        scale = meta.get("scale", 1.0)
        if meta.get("real_height_mm"):  # a turntable scan has no scale of its own: size it from the real height
            objects.convert_object(d / "scan.usdz", d, 1.0)
            z = json.loads((d / "report.json").read_text()).get("size_mm", [0, 0, 0])[2] if (d / "report.json").exists() else 0
            if z > 0:
                scale = meta["real_height_mm"] / z
                meta["scale"] = scale
        ok, out = objects.convert_object(d / "scan.usdz", d, scale)
        meta["status"] = "ok" if (d / "model.stl").exists() else "error"
        meta["printable"] = ok
        meta["log"] = out
        meta["converted_at"] = time.time()
        meta["settings"] = _settings(d, meta)
        (d / "meta.json").write_text(json.dumps(meta, indent=1))
        return meta
    try:
        script, log = convert.convert(json.loads((d / "scan.json").read_text()))
        if (d / "photos.json").exists():
            idx = photo_index.index_photos(json.loads((d / "scan.json").read_text()), json.loads((d / "photos.json").read_text()))
            (d / "photo_index.json").write_text(json.dumps(idx))
            log.append(f"photos cover {len(idx)} surfaces")
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


def _json_or_empty(text: str) -> dict:
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _settings(d: Path, meta: dict) -> dict:
    """What this model was made with: the converter's own notes plus the scan's basics."""
    p = d / "settings.json"
    out = json.loads(p.read_text()) if p.exists() else {}
    if meta.get("kind") == "object" and "mode" not in out:
        out.update(mode="Object capture (photos, built on the phone)", cleanup="largest piece, holes filled, normals fixed, centred on the bed")
    out.setdefault("scale", meta.get("scale", 1.0))
    if meta.get("real_height_mm"):
        out["real_height_mm"] = meta["real_height_mm"]
    out["photos"] = meta.get("photo_count", 0)
    return out


@app.post("/api/scans")
async def upload(json_file: UploadFile | None = File(None, alias="json"),
                 usdz: UploadFile | None = File(None), mesh: UploadFile | None = File(None), footprint: UploadFile | None = File(None), name: str = Form("Scan"),
                 kind: str = Form("room"), scale: float = Form(1.0), real_height_mm: float = Form(0.0), turntable_scan: bool = Form(False), ring_scan: bool = Form(False), info: str = Form(""), project: str = Form(""),
                 photos: list[UploadFile] = File(default=[]), photos_meta: UploadFile | None = File(None),
                 masks: list[UploadFile] = File(default=[])):
    if kind not in ("room", "object", "mesh"):
        raise HTTPException(422, "kind must be room, object or mesh")
    if kind == "mesh" and mesh is None and not turntable_scan and not ring_scan:
        raise HTTPException(422, "mesh scans need a mesh (ply) file")
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
    if mesh is not None:
        with open(d / "scan.ply", "wb") as f:
            shutil.copyfileobj(mesh.file, f)
    if footprint is not None:
        (d / "footprint.json").write_bytes(await footprint.read())
    if masks:  # the part's outline in each photo, made on the phone
        (d / "masks").mkdir(exist_ok=True)
        names = []
        for m in masks:
            fname = Path(m.filename or "").name
            if MASK_NAME.match(fname) and fname.endswith(".png"):
                (d / "masks" / fname).write_bytes(await m.read())
                names.append(fname.replace(".png", ".jpg"))
        (d / "masks" / "phone.json").write_text(json.dumps(names))
    photo_count = 0
    if photos:
        (d / "photos").mkdir()
        for p in photos:
            fname = Path(p.filename or "").name
            if PHOTO_NAME.match(fname):
                with open(d / "photos" / fname, "wb") as f:
                    shutil.copyfileobj(p.file, f)
                photo_count += fname.endswith(".jpg")
        if photos_meta is not None:
            (d / "photos.json").write_bytes(await photos_meta.read())
    (d / "meta.json").write_text(json.dumps({"id": scan_id, "name": name, "kind": kind, "scale": scale,
                                             "photo_count": photo_count, "project": project.strip(), "created_at": time.time(),
                                             **({"real_height_mm": real_height_mm} if real_height_mm > 0 else {}),
                                             **({"turntable": True} if turntable_scan else {}),
                                             **({"ring": True} if ring_scan else {}),
                                             **({"phone": _json_or_empty(info)} if info else {})}))
    meta = await anyio.to_thread.run_sync(_convert, d)
    hub.live = None
    await hub.broadcast({"type": "scan", "id": scan_id, "status": meta["status"]})
    await hub.broadcast({"type": "live_end"})
    return {"id": scan_id, "status": meta["status"]}


@app.get("/api/scans/{scan_id}/frames")
def list_frames(scan_id: str):
    d = _dir(scan_id)
    edited = set(json.loads((d / "masks" / "edited.json").read_text())) if (d / "masks" / "edited.json").exists() else set()
    return [{"file": p["file"], "width": p["width"], "height": p["height"], "has_depth": bool(p.get("depth_file")),
             "has_mask": frame_tools.mask_path(d, p).exists(), "edited": p["file"] in edited} for p in frame_tools.manifest(d)]


@app.get("/api/scans/{scan_id}/masks/{name}")
def get_mask(scan_id: str, name: str, overlay: bool = False, solid: bool = False):
    """The mask PNG. With ?overlay=1 a red, half-transparent version to lay over the photo."""
    p = _dir(scan_id) / "masks" / name
    if not MASK_NAME.match(name) or not p.exists():
        raise HTTPException(404, "no mask")
    if not overlay:
        return FileResponse(p)
    from io import BytesIO

    import numpy as np
    from PIL import Image
    m = np.asarray(Image.open(p).convert("L")) > 127
    rgba = np.zeros(m.shape + (4,), dtype=np.uint8)
    rgba[m] = (255, 40, 40, 255 if solid else 130)
    buf = BytesIO()
    Image.fromarray(rgba).save(buf, "PNG")
    from fastapi.responses import Response
    return Response(buf.getvalue(), media_type="image/png")


@app.put("/api/scans/{scan_id}/masks/{name}")
async def put_mask(scan_id: str, name: str, request: Request):
    """Save a hand-corrected mask: a PNG whose alpha (or brightness, if opaque) marks the part."""
    d = _dir(scan_id)
    if not MASK_NAME.match(name) or not name.endswith(".png"):
        raise HTTPException(422, "bad mask name")
    from io import BytesIO

    import numpy as np
    from PIL import Image
    try:
        img = Image.open(BytesIO(await request.body()))
        arr = np.asarray(img.convert("RGBA"))
    except Exception:
        raise HTTPException(422, "not a PNG")
    m = arr[..., 3] > 127 if (arr[..., 3] < 255).any() else arr[..., :3].max(axis=-1) > 127
    (d / "masks").mkdir(exist_ok=True)
    Image.fromarray((m * 255).astype(np.uint8)).save(d / "masks" / name)
    frame_tools.mark_edited(d, name.replace(".png", ".jpg"))
    return {"ok": True}


@app.post("/api/scans/{scan_id}/frames/{file}/detect")
def detect_frame(scan_id: str, file: str):
    d = _dir(scan_id)
    n = frame_tools.detect_all(d, only=file)
    if not n:
        raise HTTPException(404, "no depth for that frame")
    return {"ok": True}


@app.post("/api/scans/{scan_id}/rebuild")
def rebuild(scan_id: str):
    """Fuse every frame (its depth, colour and mask) into a new point cloud and surface."""
    d = _dir(scan_id)
    meta = _meta(d)
    if meta.get("ring"):        # ring scans: the outlines (phone or corrected by hand) carve the shape
        ok, out = ring_scan_mod.fuse(d, meta.get("scale", 1.0))
        meta["settings"] = _settings(d, meta)
    else:
        ok, out = frame_tools.fuse(d, meta.get("scale", 1.0))
    meta["status"] = "ok" if ok else "error"
    meta["log"] = out
    meta["converted_at"] = time.time()
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    from_thread.run(hub.broadcast, {"type": "scan", "id": scan_id, "status": meta["status"]})
    return meta


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
        meta.pop("real_height_mm", None)  # a scale typed by hand wins over the one worked out from the real height
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


class ScanPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    project: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=5000)


@app.patch("/api/scans/{scan_id}")
async def patch_scan(scan_id: str, patch: ScanPatch):
    d = _dir(scan_id)
    meta = _meta(d)
    for k, v in patch.model_dump(exclude_none=True).items():
        meta[k] = v.strip() if k != "notes" else v
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    await hub.broadcast({"type": "list"})
    return meta


@app.delete("/api/scans/{scan_id}")
async def delete_scan(scan_id: str):
    shutil.rmtree(_dir(scan_id))
    await hub.broadcast({"type": "list", "deleted": scan_id})
    return {"deleted": scan_id}


def _zip_scans(ids: list[str]) -> Path:
    """Zip scan folders as <id>/<files>, plus a small manifest. Returns a temp file for the caller to delete."""
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("roomscan-export.json", json.dumps({"version": 1, "scans": ids}))
        for sid in ids:
            d = DATA / sid
            for f in d.rglob("*"):
                if f.is_file():
                    z.write(f, f"{sid}/{f.relative_to(d).as_posix()}")
    return Path(tmp.name)


def _zip_response(path: Path, filename: str) -> FileResponse:
    return FileResponse(path, filename=filename, media_type="application/zip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.get("/api/scans/{scan_id}/bundle.zip")
def scan_bundle(scan_id: str):
    _dir(scan_id)
    return _zip_response(_zip_scans([scan_id]), f"{_meta(DATA / scan_id).get('name', scan_id)}.zip".replace("/", "_"))


@app.get("/api/export.zip")
def export_all(project: str | None = None):
    metas = [_meta(d) for d in DATA.glob("*") if (d / "meta.json").exists()] if DATA.exists() else []
    ids = [m["id"] for m in metas if project is None or m.get("project", "") == project]
    if not ids:
        raise HTTPException(404, "nothing to export")
    return _zip_response(_zip_scans(ids), f"{project or 'roomscan-all'}.zip".replace("/", "_"))


@app.post("/api/import")
async def import_zip(file: UploadFile = File(...), project: str | None = Form(None)):
    """Restore scans from an export zip. Only known artifact names are extracted, and every scan gets a new id."""
    imported = []
    try:
        z = zipfile.ZipFile(file.file)
    except zipfile.BadZipFile:
        raise HTTPException(422, "not a zip file")
    with z:
        groups: dict[str, list[tuple[zipfile.ZipInfo, str]]] = {}
        total = 0
        for info in z.infolist():
            parts = PurePosixPath(info.filename).parts
            if info.is_dir() or len(parts) < 2:
                continue
            rel = "/".join(parts[1:])
            known = rel in ARTIFACTS or (len(parts) == 3 and parts[1] == "photos" and PHOTO_NAME.match(parts[2])) \
                or (len(parts) == 3 and parts[1] == "masks" and MASK_NAME.match(parts[2]))
            if known:
                total += info.file_size
                groups.setdefault(parts[0], []).append((info, rel))
        if total > MAX_IMPORT_BYTES:
            raise HTTPException(413, "bundle too large")
        for old_id, members in groups.items():
            if not any(rel == "meta.json" for _, rel in members):
                continue
            new_id = uuid.uuid4().hex[:12]
            d = DATA / new_id
            for info, rel in members:
                target = d / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            try:
                meta = json.loads((d / "meta.json").read_text())
                meta.update({"id": new_id, "imported_from": old_id})
                if project is not None:
                    meta["project"] = project.strip()
                (d / "meta.json").write_text(json.dumps(meta, indent=1))
            except (ValueError, OSError):
                shutil.rmtree(d, ignore_errors=True)
                continue
            imported.append({"id": new_id, "name": meta.get("name", new_id)})
    if not imported:
        raise HTTPException(422, "no scans found in zip")
    await hub.broadcast({"type": "list"})
    return {"imported": imported}


def _projects_file() -> Path:
    return DATA.parent / "projects.json"


def _stored_projects() -> list[str]:
    try:
        return json.loads(_projects_file().read_text())
    except (OSError, ValueError):
        return []


def _save_projects(names: list[str]):
    _projects_file().parent.mkdir(parents=True, exist_ok=True)
    _projects_file().write_text(json.dumps(sorted(set(names))))


def _all_metas() -> list[dict]:
    return [_meta(d) for d in DATA.glob("*") if (d / "meta.json").exists()] if DATA.exists() else []


def _project_name(name: str) -> str:
    name = name.strip()
    if not name or "/" in name or len(name) > 200:
        raise HTTPException(422, "project name must be 1-200 characters and contain no '/'")
    return name


class ProjectBody(BaseModel):
    name: str


@app.get("/api/projects")
def list_projects():
    """A project is a home: a named group of rooms and objects. Empty projects exist until deleted."""
    counts: dict[str, int] = {}
    for m in _all_metas():
        if m.get("project"):
            counts[m["project"]] = counts.get(m["project"], 0) + 1
    names = set(_stored_projects()) | set(counts)
    return [{"name": n, "scans": counts.get(n, 0)} for n in sorted(names, key=str.lower)]


@app.post("/api/projects")
async def create_project(body: ProjectBody):
    name = _project_name(body.name)
    _save_projects(_stored_projects() + [name])
    await hub.broadcast({"type": "list"})
    return {"name": name}


@app.patch("/api/projects/{name}")
async def rename_project(name: str, body: ProjectBody):
    new = _project_name(body.name)
    for m in _all_metas():
        if m.get("project") == name:
            m["project"] = new
            (DATA / m["id"] / "meta.json").write_text(json.dumps(m, indent=1))
    _save_projects([new if n == name else n for n in _stored_projects()] + [new])
    _save_projects([n for n in _stored_projects() if n != name])
    await hub.broadcast({"type": "list"})
    return {"name": new}


@app.delete("/api/projects/{name}")
async def delete_project(name: str, delete_scans: bool = False):
    """Removes the project. Its rooms are kept (moved to 'no project') unless delete_scans is set."""
    removed = 0
    for m in _all_metas():
        if m.get("project") == name:
            if delete_scans:
                shutil.rmtree(DATA / m["id"])
                removed += 1
            else:
                m["project"] = ""
                (DATA / m["id"] / "meta.json").write_text(json.dumps(m, indent=1))
    _save_projects([n for n in _stored_projects() if n != name])
    await hub.broadcast({"type": "list"})
    return {"deleted": name, "scans_deleted": removed}


@app.get("/api/scans/{scan_id}/photos/{name}")
def photo(scan_id: str, name: str, up: bool = False):
    """The photo. With ?up=1 turned the right way up using the phone's position (the sensor image is always landscape)."""
    d = _dir(scan_id)
    p = d / "photos" / name
    if not PHOTO_NAME.match(name) or not p.exists():
        raise HTTPException(404, "photo not found")
    if up and name.endswith(".jpg"):
        meta = next((x for x in frame_tools.manifest(d) if x.get("file") == name), None)
        if meta and meta.get("transform"):
            x_up, y_up = meta["transform"][0][1], meta["transform"][1][1]   # world height of the camera's right and up axes
            angle = (90 if x_up > 0 else -90) if abs(x_up) > abs(y_up) else (180 if y_up < 0 else 0)
            if angle:
                from io import BytesIO

                from fastapi.responses import Response
                from PIL import Image
                buf = BytesIO()
                Image.open(p).rotate(angle, expand=True).save(buf, "JPEG", quality=88)
                return Response(buf.getvalue(), media_type="image/jpeg")
    return FileResponse(p)


@app.get("/api/scans/{scan_id}/{file}")
def download(scan_id: str, file: str):
    p = _dir(scan_id) / file
    if file not in ARTIFACTS or not p.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(p, filename=file)


STATIC.mkdir(exist_ok=True)


# ---- printed ring: what the user measured, and a photo check of it
@app.get("/api/ring")
def ring_info():
    cal = ring_scan_mod.calibration_file(DATA)
    if cal.exists():
        j = json.loads(cal.read_text())
        return {"calibrated": True, "marker_size_mm": j["config"]["marker_size_mm"], "centre_radius_mm": j["config"]["centre_radius_mm"],
                "saved_at": j.get("saved_at"), "error_px": j.get("error_px"), "lidar_scale": j.get("lidar_scale")}
    c = ring_scan_mod.build_config()
    return {"calibrated": False, "marker_size_mm": c["marker_size_mm"], "centre_radius_mm": c["centre_radius_mm"]}


@app.post("/api/ring/calibrate")
async def ring_calibrate(marker_mm: float = Form(0.0), centre_distance_mm: float = Form(0.0), save: bool = Form(True),
                         photos: list[UploadFile] = File(default=[]), photos_meta: UploadFile | None = File(None)):
    """Photos of the printed ring (with LiDAR depth) plus the caliper readings. Saved for ring scans only if the photos check out."""
    import tempfile
    if photos_meta is None or not photos:
        raise HTTPException(422, "send photos and photos_meta")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "photos").mkdir()
        for p in photos:
            fname = Path(p.filename or "").name
            if PHOTO_NAME.match(fname):
                (work / "photos" / fname).write_bytes(await p.read())
        (work / "photos.json").write_bytes(await photos_meta.read())
        result = await anyio.to_thread.run_sync(ring_scan_mod.calibrate, work, marker_mm or None, centre_distance_mm or None)
    result["saved"] = False
    if save and result["good"]:
        cal = ring_scan_mod.calibration_file(DATA)
        cal.parent.mkdir(parents=True, exist_ok=True)
        cal.write_text(json.dumps({**{k: result[k] for k in ("error_px", "lidar_scale", "markers_avg", "edge_px_avg")},
                                   "config": result["config"], "saved_at": time.time()}, indent=1))
        result["saved"] = True
    result.pop("config", None)
    return result


@app.post("/api/ring/measured")
def ring_measured(marker_mm: float = Form(...), across_mm: float = Form(...)):
    """Caliper readings without photos: the marker size and the distance across the ring (outer edge of a marker to the outer
    edge of the one opposite). The centre radius follows: across = 2 x radius + marker size."""
    if not (20 <= marker_mm <= 24 and 176 <= across_mm <= 192):
        raise HTTPException(422, "marker size should be about 22 mm and the distance across about 184 mm")
    cfg = ring_scan_mod.build_config(marker_mm, (across_mm - marker_mm) / 2)
    cal = ring_scan_mod.calibration_file(DATA)
    cal.parent.mkdir(parents=True, exist_ok=True)
    cal.write_text(json.dumps({"config": cfg, "saved_at": time.time(), "manual": True,
                               "marker_mm": marker_mm, "across_mm": across_mm}, indent=1))
    return {"saved": True, "marker_size_mm": cfg["marker_size_mm"], "centre_radius_mm": round(cfg["centre_radius_mm"], 3)}


@app.delete("/api/ring/calibration")
def ring_reset():
    ring_scan_mod.calibration_file(DATA).unlink(missing_ok=True)
    return {"ok": True}


# ---- problem reports from the app, so they can be read here instead of described from a phone screen ----
def _issues() -> Path:
    return DATA.parent / "issues"


@app.post("/api/issues")
async def add_issue(body: dict = Body(...)):
    """The app posts what went wrong plus its recent log. Kept as one small JSON file per report."""
    raw = json.dumps(body)
    if len(raw) > 1_000_000:
        raise HTTPException(413, "report too large")
    _issues().mkdir(parents=True, exist_ok=True)
    issue_id = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    body = {**body, "id": issue_id, "received_at": time.time()}
    (_issues() / f"{issue_id}.json").write_text(json.dumps(body, indent=1))
    await hub.broadcast({"type": "issue", "id": issue_id})
    return {"id": issue_id}


@app.get("/api/issues")
def list_issues():
    if not _issues().exists():
        return []
    items = [json.loads(p.read_text()) for p in _issues().glob("*.json")]
    return sorted(items, key=lambda i: i.get("received_at", 0), reverse=True)


@app.delete("/api/issues/{issue_id}")
def delete_issue(issue_id: str):
    p = _issues() / f"{issue_id}.json"
    if not re.fullmatch(r"[\w-]+", issue_id) or not p.exists():
        raise HTTPException(404, "issue not found")
    p.unlink()
    return {"ok": True}


class NoCacheStatic(StaticFiles):
    """The page changes with every deploy: make the browser check for a new copy instead of showing an old one."""

    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


app.mount("/", NoCacheStatic(directory=STATIC, html=True), name="ui")
