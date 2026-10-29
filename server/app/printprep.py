"""Get a scanned model ready to print: cut it into parts, draw a cross-section, suggest filament colours.
All sizes are mm in model.stl's own frame (Z up, centred on the bed, sitting on Z=0)."""
import io
import json
import math
import os
import zipfile
from pathlib import Path

import numpy as np
import trimesh

AXES = {"x": 0, "y": 1, "z": 2}
# Every connector is two boolean operations, and a messy cut face can have hundreds of crumbs: only the biggest islands get
# connectors, and a cut face and the whole job have a ceiling, so a cut always ends in seconds.
MAX_ISLANDS = 8            # islands of one cut face that get connectors (largest first)
MAX_FACE_CONNECTORS = 16   # connectors on one cut face
MAX_CONNECTORS = 48        # connectors in one cut job
BED_MM = [float(v) for v in os.environ.get("BED_MM", "220,220,250").split(",")]

# Common filament colours: name -> sRGB. Scan colours are matched to the nearest one.
FILAMENTS = {
    "White": (240, 240, 240), "Light grey": (180, 180, 180), "Grey": (120, 120, 120), "Black": (30, 30, 30),
    "Red": (200, 40, 40), "Orange": (235, 120, 30), "Yellow": (240, 210, 50), "Beige": (215, 190, 150),
    "Brown": (120, 80, 50), "Light green": (140, 200, 90), "Green": (40, 140, 70), "Cyan": (60, 190, 210),
    "Blue": (40, 90, 190), "Navy": (25, 40, 100), "Purple": (110, 60, 160), "Pink": (235, 130, 170),
}


def load(model: Path) -> trimesh.Trimesh:
    return trimesh.load(model, force="mesh")


def suggest_cut(size_mm: list[float], bed: list[float] = BED_MM) -> dict | None:
    """None if the part fits the bed; else the axis it overflows most and the middle of that axis."""
    over = [s / b for s, b in zip(size_mm, bed)]
    if max(over) <= 1:
        return None
    i = int(np.argmax(over))
    return {"axis": "xyz"[i], "at": round(size_mm[i] / 2, 1), "bed_mm": bed}


def _bounds(mesh: trimesh.Trimesh, axis: str) -> tuple[float, float]:
    lo, hi = mesh.bounds[:, AXES[axis]]
    return float(lo), float(hi)


CONNECTORS = ("none", "dowel", "peg", "key")
SHAPES = ("circle", "square", "triangle", "hexagon")
WALL_MM = 2.0   # material kept between a connector and the outside of the part
GAP_MM = 10.0   # space between parts laid out for the bed / in the 3MF
EPS_MM = 0.01


class Piece:
    """One part while cutting: its mesh and the cut faces it has (outward normal, a point on the face, has pegs)."""
    def __init__(self, mesh, faces=()):
        self.mesh, self.faces = mesh, list(faces)
        self.flip = False


def _axis_vec(i: int, sign: float = 1.0) -> np.ndarray:
    v = np.zeros(3); v[i] = sign
    return v


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n < 1e-9:
        raise ValueError("the cut plane has no direction")
    return v / n


def plane_basis(origin, normal) -> np.ndarray:
    """4x4 matrix taking plane coordinates (u, v, w) to model mm: u and v lie in the plane, w is its normal.
    It depends only on (origin, normal), so the browser and the server always agree where a given (u, v) is."""
    n = _unit(normal)
    ref = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = _unit(np.cross(ref, n))
    v = np.cross(n, u)
    b = np.eye(4)
    b[:3, 0], b[:3, 1], b[:3, 2], b[:3, 3] = u, v, n, np.asarray(origin, dtype=float)
    return b


def axis_plane(mesh: trimesh.Trimesh, axis: str, at: float) -> tuple[np.ndarray, np.ndarray]:
    """The plane `axis = at` mm from the low end of the part, as (origin, normal)."""
    i = AXES[axis]
    lo, _ = _bounds(mesh, axis)
    o = mesh.bounds.mean(axis=0).copy()
    o[i] = lo + at
    return o, _axis_vec(i)


