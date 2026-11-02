"""Points clicked while walking and filming, turned into a plan.

The clicks, the frames and the LiDAR share one set of coordinates, so each click can be looked up on the scanned surface. The flat surfaces round a click say what
it is: two walls meeting make a corner, a wall and the floor a floor edge, and so on. Clicks on one wall agree on one line, and corners are where those lines meet,
which is more exact than any click. The order of the clicks means nothing: each knows where it is, and the walls are joined by what the LiDAR saw between them.
Doors and windows can't be told from the shape, so they come from a tag given on the phone or later on the page."""
import json
import math
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree

from PIL import Image

from . import frames, layout, meshscan

KINDS = ("corner", "floor edge", "ceiling edge", "wall", "floor", "ceiling", "door", "window", "ignore", "unknown")
NEAR = 0.3             # metres round a click to look for surfaces
ON_PLANE = 0.02        # metres
MIN_ON_PLANE = 30      # readings
SNAP = 0.25            # metres a click may move to the surface
LEVEL_NEAR = 0.4       # metres from the floor or ceiling that counts as at it: a click aimed at the join from close by lands about this near
SAME = 0.15            # metres: clicks this close are one corner
STRAIGHT_DEG = 8       # a corner that turns less than this is just a point along a wall
SUPPORT = 0.35         # share of a stretch that must have readings for it to be a wall
OFF_WALL = 0.12        # metres
SAME_WALL_DEG = 8      # planes this close in direction, and
SAME_WALL_M = 0.1      # this close in place, are one wall
WALL_FIT = 0.04         # metres either side of a wall's line that count as on it
NEAR_CORNER = 1.5       # metres: walls crossing this near the clicks on them meet in a corner
OFF_PLAN = 0.25         # metres from the plan's walls that gets a point flagged
OPENING_LINK = 1.8      # metres along a wall: clicks further apart are two doors or windows
OPENING_MIN = 0.3       # metres: clicks spreading less than this don't show a size
USUAL = {"door": {"width": 0.9, "sill": 0.0, "head": 2.1}, "window": {"width": 1.2, "sill": 0.9, "head": 2.1}}
PHOTO_FAR = 5.0         # metres
PHOTO_APART = 8         # frames: photos shown of one point differ by at least this
FAR_CORNER = 4.0       # metres: an unclicked corner further than this from the clicks either side is not believed


def surface(d: Path, most: int = 400_000) -> np.ndarray:
    """What the scan measured, as world points."""
    for name in ("colored.ply", "scan.ply"):
        if (d / name).exists():
            v = np.asarray(trimesh.load(d / name, process=False).vertices, float)
            v = v[np.isfinite(v).all(axis=1)]
            return v[np.random.default_rng(0).choice(len(v), most, replace=False)] if len(v) > most else v
    return np.zeros((0, 3))


def heights(P: np.ndarray) -> tuple[float | None, float | None]:
    """Floor and ceiling: flat surfaces pile up many readings at one height, walls spread theirs out."""
    if len(P) < 500:
        return None, None
    y = P[:, 1]
    lo, hi = np.percentile(y, [0.5, 99.5])
    if hi - lo < 1:
        return None, None
    edges = np.arange(lo - 0.02, hi + 0.04, 0.02)
    count, _ = np.histogram(y, edges)
    mid = (edges[:-1] + edges[1:]) / 2
    def peak(keep: np.ndarray) -> float | None:
        if not keep.any():
            return None
        c = np.where(keep, count, 0)
        return float(mid[np.argmax(c)]) if c.max() >= 3 * np.median(count[keep]) else None
    span = hi - lo
    return peak(mid < lo + 0.4 * span), peak(mid > hi - 0.4 * span)


