"""A room's layout: its walls as a plan you can correct, no pictures, and what turns it into BIMCraft and Blender files.

Starts from the outline a sweep found (sweep.room_outline). Plan coordinates are metres, the same as BIMCraft's axes, turned by `turn_deg` so the longest wall lies along an axis. The walls run clockwise round the room, so in BIMCraft they face out."""
import json
import math
from pathlib import Path

import cv2
import numpy as np
import trimesh
from shapely.geometry import Polygon

from . import convert, join as join_mod

WALL_THICK = 0.1           # metres, for the Blender model
WALL_TYPE = "Generic - 200mm"
WALL_CENTRE = 0.1          # metres from the room's face to the middle of that wall
MERGE_TIDY = 0.12          # metres an outline may move to lose small steps when two are joined
OPEN_PAST = 0.4            # metres past a line that count as seeing through it
OPEN_WIDE = 0.1            # metres either side of the sight line
OPEN_SHARE = 0.5           # the share of a stretch that must be seen through
OVERLAY_CELL = 0.01        # metres per pixel of the scan seen from above
OVERLAY_MAX_EDGE = 0.35    # metres
CELL = 0.05                # metres, the grid of the grey readings on the plan


def _turn(walls: list[dict]) -> float:
    """Degrees that bring the longest wall onto the page's axes, so the plan is drawn straight."""
    if not walls:
        return 0.0
    w = max(walls, key=lambda w: math.dist(w["a"], w["b"]))
    ang = math.degrees(math.atan2(-(w["b"][1] - w["a"][1]), w["b"][0] - w["a"][0]))
    return -((ang + 45) % 90 - 45)


def _rotate(p: list[float], deg: float) -> list[float]:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [round(p[0] * c - p[1] * s, 3), round(p[0] * s + p[1] * c, 3)]


def default(room: dict) -> dict:
    walls = (room.get("outline") or {}).get("walls", [])
    turn = _turn(walls)
    verts: list[list[float]] = []
    for w in walls:
        for x, z in (w["a"], w["b"]):
            p = _rotate([x, -z], turn)
            if not verts or math.dist(verts[-1], p) > 0.03:
                verts.append(p)
    if len(verts) > 2 and math.dist(verts[0], verts[-1]) < 0.03:
        verts.pop()
    return {"vertices": verts, "openings": [], "height_m": room.get("height_m") or 2.5, "turn_deg": round(turn, 3), "edited": False}


def open_edges(verts: list[list[float]], cells: np.ndarray, spot: list[float]) -> list[bool]:
    """Which stretches of the outline have no wall: from where the phone stood, it saw things well past them, as into a kitchen. A wall hides what is behind it."""
    out = []
    for a, b in zip(verts, verts[1:] + verts[:1]):
        L = math.dist(a, b)
        n = max(2, int(L / 0.1))
        seen = 0
        for t in (np.arange(n) + 0.5) / n:
            q = np.array([a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]) - spot
            dist = np.linalg.norm(q)
            if dist < 1e-6 or not len(cells):
                continue
            u = q / dist
            rel = cells - spot
            along = rel @ u
            across = np.abs(rel[:, 0] * u[1] - rel[:, 1] * u[0])
            if ((along > dist + OPEN_PAST) & (across < OPEN_WIDE)).any():
                seen += 1
        out.append(seen / n > OPEN_SHARE)
    return out


def load(d: Path) -> dict:
    f, room_file = d / "layout.json", d / "room.json"
    room = json.loads(room_file.read_text()) if room_file.exists() else None
    if f.exists():
        lay = json.loads(f.read_text())
    elif room:
        lay = default(room)
    else:
        return {"vertices": [], "openings": [], "open": [], "height_m": 2.5, "turn_deg": 0.0, "edited": False}
    if len(lay.get("open") or []) != len(lay["vertices"]):       # worked out from the readings until someone says otherwise
        p = seen_cells(d, lay)
        cells = np.array(p["top"] + p["all"], float).reshape(-1, 2)
        turn = lay.get("turn_deg", 0.0)
        spots = [room["centre"]] if room else []
        for j in joins(d):       # stretch is open if the phone saw through it from any spot
            other = d.parent / j["scan"] / "room.json"
            if other.exists():
                spots.append(join_mod.world_point(json.loads(other.read_text())["centre"], j["fits"][j["pick"]], j["dy"]))
        flags = [False] * len(lay["vertices"])
        for c in spots:
            found = open_edges(lay["vertices"], cells, np.array(_rotate([c[0], -c[2]], turn)))
            flags = [x or y for x, y in zip(flags, found)]
        lay["open"] = flags
    return lay