def _crosses(mesh: trimesh.Trimesh, origin, normal) -> bool:
    d = (mesh.vertices - origin) @ normal
    return bool(d.min() < -EPS_MM and d.max() > EPS_MM)


def _split_plane(mesh: trimesh.Trimesh, origin, normal):
    """(low, high): the parts on the -normal and +normal side, each capped and closed."""
    low = mesh.slice_plane(origin, -normal, cap=True)
    high = mesh.slice_plane(origin, normal, cap=True)
    for half in (low, high):
        if len(half.faces) == 0:
            raise ValueError("one side of the cut is empty")
        if not half.is_watertight:
            trimesh.repair.fill_holes(half)
    return low, high


def section_polygons(mesh: trimesh.Trimesh, origin, normal):
    """The cut face as shapely polygons in the plane's own (u, v) mm, plus the plane basis. ([], basis) if the plane misses."""
    o, n = np.asarray(origin, dtype=float), _unit(normal)
    basis = plane_basis(o, n)
    sec = mesh.section(plane_origin=o, plane_normal=n)
    if sec is None:
        return [], basis
    planar, _ = sec.to_2D(to_2D=np.linalg.inv(basis))
    return list(planar.polygons_full), basis


def inscribed_radius(poly) -> float:
    from shapely.ops import polylabel
    return float(poly.boundary.distance(polylabel(poly, tolerance=0.25)))


def auto_points(polys, size: float, count: int, log: list[str] | None = None) -> list[tuple[float, float]]:
    """Where connectors go on this cut face: spread out, each with WALL_MM of material round it. Only the MAX_ISLANDS biggest
    islands that fit one, and at most MAX_FACE_CONNECTORS in all (said in `log` when capped)."""
    from shapely.geometry import Point
    from shapely.ops import polylabel
    pts: list[tuple[float, float]] = []
    fitting = 0
    for poly in sorted(polys, key=lambda p: -p.area):
        if len(pts) >= MAX_FACE_CONNECTORS:
            break
        inset = poly.buffer(-(size / 2 + WALL_MM))
        if inset.is_empty:
            continue
        fitting += 1
        if fitting > MAX_ISLANDS:
            break
        if inset.geom_type == "MultiPolygon":
            inset = max(inset.geoms, key=lambda g: g.area)
        seed = polylabel(inset, tolerance=0.25)
        chosen = [(seed.x, seed.y)]
        want = min(count, MAX_FACE_CONNECTORS - len(pts))
        if want > 1:
            x0, y0, x1, y1 = inset.bounds
            step = max(0.5, max(x1 - x0, y1 - y0) / 40)
            grid = [(x, y) for x in np.arange(x0, x1 + step, step) for y in np.arange(y0, y1 + step, step)
                    if inset.contains(Point(x, y))]
            while len(chosen) < want and grid:   # farthest from the ones already chosen
                far = max(grid, key=lambda g: min(np.hypot(g[0] - c[0], g[1] - c[1]) for c in chosen))
                if min(np.hypot(far[0] - c[0], far[1] - c[1]) for c in chosen) < size * 2.5:
                    break
                chosen.append(far)
        pts += chosen[:want]
    if log is not None and (fitting > MAX_ISLANDS or len(pts) >= MAX_FACE_CONNECTORS):
        log.append(f"the cut face has {len(polys)} separate pieces: connectors only on the biggest "
                   f"(at most {MAX_ISLANDS} pieces, {MAX_FACE_CONNECTORS} connectors), to keep the cut quick")
    return [(float(x), float(y)) for x, y in pts]


def usable_points(polys, wanted, size: float) -> list[tuple[float, float]]:
    """The wanted (u, v) spots that sit on this cut face with WALL_MM of material round the connector."""
    from shapely.geometry import Point
    insets = [p.buffer(-(size / 2 + WALL_MM)) for p in polys]
    return [(float(u), float(v)) for u, v in wanted if any(not g.is_empty and g.contains(Point(u, v)) for g in insets)]


