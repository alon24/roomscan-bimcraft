"""A room's layout: its walls as a plan you can correct, no pictures, and what turns it into BIMCraft and Blender files.

Starts from the outline a sweep found (sweep.room_outline). Plan coordinates are metres, the same as BIMCraft's axes, turned by `turn_deg` so the longest wall lies along an axis. The walls run clockwise round the room, so in BIMCraft they face out."""
import json
import math
from pathlib import Path

import cv2
import numpy as np
import trimesh
from shapely.geometry import Polygon

from . import convert

WALL_THICK = 0.1           # metres, for the Blender model
WALL_TYPE = "Generic - 200mm"
WALL_CENTRE = 0.1          # metres from the room's face to the middle of that wall
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


def load(d: Path) -> dict:
    f = d / "layout.json"
    if f.exists():
        return json.loads(f.read_text())
    room = d / "room.json"
    return default(json.loads(room.read_text())) if room.exists() else {"vertices": [], "openings": [], "height_m": 2.5, "turn_deg": 0.0, "edited": False}


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
    return {"vertices": clean, "openings": openings, "height_m": round(float(h), 3), "turn_deg": float(body.get("turn_deg") or 0), "edited": True}


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


def overlay(d: Path, turn_deg: float, floor_y: float, cut_m: float) -> tuple[Path, dict] | None:
    """The scan seen from above in its real colours, everything higher than `cut_m` over the floor taken off. Returns the PNG and its extent in plan
    metres. Built when first asked for."""
    tag = f"{round(cut_m * 100)}"
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
    return None


def _on_wall(at: list[float], v: list[list[float]], half: float) -> list[float]:
    """The spot on the nearest wall's centre line: the corners are the room's inside faces, and clockwise walls have their outside to the left."""
    best = None
    for a, b in zip(v, v[1:] + v[:1]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy or 1e-9
        t = max(0.0, min(1.0, ((at[0] - a[0]) * dx + (at[1] - a[1]) * dy) / L2))
        q = [a[0] + t * dx, a[1] + t * dy]
        dist = math.dist(at, q)
        if best is None or dist < best[0]:
            L = math.sqrt(L2)
            best = (dist, [q[0] - dy / L * half, q[1] + dx / L * half])
    return best[1]


def script(layout: dict, name: str = "Room") -> str:
    v = layout["vertices"]
    if _area(v) > 0:           # counter-clockwise: BIMCraft walls face out when drawn clockwise
        v = v[::-1]
    mm = [[round(x * 1000), round(y * 1000)] for x, y in v]
    h = round(layout["height_m"] * 1000)
    inside = _inside_point(v)
    lines = [f"wall {json.dumps({'points': mm, 'closed': True, 'type': WALL_TYPE, 'location': 'interior-finish', 'height': h})}",
             f"floor {json.dumps({'boundary': mm})}",
             f"ceiling {json.dumps({'boundary': mm, 'offset': h})}",
             f"room {json.dumps({'point': [round(inside[0] * 1000), round(inside[1] * 1000)], 'name': name})}"]
    for o in layout.get("openings") or []:
        x, y = _on_wall(o["at"], v, WALL_CENTRE)
        lines.append(f"{o['kind']} {json.dumps({'at': [round(x * 1000), round(y * 1000)]})}")
    return "\n".join(lines) + "\n"


def blender_model(layout: dict) -> trimesh.Trimesh:
    """Walls and a floor in metres, Y up, for glTF: Blender turns it to its own Z up on import. Doors and windows are not cut out."""
    v, h = layout["vertices"], layout["height_m"]
    parts = []
    for a, b in zip(v, v[1:] + v[:1]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1e-3:
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