def validate(body: dict) -> dict:
    verts = body.get("vertices")
    h = body.get("height_m")
    if not isinstance(verts, list) or not isinstance(h, (int, float)) or not 1.0 <= h <= 10.0:
        raise ValueError("a layout needs vertices and a height between 1 and 10 m")
    clean = [[round(float(x), 3), round(float(y), 3)] for x, y in verts]
    openings = []
    for o in body.get("openings") or []:
        if o.get("kind") not in ("door", "window"):
            raise ValueError("an opening is a door or a window")
        openings.append({"kind": o["kind"], "at": [round(float(o["at"][0]), 3), round(float(o["at"][1]), 3)]})
    if not all(math.isfinite(v) and abs(v) < 500 for p in clean + [o["at"] for o in openings] for v in p):
        raise ValueError("a point is out of range")
    gaps = body.get("open")
    gaps = [bool(x) for x in gaps] if isinstance(gaps, list) and len(gaps) == len(clean) else [False] * len(clean)
    return {"vertices": clean, "openings": openings, "open": gaps, "height_m": round(float(h), 3), "turn_deg": float(body.get("turn_deg") or 0), "edited": True}


def save_points(d: Path, P: np.ndarray, centre: np.ndarray, floor_m: float | None, ceiling_m: float | None) -> None:
    """The readings seen from above for the plan: `top` the band just under the ceiling (mostly walls), `all` anything lower, above the floor (furniture)."""
    rel = P - centre
    with np.errstate(invalid="ignore"):
        ok = np.isfinite(rel[..., 0]) & (np.linalg.norm(rel, axis=-1) < 12)
        if floor_m:
            ok &= rel[..., 1] > -floor_m + 0.1
        c = ceiling_m or 1.0
        top = ok & (rel[..., 1] > c - 0.65) & (rel[..., 1] < c - 0.08)      # the band room_outline reads: walls, not the ceiling
        ok &= rel[..., 1] < c - 0.08
    def cells(mask: np.ndarray) -> list[list[float]]:
        q = P[mask][:, [0, 2]] * [1, -1]
        return [[round(float(a), 3), round(float(b), 3)] for a, b in (np.unique(np.floor(q / CELL), axis=0) + 0.5) * CELL]
    (d / "layout_points.json").write_text(json.dumps({"top": cells(top), "all": cells(ok & ~top)}))


def points(d: Path, turn_deg: float = 0.0) -> dict:
    f = d / "layout_points.json"
    if not f.exists():
        return {"top": [], "all": []}
    return {k: [_rotate(p, turn_deg) for p in v] for k, v in json.loads(f.read_text()).items()}


def overlay(d: Path, turn_deg: float, floor_y: float, cut_m: float, fit: dict | None = None, dy: float = 0.0) -> tuple[Path, dict] | None:
    """The scan seen from above in its real colours, everything higher than `cut_m` over the floor taken off. Returns the PNG and its extent in plan
    metres. Built when first asked for."""
    tag = f"{round(cut_m * 100)}"
    if fit:           # this scan as it lies in another's plan
        tag += f"_{fit['deg']:.2f}_{fit['tx']:.3f}_{fit['ty']:.3f}_{dy:.3f}_{turn_deg:.2f}".replace("-", "m")
    src, png, meta = d / "colored.ply", d / f"layout_overlay_{tag}.png", d / f"layout_overlay_{tag}.json"
    if not src.exists():
        return None
    if png.exists() and meta.exists() and png.stat().st_mtime >= src.stat().st_mtime:
        m = json.loads(meta.read_text())
        if m["turn_deg"] == turn_deg:
            return png, m
    mesh = trimesh.load(src, process=False)
    V, F = np.asarray(mesh.vertices, float), np.asarray(mesh.faces)
    C = np.asarray(mesh.visual.vertex_colors)[:, :3]
    if fit:
        M = np.array(join_mod.matrix(fit, dy)).reshape(4, 4, order="F")
        V = V @ M[:3, :3].T + M[:3, 3]
    cut = floor_y + cut_m
    c, s = math.cos(math.radians(turn_deg)), math.sin(math.radians(turn_deg))
    x, y = V[:, 0] * c + V[:, 2] * s, V[:, 0] * s - V[:, 2] * c           # world to plan: (x, -z) turned by turn_deg
    tri_x, tri_y, tri_h = x[F], y[F], V[:, 1][F]
    edge = np.maximum.reduce([np.hypot(tri_x[:, i] - tri_x[:, (i + 1) % 3], tri_y[:, i] - tri_y[:, (i + 1) % 3]) for i in range(3)])
    keep = (tri_h.max(axis=1) < cut) & (edge < OVERLAY_MAX_EDGE)         # no ceiling, and no skins stretched from something near to the wall behind it
    F, tri_x, tri_y, tri_h = F[keep], tri_x[keep], tri_y[keep], tri_h[keep]
    if not len(F):
        return None
    x0, x1, y0, y1 = float(tri_x.min()), float(tri_x.max()), float(tri_y.min()), float(tri_y.max())
    w, h = int((x1 - x0) / OVERLAY_CELL) + 2, int((y1 - y0) / OVERLAY_CELL) + 2
    img = np.zeros((h, w, 4), np.uint8)
    px = np.stack([(tri_x - x0) / OVERLAY_CELL, (y1 - tri_y) / OVERLAY_CELL], axis=-1).round().astype(np.int32)
    col = C[F].mean(axis=1).astype(np.uint8)
    for i in np.argsort(tri_h.mean(axis=1)):                              # lowest first: what is higher covers it, as seen from above
        r, g, b = col[i]
        cv2.fillConvexPoly(img, px[i], (int(b), int(g), int(r), 255))
    cv2.imwrite(str(png), img)
    m = {"x0": round(x0, 3), "y0": round(y1 - h * OVERLAY_CELL, 3), "x1": round(x0 + w * OVERLAY_CELL, 3), "y1": round(y1, 3), "turn_deg": turn_deg}
    meta.write_text(json.dumps(m))
    return png, m