def _shape_mesh(shape: str, size: float, height: float) -> trimesh.Trimesh:
    """A bar `height` long along z. size = width (round, square) or the circle through the corners (triangle, hexagon)."""
    if shape == "square":
        return trimesh.creation.box(extents=(size, size, height))
    sections = {"circle": 48, "triangle": 3, "hexagon": 6}[shape]
    return trimesh.creation.cylinder(radius=size / 2, height=height, sections=sections)


def _grown(shape: str, size: float, clearance: float) -> float:
    """`size` made bigger so that every flat side has `clearance` of play (corner-to-corner for triangle and hexagon)."""
    sides = {"triangle": 3, "hexagon": 6}.get(shape)
    return size + 2 * clearance / (math.cos(math.pi / sides) if sides else 1.0)


def _prism(shape: str, size: float, z0: float, z1: float, to3d, centre) -> trimesh.Trimesh:
    """A bar standing on the cut plane at `centre`, from z0 to z1 along the plane's own z (its normal)."""
    m = _shape_mesh(shape, size, z1 - z0)
    m.apply_translation([0, 0, (z0 + z1) / 2])
    rot = np.array(to3d, dtype=float).copy()
    rot[:3, 3] = centre
    m.apply_transform(rot)
    return m


def _boolean(op: str, a: trimesh.Trimesh, b: trimesh.Trimesh) -> trimesh.Trimesh | None:
    try:
        out = getattr(trimesh.boolean, op)([a, b], engine="manifold")
    except Exception:
        return None
    return out if out is not None and len(out.faces) and out.is_watertight else None


def _join(low: trimesh.Trimesh, high: trimesh.Trimesh, basis, kind: str, shape: str, size: float, depth: float,
          clearance: float, pts, log: list[str], pins: list, label: str):
    """Add connectors at the plane (u, v) spots `pts` between `low` and `high`. Returns (low, high, pegged)."""
    if not pts:
        log.append(f"cut face too small for a {size:g} mm connector: joined with nothing")
        return low, high, False
    if not (low.is_watertight and high.is_watertight):
        log.append("a part is not watertight, so no connectors were added")
        return low, high, False
    grown = _grown(shape, size, clearance)
    done = 0
    for u, v in pts:
        c = (np.asarray(basis) @ [u, v, 0, 1])[:3]
        if kind == "dowel":   # a hole in each half and a loose pin between them
            l2 = _boolean("difference", low, _prism(shape, grown, -depth, 0.5, basis, c))
            h2 = _boolean("difference", high, _prism(shape, grown, -0.5, depth, basis, c))
            if l2 is None or h2 is None:
                log.append("a dowel hole could not be cut")
                continue
            low, high = l2, h2
            pins.append((size, 2 * depth - 1.0, shape))
        else:                 # peg or key standing from the low half into a socket in the high half
            peg = _prism(shape, size, -1.0, depth, basis, c)
            sock = _prism(shape, grown, -0.5, depth + 0.5, basis, c)
            l2, h2 = _boolean("union", low, peg), _boolean("difference", high, sock)
            if l2 is None or h2 is None:
                log.append("a peg could not be added")
                continue
            low, high = l2, h2
        done += 1
    if done:
        noun = ("dowel hole pairs" if kind == "dowel" else "square keys" if kind == "key" and shape == "square"
                else "round pegs" if shape == "circle" else f"{shape} pegs")
        log.append(f"{done} {noun} ({size:g} mm, {depth:g} mm deep) at {label}")
    return low, high, done > 0 and kind in ("peg", "key")


def _face_area(mesh: trimesh.Trimesh, normal, origin) -> float:
    """Area of the triangles lying on the cut face (facing `normal`, within 0.5 mm of the plane through `origin`)."""
    facing = mesh.face_normals @ normal > 0.99
    near = np.abs((mesh.triangles_center - origin) @ normal) < 0.5
    return float(mesh.area_faces[facing & near].sum())


