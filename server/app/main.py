import os
import io
import json
import re
import secrets
import shutil
import tempfile
import time
import uuid
import zipfile
from typing import Annotated
from pathlib import Path, PurePosixPath

import anyio.to_thread
import numpy as np
import trimesh
from anyio import from_thread
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import apps, photomeasure, features as features_mod, fit as fit_mod, quality, usdzout, convert, frames as frame_tools, meshscan, ring as ring_scan_mod, turntable, objects, photos as photo_index, printprep

DATA = Path(__file__).resolve().parent.parent / "data" / "scans"  # /app/data/scans in Docker
STATIC = Path(__file__).parent / "static"
ARTIFACTS = {"scan.json", "scan.usdz", "scan.ply", "colored.ply", "mesh.bin", "points.bin", "tri_photo.bin", "texturing.json", "footprint.json", "settings.json", "script.txt", "room.bimcraft", "room.ifc", "plan.png", "meta.json",
             "model.stl", "model.3mf", "preview.json", "textured.json", "texture.jpg", "report.json", "photos.json", "photo_index.json",
             "model_parts.3mf", "colours.json", "dowels.stl", "model.usdz", "features.json"}
PRINT_FILE = re.compile(r"^(model_part\d+\.stl|dowels\.stl|model_parts\.3mf|colours\.json|model\.usdz|features\.json)$")  # derived from model.stl: stale once it changes


def _clear_print_prep(d: Path, keep_colours: bool = False):
    for f in d.iterdir():
        if PRINT_FILE.match(f.name) and not (keep_colours and f.name == "colours.json"):
            f.unlink()
MAX_IMPORT_BYTES = 2 * 1024**3  # uncompressed, for the whole zip
SOURCE_FILE = re.compile(r"^source\.(stl|3mf|obj|ply|glb|gltf|off)$")   # a model uploaded by hand, kept as it came
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
    _clear_print_prep(d)
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
        source = next(iter(d.glob("source.*")), None) if not (d / "scan.usdz").exists() else None
        ok, out = (objects.convert_mesh_file(source, d, scale) if source else objects.convert_object(d / "scan.usdz", d, scale))
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
                 usdz: UploadFile | None = File(None), model: UploadFile | None = File(None), mesh: UploadFile | None = File(None), footprint: UploadFile | None = File(None), name: str = Form("Scan"),
                 kind: str = Form("room"), scale: float = Form(1.0), real_height_mm: float = Form(0.0), turntable_scan: bool = Form(False), ring_scan: bool = Form(False), info: str = Form(""), project: str = Form(""),
                 photos: list[UploadFile] = File(default=[]), photos_meta: UploadFile | None = File(None),
                 masks: list[UploadFile] = File(default=[])):
    if kind not in ("room", "object", "mesh"):
        raise HTTPException(422, "kind must be room, object or mesh")
    if kind == "mesh" and mesh is None and not turntable_scan and not ring_scan:
        raise HTTPException(422, "mesh scans need a mesh (ply) file")
    if kind == "room" and json_file is None:
        raise HTTPException(422, "room scans need a json file")
    if kind == "object" and usdz is None and model is None:
        raise HTTPException(422, "object scans need a usdz file (or a model file: stl, 3mf, obj)")
    model_ext = Path(model.filename or "").suffix.lower() if model is not None else ""
    if model is not None and model_ext not in objects.MESH_EXTS:
        raise HTTPException(422, f"model files are {', '.join(objects.MESH_EXTS)}")
    scan_id = uuid.uuid4().hex[:12]
    d = DATA / scan_id
    d.mkdir(parents=True)
    if json_file is not None:
        with open(d / "scan.json", "wb") as f:
            shutil.copyfileobj(json_file.file, f)
    if usdz is not None:
        with open(d / "scan.usdz", "wb") as f:
            shutil.copyfileobj(usdz.file, f)
    if model is not None:   # a ready-made model: kept as it came, so a re-convert (new scale) starts from it
        with open(d / f"source{model_ext}", "wb") as f:
            shutil.copyfileobj(model.file, f)
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
    _clear_print_prep(d)
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
    tags: list[str] | None = Field(None, max_length=12)


