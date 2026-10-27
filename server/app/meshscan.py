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


def colour_vertices(mesh: trimesh.Trimesh, d: Path, photos: list[dict], with_mask: bool = False):
    """Per-vertex RGB (uint8) and the fraction of vertices that got a photo colour (plus which ones, with_mask)."""
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
    covered = float((best > 0).mean()) if len(pts) else 0.0
    return (rgb, covered, best > 0) if with_mask else (rgb, covered)


def pack_preview(mesh: trimesh.Trimesh, rgb: np.ndarray) -> bytes:
    """float32 xyz | uint8 rgb (padded to 4 bytes) | uint32 triangle indices. Counts are in report.json."""
    colours = rgb.astype(np.uint8).tobytes()
    colours += b"\0" * (-len(colours) % 4)
    return (np.asarray(mesh.vertices, dtype="<f4").tobytes() + colours
            + np.asarray(mesh.faces, dtype="<u4").tobytes())


def poisson(points: np.ndarray, colours: np.ndarray, depth: int = 8) -> trimesh.Trimesh | None:
    """Surface from an oriented point cloud (Open3D Poisson). None if Open3D is not installed."""
    try:
        import open3d as o3d
    except ImportError:
        return None
    pc = o3d.geometry.PointCloud()
    pc.points = o3d.utility.Vector3dVector(points)
    pc.colors = o3d.utility.Vector3dVector(colours[:, :3] / 255.0)
    pc.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=0.06, max_nn=30))
    pc.orient_normals_consistent_tangent_plane(20)
    mesh, density = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pc, depth=depth)
    d = np.asarray(density)
    mesh.remove_vertices_by_mask(d < np.quantile(d, 0.06))  # trim the balloon Poisson grows where there is no data
    if len(mesh.triangles) == 0:
        return None
    out = trimesh.Trimesh(np.asarray(mesh.vertices), np.asarray(mesh.triangles), process=False)
    out.visual = trimesh.visual.ColorVisuals(out, vertex_colors=(np.asarray(mesh.vertex_colors) * 255).astype(np.uint8))
    return out


def pack_points(points: np.ndarray, rgb: np.ndarray) -> bytes:
    colours = rgb[:, :3].astype(np.uint8).tobytes()
    return points.astype("<f4").tobytes() + colours


def _inside(xz: np.ndarray, poly: np.ndarray, grow: float = 1.0) -> np.ndarray:
    """Which (x, z) points are inside the polygon (ray casting), the polygon scaled about its centre by `grow`."""
    c = poly.mean(axis=0)
    q = c + (poly - c) * grow
    x, z = xz[:, 0], xz[:, 1]
    inside = np.zeros(len(xz), dtype=bool)
    j = len(q) - 1
    for i in range(len(q)):
        xi, zi, xj, zj = q[i, 0], q[i, 1], q[j, 0], q[j, 1]
        crosses = (zi > z) != (zj > z)
        with np.errstate(divide="ignore", invalid="ignore"):
            xcross = (xj - xi) * (z - zi) / (zj - zi) + xi
        inside ^= crosses & (x < xcross)
        j = i
    return inside


def apply_footprint(pts: np.ndarray, cols: np.ndarray, fp: dict) -> tuple[np.ndarray, np.ndarray]:
    """Keep only what is inside the traced outline (extruded up by the height), and add a flat base across the outline.
    The base closes the underside, so the surface fit makes a solid without the part ever being flipped."""
    poly = np.array(fp["polygon"], dtype=float)
    base_y, height = float(fp["base_y"]), float(fp["height"])
    keep = _inside(pts[:, [0, 2]], poly, 1.08) & (pts[:, 1] >= base_y - 0.02) & (pts[:, 1] <= base_y + height + 0.02)
    pts, cols = pts[keep], cols[keep]
    lo, hi = poly.min(axis=0), poly.max(axis=0)
    step = max(0.006, float(np.max(hi - lo)) / 70)
    gx, gz = np.meshgrid(np.arange(lo[0], hi[0], step), np.arange(lo[1], hi[1], step))
    grid = np.c_[gx.ravel(), gz.ravel()]
    grid = grid[_inside(grid, poly, 1.0)]
    base = np.c_[grid[:, 0], np.full(len(grid), base_y), grid[:, 1]]
    colour = cols[:, :3].mean(axis=0) if len(cols) else np.array([150, 150, 150])
    base_cols = np.tile(np.r_[colour, 255], (len(base), 1))
    return np.vstack([pts, base]), np.vstack([cols, base_cols]).astype(np.uint8)