def _planes(near: np.ndarray, rng: np.random.Generator) -> list[tuple[np.ndarray, float, int]]:
    """Up to three flat surfaces among the readings near a click: (unit normal, offset, how many readings). Largest first."""
    out = []
    pts = near
    for _ in range(3):
        if len(pts) < MIN_ON_PLANE:
            break
        best, best_n, best_d = None, None, 0.0
        for _ in range(80):
            a, b, c = pts[rng.choice(len(pts), 3, replace=False)]
            n = np.cross(b - a, c - a)
            L = np.linalg.norm(n)
            if L < 1e-6:
                continue
            n = n / L
            on = np.abs((pts - a) @ n) < ON_PLANE
            if best is None or on.sum() > best.sum():
                best, best_n, best_d = on, n, float(a @ n)
        if best is None or best.sum() < MIN_ON_PLANE:
            break
        # the best fit through all its readings, not just the three it started from
        q = pts[best]
        c = q.mean(axis=0)
        n = np.linalg.svd(q - c, full_matrices=False)[2][2]
        out.append((n, float(c @ n), int(best.sum())))
        pts = pts[np.abs((pts - c) @ n) >= ON_PLANE * 2]
    return out


def classify(click: dict, tree: cKDTree | None, P: np.ndarray, floor_y: float | None, ceil_y: float | None, rng: np.random.Generator) -> dict:
    """What a click is, and where it really lies."""
    p = np.array([click["x"], click["y"], click["z"]], float)
    out = {"id": click.get("id"), "x": float(p[0]), "y": float(p[1]), "z": float(p[2]), "tag": click.get("tag"), "snapped_cm": 0.0, "walls": 0}
    tag = click.get("tag")
    walls, flats = [], []
    if tree is not None:
        idx = tree.query_ball_point(p, NEAR)
        if len(idx) >= MIN_ON_PLANE:
            for n, d, _ in _planes(P[idx], rng):
                if abs(n[1]) < 0.2:
                    walls.append((n, d))
                elif abs(n[1]) > 0.9:
                    flats.append((n, d))
    at_floor = floor_y is not None and abs(p[1] - floor_y) < LEVEL_NEAR
    at_ceil = ceil_y is not None and abs(p[1] - ceil_y) < LEVEL_NEAR
    corner = None
    for i, a in enumerate(walls):
        for b in walls[i + 1:]:
            cross = a[0][0] * b[0][2] - a[0][2] * b[0][0]
            if abs(cross) > math.sin(math.radians(30)):          # two walls at a real angle
                corner = _meet(a, b)
                break
        if corner is not None:
            break
    guess = "unknown"
    q = p.copy()
    if corner is not None and math.hypot(corner[0] - p[0], corner[1] - p[2]) < SNAP:
        guess = "corner"
        q[0], q[2] = corner
    elif walls:
        n, d = walls[0]
        moved = p - n * (p @ n - d)
        if np.linalg.norm(moved - p) < SNAP:
            q = moved
        guess = "floor edge" if at_floor else "ceiling edge" if at_ceil else "wall"
    elif flats or at_floor or at_ceil:
        guess = "floor" if at_floor or (flats and floor_y is not None and abs(p[1] - floor_y) < abs(p[1] - (ceil_y if ceil_y is not None else p[1] + 9))) else "ceiling"
    if click.get("fixed"):       # put there by hand: the scan doesn't get to move it
        q = p.copy()
    # a wall as a level line: nx * x + nz * z = d, with the tilt left out
    lines = [[float(n[0] / math.hypot(n[0], n[2])), float(n[2] / math.hypot(n[0], n[2])), float(d / math.hypot(n[0], n[2]))] for n, d in walls if abs(float(n @ p - d)) < SNAP]
    out.update(kind=tag or guess, guess=guess, x=float(q[0]), y=float(q[1]), z=float(q[2]), snapped_cm=round(float(np.linalg.norm(q - p)) * 100, 1), walls=len(walls), lines=lines, fixed=bool(click.get("fixed")))
    return out


def _meet(a: tuple, b: tuple) -> tuple[float, float]:
    """Where two upright walls meet, as world x, z."""
    A = np.array([[a[0][0], a[0][2]], [b[0][0], b[0][2]]], float)
    # the walls' normals are nearly level, so their small tilt is left out of the crossing
    x, z = np.linalg.solve(A, [a[1], b[1]])
    return float(x), float(z)