@app.patch("/api/scans/{scan_id}")
async def patch_scan(scan_id: str, patch: ScanPatch):
    d = _dir(scan_id)
    meta = _meta(d)
    for k, v in patch.model_dump(exclude_none=True).items():
        if k == "tags":
            meta[k] = sorted({t.strip().lstrip("#")[:30] for t in v if t.strip()})
        else:
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
def export_all(project: str | None = None, ids: str | None = None):
    metas = [_meta(d) for d in DATA.glob("*") if (d / "meta.json").exists()] if DATA.exists() else []
    wanted = set(ids.split(",")) if ids else None
    ids = [m["id"] for m in metas if (project is None or m.get("project", "") == project) and (wanted is None or m["id"] in wanted)]
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
            known = rel in ARTIFACTS or SOURCE_FILE.match(rel) or (len(parts) == 3 and parts[1] == "photos" and PHOTO_NAME.match(parts[2])) \
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


def _model(d: Path):
    if not (d / "model.stl").exists():
        raise HTTPException(404, "this scan has no printable model.stl")
    return printprep.load(d / "model.stl")


@app.get("/api/scans/{scan_id}/print")
def print_info(scan_id: str):
    """Size, bed fit and a suggested cut for the printable model."""
    m = _model(_dir(scan_id))
    size = [round(float(x), 1) for x in m.extents]
    return {"size_mm": size, "bed_mm": printprep.BED_MM, "suggested_cut": printprep.suggest_cut(size)}


class Cut(BaseModel):
    axis: str = Field(pattern="^[xyz]$")
    at: float = Field(gt=0, description="mm from the low end of the whole part along the axis")


class Connector(BaseModel):
    type: str = Field("dowel", pattern="^(none|dowel|peg|key)$")
    diameter: float = Field(6.0, ge=2, le=30, description="mm; a key is this wide")
    depth: float = Field(8.0, ge=2, le=40, description="mm into each half")
    clearance: float = Field(0.3, ge=0, le=1, description="mm of play in each hole")
    count: int = Field(2, ge=1, le=6, description="per cut face")


class CutRequest(BaseModel):
    cuts: list[Cut] = Field(min_length=1, max_length=8)
    connector: Connector = Connector()


def _store_cut(d: Path, parts: list, pins: list, log: list[str]) -> dict:
    """Write the parts, pins and the one 3MF that holds them, and describe the result."""
    _clear_print_prep(d, keep_colours=True)   # an earlier cut's parts are replaced; the colours still hold
    files = []
    for i, part in enumerate(parts, 1):
        part.export(d / f"model_part{i}.stl")
        files.append(f"model_part{i}.stl")
    pin_meshes = printprep.pin_meshes(pins)
    if pin_meshes:
        trimesh.util.concatenate(pin_meshes).export(d / "dowels.stl")
        files.append("dowels.stl")
    printprep.lay_out(parts, pin_meshes).export(d / "model_parts.3mf")
    files.append("model_parts.3mf")
    est = [printprep.estimate(p) for p in parts]
    bed = printprep.BED_MM
    return {"files": files[-1:] + files[:-1], "log": log, "parts": len(parts), "pins": len(pins),
            "size_mm": [[round(float(x), 1) for x in p.extents] for p in parts], "estimates": est,
            "fits_bed": all(p.extents[0] <= bed[0] and p.extents[1] <= bed[1] and p.extents[2] <= bed[2] for p in parts),
            "total": {"grams": round(sum(e["grams"] for e in est), 1), "hours": round(sum(e["hours"] for e in est), 1)}}


def _do_cut(d: Path, cuts: list[tuple[str, float]], c: Connector) -> dict:
    try:
        parts, pins, log = printprep.cut_all(_model(d), cuts, c.type, c.diameter, c.depth, c.clearance, c.count)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _store_cut(d, parts, pins, log)


@app.post("/api/scans/{scan_id}/cut")
def cut_model(scan_id: str, req: CutRequest):
    """Split model.stl at every plane, join the cuts with connectors, lay each part flat. One 3MF holds all parts."""
    return _do_cut(_dir(scan_id), [(x.axis, x.at) for x in req.cuts], req.connector)


Vec3 = Annotated[list[float], Field(min_length=3, max_length=3)]


class PlaneConnector(BaseModel):
    type: str = Field("dowel", pattern="^(none|dowel|peg|key)$")
    shape: str | None = Field(None, pattern="^(circle|square|triangle|hexagon)$", description="default: square for a key, else circle")
    size: float = Field(6.0, ge=2, le=30, description="mm across (triangle, hexagon: across the corners)")
    depth: float = Field(8.0, ge=2, le=40, description="mm into each half")
    clearance: float = Field(0.3, ge=0, le=1, description="mm of play in each hole")
    count: int = Field(2, ge=1, le=6, description="per cut face, when the spots are automatic")


