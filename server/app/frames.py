"""Per-frame review: the phone sends every frame (photo + LiDAR depth + pose). Here each frame gets an automatic
"this is the part" mask from the depth and the traced outline, the user can correct masks by hand, and the
corrected frames are fused again into one point cloud and surface."""
import json
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image
from scipy import ndimage as ndi

from . import meshscan
from .photos import _mat


def manifest(d: Path) -> list[dict]:
    p = d / "photos.json"
    if not p.exists():
        return []
    m = json.loads(p.read_text())
    return m.get("photos", []) if isinstance(m, dict) else m


def _footprint(d: Path) -> dict | None:
    p = d / "footprint.json"
    return json.loads(p.read_text()) if p.exists() else None


def mask_path(d: Path, photo: dict) -> Path:
    return d / "masks" / (Path(photo["file"]).stem + ".png")


def world_points(photo: dict, depth: np.ndarray) -> np.ndarray:
    """World position (H x W x 3) of every depth pixel. ARKit camera: +X right, +Y up, looks down -Z; image rows go down."""
    h, w = depth.shape
    k = np.array(photo["intrinsics"], dtype=float)  # columns [fx,0,0],[0,fy,0],[cx,cy,1] for the photo size
    sx, sy = w / photo["width"], h / photo["height"]
    fx, fy, cx, cy = k[0][0] * sx, k[1][1] * sy, k[2][0] * sx, k[2][1] * sy
    u, v = np.meshgrid(np.arange(w), np.arange(h))
    cam = np.stack([(u - cx) * depth / fx, -(v - cy) * depth / fy, -depth, np.ones_like(depth)], axis=-1)
    return (cam.reshape(-1, 4) @ _mat(photo["transform"]).T)[:, :3].reshape(h, w, 3)


def auto_mask(d: Path, photo: dict, fp: dict | None) -> np.ndarray | None:
    """Photo-sized boolean mask of the part. With a traced outline: what the LiDAR sees inside it. Without: the surface in the
    middle of the picture and everything connected to it at about the same distance."""
    dm = meshscan._depth_map(d, photo)
    if dm is None:
        return None
    valid = (dm > 0.1) & (dm < 5)
    if fp:
        pts = world_points(photo, dm)
        poly = np.array(fp["polygon"], dtype=float)
        obj = valid & meshscan._inside(pts[..., [0, 2]].reshape(-1, 2), poly, 1.08).reshape(dm.shape) \
            & (pts[..., 1] >= fp["base_y"] - 0.02) & (pts[..., 1] <= fp["base_y"] + fp["height"] + 0.02)
    else:
        h, w = dm.shape
        centre = dm[h // 3: 2 * h // 3, w // 3: 2 * w // 3]
        centre = centre[(centre > 0.1) & (centre < 5)]
        if centre.size == 0:
            return None
        obj = valid & (np.abs(dm - np.median(centre)) < 0.12)
    obj = ndi.binary_closing(ndi.binary_opening(obj, iterations=1), iterations=2)
    obj = ndi.binary_fill_holes(obj)
    lab, n = ndi.label(obj)
    if n > 1:  # keep the biggest piece
        obj = lab == (1 + int(np.argmax(ndi.sum(obj, lab, range(1, n + 1)))))
    big = Image.fromarray((obj * 255).astype(np.uint8)).resize((photo["width"], photo["height"]), Image.BILINEAR)
    return np.asarray(big) > 127


def save_mask(d: Path, photo: dict, mask: np.ndarray) -> None:
    p = mask_path(d, photo)
    p.parent.mkdir(exist_ok=True)
    Image.fromarray((mask * 255).astype(np.uint8)).save(p)


def detect_all(d: Path, only: str | None = None, keep_edited: bool = True) -> int:
    """Automatic masks for every frame that has depth. Frames the user edited by hand are left alone."""
    fp = _footprint(d)
    edited = set(json.loads((d / "masks" / "edited.json").read_text())) if (d / "masks" / "edited.json").exists() else set()
    n = 0
    for ph in manifest(d):
        if only and ph["file"] != only:
            continue
        if keep_edited and ph["file"] in edited and not only:
            continue
        m = auto_mask(d, ph, fp)
        if m is not None:
            save_mask(d, ph, m)
            n += 1
    if only and only in edited:
        edited.discard(only)
        (d / "masks" / "edited.json").write_text(json.dumps(sorted(edited)))
    return n


def mark_edited(d: Path, name: str) -> None:
    (d / "masks").mkdir(exist_ok=True)
    p = d / "masks" / "edited.json"
    edited = set(json.loads(p.read_text())) if p.exists() else set()
    edited.add(name)
    p.write_text(json.dumps(sorted(edited)))


def fuse(d: Path, scale: float = 1.0) -> tuple[bool, str]:
    """Rebuild the cloud from every frame's depth and colour, keeping only masked pixels, then mesh it as usual."""
    pts_all, col_all, used = [], [], 0
    for ph in manifest(d):
        dm = meshscan._depth_map(d, ph)
        img_path = d / "photos" / ph["file"]
        if dm is None or not img_path.exists():
            continue
        h, w = dm.shape
        ok = (dm > 0.1) & (dm < 5)
        # flying pixels at depth edges
        ok &= (ndi.maximum_filter(dm, 3) - ndi.minimum_filter(dm, 3)) < (0.04 * dm + 0.01)
        mp = mask_path(d, ph)
        if mp.exists():
            m = Image.open(mp).convert("L").resize((w, h), Image.NEAREST)
            ok &= np.asarray(m) > 127
        if not ok.any():
            continue
        img = Image.open(img_path).convert("RGB").resize((w, h), Image.BILINEAR)
        pts = world_points(ph, dm)
        pts_all.append(pts[ok])
        col_all.append(np.asarray(img)[ok])
        used += 1
    if not pts_all:
        return False, "no frame had depth data"
    pts, cols = np.vstack(pts_all), np.vstack(col_all)
    # one point per 5 mm cell
    cell = np.floor(pts / 0.005).astype(np.int64)
    _, first = np.unique(cell, axis=0, return_index=True)
    pts, cols = pts[first], cols[first]
    cloud = trimesh.PointCloud(pts, colors=np.c_[cols, np.full(len(cols), 255, dtype=np.uint8)])
    ok, log = meshscan.convert_cloud(d, cloud, scale)
    return ok, f"fused {used} frames: " + log