def from_walk(d: Path, body: dict) -> dict:
    """A plan walked on the phone: its corners in the phone's world as [x, z], the floor and ceiling heights, and a flat list of what the LiDAR saw [x, y, z, ...]."""
    verts, gaps, fy, cy = body.get("corners"), body.get("open"), body.get("floor_y"), body.get("ceiling_y")
    if not isinstance(verts, list) or len(verts) < 3 or not isinstance(fy, (int, float)) or not isinstance(cy, (int, float)) or not 1.0 <= cy - fy <= 10.0:
        raise ValueError("a plan needs at least 3 corners, a floor and a ceiling")
    plan = [[float(x), -float(z)] for x, z in verts]
    walls = [{"a": [a[0], -a[1]], "b": [b[0], -b[1]]} for a, b in zip(plan, plan[1:] + plan[:1])]
    turn = _turn(walls)
    lay = {"vertices": [_rotate(p, turn) for p in plan], "openings": [], "open": [bool(g) for g in gaps] if isinstance(gaps, list) and len(gaps) == len(plan) else [False] * len(plan),
           "height_m": round(float(cy - fy), 3), "turn_deg": round(turn, 3), "floor_y": float(fy), "ceiling_y": float(cy), "edited": True}
    cloud = np.array(body.get("cloud") or [], float)
    cloud = cloud[: len(cloud) // 3 * 3].reshape(-1, 3)
    cloud = cloud[np.isfinite(cloud).all(axis=1)]
    def cells(mask: np.ndarray) -> list[list[float]]:
        q = cloud[mask][:, [0, 2]] * [1, -1]
        return [[round(float(a), 3), round(float(b), 3)] for a, b in (np.unique(np.floor(q / CELL), axis=0) + 0.5) * CELL] if mask.any() else []
    y = cloud[:, 1]
    top = (y > cy - 0.65) & (y < cy - 0.08)
    low = (y > fy + 0.1) & (y <= cy - 0.65)
    (d / "layout_points.json").write_text(json.dumps({"top": cells(top), "all": cells(low)}))
    (d / "layout.json").write_text(json.dumps(lay))
    return lay


def _floor_of(d: Path, lay: dict) -> float | None:
    room = d / "room.json"
    if room.exists():
        r = json.loads(room.read_text())
        return r["centre"][1] - r["floor"]["wall_m"] if r["floor"].get("wall_m") is not None else None
    return lay.get("floor_y")


def joins(d: Path) -> list[dict]:
    f = d / "joined.json"
    return json.loads(f.read_text()) if f.exists() else []


def _save_joins(d: Path, js: list[dict]) -> None:
    (d / "joined.json").write_text(json.dumps(js))


def seen_cells(d: Path, lay: dict) -> dict:
    """The readings of this scan and of every scan joined to it, in the drawing's plan."""
    turn = lay.get("turn_deg", 0.0)
    out = points(d, turn)
    for j in joins(d):
        raw = points(d.parent / j["scan"])
        f = j["fits"][j["pick"]]
        for k in ("top", "all"):
            if raw[k]:
                out[k] = out[k] + [_rotate(list(p), turn) for p in join_mod.plan_point(raw[k], f).tolist()]
    return out


def _merged(lay: dict, other: list[list[float]]) -> list[list[float]] | None:
    """The outline round both: where two scans overlap, the walls between them go."""
    a, b = Polygon(lay["vertices"]).buffer(0), Polygon(other).buffer(0)
    if not a.intersects(b):
        return None
    u = a.union(b)
    if u.geom_type != "Polygon":
        u = max(u.geoms, key=lambda g: g.area)
    # two scans of one wall never agree to the centimetre: without this their outlines weave round each other in tiny steps
    return [[round(x, 3), round(y, 3)] for x, y in list(u.simplify(MERGE_TIDY).exterior.coords)[:-1]]


def add_join(d: Path, src: Path) -> dict:
    """Fit the sweep `src` onto the scan in `d`. On a sweep the two outlines become one; a plan someone walked stays as drawn. Returns the join."""
    a, b = points(src), points(d)
    src_cells, dst_cells = np.array(a["top"], float).reshape(-1, 2), np.array(b["top"], float).reshape(-1, 2)
    if len(src_cells) < 100 or len(dst_cells) < 100:
        raise ValueError("One of the two has too few wall readings to fit. Press Re-convert on the sweep once, then try again.")
    lay, other = load(d), load(src)
    fits = join_mod.fit(src_cells, dst_cells)
    fy, oy = _floor_of(d, lay), _floor_of(src, other)
    entry = {"scan": src.name, "fits": fits, "pick": 0, "sure": join_mod.sure(fits), "dy": round(fy - oy, 4) if fy is not None and oy is not None else 0.0}
    if "floor_y" not in lay and lay.get("edited") is not None and (d / "room.json").exists():
        entry["before"] = lay
        _grow(d, lay, other, fits[0], entry)
    js = [j for j in joins(d) if j["scan"] != src.name] + [entry]
    _save_joins(d, js)
    return entry


def _grow(d: Path, lay: dict, other: dict, f: dict, entry: dict) -> None:
    turn = lay.get("turn_deg", 0.0)
    theirs = [_rotate(p, -other.get("turn_deg", 0.0)) for p in other["vertices"]]
    mine = [_rotate(list(p), turn) for p in join_mod.plan_point(theirs, f).tolist()]
    v = _merged(lay, mine)
    if not v:
        entry["apart"] = True
        return
    grown = {**lay, "vertices": v, "open": []}
    (d / "layout.json").write_text(json.dumps(grown))
    entry["merged"] = True


def _area(v: list[list[float]]) -> float:
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(v, v[1:] + v[:1])) / 2