class CutPlane(BaseModel):
    origin: Vec3 = Field(description="a point on the plane, mm, in model.stl's frame (the same frame as print/mesh.json)")
    normal: Vec3 = Field(description="the plane's direction; 'upper' is the side it points to")
    connectors: list[Annotated[list[float], Field(min_length=2, max_length=2)]] | None = Field(
        None, max_length=24, description="[u, v] spots on the cut face, in the plane's own mm (see cut/preview); automatic when absent")


class Sides(BaseModel):
    upper: bool
    lower: bool


class PlaneCutRequest(BaseModel):
    planes: list[CutPlane] = Field(min_length=1, max_length=8)
    connector: PlaneConnector = PlaneConnector()
    keep: Sides = Sides(upper=True, lower=True)
    flip: Sides = Sides(upper=False, lower=False)


class PreviewRequest(BaseModel):
    plane: CutPlane
    connector: PlaneConnector = PlaneConnector()


def _thin(m: trimesh.Trimesh, max_faces: int) -> trimesh.Trimesh:
    if len(m.faces) <= max_faces:
        return m
    import open3d as o3d
    o = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(m.vertices), o3d.utility.Vector3iVector(m.faces))
    o = o.simplify_quadric_decimation(max_faces)
    return trimesh.Trimesh(vertices=np.asarray(o.vertices), faces=np.asarray(o.triangles), process=False)


@app.post("/api/scans/{scan_id}/cut/preview")
def cut_preview(scan_id: str, req: PreviewRequest):
    """The cut face at a plane (outlines in the plane's own u, v mm) and where connectors would go automatically,
    so the browser can show the face and let the user add, move or remove connector spots."""
    c = req.connector
    try:
        return printprep.cut_preview(_model(_dir(scan_id)), req.plane.origin, req.plane.normal, c.type, c.shape, c.size, c.count)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/scans/{scan_id}/cut/plane")
def cut_plane(scan_id: str, req: PlaneCutRequest):
    """Cut on any plane (Orca-style): keep or discard each side, flip it, join the cut with connectors of a chosen shape
    at automatic or chosen spots. Same files as /cut, plus `views`: each part in place (model.stl's frame) for an exploded view."""
    d = _dir(scan_id)
    c = req.connector
    planes = [dict(origin=p.origin, normal=p.normal, points=[tuple(x) for x in p.connectors] if p.connectors is not None else None,
                   label="the cut plane" if len(req.planes) == 1 else f"cut plane {i}") for i, p in enumerate(req.planes, 1)]
    try:
        parts, pins, log, views = printprep.cut_planes(
            _model(d), planes, c.type, c.shape, c.size, c.depth, c.clearance, c.count,
            req.keep.upper, req.keep.lower, req.flip.upper, req.flip.lower)
        if c.type != "none" and req.keep.upper and req.keep.lower and not pins and not any("pegs" in l or "keys" in l for l in log):
            if any("too small" in l for l in log):
                p0 = planes[0]
                pv = printprep.cut_preview(_model(d), p0["origin"], p0["normal"], "none", c.shape, c.size, c.count)
                raise ValueError(f"a {c.size:g} mm connector does not fit on this cut face (up to {pv['max_size_mm']:g} mm fits): "
                                 "make it smaller, move the cut, or pick 'none'")
    except ValueError as e:
        raise HTTPException(400, str(e))
    out = _store_cut(d, parts, pins, log)
    out["views"] = []
    for side, m in views:
        t = _thin(m, 15000)
        out["views"].append({"side": side, "vertices": np.round(t.vertices, 2).ravel().tolist(), "faces": t.faces.ravel().tolist()})
    return out


class AutoFit(BaseModel):
    connector: Connector = Connector()


@app.post("/api/scans/{scan_id}/autofit")
def autofit(scan_id: str, req: AutoFit = AutoFit()):
    """Fit the model to the printer: cut it into bed-sized parts (equal cuts along each too-long axis, joined with the chosen connector)
    and give a rough filament and time estimate. A model that already fits just gets the estimate."""
    d = _dir(scan_id)
    m = _model(d)
    cuts = printprep.auto_cuts([float(x) for x in m.extents])
    if not cuts:
        e = printprep.estimate(m)
        return {"files": [], "parts": 1, "log": [f"the model already fits the {printprep.BED_MM[0]:g} x {printprep.BED_MM[1]:g} x {printprep.BED_MM[2]:g} mm bed"],
                "estimates": [e], "fits_bed": True, "total": {"grams": e["grams"], "hours": e["hours"]}, "size_mm": [e["size_mm"]], "pins": 0}
    out = _do_cut(d, cuts, req.connector)
    out["cuts"] = [{"axis": a, "at": round(at, 1)} for a, at in cuts]
    return out


