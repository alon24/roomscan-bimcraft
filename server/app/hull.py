"""Shape from the part's outline in every photo (a visual hull).

The ring markers give each photo's exact position, and the phone sends an outline ("mask") of the part for each photo. A grid of
small cubes over the stage is kept only where every photo agrees it is inside the part's outline. Photos are sharp (1920 px), so
this is far more accurate than the phone's LiDAR depth for small parts, which is interpolated between about 600 dots. Hollows
that no outline can see (the inside of a cup) are filled; the LiDAR surface is still there for those.
"""
import json
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image
from scipy import ndimage as ndi

from . import frames, meshscan


def trusted_masks(d: Path) -> set[str]:
    """Photos whose mask came from the phone or was drawn by hand (not the rough automatic ones)."""
    out = set()
    for name in ("phone.json", "edited.json"):
        p = d / "masks" / name
        if p.exists():
            out |= set(json.loads(p.read_text()))
    return out


def _project(ph: dict, R: np.ndarray, t: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Ring millimetres -> photo pixels (u, v)."""
    k = np.array(ph["intrinsics"], dtype=float)
    p = pts @ R.T + t
    return np.c_[k[0][0] * p[:, 0] / p[:, 2] + k[2][0], k[1][1] * p[:, 1] / p[:, 2] + k[2][1]]


def choose_instances(labels: np.ndarray, ph: dict, R: np.ndarray, t: np.ndarray, cfg: dict) -> np.ndarray | None:
    """The phone's outline tool finds separate things in a photo (the part, the plate with the ring, a hand...). Keep the things
    that stand in the middle of the stage, and leave out the one that is the ring and plate. None = keep everything."""
    h, w = labels.shape
    sx, sy = w / ph["width"], h / ph["height"]

    def labels_at(pts):
        uv = _project(ph, R, t, pts) * [sx, sy]
        ok = (uv[:, 0] >= 0) & (uv[:, 0] < w) & (uv[:, 1] >= 0) & (uv[:, 1] < h)
        return labels[uv[ok, 1].astype(int), uv[ok, 0].astype(int)], ok.sum()
    a = np.linspace(0, 2 * np.pi, 180, endpoint=False)
    rr = (cfg["ring_inner_d_mm"] / 2 + cfg["ring_outer_d_mm"] / 2) / 2
    ring_pts = np.c_[rr * np.cos(a), rr * np.sin(a), np.zeros_like(a)]
    on_ring, n_ring = labels_at(ring_pts)
    column = np.c_[np.zeros(14), np.zeros(14), np.arange(10, 80, 5.0)]
    on_col, _ = labels_at(column)
    plate = {int(l) for l in set(on_ring.tolist()) if l and (on_ring == l).sum() > 0.3 * max(n_ring, 1)}
    keep = {int(l) for l in set(on_col.tolist()) if l and l not in plate}
    if not keep:
        return None
    return np.isin(labels, list(keep))


def carve(d: Path, scale: float = 1.0, voxel: float = 1.5, misses: int | None = None, height: float = 320.0):
    """Returns (mesh in metres with Y up, colours, log) or None if there are too few outlined photos."""
    from skimage.measure import marching_cubes
    from .ring import _detector, config, ring_pose
    cfg = config(d)
    det = _detector()
    inner_r = cfg["stage_d_mm"] / 2 - 1.0
    trusted = trusted_masks(d)
    views = []
    for ph in frames.manifest(d):
        mp = frames.mask_path(d, ph)
        path = d / "photos" / ph["file"]
        if ph["file"] not in trusted or not mp.exists() or not path.exists():
            continue
        rgb = Image.open(path).convert("RGB")
        pose = ring_pose(np.asarray(rgb.convert("L")), ph, cfg, det)
        if pose is None or pose[3] > 3.0:
            continue
        R, t = pose[0], pose[1]
        m = Image.open(mp).convert("L")
        if m.size != rgb.size:
            m = m.resize(rgb.size, Image.NEAREST)
        mask = np.asarray(m) > 127          # an outline that includes the plate is fine: the other photos carve the plate away
        lp = d / "masks" / (Path(ph["file"]).stem + "-labels.png")
        if lp.exists():                     # the phone sent the separate things it found: drop the ring and plate
            chosen = choose_instances(np.asarray(Image.open(lp)), ph, R, t, cfg)
            if chosen is not None:
                mask = np.asarray(Image.fromarray(chosen.astype(np.uint8) * 255).resize(rgb.size, Image.NEAREST)) > 127
        if mask.sum() < 100:
            continue
        views.append((ph, R, t, mask, np.asarray(rgb.resize((rgb.width // 2, rgb.height // 2)))))
    if len(views) < 6:
        return None
    if misses is None:                                   # a few photos with a poor outline must not cut the part
        misses = int(round(0.05 * len(views)))
    xs = np.arange(-inner_r, inner_r, voxel) + voxel / 2
    zs = np.arange(0, height, voxel) + voxel / 2
    gx, gy, gz = np.meshgrid(xs, xs, zs, indexing="ij")
    X = np.c_[gx.ravel(), gy.ravel(), gz.ravel()]
    alive = np.hypot(X[:, 0], X[:, 1]) < inner_r
    miss = np.zeros(len(X), dtype=np.int16)
    seen_n = np.zeros(len(X), dtype=np.int16)          # a cube no photo looks at proves nothing: it must be seen often
    for ph, R, t, mask, _ in views:
        k = np.array(ph["intrinsics"], dtype=float)
        h, w = mask.shape
        sx, sy = w / ph["width"], h / ph["height"]
        p = X @ R.T + t
        zc = p[:, 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (k[0][0] * p[:, 0] / zc + k[2][0]) * sx
            v = (k[1][1] * p[:, 1] / zc + k[2][1]) * sy
        seen = (zc > 1) & (u >= 0) & (u < w - 1) & (v >= 0) & (v < h - 1)
        hit = np.zeros(len(X), dtype=bool)
        hit[seen] = mask[v[seen].astype(int), u[seen].astype(int)]
        miss += (seen & ~hit).astype(np.int16)
        seen_n += seen.astype(np.int16)
    occ = (alive & (miss <= misses) & (seen_n >= max(4, len(views) // 2))).reshape(gx.shape)
    lab, n = ndi.label(occ)
    if n == 0:
        return None
    occ = lab == (1 + int(np.argmax(ndi.sum(occ, lab, range(1, n + 1)))))
    if occ[..., -1].any():
        top = "the part reaches the top of the carving space; it may be taller than %d mm" % int(height)
    else:
        top = ""
    vol = np.pad(ndi.gaussian_filter(occ.astype(float), 0.8), 1)
    verts, faces, normals, _ = marching_cubes(vol, 0.5, spacing=(voxel, voxel, voxel))
    verts = verts - voxel + np.array([xs[0], xs[0], zs[0]])           # undo the padding, back to ring millimetres
    mm = trimesh.Trimesh(verts, faces[:, ::-1], process=True)          # outward-facing triangles, duplicates merged
    mm.update_faces(mm.nondegenerate_faces())
    mm.remove_unreferenced_vertices()
    pts, nrm = np.asarray(mm.vertices), np.asarray(mm.vertex_normals)
    # colour: from the photo that looks most squarely at each point
    best = np.full(len(pts), -1.0)
    col = np.full((len(pts), 3), 170, dtype=np.uint8)
    for ph, R, t, mask, small in views:
        to_cam = (-R.T @ t) - pts
        facing = np.einsum("ij,ij->i", nrm, to_cam) / np.maximum(np.linalg.norm(to_cam, axis=1), 1e-6)
        k = np.array(ph["intrinsics"], dtype=float)
        p = pts @ R.T + t
        hs, ws = small.shape[:2]
        u = (k[0][0] * p[:, 0] / p[:, 2] + k[2][0]) * ws / ph["width"]
        v = (k[1][1] * p[:, 1] / p[:, 2] + k[2][1]) * hs / ph["height"]
        ok = (facing > best) & (u >= 0) & (u < ws - 1) & (v >= 0) & (v < hs - 1)
        col[ok] = small[v[ok].astype(int), u[ok].astype(int)]
        best[ok] = facing[ok]
    mesh = trimesh.Trimesh(np.c_[pts[:, 0], pts[:, 2], -pts[:, 1]] / 1000.0 * scale, mm.faces, process=False)   # metres, Y up
    log = (f"outline carving from {len(views)} photos, {voxel} mm cubes, {len(mesh.faces)} triangles" + (f"; {top}" if top else ""))
    return mesh, col, log, len(views)