def _cluster(points: list[dict], band: np.ndarray | None = None) -> list[list[float]]:
    """Clicks on one wall found the same line several times over: one line per real wall. Each point's `lines` becomes the numbers of its walls.
    A click only sees 30 cm of wall, so a line from it can be a degree out, which is centimetres at the far end. `band` is what the LiDAR read at wall height, as world
    x, z: each line is then fitted through everything along the whole wall."""
    walls: list[list] = []
    for p in points:
        p["wall_ids"] = []
        for nx, nz, d in p.get("lines", []):
            for i, w in enumerate(walls):
                sign = 1.0 if nx * w[0] + nz * w[1] >= 0 else -1.0
                level = w[0] * p["x"] + w[1] * p["z"] - w[2]        # measured here, at the click: a small tilt makes offsets far from the origin meaningless
                if nx * w[0] * sign + nz * w[1] * sign > math.cos(math.radians(SAME_WALL_DEG)) and abs(level) < SAME_WALL_M:
                    n = w[3]
                    w[0], w[1], w[2] = (w[0] * n + nx * sign) / (n + 1), (w[1] * n + nz * sign) / (n + 1), (w[2] * n + d * sign) / (n + 1)
                    w[3] = n + 1
                    p["wall_ids"].append(i)
                    break
            else:
                walls.append([nx, nz, d, 1])
                p["wall_ids"].append(len(walls) - 1)
    out = []
    for w in walls:
        L = math.hypot(w[0], w[1])
        line = [w[0] / L, w[1] / L, w[2] / L]
        out.append(_along_wall(line, band) if band is not None and len(band) else line)
    # two groups that turn out to be one wall once fitted properly
    same = list(range(len(out)))
    for i in range(len(out)):
        for j in range(i):
            if same[j] == j and abs(out[i][0] * out[j][0] + out[i][1] * out[j][1]) > math.cos(math.radians(3)):
                sign = 1.0 if out[i][0] * out[j][0] + out[i][1] * out[j][1] > 0 else -1.0
                if abs(out[i][2] * sign - out[j][2]) < WALL_FIT * 2:
                    same[i] = j
                    break
    for p in points:
        p["wall_ids"] = sorted({same[i] for i in p["wall_ids"]})
    return out


def _along_wall(line: list[float], band: np.ndarray) -> list[float]:
    """The line through all the readings on it, closing in as it gets better. Anything else in the room is too far off to count."""
    nx, nz, d = line
    for near in (WALL_FIT * 2, WALL_FIT, WALL_FIT):
        q = band[np.abs(band[:, 0] * nx + band[:, 1] * nz - d) < near]
        if len(q) < 50:
            break
        c = q.mean(axis=0)
        n = np.linalg.svd(q - c, full_matrices=False)[2][1]
        if n[0] * nx + n[1] * nz < 0:
            n = -n
        nx, nz, d = float(n[0]), float(n[1]), float(c @ n)
    return [nx, nz, d]


def _cross(a: list[float], b: list[float]) -> tuple[float, float] | None:
    det = a[0] * b[1] - a[1] * b[0]
    if abs(det) < math.sin(math.radians(30)):
        return None
    return (a[2] * b[1] - a[1] * b[2]) / det, (a[0] * b[2] - a[2] * b[0]) / det


def _support(a: list[float], b: list[float], tree: cKDTree | None) -> float:
    """The share of a line between two spots that has readings along it: 1 along a wall, near 0 across open floor."""
    if tree is None:
        return 0.0
    n = max(2, int(math.dist(a, b) / 0.1))
    q = np.array([[a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t] for t in (np.arange(n) + 0.5) / n])
    return float((tree.query(q)[0] < OFF_WALL).mean())