def _lay_down(piece: Piece, log: list[str], number: int):
    """Rotate a part so its biggest flat cut face (one without pegs) lies on the bed (face up if the part is flipped).
    A part whose cut faces all carry pegs stands pegs up instead, and may need supports."""
    m = piece.mesh
    down, up = np.array([0.0, 0.0, -1.0]), np.array([0.0, 0.0, 1.0])
    flat = [f for f in piece.faces if not f["pegged"]]
    if flat:
        f = max(flat, key=lambda f: _face_area(m, f["normal"], f["origin"]))
        m.apply_transform(trimesh.geometry.align_vectors(f["normal"], up if piece.flip else down))
        if piece.flip:
            log.append(f"part {number} is turned over: its cut face is up")
    elif piece.faces:
        f = piece.faces[0]
        m.apply_transform(trimesh.geometry.align_vectors(f["normal"], down if piece.flip else up))
        log.append(f"part {number} prints with its pegs {'down' if piece.flip else 'up'}: it may need supports (dowel holes lie flat instead)")
    lo, hi = m.bounds
    m.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])


def cut_planes(mesh: trimesh.Trimesh, planes: list[dict], kind: str = "none", shape: str | None = None, size: float = 6.0,
               depth: float = 8.0, clearance: float = 0.3, count: int = 2, keep_upper: bool = True, keep_lower: bool = True,
               flip_upper: bool = False, flip_lower: bool = False):
    """Cut the part at every plane and join each cut with connectors of `kind`.
    A plane is {origin, normal, points (optional [(u, v)] spots on the face; automatic when None), label (optional)}.
    'Upper' is the side the normal points to. keep/flip work with a single plane (Orca's cut dialog).
    Returns (parts laid flat, pins as (size, length, shape), log, views) where views is [(side, mesh in place)]."""
    if kind not in CONNECTORS:
        raise ValueError(f"connector must be one of {', '.join(CONNECTORS)}")
    shape = shape or ("square" if kind == "key" else "circle")
    if shape not in SHAPES:
        raise ValueError(f"shape must be one of {', '.join(SHAPES)}")
    if not planes:
        raise ValueError("no cuts given")
    single = len(planes) == 1
    if not single and (not (keep_upper and keep_lower) or flip_upper or flip_lower):
        raise ValueError("keep, discard and flip work with one cut plane")
    if not (keep_upper or keep_lower):
        raise ValueError("both parts are discarded")
    log: list[str] = []
    pins: list = []
    if kind != "none" and not (keep_upper and keep_lower):
        kind = "none"
        log.append("a part is discarded, so there is nothing to join")
    mesh = mesh.copy()
    if kind != "none" and not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
        if not mesh.is_watertight:
            raise ValueError("the model has holes, so connectors cannot be added: cut with 'none' or rebuild the model")
    pieces = [Piece(mesh)]
    placed = 0
    for pl in planes:
        o, n = np.asarray(pl["origin"], dtype=float), _unit(pl["normal"])
        label = pl.get("label") or "the cut plane"
        nxt, crossed = [], 0
        for pc in pieces:
            if not _crosses(pc.mesh, o, n):
                nxt.append(pc)
                continue
            crossed += 1
            low, high = _split_plane(pc.mesh, o, n)
            pegged = False
            if kind != "none":
                polys, basis = section_polygons(pc.mesh, o, n)
                if pl.get("points") is None:
                    pts = auto_points(polys, size, count, log)
                else:
                    pts = usable_points(polys, pl["points"], size)
                    if len(pts) < len(pl["points"]):
                        log.append(f"{len(pl['points']) - len(pts)} connector spot(s) too close to the edge of the cut face: skipped")
                    if not pts and pl["points"]:
                        raise ValueError(f"a {size:g} mm connector does not fit at the chosen spots (keep {size / 2 + WALL_MM:g} mm from the edge)")
                left = MAX_CONNECTORS - placed
                if pts and len(pts) > left:
                    log.append(f"{len(pts) - left} connector(s) at {label} left out: one cut job has at most {MAX_CONNECTORS}")
                    pts = pts[:left]
                    capped = not pts
                else:
                    capped = False
                placed += len(pts)
                if not capped:
                    low, high, pegged = _join(low, high, basis, kind, shape, size, depth, clearance, pts, log, pins, label)
            nxt.append(Piece(low, pc.faces + [dict(normal=n, origin=o, pegged=pegged)]))
            nxt.append(Piece(high, pc.faces + [dict(normal=-n, origin=o, pegged=False)]))
        if not crossed:
            raise ValueError(f"{label} misses the part")
        pieces = nxt
    sides = ["lower", "upper"] if single else ["part"] * len(pieces)
    if single:
        pieces[0].flip, pieces[1].flip = flip_lower, flip_upper
        chosen = [(s, pc) for s, pc, k in zip(sides, pieces, (keep_lower, keep_upper)) if k]
    else:
        chosen = list(zip(sides, pieces))
    views = [(s, pc.mesh.copy()) for s, pc in chosen]
    for n, (_, pc) in enumerate(chosen, 1):
        _lay_down(pc, log, n)
        log.append(f"part {n}: {'watertight' if pc.mesh.is_watertight else 'NOT watertight'}, "
                   + " × ".join(f"{x:.0f}" for x in pc.mesh.extents) + " mm")
    return [pc.mesh for _, pc in chosen], pins, log, views


