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


def calibration_file(data_dir: Path) -> Path:
    """Where the measured ring is kept: next to the scans folder (data/ring_calibration.json)."""
    return data_dir.parent / "ring_calibration.json"


def build_config(marker_mm: float | None = None, centre_radius_mm: float | None = None) -> dict:
    """The bundled ring with measured marker size and marker-centre radius; all corner coordinates are recomputed from them."""
    cfg = json.loads(BUNDLED.read_text())
    if marker_mm:
        cfg["marker_size_mm"] = float(marker_mm)
    if centre_radius_mm:
        cfg["centre_radius_mm"] = float(centre_radius_mm)
    n, h, rc = cfg["count"], cfg["marker_size_mm"] / 2, cfg["centre_radius_mm"]
    ids = cfg["marker_ids"]
    cfg["corners_mm"] = {}
    for i, mid in enumerate(ids):
        a = np.radians(i * 360.0 / n)
        u, r, c = np.array([np.cos(a), np.sin(a)]), np.array([np.sin(a), -np.cos(a)]), rc * np.array([np.cos(a), np.sin(a)])
        cfg["corners_mm"][str(mid)] = [(c + h * (-r + u)).tolist(), (c + h * (r + u)).tolist(),
                                       (c + h * (r - u)).tolist(), (c + h * (-r - u)).tolist()]
    return cfg


def config(d: Path | None = None) -> dict:
    """The ring dimensions: what the user measured and calibrated (data/ring_calibration.json), else the bundled design."""
    if d is not None:
        cal = calibration_file(d.parent)
        if cal.exists():
            return json.loads(cal.read_text())["config"]
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
    clouds, colours, used, markers, errs, skipped, no_depth = [], [], 0, 0, [], 0, 0
    for ph in frames.manifest(d):
        dm = meshscan._depth_map(d, ph)
        path = d / "photos" / ph["file"]
        if dm is None or not path.exists():
            no_depth += 1
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
    if used < 3 and no_depth and not skipped:
        return False, (f"{no_depth} frames have no LiDAR depth. The ring scan needs the depth of the part from a LiDAR iPhone "
                       "(the markers give the position, the LiDAR gives the shape).")
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
    ok, log = meshscan.convert_cloud(d, cloud, scale, footprint=fp, recolour=False)
    return ok, (f"ring: {used} frames used, {skipped} skipped, about {markers / used:.1f} markers per frame, "
                f"mean pose error {np.mean(errs):.2f} px; {log}")


def calibrate(work: Path, marker_mm: float | None, centre_distance_mm: float | None) -> dict:
    """Check a few photos of the printed ring against the measured sizes. `work` holds photos/ and photos.json like a scan.
    Returns the numbers the app shows and, if the photos are good, the config to save."""
    nominal = build_config()
    cfg = build_config(marker_mm, centre_distance_mm / 2 if centre_distance_mm else None)
    det = _detector()
    rows, ratios, edge_px = [], [], []
    for ph in frames.manifest(work):
        path = work / "photos" / ph["file"]
        if not path.exists():
            continue
        gray = np.asarray(Image.open(path).convert("L"))
        pose = ring_pose(gray, ph, cfg, det)
        pose_nom = ring_pose(gray, ph, nominal, det)
        corners, ids, _ = det.detectMarkers(gray)
        if ids is not None:
            edge_px += [float(np.mean([np.linalg.norm(c.reshape(4, 2)[k] - c.reshape(4, 2)[(k + 1) % 4]) for k in range(4)])) for c in corners]
        row = {"file": ph["file"], "markers": 0 if pose is None else pose[2], "error_px": None if pose is None else round(pose[3], 2),
               "error_px_nominal": None if pose_nom is None else round(pose_nom[3], 2), "lidar_ratio": None}
        dm = meshscan._depth_map(work, ph)
        if pose is not None and dm is not None and ids is not None:
            R, t = pose[0], pose[1]
            # depth pixels inside the detected marker squares are on the ring plane, whatever sizes were entered
            dh, dw = dm.shape
            mask = np.zeros((dh, dw), dtype=np.uint8)
            for c in corners:
                cv2.fillPoly(mask, [(c.reshape(4, 2) * [dw / gray.shape[1], dh / gray.shape[0]]).astype(np.int32)], 1)
            sel = (mask > 0) & (dm > 0.1) & (dm < 5)
            if sel.sum() > 300:
                w = frames.world_points(ph, dm)[sel]
                centroid = w.mean(axis=0)
                normal = np.linalg.svd(w - centroid, full_matrices=False)[2][-1]
                cam_world = _mat(ph["transform"])[:3, 3]
                d_lidar = abs(float(np.dot(cam_world - centroid, normal))) * 1000.0         # mm
                d_pnp = abs(float((-R.T @ t)[2]))                                             # mm, camera height above the ring plane
                if d_pnp > 1:
                    row["lidar_ratio"] = round(d_lidar / d_pnp, 4)
                    ratios.append(d_lidar / d_pnp)
        rows.append(row)
    found = [r for r in rows if r["markers"] >= 4]
    errs = [r["error_px"] for r in found]
    out = {"photos": len(rows), "photos_good": len(found), "rows": rows, "config": cfg,
           "markers_avg": round(float(np.mean([r["markers"] for r in found])), 1) if found else 0,
           "edge_px_avg": round(float(np.mean(edge_px)), 1) if edge_px else 0,
           "error_px": round(float(np.mean(errs)), 2) if errs else None,
           "lidar_scale": round(float(np.median(ratios)), 3) if ratios else None}
    msgs = []
    if not rows:
        msgs.append("No photos arrived.")
    elif len(found) < max(3, int(0.8 * len(rows))):
        msgs.append(f"The ring was found with 4 or more markers in only {len(found)} of {len(rows)} photos. Move closer, add light, avoid glare.")
    if out["edge_px_avg"] and out["edge_px_avg"] < 45:
        msgs.append(f"Markers are only {out['edge_px_avg']} px wide: move closer (aim for 60 px or more).")
    if errs and out["error_px"] > 1.5:
        msgs.append(f"The markers do not fit the sizes you entered well (error {out['error_px']} px, should be under 1.5). Re-measure the marker size and the distance across the ring.")
    if out["lidar_scale"]:
        diff = (out["lidar_scale"] - 1.0) * 100
        if abs(diff) > 5:
            msgs.append(f"The LiDAR says the ring is about {abs(diff):.0f}% {'bigger' if diff > 0 else 'smaller'} than the sizes you entered "
                        "(the LiDAR is accurate to about 3%). Check your caliper readings.")
        else:
            msgs.append(f"The LiDAR agrees with your sizes within {abs(diff):.1f}% (its own accuracy is about 3%).")
    out["messages"] = msgs
    out["good"] = bool(found) and len(found) >= max(3, int(0.8 * len(rows))) and (not errs or out["error_px"] <= 1.5) \
        and (out["lidar_scale"] is None or abs(out["lidar_scale"] - 1.0) <= 0.05)
    return out