def _crosses(v: list[list[float]]) -> bool:
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    n = len(v)
    for i in range(n):
        a, b = v[i], v[(i + 1) % n]
        for j in range(i + 2, n):
            if (j + 1) % n == i:
                continue
            c, e = v[j], v[(j + 1) % n]
            if side(a, b, c) * side(a, b, e) < 0 and side(c, e, a) * side(c, e, b) < 0:
                return True
    return False


def _inside_point(v: list[list[float]]) -> list[float]:
    """A point inside the outline, for the room's name: the middle of the longest stretch of a line across it."""
    ys = sorted(p[1] for p in v)
    y = (ys[0] + ys[-1]) / 2 + 0.013
    xs = sorted(a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]) for a, b in zip(v, v[1:] + v[:1]) if (a[1] - y) * (b[1] - y) < 0)
    spans = [(xs[i + 1] - xs[i], (xs[i + 1] + xs[i]) / 2) for i in range(0, len(xs) - 1, 2)]
    return [max(spans)[1], y] if spans else [sum(p[0] for p in v) / len(v), y]


def problem(layout: dict) -> str | None:
    v = layout["vertices"]
    if len(v) < 3:
        return "A room needs at least 3 corners."
    if _crosses(v):
        return "Two walls cross each other. Drag or delete a corner so they don't."
    if all(layout.get("open") or [False]):
        return "Every stretch is marked open, so there are no walls to build."
    return None