def _tour(pts: list[list[float]], tree: cKDTree | None) -> list[int]:
    """The way round all the points that keeps to the walls: short, and along readings. A line with no readings costs three times its length, so the tour only
    crosses open space where it has to. The result never crosses itself, because uncrossing always shortens it."""
    n = len(pts)
    cx, cy = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
    order = sorted(range(n), key=lambda i: math.atan2(pts[i][1] - cy, pts[i][0] - cx))     # a first guess, seen from the middle
    cost = [[math.dist(pts[i], pts[j]) * (1 + 2 * (1 - _support(pts[i], pts[j], tree))) if i != j else 0.0 for j in range(n)] for i in range(n)]
    def total(o: list[int]) -> float:
        return sum(cost[o[i]][o[(i + 1) % n]] for i in range(n))
    best = total(order)
    improved = True
    while improved:
        improved = False
        for i in range(n - 1):                    # reverse a stretch
            for j in range(i + 2, n):
                o = order[:i + 1] + order[i + 1:j + 1][::-1] + order[j + 1:]
                c = total(o)
                if c < best - 1e-6:
                    order, best, improved = o, c, True
        for i in range(n):                        # move one point elsewhere
            for j in range(n):
                if i == j:
                    continue
                o = order[:]
                o.insert(j, o.pop(i))
                c = total(o)
                if c < best - 1e-6:
                    order, best, improved = o, c, True
    return order


def _outline(points: list[dict], tree: cKDTree | None = None, band: np.ndarray | None = None) -> list[list[float]]:
    """The room's outline as plan x, y. It doesn't use the order of the clicks. Clicks close together merge, points are moved onto their walls, and a corner nobody
    clicked is put where two walls meet, if the readings run along them to it."""
    use = [p for p in points if p["kind"] in ("corner", "floor edge", "ceiling edge", "wall")]
    walls = _cluster(use, band)
    groups: list[dict] = []
    for p in use:
        xy = [p["x"], p["z"]]
        for g in groups:
            if math.dist(g["xy"], xy) < SAME:
                g["ids"] |= set(p["wall_ids"])
                g["corner"] = g["corner"] or p["kind"] == "corner"
                if p.get("fixed"):
                    g["xy"], g["fixed"] = xy, True
                break
        else:
            groups.append({"xy": xy, "ids": set(p["wall_ids"]), "corner": p["kind"] == "corner", "fixed": bool(p.get("fixed"))})
    if len(groups) < 3:
        return [[g["xy"][0], -g["xy"][1]] for g in groups]
    for g in groups:                              # onto the walls: where two meet if it has two, else straight onto the one
        if g["fixed"]:
            continue
        ids = sorted(g["ids"])
        spot = None
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                spot = spot or _cross(walls[a], walls[b])
        if spot and math.dist(spot, g["xy"]) < SNAP:
            g["xy"] = list(spot)
        elif ids:
            w = walls[ids[0]]
            off = w[0] * g["xy"][0] + w[1] * g["xy"][1] - w[2]
            if abs(off) < SNAP:
                g["xy"] = [g["xy"][0] - w[0] * off, g["xy"][1] - w[1] * off]
    plan = [[g["xy"][0], -g["xy"][1]] for g in groups]
    order = _tour(plan, tree)
    v: list[list[float]] = []
    for k, i in enumerate(order):
        a, b = groups[i], groups[order[(k + 1) % len(order)]]
        pa, pb = plan[i], plan[order[(k + 1) % len(order)]]
        v.append(pa)
        if a["ids"] & b["ids"] or not a["ids"] or not b["ids"]:
            continue
        found = None
        for x in a["ids"]:
            for y in b["ids"]:
                found = found or _cross(walls[x], walls[y])
        if found:
            q = [found[0], -found[1]]
            direct, via = _support(pa, pb, tree), min(_support(pa, q, tree), _support(q, pb, tree))
            reach = max(math.dist(pa, q), math.dist(pb, q))
            # close by, two walls crossing is enough, as far corners often scan rounded. Further off, the readings must run to it, or it may be open floor
            if reach < NEAR_CORNER or (reach < FAR_CORNER and via >= SUPPORT and via > direct + 0.1):
                v.append(q)
    changed = True
    while changed and len(v) > 3:
        changed = False
        for i in range(len(v)):
            a, b, c = v[i - 1], v[i], v[(i + 1) % len(v)]
            u, w = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
            turn = abs(math.degrees(math.atan2(u[0] * w[1] - u[1] * w[0], u[0] * w[0] + u[1] * w[1])))
            if turn < STRAIGHT_DEG:
                v.pop(i)
                changed = True
                break
    return v


