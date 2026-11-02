"""LiDAR calibration with a printed target (docs/lidar-target.md).

The target is a 200 x 200 mm white plate with four black ArUco markers (DICT_4X4_50, ids 40-43, never used by the ring's 0-17),
four square pads 10/20/30/40 mm high and four wedges sloping 15/30/45/60 degrees, plus markers 44 and 45 on the tops of the 20 and
40 mm pads (corners at three heights: a much better pose than from one plane), a blue and a red patch on the other two pads (how
the LiDAR reads coloured surfaces) and a 100 mm ruler. This is print v2; v1 was never printed and is not supported. Lay it flat on a hard floor and photograph it from
the stations the app lists (distance x tilt). For every frame:

1. the markers give the camera's exact pose relative to the print (metric, because the print's size is known: the camera
   measures against the print, not against ARKit's own sense of scale),
2. every LiDAR depth pixel's ray is cast against the known shape, which gives the true depth it should read and the angle at which
   it meets the surface,
3. true and measured depths from all frames are fitted: `true = a * measured + b` (the same form as the app's tape calibration),
   with the leftover error listed by distance and by angle of incidence.

Units: the geometry here is in millimetres in the plate's frame (origin at the middle of the bottom face, z up, +y towards
markers 40 and 41); depths and the fit are in metres."""
import json
import math
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage as ndi

from . import frames, meshscan
from .photos import _mat

VERSION = 2
SIZE = 200.0                  # plate width and depth, mm
PLATE = 5.0                   # plate thickness, mm
INLAY = 0.4                   # black marker inlays stand this much proud of the plate (two 0.2 mm layers)
MARKER = 36.0                 # marker side of the plate's markers, black border included (6 cells of 6 mm)
PADS = [((-23.0, 23.0), 10.0), ((23.0, 23.0), 20.0), ((23.0, -23.0), 30.0), ((-23.0, -23.0), 40.0)]   # centre, height above the plate
PAD = 40.0
# marker id: centre x, y, the surface it lies on (z) and its side, mm
MARKERS = {40: (-76.0, 76.0, PLATE, MARKER), 41: (76.0, 76.0, PLATE, MARKER), 42: (76.0, -76.0, PLATE, MARKER), 43: (-76.0, -76.0, PLATE, MARKER),
           44: (23.0, 23.0, PLATE + 20, 30.0), 45: (-23.0, -23.0, PLATE + 40, 30.0)}
PATCHES = [((-23.0, 23.0), PLATE + 10, "blue"), ((23.0, -23.0), PLATE + 30, "red")]     # 36 mm squares, 0.4 mm proud
PATCH = 36.0
WEDGES = [("+y", 15.0), ("-y", 30.0), ("+x", 45.0), ("-x", 60.0)]   # which edge, slope; each 30 mm deep, 50 mm wide, high side inwards
WEDGE_IN, WEDGE_OUT, WEDGE_HALF = 61.0, 91.0, 25.0

# where to hold the phone: distance to the middle of the print along the camera's line of sight, and tilt from straight down
DISTANCES = (0.3, 0.5, 0.8, 1.2)
TILTS = (0, 30, 55)
FRAMES_PER_STATION = 3
DIST_TOL, TILT_TOL = 0.15, 7.0      # within 15% of the distance and 7 degrees of the tilt counts as at the station


def stations() -> list[dict]:
    out = []
    for dist in DISTANCES:
        for tilt in TILTS:
            if dist >= 1.2 and tilt == 55:
                continue              # the markers are too foreshortened to read that far out at that slant
            out.append({"id": f"d{int(dist * 100)}t{tilt}", "distance_m": dist, "tilt_deg": tilt, "frames": FRAMES_PER_STATION,
                        "say": f"{int(dist * 100)} cm away, " + ("straight down" if tilt == 0 else f"tilted {tilt}° from straight down")})
    return out