def printable(mesh: trimesh.Trimesh, d: Path) -> dict:
    """Write model.stl / model.3mf (mm, Z up, on the bed) and return what the printer needs to know."""
    from . import objects
    m = trimesh.Trimesh(np.c_[mesh.vertices[:, 0], -mesh.vertices[:, 2], mesh.vertices[:, 1]] * 1000.0, mesh.faces, process=False)
    m, _ = objects.clean(m)
    m.export(d / "model.stl")
    m.export(d / "model.3mf")
    return objects.report(m)


def recolour_from_photos(mesh: trimesh.Trimesh, d: Path, rgb: np.ndarray, limit: int = 60) -> tuple[np.ndarray, float]:
    """Take the colour of each surface point from the full-size photos (the one that sees it head-on, not blocked by something
    nearer) instead of the small image the phone sampled while scanning. Points no photo sees keep their old colour."""
    mf = d / "photos.json"
    if not mf.exists():
        return rgb, 0.0
    m = json.loads(mf.read_text())
    photos = [p for p in (m.get("photos", []) if isinstance(m, dict) else m) if (d / "photos" / p["file"]).exists()]
    if not photos:
        return rgb, 0.0
    step = max(1, len(photos) // limit)
    new, _, seen = colour_vertices(mesh, d, photos[::step], with_mask=True)
    out = np.where(seen[:, None], new, rgb[:, :3])
    return out, float(seen.mean())


def convert_cloud(d: Path, cloud: trimesh.PointCloud, scale: float, footprint: dict | None = None, recolour: bool = True) -> tuple[bool, str]:
    """A depth point cloud from the phone: keep it for the viewer, and mesh it if Open3D is available."""
    pts = np.asarray(cloud.vertices, dtype=float) * scale
    cols = np.asarray(cloud.colors) if cloud.colors is not None and len(cloud.colors) == len(pts) else np.tile([150, 150, 150, 255], (len(pts), 1))
    fp_note = ""
    has_fp = footprint is not None or (d / "footprint.json").exists()
    if has_fp:
        fp = footprint or json.loads((d / "footprint.json").read_text())
        pts, cols = apply_footprint(pts, cols, fp)
        fp_note = " (cropped to the traced outline, flat base added)"
    (d / "points.bin").write_bytes(pack_points(pts, cols))
    rep = {"points": int(len(pts)), "size_m": [round(float(x), 2) for x in np.ptp(pts, axis=0)], "vertices": 0, "faces": 0}
    log = f"{len(pts)} points" + fp_note
    mesh = poisson(pts, cols, depth=9 if len(pts) < 300_000 else 8) if len(pts) >= 500 else None
    if mesh is not None:
        rgb = np.asarray(mesh.visual.vertex_colors)[:, :3]
        if recolour and scale == 1.0:       # the cloud is in the phone's world frame, the same one the photos' poses use
            try:
                rgb, seen = recolour_from_photos(mesh, d, rgb)
                log += f", {round(seen * 100)}% of the surface coloured from the photos"
                mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=np.c_[rgb, np.full(len(rgb), 255, dtype=np.uint8)])
            except Exception as e:
                log += f", photo colours skipped ({type(e).__name__}: {e})"
        (d / "mesh.bin").write_bytes(pack_preview(mesh, rgb))
        mesh.export(d / "colored.ply")
        rep.update(vertices=int(len(mesh.vertices)), faces=int(len(mesh.faces)), coloured=1.0)
        log += f", surface of {rep['faces']} triangles"
        if has_fp:  # a traced part is a printable solid
            try:
                pr = printable(mesh, d)
                rep.update(stl=True, watertight=pr["watertight"], volume_mm3=pr["volume_mm3"], size_mm=pr["size_mm"])
                log += f", {'watertight' if pr['watertight'] else 'NOT watertight'}, {pr['size_mm']} mm"
            except Exception as e:
                log += f", no STL: {type(e).__name__}: {e}"
    (d / "report.json").write_text(json.dumps(rep))
    return True, log


def convert_mesh(d: Path, scale: float = 1.0) -> tuple[bool, str]:
    """Write mesh.bin (viewer), colored.ply and report.json into d."""
    try:
        mesh = trimesh.load(d / "scan.ply", process=False)
        if isinstance(mesh, trimesh.PointCloud):
            return convert_cloud(d, mesh, scale)
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
            return False, "scan.ply has no triangles or points"
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