def cut_all(mesh: trimesh.Trimesh, cuts: list[tuple[str, float]], kind: str = "none", size: float = 6.0,
            depth: float = 8.0, clearance: float = 0.3, count: int = 2):
    """Cut the part at every axis plane (`axis`, mm from the low end of the whole part), joining each cut
    with connectors of `kind` (none, dowel, peg, key). Returns (parts laid flat, pins as (diameter, length, shape), log)."""
    if not cuts:
        raise ValueError("no cuts given")
    planes = []
    for axis, at in cuts:
        if axis not in AXES:
            raise ValueError("axis must be x, y or z")
        lo, hi = _bounds(mesh, axis)
        if not (lo < lo + at < hi):
            raise ValueError(f"cut at {at:g} mm is outside the part (0 to {hi - lo:.1f} mm along {axis})")
        o, n = axis_plane(mesh, axis, at)
        planes.append(dict(origin=o, normal=n, points=None, label=f"the cut {axis.upper()} = {at:g} mm"))
    parts, pins, log, _ = cut_planes(mesh, planes, kind, None, size, depth, clearance, count)
    return parts, pins, log


def cut_preview(mesh: trimesh.Trimesh, origin, normal, kind: str = "dowel", shape: str | None = None,
                size: float = 6.0, count: int = 2) -> dict:
    """What the cut face looks like at this plane, for the browser's connector editor: the outline(s) in the plane's
    own (u, v) mm, that plane's basis, and where connectors would go automatically."""
    o, n = np.asarray(origin, dtype=float), _unit(normal)
    if not _crosses(mesh, o, n):
        raise ValueError("the plane does not touch the part")
    polys, basis = section_polygons(mesh, o, n)
    if not polys:
        raise ValueError("the plane does not touch the part")
    pts = auto_points(polys, size, count) if kind != "none" else []
    best = max(2 * (inscribed_radius(p) - WALL_MM) for p in polys)
    ring = lambda r: [[round(float(x), 3), round(float(y), 3)] for x, y in r.coords]
    return {
        "origin": [round(float(x), 4) for x in o], "u": [round(float(x), 6) for x in basis[:3, 0]],
        "v": [round(float(x), 6) for x in basis[:3, 1]], "normal": [round(float(x), 6) for x in basis[:3, 2]],
        "polygons": [{"outer": ring(p.exterior), "holes": [ring(h) for h in p.interiors]} for p in polys],
        "points": [[round(x, 3), round(y, 3)] for x, y in pts],
        "area_mm2": round(float(sum(p.area for p in polys)), 1),
        "max_size_mm": round(max(best, 0.0), 1),
        "fits": kind == "none" or bool(pts),
    }


def pin_meshes(pins: list) -> list[trimesh.Trimesh]:
    """Loose dowel pins, lying on their side (printed along their length they are strongest)."""
    out = []
    for size, length, shape in pins:
        m = _shape_mesh(shape, size - 0.1, length)
        m.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
        lo, hi = m.bounds
        m.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
        out.append(m)
    return out