def _view_mesh(d: Path, max_faces: int) -> trimesh.Trimesh:
    """model.stl for the browser: very dense meshes are thinned."""
    return _thin(_model(d), max_faces)


@app.get("/api/scans/{scan_id}/print/mesh.json")
def print_mesh(scan_id: str, max_faces: int = 60000):
    """model.stl as vertices and faces (mm, Z up) for the cut view."""
    m = _view_mesh(_dir(scan_id), max_faces)
    return {"vertices": np.round(m.vertices, 2).ravel().tolist(), "faces": m.faces.ravel().tolist(),
            "size_mm": [round(float(x), 1) for x in m.extents], "origin_mm": [round(float(x), 2) for x in m.bounds[0]]}


@app.post("/api/scans/{scan_id}/fit")
def fit_check(scan_id: str, reference: UploadFile | None = File(None), against: str | None = Form(None)):
    """Line a reference mesh (STL, 3MF, OBJ, PLY, in mm) up on this scan and return how far each point of the scan is from it.
    The reference can also be another scan on this server (`against` = its id), to see how a part changed between two scans."""
    d = _dir(scan_id)
    ext = Path(reference.filename or "").suffix.lower().lstrip(".") if reference else ""
    if not against and ext not in ("stl", "3mf", "obj", "ply", "glb", "off"):
        raise HTTPException(400, "the reference must be an stl, 3mf, obj, ply, glb or off file")
    try:
        if against:
            ref = _model(_dir(against))
        else:
            loaded = trimesh.load(io.BytesIO(reference.file.read()), file_type=ext, force="mesh")
            ref = loaded if isinstance(loaded, trimesh.Trimesh) else trimesh.util.concatenate(list(loaded.geometry.values()))
        if not len(ref.faces):
            raise ValueError("no surface in the file")
        scan = _view_mesh(d, 40000)
        res = fit_mod.compare(scan, ref)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"could not compare: {type(e).__name__}: {e}")
    res.update(vertices=np.round(scan.vertices, 2).ravel().tolist(), faces=scan.faces.ravel().tolist())
    return res


@app.get("/api/scans/{scan_id}/section.svg")
def section(scan_id: str, axis: str = "z", at: float = 1.0):
    """Cross-section outline of model.stl at a plane (mm from the low end along the axis)."""
    from fastapi.responses import Response
    if axis not in "xyz" or len(axis) != 1:
        raise HTTPException(400, "axis must be x, y or z")
    m = _model(_dir(scan_id))
    lo, hi = m.bounds[:, "xyz".index(axis)]
    try:
        svg = printprep.section_svg(m, axis, min(max(at, 0.01), float(hi - lo) - 0.01))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(svg, media_type="image/svg+xml", headers={"Content-Disposition": 'inline; filename="section.svg"'})


@app.get("/api/scans/{scan_id}/colours")
def colours(scan_id: str, k: int = 4):
    """Filament colour suggestions from the scan's own colours (cached as colours.json)."""
    d = _dir(scan_id)
    p = d / "colours.json"
    if not p.exists() or k != 4:
        try:
            res = printprep.suggest_colours(d, max(1, min(k, 8)))
        except ValueError as e:
            raise HTTPException(404, str(e))
        if k == 4:
            p.write_text(json.dumps(res))
        return res
    return json.loads(p.read_text())


