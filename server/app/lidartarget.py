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
3. the floor round the print (z = 0, the print lies on it) gives the same for thousands more pixels at any distance and angle,
4. the flat surfaces only (plate top and floor) are fitted: `true = a * measured + b + h(angle of incidence)`. The pads and wedges are
   left out: LiDAR has too few real dots to resolve them and reads them 10-30 mm off even with a correct pose. The leftover error is
   listed by distance and angle, and colour is reported against the same correction.

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
FLOOR_RING = 500.0            # floor pixels are used out to this far from the print's middle, mm
FLOOR_SPREAD, FLOOR_AGREE = 0.012, 0.010    # a frame's floor is used if flat to this (m, robust spread) and within this of the plate
ANGLE_KNOTS = (0, 15, 30, 45, 60)           # the angle correction is piecewise linear between these, 0 at 0 degrees
MIN_RANGE_M, MIN_ANGLE = 0.4, 30.0          # what the photos must span for the scale and the angle part to mean anything
GOOD_RMS = 0.010
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
    plate_top = same.ravel()[ok] & (face.ravel()[ok] // 8 == 0) & (normal[ok][:, 2] > 0.999)
    # the floor: off the print, in a ring round it, where the ray meets z = 0
    tf = -origin[2] / rays[:, 2]
    fp = origin + rays * np.where(tf > 0, tf, 0)[:, None]
    fr = np.maximum(np.abs(fp[:, 0]), np.abs(fp[:, 1]))
    fl = (face.ravel() < 0) & (tf > 0) & (fr > (SIZE / 2 + 15) * scale) & (fr < FLOOR_RING * scale) & (meas.ravel() > 0.1)
    flunit = rays[fl] / np.linalg.norm(rays[fl], axis=1, keepdims=True)
    return {"file": ph["file"], "true": tz[ok] / 1000, "meas": meas.ravel()[ok], "incidence": inc, "colour": colour[ok], "plate_top": plate_top,
            "floor_true": tf[fl] / 1000, "floor_meas": meas.ravel()[fl], "floor_incidence": np.degrees(np.arccos(np.clip(np.abs(flunit[:, 2]), 0, 1))),
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


def _tents(inc: np.ndarray, knots: list[float]) -> np.ndarray:
    """Piecewise-linear basis: one column per knot after the first, 1 at that knot, 0 at the others; flat past the last."""
    x = np.clip(inc, knots[0], knots[-1])
    return np.stack([np.interp(x, knots, np.eye(len(knots))[i]) for i in range(1, len(knots))], axis=1) if len(knots) > 1 else np.zeros((len(x), 0))


def fit_flat(meas: np.ndarray, true: np.ndarray, inc: np.ndarray) -> dict:
    """true = a * meas + b + h(inc) by least squares, refitted without points over 3 robust sigmas (and 5 mm) off. Only angles with
    enough points get a knot of their own, and the first knot stays at 0 so b is the offset straight on."""
    knots = [ANGLE_KNOTS[0]] + [k for k in ANGLE_KNOTS[1:] if (np.abs(inc - k) < 7.5).sum() >= 200]
    T = _tents(inc, knots)
    A = np.c_[meas, np.ones(len(meas)), T]
    keep = np.ones(len(meas), bool)
    x = np.zeros(A.shape[1])
    for _ in range(4):
        x = np.linalg.lstsq(A[keep], true[keep], rcond=None)[0]
        r = true - A @ x
        mad = float(np.median(np.abs(r[keep] - np.median(r[keep])))) * 1.4826
        keep = np.abs(r) < max(3 * mad, 0.005)
    return {"a": float(x[0]), "b": float(x[1]), "knots": knots, "h": [0.0] + [float(v) for v in x[2:]], "keep": keep}


def _corrected(f: dict, meas: np.ndarray, inc: np.ndarray) -> np.ndarray:
    return f["a"] * meas + f["b"] + np.interp(np.clip(inc, f["knots"][0], f["knots"][-1]), f["knots"], f["h"])


def _long_short(mm: float) -> str:
    return f"{abs(mm):.0f} mm " + ("long" if mm > 0 else "short")


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
    def thin(arrs, n):
        if len(arrs[0]) <= n:
            return arrs
        pick = rng.choice(len(arrs[0]), n, replace=False)
        return [x[pick] for x in arrs]
    for ph in photos:
        s = frame_samples(d, ph, print_scale)
        if s is None:
            continue
        s["true"], s["meas"], s["incidence"], s["colour"], s["plate_top"] = thin([s["true"], s["meas"], s["incidence"], s["colour"], s["plate_top"]], max_points)
        s["floor_true"], s["floor_meas"], s["floor_incidence"] = thin([s["floor_true"], s["floor_meas"], s["floor_incidence"]], max_points)
        # this frame's floor counts only if it is flat and level with the print (not a rug, a table's edge, furniture)
        s["floor_ok"] = False
        if len(s["floor_true"]) >= 100 and s["plate_top"].sum() >= 10:
            e = s["floor_meas"] - s["floor_true"]
            spread = float(np.median(np.abs(e - np.median(e)))) * 1.4826
            top = s["meas"][s["plate_top"]] - s["true"][s["plate_top"]]
            s["floor_ok"] = spread < FLOOR_SPREAD and abs(float(np.median(e)) - float(np.median(top))) < FLOOR_AGREE
        s["station"] = station_of(s["distance_m"], s["tilt_deg"])
        per.append(s)
    res = {"version": VERSION, "frames": len(photos), "frames_used": len(per), "print_scale": print_scale, "messages": notes}
    def stop(summary: str, fix: str) -> dict:
        res.update(good=False, summary=summary, fix=fix)
        notes.append(fix)
        (d / "result.json").write_text(json.dumps(res, indent=1))
        return res
    if len(per) < 3:
        return stop(f"The camera could read the print in only {len(per)} of {len(photos)} photos.",
                    "Try again in brighter, even light with the whole print on screen, and move a little more slowly.")
    true = np.concatenate([s["true"] for s in per])
    meas = np.concatenate([s["meas"] for s in per])
    inc = np.concatenate([s["incidence"] for s in per])
    col = np.concatenate([s["colour"] for s in per])
    top = np.concatenate([s["plate_top"] for s in per])
    fl = [s for s in per if s["floor_ok"]]
    Fm = np.concatenate([meas[top]] + [s["floor_meas"] for s in fl])
    Ft = np.concatenate([true[top]] + [s["floor_true"] for s in fl])
    Fi = np.concatenate([inc[top]] + [s["floor_incidence"] for s in fl])
    if len(Fm) < 200:
        return stop("Too little of the print's flat top was seen by the LiDAR.", "Try again with the print on a bare, hard floor, starting about 30 cm above it.")
    f = fit_flat(Fm, Ft, Fi)
    a, b, keep = f["a"], f["b"], f["keep"]
    Fa = _corrected(f, Fm, Fi) - Ft
    Fb = Fm - Ft
    after_all = _corrected(f, meas, inc) - true
    res["by_colour"] = []
    for c in ("white", "black", "blue", "red"):
        m = (col == c) & (np.abs(after_all) < 0.05)
        if m.sum() >= 30:
            res["by_colour"].append({"colour": c, "n": int(m.sum()), "bias_mm": round(float(after_all[m].mean()) * 1000, 1),
                                     "rms_mm": round(float(np.sqrt((after_all[m] ** 2).mean())) * 1000, 1)})
    rms_after = float(np.sqrt((Fa[keep] ** 2).mean()))
    res["fit"] = {"a": round(a, 5), "b": round(b, 5), "points": int(len(Fm)), "kept": int(keep.sum()), "floor_frames": len(fl),
                  "rms_before_mm": round(float(np.sqrt((Fb[keep] ** 2).mean())) * 1000, 1),
                  "rms_after_mm": round(rms_after * 1000, 1),
                  "bias_before_mm": round(float(Fb[keep].mean()) * 1000, 1)}
    # what to add to a reading, in metres, by the angle it meets the surface: for the app's Measure tab
    res["angle_correction"] = [{"deg": k, "m": round(h, 5)} for k, h in zip(f["knots"], f["h"])]
    dist_edges = [0.0, 0.4, 0.65, 1.0, 1.5, 3.0]
    ang_edges = [0, 15, 30, 45, 60, 75, 90]
    res["by_distance"] = {"before": _bins(Ft[keep], Fb[keep], dist_edges, "m"), "after": _bins(Ft[keep], Fa[keep], dist_edges, "m")}
    res["by_angle"] = {"before": _bins(Fi[keep], Fb[keep], ang_edges, "deg"), "after": _bins(Fi[keep], Fa[keep], ang_edges, "deg")}
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
    span = float(np.percentile(Ft[keep], 98) - np.percentile(Ft[keep], 2))
    steepest = float(f["knots"][-1])
    res["coverage"] = {"distance_m": [round(float(np.percentile(Ft[keep], 2)), 2), round(float(np.percentile(Ft[keep], 98)), 2)], "max_angle_deg": steepest}
    res["good"] = rms_after < GOOD_RMS and abs(a - 1) < 0.1 and abs(b) < 0.05
    def err_mm(angle: float) -> float:                      # what the LiDAR reads over the truth at 1 m, at this angle
        return -float(_corrected(f, np.array([1.0]), np.array([float(angle)]))[0] - 1.0) * 1000
    said = f"Your LiDAR reads {_long_short(err_mm(0))} looking straight at a surface"
    said += f" and {_long_short(err_mm(steepest))} at {steepest:.0f}°." if steepest >= MIN_ANGLE else "."
    res["summary"] = said + f" After the correction, ±{rms_after * 1000:.0f} mm is left."
    res["fix"] = None
    if abs(a - 1) >= 0.1 or abs(b) >= 0.05:
        res["fix"] = "That correction is too large to trust. Check the plate is flat on the floor and the ruler reading you typed, then try again."
    elif rms_after >= GOOD_RMS:
        res["fix"] = "The readings are too scattered. Try again on a bare, hard, matte floor, away from sun patches."
    elif span < MIN_RANGE_M:
        res["fix"] = "It is usable, but only a short range was covered, so the scale is uncertain. Try again and rise all the way to standing."
    elif steepest < MIN_ANGLE:
        res["fix"] = "It is usable, but steep angles are missing. Try again and take the two steps back, keeping the print on screen."
    if res["fix"]:
        notes.append(res["fix"])
    res["frames_detail"] = [{"file": s["file"], "distance_m": round(s["distance_m"], 3), "tilt_deg": round(s["tilt_deg"], 1),
                             "markers": s["markers"], "station": s["station"], "points": int(len(s["true"]))} for s in per]
    (d / "result.json").write_text(json.dumps(res, indent=1))
    return res