def lay_out(parts: list[trimesh.Trimesh], extra: list[trimesh.Trimesh] = ()) -> trimesh.Scene:
    """Every part side by side on the bed (left to right, GAP_MM apart): for the one 3MF that holds all cuts."""
    scene, x = trimesh.Scene(), 0.0
    for n, m in enumerate(list(parts) + list(extra), 1):
        m = m.copy()
        w = m.extents[0]
        m.apply_translation([x - m.bounds[0][0], 0, 0])
        scene.add_geometry(m, node_name=f"part{n}" if n <= len(parts) else f"pin{n - len(parts)}",
                           geom_name=f"part{n}" if n <= len(parts) else f"pin{n - len(parts)}")
        x += w + GAP_MM
    return scene


PLA_G_PER_MM3 = 0.00124      # PLA, grams per mm3
FLOW_MM3_S = 9.0             # a typical average extrusion rate over a whole print, mm3 per second


def auto_cuts(size_mm: list[float], bed: list[float] = BED_MM, margin: float = 4.0) -> list[tuple[str, float]]:
    """Equal cuts along every axis that is longer than the bed (minus a margin for connectors and the brim)."""
    cuts = []
    for axis, s, b in zip("xyz", size_mm, bed):
        n = math.ceil(s / max(b - margin, 1.0))
        cuts += [(axis, s * k / n) for k in range(1, n)]
    return cuts


def estimate(mesh: trimesh.Trimesh) -> dict:
    """A rough print estimate for a part lying on the bed: filament, time, and how much of the surface hangs over (supports).
    Walls of 1.2 mm and 15% infill; the time uses an average flow, so treat it as a guide within about 30%."""
    area, vol = float(mesh.area), abs(float(mesh.volume)) if mesh.is_watertight else 0.0
    shell = min(vol, area * 1.2) if vol else area * 1.2
    material = shell + 0.15 * max(vol - shell, 0.0)
    n = mesh.face_normals
    down = n[:, 2] < -0.707                                         # steeper than 45 degrees from vertical, facing down
    above_bed = mesh.triangles_center[:, 2] > 0.6                   # a face lying on the bed needs no support
    overhang = float(mesh.area_faces[down & above_bed].sum() / area) if area else 0.0
    return {"grams": round(material * PLA_G_PER_MM3, 1), "hours": round(material / FLOW_MM3_S / 3600 * 1.3, 1), "overhang": round(overhang, 3),
            "supports": "none" if overhang < 0.02 else "a little" if overhang < 0.12 else "yes",
            "size_mm": [round(float(x), 1) for x in mesh.extents]}


CLEARANCE_STEPS = (0.1, 0.2, 0.3, 0.4, 0.5)


def tolerance_test(size: float = 6.0, depth: float = 8.0, steps=CLEARANCE_STEPS) -> trimesh.Scene:
    """A small bar with one blind hole per clearance (hole = size + 2 * clearance) and two loose pins of the connector size, to find
    out which clearance fits on this printer. Hole n has n notches on the front edge: n = 1 is the tightest."""
    pitch = size + 10.0
    plate_h = depth + 3.0
    length, width = pitch * len(steps) + 6.0, size + 12.0
    bar = trimesh.creation.box(extents=(length, width, plate_h))
    bar.apply_translation([length / 2, 0, plate_h / 2])
    for n, c in enumerate(steps, 1):
        x = 3.0 + pitch * (n - 0.5)
        hole = trimesh.creation.cylinder(radius=size / 2 + c, height=depth + 1.0, sections=64)
        hole.apply_translation([x, 0, plate_h - depth / 2 + 0.5])
        cut = _boolean("difference", bar, hole)
        if cut is None:
            raise ValueError("could not cut a hole in the test bar")
        bar = cut
        for k in range(n):                                   # n notches on the front edge, 1.5 mm wide, 2 mm apart
            notch = trimesh.creation.box(extents=(1.5, 1.6, 1.5))
            notch.apply_translation([x + (k - (n - 1) / 2) * 3.0, -width / 2 + 0.6, plate_h - 0.7])
            cut = _boolean("difference", bar, notch)
            if cut is not None:
                bar = cut
    bar.apply_translation([-length / 2, 0, 0])
    return lay_out([bar], pin_meshes([(size, 2 * depth - 1.0, "circle")] * 2))