@app.get("/api/print/tolerance.3mf")
def tolerance_3mf(size: float = 6.0, depth: float = 8.0):
    """A test bar with holes of 0.1 to 0.5 mm clearance and two pins: print it to find the clearance that fits this printer."""
    try:
        scene = printprep.tolerance_test(max(2.0, min(size, 20.0)), max(3.0, min(depth, 20.0)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(scene.export(file_type="3mf"), media_type="model/3mf",
                    headers={"Content-Disposition": f'attachment; filename="tolerance-test-{size:g}mm.3mf"'})


@app.get("/api/scans/{scan_id}/model.usdz")
def model_usdz(scan_id: str):
    """The model in real size for AR Quick Look on the phone (made on first request)."""
    d = _dir(scan_id)
    out = d / "model.usdz"
    if not out.exists():
        try:
            usdzout.make_usdz(d, out)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
    return FileResponse(out, media_type="model/vnd.usdz+zip", filename=f"{_meta(d).get('name', scan_id)}.usdz".replace("/", "-"))


@app.get("/api/scans/{scan_id}/model_colour.3mf")
def model_colour_3mf(scan_id: str, k: int = 4):
    """model.stl with one of the suggested filaments on every triangle, for a multi-colour print."""
    try:
        data, used = printprep.colour_3mf(_dir(scan_id), max(2, min(k, 6)))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return Response(data, media_type="model/3mf", headers={"Content-Disposition": 'attachment; filename="model-colour.3mf"',
                                                           "X-Colours": json.dumps(used)})


def _shared(token: str) -> Path:
    """The scan folder a share link points to. Only scans that were shared and not revoked."""
    if DATA.exists() and re.fullmatch(r"[A-Za-z0-9_-]{8,40}", token):
        for d in DATA.glob("*"):
            try:
                if (d / "meta.json").exists() and _meta(d).get("share") == token:
                    return d
            except Exception:
                continue
    raise HTTPException(404, "this link is not shared (any more)")


@app.post("/api/scans/{scan_id}/share")
def share_on(scan_id: str, request: Request):
    """Make a read-only link to the 3D model: /s/<token>. Anyone who can reach this server and has the link can look at it and
    download the STL; nothing else of the scan. DELETE revokes it."""
    d = _dir(scan_id)
    if not (d / "model.stl").exists():
        raise HTTPException(400, "only scans with a printable model can be shared")
    meta = _meta(d)
    meta.setdefault("share", secrets.token_urlsafe(9))
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    return {"token": meta["share"], "path": f"/s/{meta['share']}", "url": str(request.base_url).rstrip("/") + f"/s/{meta['share']}"}


@app.delete("/api/scans/{scan_id}/share")
def share_off(scan_id: str):
    d = _dir(scan_id)
    meta = _meta(d)
    meta.pop("share", None)
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    return {"shared": False}


@app.get("/s/{token}")
def share_page(token: str):
    _shared(token)
    return FileResponse(Path(__file__).parent / "static" / "share.html", media_type="text/html")


@app.get("/s/{token}/mesh.json")
def share_mesh(token: str):
    d = _shared(token)
    m = _view_mesh(d, 60000)
    out = {"name": _meta(d).get("name", ""), "vertices": np.round(m.vertices, 2).ravel().tolist(), "faces": m.faces.ravel().tolist(),
           "size_mm": [round(float(x), 1) for x in m.extents]}
    ply = d / "colored.ply"
    if ply.exists() and len(m.faces) == len(_model(d).faces):          # colours only when the mesh was not thinned
        rgb = printprep.vertex_rgb_for(m, ply)
        if rgb is not None:
            out["colors"] = np.round(rgb / 255.0, 3).ravel().tolist()
    return out


@app.get("/s/{token}/model.stl")
def share_stl(token: str):
    d = _shared(token)
    return FileResponse(d / "model.stl", media_type="model/stl", filename=f"{_meta(d).get('name', 'model')}.stl".replace("/", "-"))


@app.get("/api/scans/{scan_id}/features")
def scan_features(scan_id: str):
    """Flat faces, round bosses and round holes with real sizes (cached as features.json)."""
    d = _dir(scan_id)
    p = d / "features.json"
    if not p.exists():
        p.write_text(json.dumps(features_mod.detect(_model(d))))
    return json.loads(p.read_text())


@app.get("/api/scans/{scan_id}/quality")
def scan_quality(scan_id: str):
    """Score, coverage gaps and tips for a ring scan (cached as quality.json)."""
    d = _dir(scan_id)
    if not (d / "photos.json").exists() or not json.loads((d / "meta.json").read_text()).get("ring"):
        raise HTTPException(404, "the quality report is for ring scans")
    return quality.cached(d)


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
    if not (file in ARTIFACTS or PRINT_FILE.match(file)) or not p.exists():
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


app.include_router(apps.router)   # the Android APK and iPhone IPA, from data/apps
app.include_router(photomeasure.router)   # measure from a photo with a card or paper as the ruler
app.mount("/", NoCacheStatic(directory=STATIC, html=True), name="ui")
