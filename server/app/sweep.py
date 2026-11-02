"""A room scanned from one spot: sit (or stand) in the middle and sweep the phone round.

Every frame has a photo, a LiDAR depth map and the camera's pose. Instead of fusing them into a volume (which needs the phone to
travel and leaves holes where it never looked), the depth is fused into ONE spherical depth image around the spot you stood on:
for every direction (azimuth, elevation) the median range seen by any frame. That image is the room as seen from the middle:
walls, floor and ceiling, with furniture in front of them. It is meshed (cut where the depth jumps), snapped flat where it
lies on a wall/floor/ceiling, coloured from the 360 panorama, and measured: distance to the walls, floor and ceiling and the
room's width, length and height."""
import json
import math
from pathlib import Path

import cv2
import numpy as np
import trimesh
from scipy import ndimage as ndi
from scipy import sparse
from scipy.sparse import linalg as sparse_linalg

from . import frames, meshscan
from .photos import _mat

RANGE = (0.15, 6.0)           # metres of LiDAR depth we trust
SWEEP_SPREAD_M = 0.35         # all cameras within this of their middle: a sweep from one spot
SPOT_M = 1.0                  # a sweep is built from the cameras within this of the spot most of them were at; the rest were taken elsewhere
NEAR_M = 0.3                  # depth nearer than this to the phone in a sweep is your hand, not the room
SELF_SHARE = 0.4              # a sweep frame with this share of its depth nearer than 0.8 m looks at you (face, lap, legs), not the room
OUTLINE_TOL, OUTLINE_SAME, OUTLINE_LINE, OUTLINE_MIN, OUTLINE_JOIN = 0.08, 14.0, 0.14, 0.5, 0.9  # metres and degrees: bumps a wall may have, what is still one wall, shortest wall, how far two walls may run on to meet
OUTLINE_LONG, OUTLINE_ROUNDED, OUTLINE_FAR = 1.5, 1.8, 3.5                                      # a long wall; most a far corner may be rounded off by; "far" from the phone
TOP_BAND, TOP_CELLS = 0.4, 6  # a wall is seen within this far under the ceiling (m), over at least this many 20 cm cells
SPHERE = (720, 360)           # spherical depth image: 0.5 degree cells (width, height)


# ---- which scans are sweeps ----

def depth_photos(d: Path) -> list[dict]:
    return [p for p in frames.manifest(d) if p.get("depth_file") and (d / "photos" / p["depth_file"]).exists()
            and (d / "photos" / p["file"]).exists()]


def cam_positions(photos: list[dict]) -> np.ndarray:
    return np.array([_mat(p["transform"])[:3, 3] for p in photos], dtype=float)


def centre_of(photos: list[dict]) -> np.ndarray:
    return np.median(cam_positions(photos), axis=0)


def main_spot(photos: list[dict]) -> list[dict]:
    """The frames taken at the spot where most of them were. A sweep where you got up and carried on 1.2 m away (seen) mixes two
    viewpoints: the far frames' depth and colour land in the wrong directions (doubled walls) and see you from the front.
    All frames if no spot has at least a quarter of them."""
    if len(photos) < 3:
        return photos
    pos = cam_positions(photos)
    near = np.linalg.norm(pos[:, None] - pos[None], axis=2) <= SPOT_M
    c = np.median(pos[near[int(np.argmax(near.sum(1)))]], axis=0)
    keep = np.linalg.norm(pos - c, axis=1) <= SPOT_M
    if keep.sum() < max(5, len(photos) / 4):
        return photos
    return [p for p, k in zip(photos, keep) if k]


def looks_at_you(d: Path, photo: dict) -> bool:
    """A frame mostly filled with something within 0.8 m: the phone pointed back at you or down at your lap (seen: your face and legs
    painted over the walls). Such frames are left out of a sweep's depth and colour; the room behind is seen by the others."""
    if not photo.get("depth_file"):
        return False
    dm = meshscan._depth_map(d, photo)
    v = dm[dm > 0.1] if dm is not None else []
    return len(v) > 0 and float((v < 0.8).mean()) >= SELF_SHARE


def sweep_frames(d: Path, photos: list[dict]) -> list[dict]:
    """The frames a sweep is built from: those taken at the main spot, without the ones that look at you (if any are left)."""
    spot = main_spot(photos)
    room = [p for p in spot if not looks_at_you(d, p)]
    return room if len(room) >= 5 else spot


def is_sweep(d: Path, meta: dict) -> bool:
    """The phone says so, or every camera stayed within ~35 cm of the middle of the cameras (you turned on the spot)."""
    phone = meta.get("phone") or {}
    if phone.get("sweep") is False:          # the scan says it is an ordinary room walk, whatever the camera positions look like
        return False
    if phone.get("sweep") or str(phone.get("scan type", "")).lower().startswith("sweep"):
        return True
    photos = depth_photos(d)
    if len(photos) < 8:
        return False
    pos = cam_positions(photos)
    return float(np.linalg.norm(pos - np.median(pos, axis=0), axis=1).max()) <= SWEEP_SPREAD_M


# ---- the spherical grid. azimuth 0 = looking along -Z (ARKit's start heading), +90 = +X; elevation +90 = up ----

def dirs_grid(w: int, h: int) -> np.ndarray:
    """Unit direction (h, w, 3) of every cell centre of an equirectangular image."""
    az = ((np.arange(w) + 0.5) / w - 0.5) * 2 * np.pi
    el = (0.5 - (np.arange(h) + 0.5) / h) * np.pi
    ce = np.cos(el)[:, None]
    return np.stack([ce * np.sin(az)[None, :], np.broadcast_to(np.sin(el)[:, None], (h, w)), -ce * np.cos(az)[None, :]], axis=-1).astype(np.float32)


