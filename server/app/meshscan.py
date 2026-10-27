"""ARKit scene-reconstruction mesh + capture photos -> coloured mesh.

The phone sends the raw triangle mesh (PLY, ARKit world frame: Y up, metres) and the photos with
poses, intrinsics and optional depth maps. Colours are picked here, per vertex, from the photo that
sees the vertex most head-on and closest, and that is not hidden behind something nearer (depth map).
"""
import json
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

from .photos import project

GREY = np.array([150, 150, 150], dtype=np.uint8)


def _depth_map(d: Path, photo: dict) -> np.ndarray | None:
    name, w, h = photo.get("depth_file"), photo.get("depth_width"), photo.get("depth_height")
    p = d / "photos" / name if name else None
    if not (p and w and h and p.exists()):
        return None
    a = np.fromfile(p, dtype="<f4")
    return a.reshape(h, w) if a.size == w * h else None


def colour_vertices(mesh: trimesh.Trimesh, d: Path, photos: list[dict]) -> tuple[np.ndarray, float]:
    """Per-vertex RGB (uint8) and the fraction of vertices that got a photo colour."""
    pts = np.asarray(mesh.vertices, dtype=float)
    nrm = np.asarray(mesh.vertex_normals, dtype=float)
    best = np.zeros(len(pts))
    rgb = np.tile(GREY, (len(pts), 1))
    for ph in photos:
        path = d / "photos" / ph["file"]
        if not path.exists():
            continue
        img = np.asarray(Image.open(path).convert("RGB"))
        h, w = img.shape[:2]
        uv, depth = project(ph, pts)
        cam = np.array(ph["transform"], dtype=float)[3, :3]  # column 3 = camera position
        to_cam = cam - pts
        dist = np.linalg.norm(to_cam, axis=1)
        facing = np.einsum("ij,ij->i", nrm, to_cam) / np.maximum(dist, 1e-6)
        ok = (depth > 0.1) & (facing > 0.15) & (uv[:, 0] >= 0) & (uv[:, 0] < w - 1) & (uv[:, 1] >= 0) & (uv[:, 1] < h - 1)
        dm = _depth_map(d, ph)
        if dm is not None and ok.any():  # drop vertices hidden behind nearer surfaces
            dh, dw = dm.shape
            du = np.clip((uv[:, 0] * dw / w).astype(int), 0, dw - 1)
            dv = np.clip((uv[:, 1] * dh / h).astype(int), 0, dh - 1)
            seen = dm[dv, du]
            ok &= ~((seen > 0) & (depth > seen + 0.15))
        score = np.where(ok, facing / np.maximum(depth, 0.2) ** 2, 0)
        better = score > best
        if better.any():
            ui = uv[better, 0].astype(int)
            vi = uv[better, 1].astype(int)
            rgb[better] = img[vi, ui]
            best[better] = score[better]
    return rgb, float((best > 0).mean()) if len(pts) else 0.0


def pack_preview(mesh: trimesh.Trimesh, rgb: np.ndarray) -> bytes:
    """float32 xyz | uint8 rgb (padded to 4 bytes) | uint32 triangle indices. Counts are in report.json."""
    colours = rgb.astype(np.uint8).tobytes()
    colours += b"\0" * (-len(colours) % 4)
    return (np.asarray(mesh.vertices, dtype="<f4").tobytes() + colours
            + np.asarray(mesh.faces, dtype="<u4").tobytes())


def convert_mesh(d: Path, scale: float = 1.0) -> tuple[bool, str]:
    """Write mesh.bin (viewer), colored.ply and report.json into d."""
    try:
        mesh = trimesh.load(d / "scan.ply", process=False)
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
            return False, "scan.ply has no triangles"
        if scale != 1.0:
            mesh.apply_scale(scale)
        manifest = json.loads((d / "photos.json").read_text()) if (d / "photos.json").exists() else {}
        photos = manifest.get("photos", []) if isinstance(manifest, dict) else manifest
        rgb, covered = colour_vertices(mesh, d, photos)
        (d / "mesh.bin").write_bytes(pack_preview(mesh, rgb))
        mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=np.c_[rgb, np.full(len(rgb), 255, dtype=np.uint8)])
        mesh.export(d / "colored.ply")
        rep = {"vertices": int(len(mesh.vertices)), "faces": int(len(mesh.faces)),
               "size_m": [round(float(x), 2) for x in mesh.extents], "coloured": round(covered, 3), "photos": len(photos)}
        (d / "report.json").write_text(json.dumps(rep))
        return True, (f"{rep['faces']} triangles, {rep['size_m']} m, {round(covered * 100)}% of the surface coloured "
                      f"from {len(photos)} photos")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
