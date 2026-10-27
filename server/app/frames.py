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
    if (d / "masks" / "phone.json").exists():                 # the phone's outlines are better than these: keep them
        edited |= set(json.loads((d / "masks" / "phone.json").read_text()))
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


def depth_frames(d: Path) -> int:
    return sum(1 for ph in manifest(d) if ph.get("depth_file") and (d / "photos" / ph["depth_file"]).exists())


def tsdf_room(d: Path, scale: float = 1.0, voxel: float = 0.02) -> tuple[bool, str]:
    """A clean surface for a room (or any free scan): every frame's depth and colour is merged into one signed-distance volume,
    which averages out the noise and the stray points at depth edges, and leaves flat walls flat. The surface is then coloured
    from the full-size photos and prepared for per-pixel photo texturing."""
    import open3d as o3d
    # size the volume to what the frames saw (outliers ignored): a cube with a margin, at most 320 voxels a side
    seen = []
    for ph in manifest(d):
        dm = meshscan._depth_map(d, ph)
        if dm is not None:
            w = world_points(ph, dm)[::4, ::4].reshape(-1, 3)
            ok = (dm[::4, ::4].reshape(-1) > 0.1) & (dm[::4, ::4].reshape(-1) < 4.5)
            seen.append(w[ok])
    if not seen:
        return False, "no depth frames"
    seen = np.vstack(seen)
    lo, hi = np.percentile(seen, 0.5, axis=0) - 0.3, np.percentile(seen, 99.5, axis=0) + 0.3
    length = float(np.max(hi - lo))
    voxel = max(voxel, length / 320)
    vol = o3d.pipelines.integration.UniformTSDFVolume(
        length, int(np.ceil(length / voxel)), voxel * 4, o3d.pipelines.integration.TSDFVolumeColorType.RGB8, lo)
    flip = np.diag([1.0, -1.0, -1.0, 1.0])       # ARKit camera -> OpenCV camera
    used = 0
    for ph in manifest(d):
        dm = meshscan._depth_map(d, ph)
        path = d / "photos" / ph["file"]
        if dm is None or not path.exists():
            continue
        dh, dw = dm.shape
        dm = dm.astype(np.float32).copy()
        dm[~((dm > 0.1) & (dm < 4.5))] = 0
        dm[(ndi.maximum_filter(dm, 3) - ndi.minimum_filter(dm, 3)) > (0.04 * dm + 0.01)] = 0
        col = np.ascontiguousarray(np.asarray(Image.open(path).convert("RGB").resize((dw, dh), Image.BILINEAR)))
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            o3d.geometry.Image(col), o3d.geometry.Image(dm), depth_scale=1.0, depth_trunc=4.5, convert_rgb_to_intensity=False)
        k = np.array(ph["intrinsics"], dtype=float)
        sx, sy = dw / ph["width"], dh / ph["height"]
        intr = o3d.camera.PinholeCameraIntrinsic(dw, dh, k[0][0] * sx, k[1][1] * sy, k[2][0] * sx, k[2][1] * sy)
        vol.integrate(rgbd, intr, np.linalg.inv(_mat(ph["transform"]) @ flip))
        used += 1
    if used < 3:
        return False, "too few frames with depth"
    om = vol.extract_triangle_mesh()
    if len(om.triangles) == 0:
        return False, "the depth frames produced no surface"
    cluster, n_tri, _ = om.cluster_connected_triangles()
    cluster, n_tri = np.asarray(cluster), np.asarray(n_tri)
    om.remove_triangles_by_mask(n_tri[cluster] < 400)          # drop little floating pieces
    om.remove_unreferenced_vertices()
    if len(om.triangles) > 400_000:
        om = om.simplify_quadric_decimation(target_number_of_triangles=400_000)
    mesh = trimesh.Trimesh(np.asarray(om.vertices) * scale, np.asarray(om.triangles), process=False)
    rgb = (np.asarray(om.vertex_colors) * 255).astype(np.uint8)
    log = f"surface from {used} frames (merged depth), {len(mesh.faces)} triangles"
    seen = 0.0
    if scale == 1.0:
        try:
            rgb, seen = meshscan.recolour_from_photos(mesh, d, rgb)
            log += f", {round(seen * 100)}% coloured from the photos"
        except Exception as e:
            log += f", photo colours skipped ({type(e).__name__}: {e})"
    mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=np.c_[rgb, np.full(len(rgb), 255, dtype=np.uint8)])
    (d / "mesh.bin").write_bytes(meshscan.pack_preview(mesh, rgb))
    mesh.export(d / "colored.ply")
    if scale == 1.0:
        try:
            plan = meshscan.plan_texture(mesh, d)
            if plan:
                log += f", photo texture from {plan['count']} photos covers {round(plan['covered'] * 100)}% of the triangles"
        except Exception as e:
            log += f", photo texture skipped ({type(e).__name__}: {e})"
    meshscan.write_settings(d, mode="free scan (merged depth)", frames_used=used, voxel_mm=round(voxel * 1000, 1),
                            depth_range="0.1 to 4.5 m", surface="signed-distance volume, small floating pieces removed, coloured from the photos")
    rp = d / "report.json"
    rep = json.loads(rp.read_text()) if rp.exists() else {}
    rep.update(vertices=int(len(mesh.vertices)), faces=int(len(mesh.faces)), coloured=round(float(seen), 3),
               size_m=[round(float(x), 2) for x in mesh.extents])
    rp.write_text(json.dumps(rep))
    return True, log