def to_cell(dirs: np.ndarray, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """Column and row of the equirectangular cell each unit direction falls in."""
    az = np.arctan2(dirs[:, 0], -dirs[:, 2])
    el = np.arcsin(np.clip(dirs[:, 1], -1, 1))
    u = np.floor((az / (2 * np.pi) + 0.5) * w).astype(np.int64) % w
    v = np.clip(np.floor((0.5 - el / np.pi) * h).astype(np.int64), 0, h - 1)
    return u, v


def clean_depth(dm: np.ndarray) -> np.ndarray:
    """Depth with the range limited and the 'flying pixels' that LiDAR invents along depth edges removed (set to 0)."""
    dm = dm.astype(np.float32).copy()
    dm[~((dm > RANGE[0]) & (dm < RANGE[1]))] = 0
    flying = (ndi.maximum_filter(dm, 3) - ndi.minimum_filter(dm, 3)) > (0.05 * dm + 0.02)
    dm[flying] = 0
    return dm


def fuse_depth_sphere(d: Path, photos: list[dict], centre: np.ndarray, size: tuple[int, int] = SPHERE,
                      max_frames: int = 240, near: float = NEAR_M) -> tuple[np.ndarray, int]:
    """Spherical depth image about `centre`: per cell, the median distance (m) to the surface over every frame that saw it, NaN where
    no frame did. Depth nearer than `near` to the phone is left out (the hand holding it). Isolated spikes are dropped and one-cell
    gaps closed. Also returns how many frames were used."""
    w, h = size
    step = max(1, math.ceil(len(photos) / max_frames))
    bins, rng, used = [], [], 0
    for ph in photos[::step]:
        dm = meshscan._depth_map(d, ph)
        if dm is None:
            continue
        dm = clean_depth(dm)
        ok = dm > near
        if not ok.any():
            continue
        p = frames.world_points(ph, dm)[ok] - centre
        r = np.linalg.norm(p, axis=1)
        good = r > 0.05
        u, v = to_cell((p[good] / r[good, None]).astype(np.float32), w, h)
        bins.append(v * w + u)
        rng.append(r[good].astype(np.float32))
        used += 1
    if not bins:
        return np.full((h, w), np.nan, np.float32), 0
    bins, rng = np.concatenate(bins), np.concatenate(rng)
    order = np.lexsort((rng, bins))                      # sorted by cell, then by range: the median is the middle one
    bins, rng = bins[order], rng[order]
    first = np.r_[0, np.nonzero(np.diff(bins))[0] + 1]
    count = np.diff(np.r_[first, len(bins)])
    out = np.full(w * h, np.nan, np.float32)
    out[bins[first]] = rng[first + count // 2]
    D = out.reshape(h, w)
    return _tidy(D), used


def _tidy(D: np.ndarray, spike: float = 0.06, close: float = 4.0) -> np.ndarray:
    """Drop cells that disagree with their neighbours (a stray reading), then fill gaps up to `close` cells wide from the nearest value."""
    valid = np.isfinite(D)
    if not valid.any():
        return D
    dist, (iy, ix) = ndi.distance_transform_edt(~valid, return_indices=True)
    filled = D[iy, ix]
    pad = 4                                    # the image wraps round at the sides: pad with the other side so the seam is not special
    med = ndi.median_filter(np.pad(filled, ((0, 0), (pad, pad)), mode="wrap"), size=5, mode="nearest")[:, pad:-pad]
    D = D.copy()
    D[valid & (np.abs(D - med) > spike * med + 0.05)] = np.nan
    valid = np.isfinite(D)
    if not valid.any():
        return D
    dist, (iy, ix) = ndi.distance_transform_edt(~valid, return_indices=True)
    fill = (~valid) & (dist <= close)
    D[fill] = D[iy, ix][fill]
    return D


def points_of(D: np.ndarray, centre: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """World points (h, w, 3) of the spherical depth image, and the unit directions."""
    h, w = D.shape
    dirs = dirs_grid(w, h)
    return centre + dirs * D[..., None], dirs


# ---- measuring the room from the middle ----

def _mode(values: np.ndarray, width: float = 0.05) -> float | None:
    """The distance most surface points agree on: centre of the densest `width` window."""
    if len(values) < 150:
        return None
    v = np.sort(values)
    k = np.searchsorted(v, v + width)
    i = int(np.argmax(k - np.arange(len(v))))
    return float(np.median(v[i:k[i]]))


def _area_mode(off: np.ndarray, uv: np.ndarray, step: float = 0.05, tol: float = 0.03, cell: float = 0.2) -> float | None:
    """The distance of the biggest flat surface: for every candidate distance along the normal (`off`), the area (in 20 cm squares of
    the in-plane coordinates `uv`) that surface points within 3 cm of it cover. Of the ones within 15% of the biggest, the farthest.
    Counting area, not points, matters: a lap, a desk top or a chair near the phone holds more points than the floor beyond them,
    but the floor spreads out all round. Seen on a real sweep: point counts gave a 1.6 m room, area gives 2.45 m."""
    if len(off) < 150:
        return None
    sq = np.floor(uv / cell).astype(np.int64)
    key = (sq[:, 0] + 100000) * 200003 + sq[:, 1]
    areas = {}
    for c in np.arange(math.floor(off.min() / step) * step, off.max() + step, step):
        k = np.abs(off - c) < tol
        if k.sum() >= 30:
            _, cnt = np.unique(key[k], return_counts=True)
            areas[c] = int((cnt >= 3).sum())
    if not areas or max(areas.values()) == 0:
        return None
    best = max(areas.values())
    far = max(c for c, a in areas.items() if a >= 0.85 * best)
    return float(np.median(off[np.abs(off - far) < tol]))


def _wall_to_ceiling(off: np.ndarray, side: np.ndarray, up: np.ndarray, ceiling: float | None, step: float = 0.05, tol: float = 0.04, cell: float = 0.2) -> float | None:
    """The farthest surface facing the middle that reaches the ceiling: a real wall does; furniture and curtains in front of it don't, and a
    room seen through a doorway stops at the door's top. Biggest-area alone was fooled by those on a real room (TV wall 5.53 m, tape 5.95 m)."""
    if ceiling is None or len(off) < 150:
        return None
    top = (up > ceiling - TOP_BAND) & (up < ceiling + 0.1)
    if top.sum() < 30:
        return None
    key = (np.floor(side / cell).astype(np.int64) + 100000) * 200003 + np.floor(up / cell).astype(np.int64)
    best = None
    for c in np.arange(math.floor(off[top].min() / step) * step, off[top].max() + step, step):
        near = np.abs(off - c) < tol
        if (near & top).sum() < 30:
            continue
        cells, cnt = np.unique(key[near & top], return_counts=True)
        if (cnt >= 2).sum() >= TOP_CELLS:
            best = float(c)
    return float(np.median(off[np.abs(off - best) < tol])) if best is not None else None


def room_outline(P: np.ndarray, centre: np.ndarray, ceiling: float | None) -> dict | None:
    """The room's real shape from the readings, not a box. Just under the ceiling there is little but walls: no furniture, and door tops are
    lower. Round the circle, the distance there gives a plan; straight pieces of it are the walls, and where two meet is a corner. Returns walls
    and corners in world metres (x, z); a gap means that stretch wasn't seen."""
    if ceiling is None:
        return None
    rel = P - centre
    with np.errstate(invalid="ignore"):
        band = np.isfinite(rel[..., 0]) & (rel[..., 1] > ceiling - 0.65) & (rel[..., 1] < ceiling - 0.08)
    X = rel[band][:, [0, 2]]
    if len(X) < 200:
        return None
    az = np.degrees(np.arctan2(X[:, 0], -X[:, 1])) % 360
    dist = np.hypot(X[:, 0], X[:, 1])
    R = np.full(360, np.nan)
    which = az.astype(int) % 360
    for a in np.unique(which):
        m = which == a
        if m.sum() >= 4:
            R[a] = np.percentile(dist[m], 75)
    A = np.radians(np.arange(360) + 0.5)
    XY = np.c_[R * np.sin(A), -R * np.cos(A)]
    # one run per stretch seen; a full circle starts at the biggest jump, a corner, so no wall is cut in two
    jumps = np.linalg.norm(np.roll(XY, -1, axis=0) - XY, axis=1)
    start = int(np.nanargmax(jumps) + 1) % 360 if np.isfinite(jumps).any() else 0
    runs, cur = [], []
    for i in range(360):
        a = (start + i) % 360
        if np.isfinite(R[a]):
            cur.append(a)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    def split(p: np.ndarray) -> list[np.ndarray]:
        if len(p) < 3:
            return [p]
        d = p[-1] - p[0]
        e = np.abs(d[0] * (p - p[0])[:, 1] - d[1] * (p - p[0])[:, 0]) / (np.linalg.norm(d) + 1e-9)
        i = int(np.argmax(e))
        return split(p[:i + 1]) + split(p[i:]) if e[i] > OUTLINE_TOL else [p]

    pieces = []                                  # lists of plan points, in order round the circle
    for run in runs:
        pieces += [q for q in split(XY[run]) if len(q) >= 3]
    # pieces that line up are one wall with something in front of part of it
    def fit(q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        m = q.mean(0)
        return m, np.linalg.svd(q - m, full_matrices=False)[2][0]
    changed = True
    while changed and len(pieces) > 1:
        changed = False
        for i in range(len(pieces) - 1):
            m, u = fit(pieces[i])
            m2, u2 = fit(pieces[i + 1])
            if abs(float(u @ u2)) > math.cos(math.radians(OUTLINE_SAME)) and abs(float(np.cross(np.r_[u, 0], np.r_[m2 - m, 0])[2])) < OUTLINE_LINE:
                pieces[i:i + 2] = [np.vstack(pieces[i:i + 2])]
                changed = True
                break
    walls = []
    for q in pieces:
        m, u = fit(q)
        t = (q - m) @ u
        a, b = m + u * t.min(), m + u * t.max()
        if np.linalg.norm(b - a) >= OUTLINE_MIN:
            if (a - q[0]) @ (a - q[0]) > (b - q[0]) @ (b - q[0]):
                a, b = b, a                      # keep the order round the circle
            walls.append([a, b])
    if not walls:
        return None
    # far away the LiDAR rounds a corner off into short pieces: where two long walls have only that between them, they meet (a real room: TV
    # wall 5.62 m with the corner rounded, 5.85 m run on to the corner, tape 5.95 m)
    i = 0
    while i < len(walls) and len(walls) > 2:
        j = (i + 1) % len(walls)
        between = []
        while len(between) < 3 and np.linalg.norm(walls[j][1] - walls[j][0]) < OUTLINE_LONG:
            between.append(walls[j])
            j = (j + 1) % len(walls)
        a, b = walls[i], walls[j]
        if between and j != i and np.linalg.norm(a[1] - a[0]) >= OUTLINE_LONG and sum(np.linalg.norm(w[1] - w[0]) for w in between) <= OUTLINE_ROUNDED \
                and min(np.linalg.norm((w[0] + w[1]) / 2) for w in between) > OUTLINE_FAR:
            d1, d2 = a[1] - a[0], b[1] - b[0]
            cr = d1[0] * d2[1] - d1[1] * d2[0]
            if abs(cr) > 1e-6:
                s = ((b[0] - a[0])[0] * d2[1] - (b[0] - a[0])[1] * d2[0]) / cr
                p = a[0] + d1 * s
                if s > 1 and np.linalg.norm(p - a[1]) < OUTLINE_ROUNDED and np.linalg.norm(p - b[0]) < OUTLINE_ROUNDED:
                    a[1], b[0] = p, p.copy()
                    walls = [w for w in walls if not any(w is x for x in between)]
                    i = 0
                    continue
        i += 1
    corners = []
    n = len(walls)
    for i in range(n):
        a, b = walls[i], walls[(i + 1) % n]
        if n < 2 or (i == n - 1 and n < 3):
            continue
        d1, d2 = a[1] - a[0], b[1] - b[0]
        cr = d1[0] * d2[1] - d1[1] * d2[0]
        ang = math.degrees(math.acos(min(1.0, abs(float(d1 @ d2)) / (np.linalg.norm(d1) * np.linalg.norm(d2) + 1e-9))))
        if abs(cr) < 1e-6 or ang < 25:
            continue
        s = ((b[0] - a[0])[0] * d2[1] - (b[0] - a[0])[1] * d2[0]) / cr
        p = a[0] + d1 * s
        if np.linalg.norm(p - a[1]) < OUTLINE_JOIN and np.linalg.norm(p - b[0]) < OUTLINE_JOIN:
            a[1], b[0] = p, p.copy()             # the two walls run to where they really meet
            corners.append({"x": round(float(p[0] + centre[0]), 3), "z": round(float(p[1] + centre[2]), 3), "angle_deg": round(ang)})
    return {"walls": [{"a": [round(float(w[0][0] + centre[0]), 3), round(float(w[0][1] + centre[2]), 3)],
                       "b": [round(float(w[1][0] + centre[0]), 3), round(float(w[1][1] + centre[2]), 3)],
                       "length_m": round(float(np.linalg.norm(w[1] - w[0])), 2)} for w in walls], "corners": corners}


def _normals(P: np.ndarray, dirs: np.ndarray, centre: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Surface normal (h, w, 3) of every cell of the spherical depth image, turned to face the middle, and where it is known."""
    rel = P - centre
    dU = np.roll(rel, -1, axis=1) - np.roll(rel, 1, axis=1)
    dV = np.vstack([rel[1:2] - rel[:1], rel[2:] - rel[:-2], rel[-1:] - rel[-2:-1]])
    n = np.cross(dU, dV)
    ln = np.linalg.norm(n, axis=2)
    ok = np.isfinite(ln) & (ln > 1e-9)
    n = np.where(ok[..., None], n / np.where(ok, ln, 1)[..., None], 0)
    n = np.where((np.einsum("hwc,hwc->hw", n, dirs) > 0)[..., None], -n, n)       # facing the middle
    return n, ok


def _manhattan_angle(P: np.ndarray, dirs: np.ndarray, centre: np.ndarray) -> float:
    """Angle (radians) of the room's walls about the vertical: from the directions the wall surfaces face, folded into quarter turns."""
    rel = P - centre
    n, ok = _normals(P, dirs, centre)
    el = np.degrees(np.arcsin(np.clip(dirs[..., 1], -1, 1)))
    sel = ok & np.isfinite(rel[..., 0]) & (np.abs(el) < 40) & (np.abs(n[..., 1]) < 0.35)
    if sel.sum() < 100:
        return 0.0
    a = np.arctan2(n[sel][:, 2], n[sel][:, 0])
    return float(np.angle(np.exp(4j * a).sum()) / 4)


def measure(D: np.ndarray, centre: np.ndarray, heading_az: float = 0.0, snap: bool = True) -> tuple[dict, np.ndarray]:
    """Distances from the middle to the walls, floor and ceiling, plus the room's size. Returns (numbers, depth with the surfaces that lie
    on those walls/floor/ceiling snapped flat). `heading_az` is the azimuth (radians) of the first frame: the wall it faces is 'front'."""
    P, dirs = points_of(D, centre)
    h, w = D.shape
    valid = np.isfinite(D)
    rel = np.where(valid[..., None], P - centre, 0)
    el = np.degrees(np.arcsin(np.clip(dirs[..., 1], -1, 1)))
    theta = _manhattan_angle(P, dirs, centre)
    nrm, nok = _normals(P, dirs, centre)
    axes = {k: np.array([math.cos(theta + a), 0.0, math.sin(theta + a)]) for k, a in (("e1", 0), ("e2", math.pi / 2), ("-e1", math.pi), ("-e2", 3 * math.pi / 2))}
    # name the walls from the heading of the first frame: front = the one it faced
    head = np.array([math.sin(heading_az), 0.0, -math.cos(heading_az)])
    front = max(axes, key=lambda k: float(axes[k] @ head))
    order = [front]
    for _ in range(3):   # right of a heading (seen from above, Y up) is cross(heading, up)
        prev = axes[order[-1]]
        right = np.cross(prev, [0, 1, 0])
        order.append(max((k for k in axes if k not in order), key=lambda k: float(axes[k] @ right)))
    names = dict(zip(order, ["front", "right", "back", "left"]))

    horiz = np.hypot(dirs[..., 0], dirs[..., 2]) + 1e-9
    planes, out = [], {"walls": {}}
    up_flat = valid & (el > 35) & nok & (-nrm[..., 1] > 0.85)      # the ceiling first: the walls are told apart by reaching it
    ceiling = _area_mode(rel[up_flat][:, 1], rel[up_flat][:, [0, 2]]) if up_flat.sum() >= 150 else None
    for k, axis in axes.items():
        cos_az = (dirs[..., 0] * axis[0] + dirs[..., 2] * axis[2]) / horiz
        sel = valid & (np.abs(el) < 30) & (cos_az > math.cos(math.radians(22)))
        s = (rel[sel] @ axis)
        info = {"wall_m": None, "nearest_m": None, "axis": [round(float(x), 4) for x in axis]}
        if len(s):
            info["nearest_m"] = round(float(np.percentile(s, 4)), 3)
        facing = valid & nok & (np.abs(el) < 60) & ((nrm @ -axis.astype(np.float32)) > 0.8)    # surfaces that face back at the middle, as a wall does
        side = np.array([-axis[2], 0.0, axis[0]])
        wall = None
        if len(s) >= 150:            # that way was seen at all; else stray points at the edge of the sweep made up a wall (0.21 m, seen)
            off = rel[facing] @ axis
            keep = off >= info["nearest_m"] - 0.05      # a wall is behind nearly everything seen that way, never in front of it
            wall = _wall_to_ceiling(off[keep], (rel[facing] @ side)[keep], rel[facing][:, 1][keep], ceiling) if keep.sum() >= 150 else None
            wall = wall if wall is not None else (_area_mode(off[keep], np.c_[rel[facing] @ side, rel[facing][:, 1]][keep]) if keep.sum() >= 150 else None)
            wall = wall if wall is not None else _mode(s)
        if wall is not None:
            info["wall_m"] = round(wall, 3)
            planes.append((axis, wall))
        out["walls"][names[k]] = info
    for key, sign, mask in (("floor", -1.0, valid & (el < -35)), ("ceiling", 1.0, valid & (el > 35))):
        s = sign * rel[mask][:, 1]
        info = {"wall_m": None, "nearest_m": round(float(np.percentile(s, 4)), 3) if len(s) else None}
        flat = mask & nok & (-sign * nrm[..., 1] > 0.85)                    # level surfaces facing the middle: floor, desk, bed, ceiling
        m = _area_mode(sign * rel[flat][:, 1], rel[flat][:, [0, 2]]) if flat.sum() >= 150 else None
        m = m if m is not None else _mode(s)
        if m is not None:
            info["wall_m"] = round(m, 3)
            planes.append((np.array([0.0, sign, 0.0]), m))
        out[key] = info
    wl = {n: v["wall_m"] for n, v in out["walls"].items()}
    if wl["left"] and wl["right"]:
        out["width_m"] = round(wl["left"] + wl["right"], 3)
    if wl["front"] and wl["back"]:
        out["length_m"] = round(wl["front"] + wl["back"], 3)
    if out["floor"]["wall_m"] and out["ceiling"]["wall_m"]:
        out["height_m"] = round(out["floor"]["wall_m"] + out["ceiling"]["wall_m"], 3)
    out.update(centre=[round(float(x), 3) for x in centre], heading_deg=round(math.degrees(heading_az), 1), wall_angle_deg=round(math.degrees(theta) % 90, 1),
               coverage=round(float(valid.mean()), 3), height_of_phone_above_floor_m=out["floor"]["wall_m"])
    D2 = D
    if snap and planes:        # where the surface lies within 4 cm of a found wall/floor/ceiling, put it exactly on it: flat, clean walls
        D2 = D.copy()
        best = np.full(D.shape, np.inf, np.float32)
        for n, off in planes:
            ndir = dirs @ n.astype(np.float32)
            dist = np.abs(rel @ n.astype(np.float32) - off)
            hit = valid & (ndir > 0.25) & (dist < 0.04) & (dist < best)
            D2[hit] = off / ndir[hit]
            best[hit] = dist[hit]
        out["snapped_share"] = round(float(np.isfinite(best).mean()), 3)
    return out, D2


# ---- the mesh ----

def depth_mesh(D: np.ndarray, centre: np.ndarray, colours: np.ndarray | None = None, jump: float = 1.16) -> trimesh.Trimesh | None:
    """The spherical depth image as a surface seen from the middle: a grid of vertices, with the triangles left out where the depth
    jumps (an object's edge against the wall behind it), so the two are not joined by a stretched sheet."""
    h, w = D.shape
    P, dirs = points_of(D, centre)
    vid = np.arange(h * w).reshape(h, w)
    a, b = vid[:-1, :], np.roll(vid, -1, axis=1)[:-1, :]
    c, e = vid[1:, :], np.roll(vid, -1, axis=1)[1:, :]
    cell = np.stack([D[:-1, :], np.roll(D, -1, axis=1)[:-1, :], D[1:, :], np.roll(D, -1, axis=1)[1:, :]])
    fin = np.isfinite(cell)
    ok = fin.all(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        ok &= (np.max(np.where(fin, cell, -np.inf), axis=0) / np.maximum(np.min(np.where(fin, cell, np.inf), axis=0), 1e-6)) < jump
    ok &= np.abs(dirs[:-1, :, 1]) < 0.9995           # the poles collapse to a point
    a, b, c, e = a[ok], b[ok], c[ok], e[ok]
    if len(a) == 0:
        return None
    faces = np.vstack([np.c_[a, c, b], np.c_[b, c, e]])
    verts = P.reshape(-1, 3)
    m = trimesh.Trimesh(np.nan_to_num(verts), faces, process=False)
    n = m.face_normals
    toward = (centre - m.triangles_center)
    if float(np.einsum("ij,ij->i", n, toward).mean()) < 0:      # wind the triangles to face the middle, where you look from
        m.faces = m.faces[:, ::-1]
    m.update_faces(m.nondegenerate_faces())
    if colours is not None:
        m.visual = trimesh.visual.ColorVisuals(m, vertex_colors=np.c_[colours.reshape(-1, 3), np.full(h * w, 255, np.uint8)])
    m.remove_unreferenced_vertices()
    return m


def backdrop(D: np.ndarray, centre: np.ndarray, colours: np.ndarray) -> trimesh.Trimesh | None:
    """A second, unbroken shell just behind the real surface, built from the FAR side of every depth jump. The real mesh is cut where the
    depth jumps (an object against the wall behind it); without this, the page's background shows through those cuts as white outlines.
    It sits 1.5% further out so it never fights the real surface where the two coincide."""
    valid = np.isfinite(D)
    if not valid.any():
        return None
    far = ndi.maximum_filter(np.where(valid, D, 0.0), size=9)
    reach = ndi.binary_dilation(valid, iterations=4)
    far = np.where(reach & (far > 0), far * 1.015, np.nan).astype(np.float32)
    return depth_mesh(far, centre, colours, jump=50.0)


RING_SECTORS, RING_PITCH = 24, (0.0, 55.0, -50.0)     # the app's coverage dial: SweepCoverage.sectors and bandPitch
POLE_AT = 70.0                                        # and its "straight up / down"


def rings_seen(d: Path, R: np.ndarray, centre: np.ndarray) -> dict | None:
    """What the scan really has depth for, as the app's coverage dial in the coordinates of a phone adding to it (R: phone's world to the
    scan's). A cell counts when at least 70% of the directions within 16 degrees of its centre have depth."""
    photos = sweep_frames(d, depth_photos(d))
    if not photos:
        return None
    D, _ = fuse_depth_sphere(d, photos, centre)
    seen = np.isfinite(D)
    h, w = D.shape

    def share(direction: np.ndarray) -> float:
        side = np.cross(direction, [0.0, 1.0, 0.0])
        side = side / np.linalg.norm(side) if np.linalg.norm(side) > 1e-6 else np.array([1.0, 0.0, 0.0])
        other = np.cross(direction, side)
        ring = [(r, b) for r in (8.0, 16.0) for b in np.arange(0, 360, 45.0)]
        pts = [direction] + [math.cos(math.radians(r)) * direction + math.sin(math.radians(r)) * (math.cos(math.radians(b)) * side + math.sin(math.radians(b)) * other) for r, b in ring]
        u, v = to_cell(np.array(pts) @ R.T, w, h)
        return float(seen[v, u].mean())

    cells = []
    for pitch in RING_PITCH:
        p = math.radians(pitch)
        for i in range(RING_SECTORS):
            a = math.radians((i + 0.5) * 360 / RING_SECTORS)
            cells.append(share(np.array([math.sin(a) * math.cos(p), math.sin(p), -math.cos(a) * math.cos(p)])) >= 0.7)
    pole = lambda sign: bool(seen[(np.degrees(np.arcsin(np.clip(dirs_grid(w, h)[..., 1], -1, 1))) * sign) > POLE_AT].mean() >= 0.5)
    return {"rings": cells, "seen_up": pole(1), "seen_down": pole(-1)}


MARKS_TIGHT = 0.03    # marks replace the sweep's height only if each kind agrees within this. Walking round, ARKit's height wandered by 7 cm on a real
                      # session (five floor points 10.7 cm apart), and the marks gave 2.50 m where the sweep had the true 2.57 m.


WALL_TIGHT = 0.08     # wall marks on one side must agree within this (m), or they are not one flat wall
WALL_SQUARE = 20.0    # degrees: a mark further than this off the room's square is an angled wall, left out


def wall_sizes(marks: list[dict]) -> dict | None:
    """Distances between opposite walls from one session's wall marks. Each mark has a point and the direction its wall faces. The session has
    its own world, so its walls are found among themselves: the square from the facing directions, then one position per side."""
    wm = [m for m in marks if m.get("kind") == "wall" and "nx" in m and "nz" in m]
    if len(wm) < 2:
        return None
    ang = np.array([math.atan2(m["nz"], m["nx"]) for m in wm])
    base = math.atan2(float(np.sin(4 * ang).sum()), float(np.cos(4 * ang).sum())) / 4
    sides: dict[int, list[float]] = {}
    for m, a in zip(wm, ang):
        k = int(round((a - base) / (math.pi / 2))) % 4
        off = math.degrees(abs(math.remainder(a - base, math.pi / 2)))
        if off <= WALL_SQUARE:
            axis = base + k * math.pi / 2
            sides.setdefault(k, []).append(m["x"] * math.cos(axis) + m["z"] * math.sin(axis))     # how far along the way this wall faces
    out, spread = [], 0.0
    for k in (0, 1):
        if k in sides and k + 2 in sides:
            out.append(abs(float(np.median(sides[k])) + float(np.median(sides[k + 2]))))           # they face opposite ways: the positions add
            spread = max(spread, float(np.ptp(sides[k])), float(np.ptp(sides[k + 2])))
    return {"sizes": out, "spread": spread} if out else None


def _apply_wall_marks(room: dict, sessions: dict) -> None:
    """Wall marks over the sweep's wall sizes, when they agree. The session has its own world, so each size is matched to the sweep's width or length, whichever is nearer."""
    if "width_auto_m" in room:
        room["width_m"], room["length_m"] = room.pop("width_auto_m"), room.pop("length_auto_m")
    room.pop("wall_marks", None)
    found = [w for w in (wall_sizes(m) for m in sessions.values()) if w]
    n = sum(1 for m in sessions.values() for x in m if x.get("kind") == "wall")
    if not found:
        if n:
            room["wall_marks"] = {"marks": n, "used": False, "width_m": None, "length_m": None, "spread_cm": None}
        return
    best = min(found, key=lambda w: w["spread"])
    auto_w, auto_l = room.get("width_m"), room.get("length_m")
    sizes = best["sizes"]
    width = length = None
    if len(sizes) == 2:
        straight = abs(sizes[0] - (auto_w or sizes[0])) + abs(sizes[1] - (auto_l or sizes[1]))
        swapped = abs(sizes[1] - (auto_w or sizes[1])) + abs(sizes[0] - (auto_l or sizes[0]))
        width, length = sizes if straight <= swapped else sizes[::-1]
    elif auto_w is None or (auto_l is not None and abs(sizes[0] - auto_l) < abs(sizes[0] - auto_w)):
        length = sizes[0]
    else:
        width = sizes[0]
    used = best["spread"] <= WALL_TIGHT
    room["wall_marks"] = {"marks": n, "used": used, "spread_cm": round(best["spread"] * 100, 1),
                          "width_m": None if width is None else round(width, 3), "length_m": None if length is None else round(length, 3)}
    if used:
        room["width_auto_m"], room["length_auto_m"] = auto_w, auto_l
        if width is not None:
            room["width_m"] = round(width, 3)
        if length is not None:
            room["length_m"] = round(length, 3)


def apply_marks(room: dict, d: Path) -> dict:
    """The height from points you marked on the phone (marks.json), over the one worked out from the sweep. Each marking session is in its
    own ARKit world, so a height comes only from floor and ceiling marks of the same session; the median over sessions is used. No marks:
    the sweep's own numbers come back."""
    f = d / "marks.json"
    sessions = json.loads(f.read_text()).get("sessions", {}) if f.exists() else {}
    _apply_wall_marks(room, sessions)
    if "height_auto_m" in room:
        room["height_m"], room["ceiling"]["wall_m"] = room.pop("height_auto_m"), room.pop("ceiling_auto_m")
    room.pop("marks", None)
    heights, spreads, floors, ceilings = [], [], 0, 0
    for marks in sessions.values():
        fy = [m["y"] for m in marks if m.get("kind") == "floor"]
        cy = [m["y"] for m in marks if m.get("kind") == "ceiling"]
        floors += len(fy)
        ceilings += len(cy)
        if fy and cy:
            heights.append(float(np.median(cy) - np.median(fy)))
            spreads += [float(np.ptp(v)) for v in (fy, cy) if len(v) > 1]
    if heights:
        h, spread = float(np.median(heights)), max(spreads, default=0.0)
        room["marks"] = {"floor": floors, "ceiling": ceilings, "spread_cm": round(spread * 100, 1), "height_m": round(h, 3), "used": spread <= MARKS_TIGHT}
        if spread <= MARKS_TIGHT:
            room["height_auto_m"], room["ceiling_auto_m"] = room.get("height_m"), room["ceiling"]["wall_m"]
            room["height_m"] = round(h, 3)
            if room["floor"].get("wall_m"):
                room["ceiling"]["wall_m"] = round(h - room["floor"]["wall_m"], 3)
    elif floors or ceilings:
        room["marks"] = {"floor": floors, "ceiling": ceilings, "spread_cm": None, "height_m": None, "used": False}
    return room


MAX_PARTNERS = 40             # each frame is compared with at most this many others
FEATURE_WEIGHT = 3.0


def _feature_links(d: Path, photos: list[dict], F: list[dict]) -> list:
    """For frames with a view in common: the same photo features as 3D points in each (i, j, points in i, points in j)."""
    orb = cv2.ORB_create(1500)
    feats = []
    for ph, f in zip(photos, F):
        img = cv2.imread(str(d / "photos" / ph["file"]), cv2.IMREAD_REDUCED_GRAYSCALE_2)
        if img is None:
            feats.append(None)
            continue
        kp, des = orb.detectAndCompute(img, None)
        if des is None or not kp:
            feats.append(None)
            continue
        xy = np.array([k.pt for k in kp]) * 2          # full photo pixels
        u = (xy[:, 0] * f["w"] / ph["width"]).astype(int).clip(0, f["w"] - 1)
        v = (xy[:, 1] * f["h"] / ph["height"]).astype(int).clip(0, f["h"] - 1)
        ok = f["g"][v, u]
        feats.append((des[ok], f["P"][v[ok], u[ok]], _mat(ph["transform"])[:3, 2]) if ok.sum() >= 10 else None)
    match = cv2.BFMatcher(cv2.NORM_HAMMING)
    links = []
    n = len(F)
    for i in range(n):
        if feats[i] is None:
            continue
        for j in np.unique(np.linspace(i + 1, n - 1, min(max(n - i - 1, 0), MAX_PARTNERS)).astype(int)) if i + 1 < n else []:
            if feats[j] is None or float(feats[i][2] @ feats[j][2]) < 0.5:     # the views are over 60 degrees apart
                continue
            good = [p[0] for p in match.knnMatch(feats[i][0], feats[j][0], k=2) if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
            if len(good) < 8:
                continue
            Xi = feats[i][1][[g.queryIdx for g in good]]
            Xj = feats[j][1][[g.trainIdx for g in good]]
            near = np.linalg.norm(Xi - Xj, axis=1) < 0.6        # a wrong match lands far away
            if near.sum() >= 6:
                links.append((i, int(j), Xi[near], Xj[near]))
    return links


def refine_positions(d: Path, photos: list[dict], rounds: int = 6, step: int = 9, per_pair: int = 120) -> tuple[list[dict], float]:
    """The frames with their positions corrected against each other, and the largest correction (m). Turning on one spot and pointing at
    a blank ceiling give ARKit little to go on: on a real sweep its height for the phone was off by up to 29 cm from frame to frame, while
    each frame's own surfaces were level and its turn was right. So only the positions move: where two frames see the same surface, the
    point in one should lie on the surface in the other, which is linear in their shifts (point to plane). A wall says nothing about
    height, though, so nothing would tie the frames looking at the floor to those looking at the ceiling: for that, the same photo feature
    seen in two frames should be at the same place in 3D. The shift of the whole set is free, so the mean stays at zero."""
    F = []
    for ph in photos:
        D = meshscan._depth_map(d, ph)
        if D is None:
            return photos, 0.0
        D = D.astype(np.float64)
        D[(D < NEAR_M) | (D > RANGE[1])] = 0
        P = frames.world_points(ph, D)
        n = np.cross(P[2:, 1:-1] - P[:-2, 1:-1], P[1:-1, 2:] - P[1:-1, :-2])
        N = np.zeros_like(P)
        N[1:-1, 1:-1] = n / (np.linalg.norm(n, axis=2, keepdims=True) + 1e-9)
        g = D > 0
        g[:2] = g[-2:] = False
        g[:, :2] = g[:, -2:] = False
        k = np.array(ph["intrinsics"], float)
        h, w = D.shape
        F.append({"P": P, "N": N, "g": g, "inv": np.linalg.inv(_mat(ph["transform"])), "h": h, "w": w, "fx": k[0][0] * w / ph["width"],
                  "fy": k[1][1] * h / ph["height"], "cx": k[2][0] * w / ph["width"], "cy": k[2][1] * h / ph["height"]})
    n = len(F)
    t = np.zeros((n, 3))
    rng = np.random.default_rng(0)
    others = [np.unique(np.linspace(0, n - 1, min(n, MAX_PARTNERS)).astype(int)) for _ in range(n)]
    links = _feature_links(d, photos, F)
    for it in range(rounds):
        gate = max(0.35 * 0.6 ** it, 0.03)      # surfaces further apart than this are not the same surface
        rows, cols, vals, rhs = [], [], [], []
        row = 0
        for i, a_ in enumerate(F):
            X = a_["P"][a_["g"]][::step] + t[i]
            for j in others[i]:
                b_ = F[j]
                if i == j:
                    continue
                C = (X - t[j]) @ b_["inv"][:3, :3].T + b_["inv"][:3, 3]
                z = -C[:, 2]
                with np.errstate(all="ignore"):
                    u, v = b_["fx"] * C[:, 0] / z + b_["cx"], -b_["fy"] * C[:, 1] / z + b_["cy"]
                ok = (z > NEAR_M) & (u >= 2) & (u < b_["w"] - 2) & (v >= 2) & (v < b_["h"] - 2)
                if ok.sum() < 150:
                    continue
                ui, vi = u[ok].astype(int), v[ok].astype(int)
                seen = b_["g"][vi, ui]
                if seen.sum() < 150:
                    continue
                nrm = b_["N"][vi[seen], ui[seen]]
                e = ((X[ok][seen] - b_["P"][vi[seen], ui[seen]] - t[j]) * nrm).sum(1)
                near = np.abs(e) < gate
                if near.sum() < 100:
                    continue
                e, nrm = e[near], nrm[near]
                if len(e) > per_pair:
                    pick = rng.choice(len(e), per_pair, replace=False)
                    e, nrm = e[pick], nrm[pick]
                m = len(e)
                r = np.arange(row, row + m)
                row += m
                for ax in range(3):
                    rows += [r, r]
                    cols += [np.full(m, 3 * i + ax), np.full(m, 3 * j + ax)]
                    vals += [nrm[:, ax], -nrm[:, ax]]
                rhs.append(-e)
        for i, j, Xi, Xj in links:              # weighted: fewer but each one says more, and it holds in all three directions
            e = Xi + t[i] - Xj - t[j]
            near = np.linalg.norm(e, axis=1) < max(gate * 1.5, 0.06)
            if near.sum() < 4:
                continue
            e = e[near]
            m = len(e)
            for ax in range(3):
                r = np.arange(row, row + m)
                row += m
                rows += [r, r]
                cols += [np.full(m, 3 * i + ax), np.full(m, 3 * j + ax)]
                vals += [np.full(m, FEATURE_WEIGHT), np.full(m, -FEATURE_WEIGHT)]
                rhs.append(-e[:, ax] * FEATURE_WEIGHT)
        if not rhs:
            break
        A = sparse.coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(row, 3 * n)).tocsr()
        A = sparse.vstack([A, sparse.identity(3 * n, format="csr")]).tocsr()     # a frame nothing else sees stays where it is
        dt = sparse_linalg.lsqr(A, np.r_[np.concatenate(rhs), np.zeros(3 * n)], iter_lim=300)[0].reshape(n, 3)
        t += dt - dt.mean(0)
    out = []
    for ph, shift in zip(photos, t):
        c = np.array(ph["transform"], dtype=float)
        if c.ndim == 1:
            c[12:15] += shift
        else:
            c[3, :3] += shift
        out.append({**ph, "transform": c.tolist()})
    return out, float(np.abs(t).max())


def convert_sweep(d: Path, scale: float = 1.0) -> tuple[bool, str]:
    """Sweep-from-one-spot room: depth panorama -> measurements, flat clean surface, panorama colours. Writes colored.ply, mesh.bin,
    points.bin, report.json, room.json and panorama.jpg/json. Returns (ok, log)."""
    from . import panorama
    everything = depth_photos(d)
    photos = sweep_frames(d, everything)
    if len(photos) < 5:
        return False, "a sweep needs at least 5 frames with LiDAR depth"
    photos, moved = refine_positions(d, photos)
    centre = centre_of(photos)
    D, used = fuse_depth_sphere(d, photos, centre)
    cover = float(np.isfinite(D).mean())
    if cover < 0.05:
        return False, "the depth frames covered almost none of the sphere"
    heading = _heading(photos[0])
    room, D2 = measure(D, centre, heading)
    outline = room_outline(points_of(D, centre)[0], centre, room["ceiling"]["wall_m"])
    if outline:
        room["outline"] = outline
    fixed = {p["file"]: p["transform"] for p in photos}     # the corrected positions, for the colours too
    pano = panorama.ensure(d, width=panorama.WIDTH, centre=centre, depth=D2, force=True, photos=[{**p, "transform": fixed[p["file"]]} for p in panorama.usable_photos(d, set(fixed))])
    img = pano["_image"]
    ph, pw = img.shape[:2]
    h, w = D2.shape
    cu = ((2 * np.arange(w) + 1) * pw // (2 * w))[None, :].repeat(h, 0)     # the panorama pixel under each cell's centre
    cv_ = ((2 * np.arange(h) + 1) * ph // (2 * h))[:, None].repeat(w, 1)
    col = img[cv_, cu]
    mesh = depth_mesh(D2, centre, col)
    if mesh is None or len(mesh.faces) == 0:
        return False, "the depth panorama could not be turned into a surface"
    back = backdrop(D2, centre, col)
    if back is not None and len(back.faces):
        mesh = trimesh.util.concatenate([mesh, back])
    if scale != 1.0:
        mesh.apply_scale(scale)
    rgb = np.asarray(mesh.visual.vertex_colors)[:, :3]
    (d / "mesh.bin").write_bytes(meshscan.pack_preview(mesh, rgb))
    mesh.export(d / "colored.ply")
    P, _ = points_of(D2, centre)
    ok = np.isfinite(D2)
    (d / "points.bin").write_bytes(meshscan.pack_points((P[ok] * scale).astype(np.float32), col[ok]))
    for stale in ("tri_photo.bin", "texturing.json"):    # an earlier conversion's photo-texture plan no longer matches the triangles
        try:
            (d / stale).unlink()
        except FileNotFoundError:
            pass
    if scale != 1.0:
        for k in ("width_m", "length_m", "height_m"):
            if k in room:
                room[k] = round(room[k] * scale, 3)
    room.update(position_fix_cm=round(moved * 100), frames_used=used, frames_elsewhere=len(everything) - len(photos), panorama_coverage=pano["coverage"], depth_coverage=round(cover, 3))
    apply_marks(room, d)
    (d / "room.json").write_text(json.dumps(room, indent=1))
    rep = {"vertices": int(len(mesh.vertices)), "faces": int(len(mesh.faces)), "points": int(ok.sum()),
           "size_m": [round(float(x), 2) for x in mesh.extents], "coloured": pano["coverage"], "photos": len(photos)}
    (d / "report.json").write_text(json.dumps(rep))
    meshscan.write_settings(d, mode="sweep from one spot (depth panorama)", frames_used=used, surface_method="spherical depth image",
                            depth_range=f"{RANGE[0]} to {RANGE[1]} m", surface="median depth per direction, flat walls snapped, coloured from the 360 panorama",
                            photos=len(photos), scale=scale)
    size = " x ".join(f"{room[k]:g}" for k in ("width_m", "length_m", "height_m") if k in room)
    moved = (f"; {len(everything) - len(photos)} frames left out (taken more than {SPOT_M:g} m from the main spot, or pointed at you)"
             if len(photos) < len(everything) else "")
    return True, (f"sweep from one spot: {used} frames fused into a depth panorama ({round(cover * 100)}% of directions), {len(mesh.faces)} triangles, "
                  f"{round(pano['coverage'] * 100)}% coloured from the panorama" + (f", room about {size} m" if size else "") + moved)


def _heading(photo: dict) -> float:
    """Azimuth (radians, 0 = -Z, +90 = +X) the camera looks along."""
    f = -_mat(photo["transform"])[:3, 2]
    return float(math.atan2(f[0], -f[2]))