def info() -> dict:
    """Everything the app needs: the stations, their tolerances and what the print should measure."""
    return {"version": VERSION, "size_mm": SIZE, "marker_mm": MARKER, "marker_ids": sorted(MARKERS), "dictionary": "DICT_4X4_50",
            "pads_mm": [h for _, h in PADS], "wedges_deg": [a for _, a in WEDGES], "height_mm": round(PLATE + _wedge_height(60), 2),
            "stations": stations(), "distance_tolerance": DIST_TOL, "tilt_tolerance_deg": TILT_TOL,
            "colours": ["white", "black", "blue", "red"], "print_file": "/prints/lidar-target-v2-4colour.3mf"}


def _wedge_height(angle: float) -> float:
    return (WEDGE_OUT - WEDGE_IN) * math.tan(math.radians(angle))


# ---- the shape: convex solids, each a list of half-spaces n.x <= d (mm) ----

def _box(x0, x1, y0, y1, z0, z1) -> list[tuple[np.ndarray, float]]:
    return [(np.array([1.0, 0, 0]), x1), (np.array([-1.0, 0, 0]), -x0), (np.array([0, 1.0, 0]), y1),
            (np.array([0, -1.0, 0]), -y0), (np.array([0, 0, 1.0]), z1), (np.array([0, 0, -1.0]), -z0)]


def solids(scale: float = 1.0) -> list[list[tuple[np.ndarray, float]]]:
    """The target as convex pieces, grown by `scale` (the printed size over the nominal one). `colours()` lists their colours."""
    out = [_box(-SIZE / 2, SIZE / 2, -SIZE / 2, SIZE / 2, 0, PLATE)]
    for (cx, cy), h in PADS:
        out.append(_box(cx - PAD / 2, cx + PAD / 2, cy - PAD / 2, cy + PAD / 2, PLATE, PLATE + h))
    for cx, cy, z, side in MARKERS.values():
        out.append(_box(cx - side / 2, cx + side / 2, cy - side / 2, cy + side / 2, z, z + INLAY))
    for (cx, cy), z, _ in PATCHES:
        out.append(_box(cx - PATCH / 2, cx + PATCH / 2, cy - PATCH / 2, cy + PATCH / 2, z, z + INLAY))
    for edge, ang in WEDGES:
        t = math.tan(math.radians(ang))
        axis, sign = (1, 1.0) if edge == "+y" else (1, -1.0) if edge == "-y" else (0, 1.0) if edge == "+x" else (0, -1.0)
        other = 1 - axis
        e = np.zeros(3); e[axis] = sign            # outwards along the wedge
        s = np.zeros(3); s[other] = 1.0
        slope = e * t + np.array([0, 0, 1.0])      # z + (outward coordinate) * tan <= plate + outer * tan
        out.append([(e, WEDGE_OUT), (-e, -WEDGE_IN), (s, WEDGE_HALF), (-s, WEDGE_HALF), (np.array([0, 0, -1.0]), -PLATE),
                    (slope / np.linalg.norm(slope), (PLATE + WEDGE_OUT * t) / np.linalg.norm(slope))])
    return [[(n, d * scale) for n, d in piece] for piece in out]


def colours() -> list[str]:
    """The colour of each piece of `solids`, in the same order (a marker's top is mostly black: its white cells are left out of it)."""
    return ["white"] * (1 + len(PADS)) + ["black"] * len(MARKERS) + [c for _, _, c in PATCHES] + ["white"] * len(WEDGES)