def _no_wall(v: list[list[float]], cells: np.ndarray) -> list[bool]:
    """A stretch with few readings along it has no wall, as into a kitchen."""
    if not len(cells):
        return [False] * len(v)
    tree = cKDTree(cells)
    out = []
    for a, b in zip(v, v[1:] + v[:1]):
        n = max(2, int(math.dist(a, b) / 0.1))
        q = np.array([[a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t] for t in (np.arange(n) + 0.5) / n])
        out.append(float((tree.query(q)[0] < OFF_WALL).mean()) < SUPPORT)
    return out


def flag_points(points: list[dict], v: list[list[float]], floor_y: float | None, ceil_y: float | None) -> None:
    """Things worth a look. Nothing is ever dropped: a point left off the plan stays, with a note saying so."""
    def off_plan(xy: list[float]) -> float:
        best = 99.0
        for a, b in zip(v, v[1:] + v[:1]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            t = max(0.0, min(1.0, ((xy[0] - a[0]) * dx + (xy[1] - a[1]) * dy) / (dx * dx + dy * dy or 1e-9)))
            best = min(best, math.dist(xy, [a[0] + t * dx, a[1] + t * dy]))
        return best
    for p in points:
        flag = None
        if p["kind"] == "ignore":
            pass
        elif p["kind"] == "unknown":
            mid = floor_y is not None and ceil_y is not None and floor_y + 0.3 < p["y"] < ceil_y - 0.1
            flag = "Nothing solid here: maybe a door or window. Look at the photos." if mid else "The scan can't tell what this is. Look at the photos."
        elif p["kind"] in ("corner", "floor edge", "ceiling edge", "wall") and len(v) >= 3 and off_plan([p["x"], -p["z"]]) > OFF_PLAN:
            flag = "Not on the plan's walls: furniture, a door or window, or a wall the plan missed. Look at the photos."
        p["flag"] = flag


def photos_of(d: Path, p: list[float], frames: list[dict], most: int = 4) -> list[dict]:
    """The photos that see this spot best, each with where it is in the picture as shown the right way up (0 to 1). Near the middle of the picture and close by is best;
    a photo with something in front of the spot doesn't count."""
    q = np.asarray(p, float)
    scored = []
    for i, ph in enumerate(frames):
        if not ph.get("transform") or not ph.get("intrinsics") or not ph.get("width"):
            continue
        M = np.asarray(ph["transform"], float).T if np.asarray(ph["transform"]).shape == (4, 4) else None
        if M is None:
            continue
        pc = (q - M[:3, 3]) @ M[:3, :3]
        z = -pc[2]
        if not 0.25 < z < PHOTO_FAR:
            continue
        k = ph["intrinsics"]
        fx, fy, cx, cy = float(k[0][0]), float(k[1][1]), float(k[2][0]), float(k[2][1])
        w, h = float(ph["width"]), float(ph["height"])
        u, v = fx * pc[0] / z + cx, cy - fy * pc[1] / z
        if not (0.03 * w < u < 0.97 * w and 0.03 * h < v < 0.97 * h):
            continue
        centred = 1 - max(abs(u - w / 2) / (w / 2), abs(v - h / 2) / (h / 2))
        scored.append((centred - z / PHOTO_FAR, i, ph, u, v, w, h, z))
    scored.sort(key=lambda x: -x[0])
    out: list[dict] = []
    used: list[int] = []
    for _, i, ph, u, v, w, h, z in scored:
        if any(abs(i - o) < PHOTO_APART for o in used):
            continue
        dm = meshscan._depth_map(d, ph) if ph.get("depth_file") else None
        if dm is not None:
            dh, dw = dm.shape
            seen = dm[min(int(v * dh / h), dh - 1), min(int(u * dw / w), dw - 1)]
            if seen > 0.1 and z > seen + 0.25:
                continue
        # as /photos/{name}?up=1 turns it
        t = ph["transform"]
        x_up, y_up = t[0][1], t[1][1]
        if abs(x_up) > abs(y_up):
            a, b = (v / h, 1 - u / w) if x_up > 0 else (1 - v / h, u / w)
            aspect = h / w
        elif y_up < 0:
            a, b, aspect = 1 - u / w, 1 - v / h, w / h
        else:
            a, b, aspect = u / w, v / h, w / h
        out.append({"file": ph["file"], "u": round(a, 4), "v": round(b, 4), "aspect": round(aspect, 4), "away_m": round(z, 2), "transform": ph["transform"], "intrinsics": ph["intrinsics"], "width": w, "height": h})
        used.append(i)
        if len(out) >= most:
            break
    return out


def _camera(ph: dict) -> tuple:
    M = np.asarray(ph["transform"], float).T
    k = ph["intrinsics"]
    return M, float(k[0][0]), float(k[1][1]), float(k[2][0]), float(k[2][1]), float(ph["width"]), float(ph["height"])


def photo_point(d: Path, ph: dict, a: float, b: float) -> list[float] | None:
    """A spot in a photo as shown the right way up (0 to 1) becomes a place in the room, from the LiDAR reading the phone took with that photo. None if it has no reading there."""
    M, fx, fy, cx, cy, w, h = _camera(ph)
    t = ph["transform"]
    x_up, y_up = t[0][1], t[1][1]
    if abs(x_up) > abs(y_up):
        v, u = (a * h, (1 - b) * w) if x_up > 0 else ((1 - a) * h, b * w)
    elif y_up < 0:
        u, v = (1 - a) * w, (1 - b) * h
    else:
        u, v = a * w, b * h
    dm = meshscan._depth_map(d, ph) if ph.get("depth_file") else None
    if dm is None:
        return None
    dh, dw = dm.shape
    i, j = int(v * dh / h), int(u * dw / w)
    near = dm[max(i - 2, 0):i + 3, max(j - 2, 0):j + 3]
    near = near[near > 0.1]
    if not near.size:
        return None
    z = float(np.median(near))
    return [round(float(x), 4) for x in M[:3, 3] + M[:3, :3] @ np.array([(u - cx) * z / fx, (cy - v) * z / fy, -z])]


def seen_in(d: Path, ph: dict, points: list[dict]) -> list[str]:
    """The points this photo really shows: in the picture and with nothing in front of them."""
    M, fx, fy, cx, cy, w, h = _camera(ph)
    dm = meshscan._depth_map(d, ph) if ph.get("depth_file") else None
    out = []
    for p in points:
        pc = (np.array([p["x"], p["y"], p["z"]], float) - M[:3, 3]) @ M[:3, :3]
        z = -pc[2]
        if z < 0.1:
            continue
        u, v = fx * pc[0] / z + cx, cy - fy * pc[1] / z
        if not (0 <= u < w and 0 <= v < h):
            continue
        if dm is not None:
            seen = dm[min(int(v * dm.shape[0] / h), dm.shape[0] - 1), min(int(u * dm.shape[1] / w), dm.shape[1] - 1)]
            if seen > 0.1 and z > seen + 0.3:
                continue
        out.append(p["id"])
    return out


def openings_from(points: list[dict], v: list[list[float]], gaps: list[bool], floor_y: float) -> list[dict]:
    """Doors and windows from their clicks. Clicks close together along one wall are one opening: how far they spread gives its width, their lowest and highest its height.
    A size the clicks don't show is marked unknown, and a usual one is used. All in plan x, y before the drawing turn."""
    solid = [(a, b) for (a, b), g in zip(zip(v, v[1:] + v[:1]), gaps) if not g] or list(zip(v, v[1:] + v[:1]))
    near: dict[tuple, list] = {}
    for p in points:
        if p["kind"] not in ("door", "window"):
            continue
        xy = [p["x"], -p["z"]]
        best = None
        for i, (a, b) in enumerate(solid):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L = math.hypot(dx, dy) or 1e-9
            t = max(0.0, min(L, ((xy[0] - a[0]) * dx + (xy[1] - a[1]) * dy) / L))
            dist = math.dist(xy, [a[0] + dx / L * t, a[1] + dy / L * t])
            if best is None or dist < best[0]:
                best = (dist, i, t)
        near.setdefault((p["kind"], best[1]), []).append((best[2], p["y"] - floor_y, p["x"], p["z"]))
    out = []
    for (kind, i), hits in near.items():
        hits.sort()
        group = [hits[0]]
        def close(g: list) -> None:
            a, b = solid[i]
            L = math.dist(a, b) or 1e-9
            along = [h[0] for h in g]
            ys = [h[1] for h in g]
            mid = (min(along) + max(along)) / 2
            wide, tall = max(along) - min(along), max(ys) - min(ys)
            known_w, known_h = wide >= OPENING_MIN, tall >= OPENING_MIN
            usual = USUAL[kind]
            sill = 0.0 if kind == "door" else (min(ys) if known_h else usual["sill"])
            head = max(max(ys), usual["head"]) if kind == "door" else (max(ys) if known_h else usual["head"])
            one = {"kind": kind, "at": [a[0] + (b[0] - a[0]) * mid / L, a[1] + (b[1] - a[1]) * mid / L], "width": round(wide if known_w else usual["width"], 3), "sill": round(max(sill, 0.0), 3),
                   "head": round(head, 3), "width_known": known_w, "height_known": known_h or kind == "door", "clicks": len(g)}
            if known_w:          # its sides are at the clicks themselves
                one["ends"] = [[round(g[0][2], 3), round(g[0][3], 3)], [round(g[-1][2], 3), round(g[-1][3], 3)]]
                one["width"] = round(math.hypot(g[0][2] - g[-1][2], g[0][3] - g[-1][3]), 3)
            out.append(one)
        for h in hits[1:]:
            if h[0] - group[-1][0] > OPENING_LINK:
                close(group)
                group = []
            group.append(h)
        close(group)
    return out


def bake_colours(d: Path, every: int = 1) -> Path:
    """Every point of the 3D surface coloured from the photo that sees it best, using all the frames, not only the few the textured view keeps. layout_colours.bin: r, g, b per
    vertex of mesh.bin. A photo with something nearer in front of the spot doesn't count, and what no photo saw keeps the colour it had."""
    out = d / "layout_colours.bin"
    mesh, rep = d / "mesh.bin", d / "report.json"
    if out.exists() and out.stat().st_mtime >= mesh.stat().st_mtime:
        return out
    n = json.loads(rep.read_text())["vertices"]
    buf = mesh.read_bytes()
    V = np.frombuffer(buf, "<f4", n * 3).reshape(-1, 3).astype(np.float32)
    rgb = np.frombuffer(buf, np.uint8, n * 3, n * 12).copy()
    rgb = rgb.reshape(-1, 3)
    best = np.full(n, -9.0, np.float32)
    for ph in frames.manifest(d)[::every]:
        if not (ph.get("transform") and ph.get("intrinsics") and ph.get("width") and (d / "photos" / ph["file"]).exists()):
            continue
        M = np.asarray(ph["transform"], float).T
        R, t = M[:3, :3].astype(np.float32), M[:3, 3].astype(np.float32)
        k = ph["intrinsics"]
        fx, fy, cx, cy = float(k[0][0]), float(k[1][1]), float(k[2][0]), float(k[2][1])
        w, h = float(ph["width"]), float(ph["height"])
        pc = (V - t) @ R
        z = -pc[:, 2]
        front = (z > 0.2) & (z < PHOTO_FAR + 1)
        idx = np.nonzero(front)[0]
        if not len(idx):
            continue
        u = fx * pc[idx, 0] / z[idx] + cx
        v = cy - fy * pc[idx, 1] / z[idx]
        ok = (u >= 0) & (u < w - 1) & (v >= 0) & (v < h - 1)
        idx, u, v = idx[ok], u[ok], v[ok]
        if not len(idx):
            continue
        centred = 1 - np.maximum(np.abs(u - w / 2) / (w / 2), np.abs(v - h / 2) / (h / 2))
        score = centred - z[idx] / PHOTO_FAR
        win = score > best[idx]
        idx, u, v, score = idx[win], u[win], v[win], score[win]
        if not len(idx):
            continue
        dm = meshscan._depth_map(d, ph) if ph.get("depth_file") else None
        if dm is not None:
            dh, dw = dm.shape
            seen = dm[np.minimum((v * dh / h).astype(int), dh - 1), np.minimum((u * dw / w).astype(int), dw - 1)]
            clear = ~((seen > 0.1) & (z[idx] > seen + 0.1))
            idx, u, v, score = idx[clear], u[clear], v[clear], score[clear]
            if not len(idx):
                continue
        img = np.asarray(Image.open(d / "photos" / ph["file"]).convert("RGB"))
        sx, sy = img.shape[1] / w, img.shape[0] / h
        rgb[idx] = img[np.minimum((v * sy).astype(int), img.shape[0] - 1), np.minimum((u * sx).astype(int), img.shape[1] - 1)]
        best[idx] = score
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(rgb.tobytes())
    tmp.replace(out)
    return out


def build(d: Path, clicks: list[dict]) -> dict:
    """Classify the clicks and write the plan. Returns the plan's numbers; a plan needs 3 points along its walls."""
    P = surface(d)
    floor_y, ceil_y = heights(P)
    ys = [c["y"] for c in clicks]
    floor_y = floor_y if floor_y is not None else (min(ys) if ys else None)
    ceil_y = ceil_y if ceil_y is not None else (max(ys) if ys else None)
    tree = cKDTree(P) if len(P) else None
    rng = np.random.default_rng(0)
    points = [classify(c, tree, P, floor_y, ceil_y, rng) for c in clicks]
    y = P[:, 1] if len(P) else np.zeros(0)
    def cells(mask: np.ndarray) -> list[list[float]]:
        if not mask.any():
            return []
        q = P[mask][:, [0, 2]] * [1, -1]
        return [[round(float(a), 3), round(float(b), 3)] for a, b in (np.unique(np.floor(q / layout.CELL), axis=0) + 0.5) * layout.CELL]
    top, low = [], []
    if floor_y is not None and ceil_y is not None:
        top, low = cells((y > ceil_y - 0.65) & (y < ceil_y - 0.08)), cells((y > floor_y + 0.1) & (y <= ceil_y - 0.65))
    seen = np.array(top + low, float).reshape(-1, 2)
    wall_height = (y > floor_y + 0.3) & (y < ceil_y - 0.08) if floor_y is not None and ceil_y is not None else np.zeros(len(y), bool)
    v = _outline(points, cKDTree(seen) if len(seen) else None, P[wall_height][:, [0, 2]])
    flag_points(points, v, floor_y, ceil_y)
    (d / "walk_result.json").write_text(json.dumps({"points": points, "floor_y": floor_y, "ceiling_y": ceil_y}))
    if len(v) < 3 or floor_y is None or ceil_y is None or ceil_y - floor_y < 1:
        return {"built": False, "points": len(points), "outline": len(v)}
    (d / "layout_points.json").write_text(json.dumps({"top": top, "all": low}))
    turn = layout._turn([{"a": [a[0], -a[1]], "b": [b[0], -b[1]]} for a, b in zip(v, v[1:] + v[:1])])
    gaps = _no_wall(v, seen)
    openings = [{**o, "at": layout._rotate(o["at"], turn)} for o in openings_from(points, v, gaps, floor_y)]
    lay = {"vertices": [layout._rotate(p, turn) for p in v], "openings": openings, "open": gaps, "height_m": round(ceil_y - floor_y, 3), "turn_deg": round(turn, 3),
           "floor_y": floor_y, "ceiling_y": ceil_y, "edited": False}
    (d / "layout.json").write_text(json.dumps(lay))
    return {"built": True, "points": len(points), "outline": len(v), "height_m": lay["height_m"]}