def _on_wall(at: list[float], walls: list[tuple], half: float) -> list[float]:
    """The spot on the nearest wall's centre line: the corners are the room's inside faces, and clockwise walls have their outside to the left."""
    best = None
    for a, b in walls:
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy or 1e-9
        t = max(0.0, min(1.0, ((at[0] - a[0]) * dx + (at[1] - a[1]) * dy) / L2))
        q = [a[0] + t * dx, a[1] + t * dy]
        dist = math.dist(at, q)
        if best is None or dist < best[0]:
            L = math.sqrt(L2)
            best = (dist, [q[0] - dy / L * half, q[1] + dx / L * half])
    return best[1]


def _chains(v: list[list[float]], gaps: list[bool]) -> list[list[list[float]]]:
    """The runs of wall between the open stretches, each as its list of points. One run with its first point repeated if nothing is open."""
    n = len(v)
    if not any(gaps):
        return [v + v[:1]]
    first = next(i for i in range(n) if gaps[i])         # start just after an open stretch
    runs, run = [], []
    for k in range(1, n + 1):
        i = (first + k) % n
        if gaps[i]:
            if run:
                runs.append(run)
            run = []
        else:
            run = run or [v[i]]
            run.append(v[(i + 1) % n])
    return runs


def script(layout: dict, name: str = "Room") -> str:
    v = layout["vertices"]
    gaps = layout.get("open") or [False] * len(v)
    if _area(v) > 0:           # counter-clockwise: BIMCraft walls face out when drawn clockwise. Edge i runs from v[i] to v[i + 1]
        n = len(v)
        gaps = [gaps[(n - 2 - j) % n] for j in range(n)]
        v = v[::-1]
    to_mm = lambda pts: [[round(x * 1000), round(y * 1000)] for x, y in pts]
    h = round(layout["height_m"] * 1000)
    mm = to_mm(v)
    lines = []
    if any(gaps):
        for run in _chains(v, gaps):
            lines.append(f"wall {json.dumps({'points': to_mm(run), 'type': WALL_TYPE, 'location': 'interior-finish', 'height': h})}")
    else:
        lines.append(f"wall {json.dumps({'points': mm, 'closed': True, 'type': WALL_TYPE, 'location': 'interior-finish', 'height': h})}")
    lines += [f"floor {json.dumps({'boundary': mm})}", f"ceiling {json.dumps({'boundary': mm, 'offset': h})}"]
    if not any(gaps):          # BIMCraft only makes a room inside a closed loop of walls
        inside = _inside_point(v)
        lines.append(f"room {json.dumps({'point': [round(inside[0] * 1000), round(inside[1] * 1000)], 'name': name})}")
    solid = [(a, b) for (a, b), g in zip(zip(v, v[1:] + v[:1]), gaps) if not g]
    for o in layout.get("openings") or []:
        x, y = _on_wall(o["at"], solid, WALL_CENTRE)
        lines.append(f"{o['kind']} {json.dumps({'at': [round(x * 1000), round(y * 1000)]})}")
    return "\n".join(lines) + "\n"


def blender_model(layout: dict) -> trimesh.Trimesh:
    """Walls and a floor in metres, Y up, for glTF: Blender turns it to its own Z up on import. Doors and windows are not cut out."""
    v, h = layout["vertices"], layout["height_m"]
    gaps = layout.get("open") or [False] * len(v)
    parts = []
    for (a, b), gap in zip(zip(v, v[1:] + v[:1]), gaps):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1e-3 or gap:
            continue
        wall = trimesh.creation.box((length, h, WALL_THICK))
        M = trimesh.transformations.rotation_matrix(math.atan2(dy, dx), [0, 1, 0])
        M[:3, 3] = [(a[0] + b[0]) / 2, h / 2, -(a[1] + b[1]) / 2]
        wall.apply_transform(M)
        parts.append(wall)
    floor = trimesh.creation.extrude_polygon(Polygon(v), 0.02)      # plan x, y and up as z
    floor.apply_transform(trimesh.transformations.rotation_matrix(-math.pi / 2, [1, 0, 0]))     # z up to y up: plan y becomes -z
    floor.apply_translation([0, -0.02, 0])
    parts.append(floor)
    return trimesh.util.concatenate(parts)


def export(d: Path, layout: dict, name: str = "Room") -> tuple[bool, str]:
    """room.bimcraft, room.ifc, plan.png and layout.glb from the layout."""
    bad = problem(layout)
    if bad:
        return False, bad
    f = d / "layout_script.txt"
    f.write_text(script(layout, name))
    ok, log = convert.run_cli(f, d)
    if not ok:
        return False, log[-800:]
    blender_model(layout).export(d / "layout.glb")
    return True, log[-400:]