def cast(origin: np.ndarray, dirs: np.ndarray, scale: float = 1.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rays (origin (3,), dirs (n, 3), mm) against the target: distance along each ray in units of its direction's length
    (inf where it misses), the unit normal of the face it hits, and which face (solid * 8 + plane, -1 = miss)."""
    n = len(dirs)
    best = np.full(n, np.inf)
    normal = np.zeros((n, 3))
    face = np.full(n, -1, np.int64)
    for si, piece in enumerate(solids(scale)):
        N = np.array([p[0] for p in piece])                 # (k, 3)
        D = np.array([p[1] for p in piece])                 # (k,)
        den = dirs @ N.T                                    # (n, k)
        num = D[None, :] - origin @ N.T                     # (n, k)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = num / den
        enter = np.where(den < 0, t, -np.inf)
        leave = np.where(den > 0, t, np.inf)
        outside = (den == 0) & (num < 0)
        tin, which = enter.max(1), enter.argmax(1)
        tout = leave.min(1)
        hit = (tin <= tout) & (tin > 0) & ~outside.any(1) & (tin < best)
        best[hit] = tin[hit]
        normal[hit] = N[which[hit]]
        face[hit] = si * 8 + which[hit]
    return best, normal, face


# ---- the marker corners, as OpenCV orders them (top-left, top-right, bottom-right, bottom-left of the marker image) ----

def marker_corners(scale: float = 1.0) -> dict[int, np.ndarray]:
    out = {}
    for i, (cx, cy, z, side) in MARKERS.items():
        h, top = side / 2, z + INLAY
        out[i] = np.array([[cx - h, cy + h, top], [cx + h, cy + h, top], [cx + h, cy - h, top], [cx - h, cy - h, top]]) * scale
    return out


def _detector():
    p = cv2.aruco.DetectorParameters()
    p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    return cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), p)


def camera_matrix(ph: dict) -> np.ndarray:
    """OpenCV K for the photo: ARKit's intrinsics with the principal point moved half a pixel (frames.world_points' convention)."""
    k = np.array(ph["intrinsics"], dtype=float).T
    k[0, 2] -= 0.5
    k[1, 2] -= 0.5
    return k


def pose_from_markers(gray: np.ndarray, ph: dict, scale: float = 1.0):
    """(R, t, ids, error px) of the plate in OpenCV camera coordinates (x right, y down, z forward), mm; None if fewer than 2 markers."""
    corners, ids, _ = _detector().detectMarkers(gray)
    if ids is None:
        return None
    known = marker_corners(scale)
    obj, img, seen = [], [], []
    for c, i in zip(corners, ids.ravel()):
        if int(i) in known:
            obj.append(known[int(i)]); img.append(c.reshape(4, 2)); seen.append(int(i))
    if len(seen) < 2:
        return None
    obj, img = np.vstack(obj).astype(np.float64), np.vstack(img).astype(np.float64)
    K = camera_matrix(ph)
    ok, rv, tv = cv2.solvePnP(obj, img, K, None, flags=cv2.SOLVEPNP_SQPNP)     # not IPPE: that one wants the points at z = 0
    if not ok:
        return None
    rv, tv = cv2.solvePnPRefineLM(obj, img, K, None, rv, tv)
    proj, _ = cv2.projectPoints(obj, rv, tv, K, None)
    err = float(np.sqrt(((proj.reshape(-1, 2) - img) ** 2).sum(1).mean()))
    return cv2.Rodrigues(rv)[0], tv.ravel(), seen, err


# ---- one frame ----

def frame_samples(d: Path, ph: dict, scale: float = 1.0) -> dict | None:
    """True and measured depth (m), incidence angle (deg) and the surface's colour for the depth pixels that see the target away from
    its edges, plus the camera's distance and tilt as the markers give them. None when the markers are not readable."""
    img = cv2.imread(str(d / "photos" / ph["file"]), cv2.IMREAD_GRAYSCALE)
    dm = meshscan._depth_map(d, ph) if ph.get("depth_file") else None
    if img is None or dm is None:
        return None
    pose = pose_from_markers(img, ph, scale)
    if pose is None:
        return None
    R, t, seen, err = pose
    dh, dw = dm.shape
    W, H = float(ph["width"]), float(ph["height"])
    k = np.array(ph["intrinsics"], dtype=float)
    fx, fy, cx, cy = k[0][0] * dw / W, k[1][1] * dh / H, k[2][0] * dw / W, k[2][1] * dh / H
    u, v = np.meshgrid(np.arange(dw) + 0.5, np.arange(dh) + 0.5)
    rays_cam = np.stack([(u - cx) / fx, (v - cy) / fy, np.ones_like(u)], -1).reshape(-1, 3)    # z = 1: distance along it = z depth
    origin = -R.T @ t                                     # camera centre in the plate's frame
    rays = rays_cam @ R                                   # R^T applied to each row
    tz, normal, face = cast(origin, rays, scale)
    colour = np.array(colours())[np.maximum(face, 0) // 8]
    face = face.reshape(dh, dw)
    same = (ndi.maximum_filter(face, 5) == face) & (ndi.minimum_filter(face, 5) == face) & (face >= 0)   # away from every edge
    meas = dm.astype(np.float64)
    ok = same.ravel() & (meas.ravel() > 0.1) & np.isfinite(tz)
    if ok.sum() < 50:
        return None
    unit = rays[ok] / np.linalg.norm(rays[ok], axis=1, keepdims=True)
    inc = np.degrees(np.arccos(np.clip(-(unit * normal[ok]).sum(1), -1, 1)))
    centre_dir = (R.T @ np.array([0.0, 0.0, 1.0]))        # the camera's forward axis in the plate's frame
    tilt = math.degrees(math.acos(max(-1.0, min(1.0, -centre_dir[2]))))
    dist = float(np.linalg.norm(origin - np.array([0, 0, PLATE * scale]))) / 1000
    return {"file": ph["file"], "true": tz[ok] / 1000, "meas": meas.ravel()[ok], "incidence": inc, "colour": colour[ok],
            "markers": seen, "marker_error_px": err,
            "distance_m": dist, "tilt_deg": tilt, "camera_mm": origin}


# ---- fitting ----

def robust_fit(raw: np.ndarray, truth: np.ndarray) -> tuple[float, float, np.ndarray]:
    """truth = a * raw + b, refitted three times without the points more than 3 robust sigmas (and over 5 mm) off. Returns a, b
    and which points were kept."""
    keep = np.ones(len(raw), bool)
    a, b = 1.0, 0.0
    for _ in range(4):
        A = np.c_[raw[keep], np.ones(keep.sum())]
        a, b = np.linalg.lstsq(A, truth[keep], rcond=None)[0]
        r = truth - (a * raw + b)
        mad = float(np.median(np.abs(r[keep] - np.median(r[keep])))) * 1.4826
        keep = np.abs(r) < max(3 * mad, 0.005)
    return float(a), float(b), keep


def _bins(values, err, edges, label) -> list[dict]:
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (values >= lo) & (values < hi)
        if m.sum() >= 30:
            out.append({label: [lo, hi], "n": int(m.sum()), "bias_mm": round(float(err[m].mean()) * 1000, 1),
                        "rms_mm": round(float(np.sqrt((err[m] ** 2).mean())) * 1000, 1)})
    return out


def station_of(dist: float, tilt: float) -> str | None:
    for s in stations():
        if abs(dist - s["distance_m"]) <= DIST_TOL * s["distance_m"] and abs(tilt - s["tilt_deg"]) <= TILT_TOL:
            return s["id"]
    return None


def analyse(d: Path, print_scale: float = 1.0, max_points: int = 4000) -> dict:
    """The calibration from a folder of frames (photos/, photos.json). Writes result.json there and returns it."""
    photos = frames.manifest(d)
    per, notes = [], []
    rng = np.random.default_rng(0)
    for ph in photos:
        s = frame_samples(d, ph, print_scale)
        if s is None:
            continue
        if len(s["true"]) > max_points:                     # every frame counts about the same, however much of it the target fills
            pick = rng.choice(len(s["true"]), max_points, replace=False)
            for k in ("true", "meas", "incidence", "colour"):
                s[k] = s[k][pick]
        s["station"] = station_of(s["distance_m"], s["tilt_deg"])
        per.append(s)
    res = {"version": VERSION, "frames": len(photos), "frames_used": len(per), "print_scale": print_scale, "messages": notes}
    if len(per) < 3:
        notes.append(f"Only {len(per)} of {len(photos)} photos showed two or more of the print's markers clearly. Keep the whole print "
                     "in view, in good light, and hold still for each photo.")
        res["good"] = False
        (d / "result.json").write_text(json.dumps(res, indent=1))
        return res
    true = np.concatenate([s["true"] for s in per])
    meas = np.concatenate([s["meas"] for s in per])
    inc = np.concatenate([s["incidence"] for s in per])
    col = np.concatenate([s["colour"] for s in per])
    # fitted on the white surfaces (walls are mostly light); each colour's own error after that correction is listed below
    white = col == "white"
    if white.sum() < 200:
        white = np.ones(len(col), bool)
    a, b, kw = robust_fit(meas[white], true[white])
    keep = np.zeros(len(col), bool)
    keep[np.nonzero(white)[0][kw]] = True
    before, after = meas - true, a * meas + b - true
    res["by_colour"] = []
    for c in ("white", "black", "blue", "red"):
        m = (col == c) & (np.abs(after) < 0.05)
        if m.sum() >= 30:
            res["by_colour"].append({"colour": c, "n": int(m.sum()), "bias_mm": round(float(after[m].mean()) * 1000, 1),
                                     "rms_mm": round(float(np.sqrt((after[m] ** 2).mean())) * 1000, 1)})
    res["fit"] = {"a": round(a, 5), "b": round(b, 5), "points": int(len(true)), "kept": int(keep.sum()),
                  "rms_before_mm": round(float(np.sqrt((before[keep] ** 2).mean())) * 1000, 1),
                  "rms_after_mm": round(float(np.sqrt((after[keep] ** 2).mean())) * 1000, 1),
                  "bias_before_mm": round(float(before[keep].mean()) * 1000, 1)}
    dist_edges = [0.0, 0.4, 0.65, 1.0, 1.5, 3.0]
    ang_edges = [0, 15, 30, 45, 60, 75, 90]
    res["by_distance"] = {"before": _bins(true[keep], before[keep], dist_edges, "m"), "after": _bins(true[keep], after[keep], dist_edges, "m")}
    res["by_angle"] = {"before": _bins(inc[keep], before[keep], ang_edges, "deg"), "after": _bins(inc[keep], after[keep], ang_edges, "deg")}
    # the camera: how well the markers fit, and whether ARKit's scale agrees with the print's (camera baselines both ways)
    res["camera"] = {"marker_error_px": round(float(np.median([s["marker_error_px"] for s in per])), 2),
                     "markers_per_photo": round(float(np.mean([len(s["markers"]) for s in per])), 1)}
    byfile = {p["file"]: p for p in photos}
    arkit = np.array([_mat(byfile[s["file"]]["transform"])[:3, 3] for s in per])
    marker = np.array([s["camera_mm"] / 1000 for s in per])
    i, j = np.triu_indices(len(per), 1)
    la, lm = np.linalg.norm(arkit[i] - arkit[j], axis=1), np.linalg.norm(marker[i] - marker[j], axis=1)
    far = lm > 0.15
    if far.sum() >= 3:
        res["camera"]["tracking_scale"] = round(float(np.median(la[far] / lm[far])), 4)     # 1.0: ARKit's metres are the print's metres
    # stations
    done = {}
    for s in per:
        if s["station"]:
            done[s["station"]] = done.get(s["station"], 0) + 1
    res["stations"] = [{**st, "taken": done.get(st["id"], 0)} for st in stations()]
    missing = [st["say"] for st in res["stations"] if st["taken"] == 0]
    if missing:
        notes.append("Not photographed yet: " + "; ".join(missing) + ". The fit is less sure at those distances and angles.")
    res["good"] = res["fit"]["rms_after_mm"] < 15 and abs(a - 1) < 0.1 and abs(b) < 0.05
    if abs(a - 1) >= 0.1 or abs(b) >= 0.05:
        notes.append("The correction is unusually large (scale more than 10% off or offset over 5 cm). Check the print's size "
                     "(type what your ruler says), that it lies flat, and that the markers are not glossy.")
    res["frames_detail"] = [{"file": s["file"], "distance_m": round(s["distance_m"], 3), "tilt_deg": round(s["tilt_deg"], 1),
                             "markers": s["markers"], "station": s["station"], "points": int(len(s["true"]))} for s in per]
    (d / "result.json").write_text(json.dumps(res, indent=1))
    return res