def section_svg(mesh: trimesh.Trimesh, axis: str, at_mm: float) -> str:
    """Outline of the cross-section at `axis = at_mm` as an SVG (mm), the cut face seen from the high side."""
    polys, _ = section_polygons(mesh, *axis_plane(mesh, axis, at_mm))
    if not polys:
        raise ValueError("the plane does not touch the part")
    b = np.array([p.bounds for p in polys])
    x0, y0, x1, y1 = b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max()
    pad = 4
    w, h = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
    ring = lambda r: "M" + " L".join(f"{x - x0 + pad:.2f},{y1 - y + pad:.2f}" for x, y in r.coords) + " Z"
    d = " ".join(" ".join(ring(r) for r in (p.exterior, *p.interiors)) for p in polys)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}mm" height="{h:.1f}mm" viewBox="0 0 {w:.2f} {h:.2f}">'
            f'<path d="{d}" fill="#c9a46c" fill-opacity=".5" fill-rule="evenodd" stroke="#333" stroke-width=".4"/></svg>')


def _nearest(rgb: np.ndarray) -> str:
    names = list(FILAMENTS)
    d = ((np.array([FILAMENTS[n] for n in names]) - rgb) ** 2).sum(axis=1)
    return names[int(np.argmin(d))]


def cluster_colours(rgb: np.ndarray, weights: np.ndarray, k: int = 4) -> list[dict]:
    """k-means on sRGB samples (area-weighted); returns clusters biggest first, with a filament name."""
    from scipy.cluster.vq import kmeans2
    rgb = rgb.astype(float)
    k = max(1, min(k, len(rgb)))
    rng = np.random.default_rng(0)
    pick = rng.choice(len(rgb), size=min(len(rgb), 20000), replace=False)
    x, w = rgb[pick], weights[pick]
    centres, _ = kmeans2(x, k, minit="++", seed=0)
    label = ((x[:, None, :] - centres[None]) ** 2).sum(axis=2).argmin(axis=1)
    out = []
    for c in range(k):
        m = label == c
        if not m.any():
            continue
        mean = np.average(x[m], axis=0, weights=w[m])
        out.append({"hex": "#%02x%02x%02x" % tuple(int(round(v)) for v in mean),
                    "percent": round(float(w[m].sum() / w.sum() * 100), 1), "filament": _nearest(mean)})
    out.sort(key=lambda c: -c["percent"])
    # two clusters that land on the same filament are one colour to the printer
    merged: dict[str, dict] = {}
    for c in out:
        if c["filament"] in merged:
            merged[c["filament"]]["percent"] = round(merged[c["filament"]]["percent"] + c["percent"], 1)
        else:
            merged[c["filament"]] = dict(c)
    return sorted(merged.values(), key=lambda c: -c["percent"])


def textured_colours(textured: dict, texture: bytes, k: int = 4) -> list[dict]:
    """Colours of the surface itself: the texture sampled at each triangle's UV centre, weighted by
    triangle area, so unused parts of the texture image never count."""
    from PIL import Image
    img = np.asarray(Image.open(io.BytesIO(texture)).convert("RGB"))
    pos = np.array(textured["positions"], dtype=float).reshape(-1, 3, 3)
    uv = np.array(textured["uvs"], dtype=float).reshape(-1, 3, 2).mean(axis=1)
    area = np.linalg.norm(np.cross(pos[:, 1] - pos[:, 0], pos[:, 2] - pos[:, 0]), axis=1) / 2
    h, w = img.shape[:2]
    px = np.clip((uv[:, 0] * (w - 1)).round().astype(int), 0, w - 1)
    py = np.clip(((1 - uv[:, 1]) * (h - 1)).round().astype(int), 0, h - 1)  # USD's v runs up, the image's y runs down
    keep = area > 0
    return cluster_colours(img[py, px][keep], area[keep], k)


