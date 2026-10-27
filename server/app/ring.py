"""Scans made with the printed marker ring (docs/turntable-ring.md).

Every frame shows part of the ring. The printed markers give the ring's position relative to the camera, so the frame's LiDAR
points can be written in the ring's own frame, whatever the phone or the spool did. Frames add up to the whole part, in real
millimetres, with no assumption about turning speed or a steady phone.
"""
import json
import os
from pathlib import Path

import cv2
import numpy as np
import trimesh
from PIL import Image

from . import frames, meshscan
from .photos import _mat

BUNDLED = Path(__file__).with_name("ring.json")
F = np.diag([1.0, -1.0, -1.0])     # ARKit camera (x right, y up, looks down -z)  ->  OpenCV camera (x right, y down, looks along +z)


def config(d: Path | None = None) -> dict:
    """The ring dimensions. A measured file at data/ring.json (next to the scans folder) overrides the bundled design."""
    override = Path(os.environ.get("RING_JSON", "")) if os.environ.get("RING_JSON") else None
    if override and override.exists():
        return json.loads(override.read_text())
    return json.loads(BUNDLED.read_text())


def _detector() -> "cv2.aruco.ArucoDetector":
    p = cv2.aruco.DetectorParameters()
    p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    return cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), p)


def ring_pose(img: np.ndarray, photo: dict, cfg: dict, det) -> tuple[np.ndarray, np.ndarray, int, float] | None:
    """Ring-to-camera pose (OpenCV convention) from the markers seen in one photo, the number of markers and the mean reprojection error (px)."""
    corners, ids, _ = det.detectMarkers(img)
    if ids is None:
        return None
    table = cfg["corners_mm"]
    obj, im = [], []
    for c, i in zip(corners, ids.ravel()):
        if str(int(i)) in table:
            obj += [[x, y, 0.0] for x, y in table[str(int(i))]]
            im += c.reshape(4, 2).tolist()
    if len(obj) < 4:
        return None
    k = np.array(photo["intrinsics"], dtype=float).T   # columns -> 3x3
    obj, im = np.array(obj, dtype=np.float64), np.array(im, dtype=np.float64)
    ok, rvec, tvec = cv2.solvePnP(obj, im, k, None, flags=cv2.SOLVEPNP_ITERATIVE if len(obj) > 4 else cv2.SOLVEPNP_IPPE_SQUARE)
    if not ok:
        return None
    proj, _ = cv2.projectPoints(obj, rvec, tvec, k, None)
    err = float(np.linalg.norm(proj.reshape(-1, 2) - im, axis=1).mean())
    R, _ = cv2.Rodrigues(rvec)
    return R, tvec.reshape(3), len(obj) // 4, err


def to_ring(points_world: np.ndarray, photo: dict, R: np.ndarray, t: np.ndarray) -> np.ndarray:
    """World points -> ring frame (mm, Z up out of the ring surface)."""
    M = _mat(photo["transform"])                        # camera -> world
    inv = np.linalg.inv(M)
    cam = (inv @ np.c_[points_world, np.ones(len(points_world))].T).T[:, :3]   # ARKit camera frame, metres
    cv = (F @ cam.T).T * 1000.0                          # OpenCV camera frame, mm
    return (R.T @ (cv - t).T).T


def fuse(d: Path, scale: float = 1.0) -> tuple[bool, str]:
    cfg = config(d)
    det = _detector()
    inner_r = cfg["stage_d_mm"] / 2 - 1.0
    clouds, colours, used, markers, errs, skipped = [], [], 0, 0, [], 0
    for ph in frames.manifest(d):
        dm = meshscan._depth_map(d, ph)
        path = d / "photos" / ph["file"]
        if dm is None or not path.exists():
            continue
        gray = np.asarray(Image.open(path).convert("L"))
        pose = ring_pose(gray, ph, cfg, det)
        if pose is None:
            skipped += 1
            continue
        R, t, n, err = pose
        if err > 3.0:                                    # a bad fit would smear the model: leave this frame out
            skipped += 1
            continue
        ok = (dm > 0.1) & (dm < 5) & (frames.ndi.maximum_filter(dm, 3) - frames.ndi.minimum_filter(dm, 3) < 0.04 * dm + 0.01)
        world = frames.world_points(ph, dm)
        pr = to_ring(world[ok], ph, R, t)
        keep = (np.hypot(pr[:, 0], pr[:, 1]) < inner_r) & (pr[:, 2] > 3.0) & (pr[:, 2] < 400.0)   # above the stage, inside the ring
        if keep.sum() < 50:
            skipped += 1
            continue
        img = np.asarray(Image.open(path).convert("RGB").resize((dm.shape[1], dm.shape[0]), Image.BILINEAR))
        clouds.append(pr[keep])
        colours.append(img[ok][keep])
        used += 1
        markers += n
        errs.append(err)
    if used < 3:
        return False, (f"The ring was found with a good fit in only {used} frames ({skipped} frames skipped). "
                       "Keep at least 4 markers in view, in focus, without glare.")
    pts, cols = np.vstack(clouds), np.vstack(colours)
    cell = np.floor(pts / 3.0).astype(np.int64)          # one point per 3 mm cell
    _, first = np.unique(cell, axis=0, return_index=True)
    pts, cols = pts[first], cols[first]
    yup = np.c_[pts[:, 0], pts[:, 2], -pts[:, 1]] / 1000.0   # ring frame (Z up) -> metres, Y up
    from scipy.spatial import ConvexHull
    hull = yup[:, [0, 2]][ConvexHull(yup[:, [0, 2]]).vertices]
    fp = {"polygon": hull.tolist(), "base_y": 0.0, "height": float(yup[:, 1].max()) + 0.005}
    cloud = trimesh.PointCloud(yup, colors=np.c_[cols, np.full(len(cols), 255, dtype=np.uint8)])
    ok, log = meshscan.convert_cloud(d, cloud, scale, footprint=fp)
    return ok, (f"ring: {used} frames used, {skipped} skipped, about {markers / used:.1f} markers per frame, "
                f"mean pose error {np.mean(errs):.2f} px; {log}")