def vertex_colours(ply: Path, k: int = 4) -> list[dict]:
    m = trimesh.load(ply, process=False)
    rgb = np.asarray(m.visual.vertex_colors)[:, :3]
    return cluster_colours(rgb, np.ones(len(rgb)), k)


def suggest_colours(scan_dir: Path, k: int = 4) -> dict:
    """colours.json: filament suggestions from whatever colour the scan has. Scan lighting darkens
    and dulls colours, so these are starting points, not matches."""
    if (scan_dir / "textured.json").exists() and (scan_dir / "texture.jpg").exists():
        cols = textured_colours(json.loads((scan_dir / "textured.json").read_text()), (scan_dir / "texture.jpg").read_bytes(), k)
        source = "photo texture"
    elif (scan_dir / "colored.ply").exists():
        cols = vertex_colours(scan_dir / "colored.ply", k)
        source = "vertex colours"
    else:
        raise ValueError("this scan has no colour information")
    return {"source": source, "colours": cols,
            "note": "Suggestions only: scan lighting darkens and dulls colours. Print the biggest share in the main filament, "
                    "or paint/swap filament for the rest."}


def vertex_rgb_for(mesh_mm: trimesh.Trimesh, ply: Path) -> np.ndarray | None:
    """Colour of every vertex of model.stl (mm, Z up), looked up in colored.ply (metres, Y up) by position, because the STL is
    cleaned and re-read in another vertex order. None when the two do not describe the same surface (nothing within 0.5 mm)."""
    from scipy.spatial import cKDTree
    col = trimesh.load(ply, process=False)
    if col.visual.kind != "vertex" or not len(col.vertices):
        return None
    v = np.asarray(col.vertices)
    moved = np.c_[v[:, 0], -v[:, 2], v[:, 1]] * 1000.0
    moved = moved - moved.min(axis=0) + mesh_mm.bounds[0]       # the STL was also moved onto the bed: line the two up by their boxes
    d, i = cKDTree(moved).query(mesh_mm.vertices)
    if float(np.percentile(d, 99)) > 0.5:
        return None
    return np.asarray(col.visual.vertex_colors)[i][:, :3]


def colour_3mf(scan_dir: Path, k: int = 4) -> tuple[bytes, list[dict]]:
    """model.stl as one 3MF whose every triangle carries one of the suggested filaments (3MF basematerials, per triangle).
    Slicers differ in how much of this they read: PrusaSlicer/Orca show the colours and let you map each to a filament slot."""
    stl, ply = scan_dir / "model.stl", scan_dir / "colored.ply"
    if not stl.exists() or not ply.exists():
        raise ValueError("this scan has no colours to print with")
    mesh = trimesh.load(stl)
    rgb = vertex_rgb_for(mesh, ply)
    if rgb is None:
        raise ValueError("the coloured model and the printable model do not match any more: re-convert the scan first")
    rgb = rgb.astype(float)
    wanted = [c["filament"] for c in vertex_colours(ply, k)]
    names = list(dict.fromkeys(wanted))
    palette = np.array([FILAMENTS[n] for n in names], dtype=float)
    face_rgb = rgb[mesh.faces].mean(axis=1)
    pick = ((face_rgb[:, None, :] - palette[None]) ** 2).sum(axis=2).argmin(axis=1)
    hexes = ["#%02X%02X%02X" % FILAMENTS[n] for n in names]
    bases = "".join(f'<base name="{n}" displaycolor="{h}"/>' for n, h in zip(names, hexes))
    verts = "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in mesh.vertices)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}" pid="1" p1="{int(i)}"/>' for (a, b, c), i in zip(mesh.faces, pick))
    model = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
             f'<basematerials id="1">{bases}</basematerials>'
             f'<object id="2" type="model" pid="1" pindex="0"><mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh></object>'
             '</resources><build><item objectid="2"/></build></model>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", model)
    share = np.bincount(pick, minlength=len(names)) / len(pick)
    return buf.getvalue(), [{"filament": n, "hex": h, "percent": round(float(p) * 100, 1)} for n, h, p in zip(names, hexes, share)]
