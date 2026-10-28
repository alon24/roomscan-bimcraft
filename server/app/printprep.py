"""Get a scanned model ready to print: cut it into parts, draw a cross-section, suggest filament colours.
All sizes are mm in model.stl's own frame (Z up, centred on the bed, sitting on Z=0)."""
import io
import json
import os
from pathlib import Path

import numpy as np
import trimesh

AXES = {"x": 0, "y": 1, "z": 2}
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
    axis = "xyz"[int(np.argmax(over))]
    return {"axis": axis, "at": round(size_mm["xyz".index(axis)] / 2, 1), "bed_mm": bed}


def _bounds(mesh: trimesh.Trimesh, axis: str) -> tuple[float, float]:
    lo, hi = mesh.bounds[:, AXES[axis]]
    return float(lo), float(hi)


CONNECTORS = ("none", "dowel", "peg", "key")
WALL_MM = 2.0   # material kept between a connector and the outside of the part
GAP_MM = 10.0   # space between parts laid out for the bed / in the 3MF


class Piece:
    """One part while cutting: its mesh and the cut faces it has (axis index, outward sign, position, has pegs)."""
    def __init__(self, mesh, faces=()):
        self.mesh, self.faces = mesh, list(faces)


def _axis_vec(i: int, sign: float = 1.0) -> np.ndarray:
    v = np.zeros(3); v[i] = sign
    return v


def _split(mesh: trimesh.Trimesh, i: int, pos: float):
    origin, normal = _axis_vec(i) * pos, _axis_vec(i)
    low = mesh.slice_plane(origin, -normal, cap=True)
    high = mesh.slice_plane(origin, normal, cap=True)
    for half in (low, high):
        if len(half.faces) == 0:
            raise ValueError("one side of the cut is empty")
        if not half.is_watertight:
            trimesh.repair.fill_holes(half)
    return low, high


def connector_points(mesh: trimesh.Trimesh, i: int, pos: float, size: float, count: int):
    """Where connectors go on this cut face: spread out, each with WALL_MM of material round it.
    Returns (points as 3D, to3d transform of the section's plane, log)."""
    from shapely.ops import polylabel
    sec = mesh.section(plane_origin=_axis_vec(i) * pos, plane_normal=_axis_vec(i))
    if sec is None:
        return [], None, ["the cut misses the part"]
    planar, to3d = sec.to_planar()
    pts = []
    for poly in planar.polygons_full:
        inset = poly.buffer(-(size / 2 + WALL_MM))
        if inset.is_empty:
            continue
        if inset.geom_type == "MultiPolygon":
            inset = max(inset.geoms, key=lambda g: g.area)
        seed = polylabel(inset, tolerance=0.25)
        chosen = [(seed.x, seed.y)]
        if count > 1:
            x0, y0, x1, y1 = inset.bounds
            step = max(0.5, max(x1 - x0, y1 - y0) / 40)
            from shapely.geometry import Point
            grid = [(x, y) for x in np.arange(x0, x1 + step, step) for y in np.arange(y0, y1 + step, step)
                    if inset.contains(Point(x, y))]
            while len(chosen) < count and grid:   # farthest from the ones already chosen
                far = max(grid, key=lambda g: min(np.hypot(g[0] - c[0], g[1] - c[1]) for c in chosen))
                if min(np.hypot(far[0] - c[0], far[1] - c[1]) for c in chosen) < size * 2.5:
                    break
                chosen.append(far)
        pts += chosen
    return [np.array(to3d @ [x, y, 0, 1])[:3] for x, y in pts], to3d, []


def _prism(shape: str, size: float, z0: float, z1: float, to3d, centre) -> trimesh.Trimesh:
    """A round or square bar standing on the cut plane at `centre`, from z0 to z1 along the plane's own z."""
    m = (trimesh.creation.cylinder(radius=size / 2, height=z1 - z0, sections=48) if shape == "round"
         else trimesh.creation.box(extents=(size, size, z1 - z0)))
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


def _join(low: trimesh.Trimesh, high: trimesh.Trimesh, i: int, pos: float, mesh: trimesh.Trimesh, kind: str,
          size: float, depth: float, clearance: float, count: int, log: list[str], pins: list, at_mm: float):
    """Add connectors across the cut `i = pos` between `low` and `high`. Returns (low, high, pegged)."""
    pts, to3d, notes = connector_points(mesh, i, pos, size, count)
    log += notes
    if not pts:
        log.append(f"cut face too small for a {size:g} mm connector: joined with nothing")
        return low, high, False
    if not (low.is_watertight and high.is_watertight):
        log.append("a part is not watertight, so no connectors were added")
        return low, high, False
    s = 1.0 if np.dot(np.array(to3d, dtype=float)[:3, 2], _axis_vec(i)) > 0 else -1.0  # plane z -> +axis
    shape = "square" if kind == "key" else "round"
    done = 0
    for c in pts:
        if kind == "dowel":   # a hole in each half and a loose pin between them
            hole = lambda z0, z1: _prism("round", size + 2 * clearance, *sorted((s * z0, s * z1)), to3d, c)
            l2 = _boolean("difference", low, hole(-depth, 0.5))
            h2 = _boolean("difference", high, hole(-0.5, depth))
            if l2 is None or h2 is None:
                log.append("a dowel hole could not be cut")
                continue
            low, high = l2, h2
            pins.append((size, 2 * depth - 1.0))
        else:                 # peg or key standing from the low half into a socket in the high half
            peg = _prism(shape, size, *sorted((s * -1.0, s * depth)), to3d, c)
            sock = _prism(shape, size + 2 * clearance, *sorted((s * -0.5, s * (depth + 0.5))), to3d, c)
            l2, h2 = _boolean("union", low, peg), _boolean("difference", high, sock)
            if l2 is None or h2 is None:
                log.append("a peg could not be added")
                continue
            low, high = l2, h2
        done += 1
    if done:
        log.append(f"{done} {'dowel hole pairs' if kind == 'dowel' else 'square keys' if kind == 'key' else 'round pegs'} "
                   f"({size:g} mm, {depth:g} mm deep) at the cut {'xyz'[i].upper()} = {at_mm:g} mm")
    return low, high, done > 0 and kind in ("peg", "key")


def _face_area(mesh: trimesh.Trimesh, i: int, sign: float, pos: float) -> float:
    n = mesh.face_normals[:, i] * sign
    near = np.abs(mesh.triangles_center[:, i] - pos) < 0.5
    return float(mesh.area_faces[(n > 0.99) & near].sum())


def _lay_down(piece: Piece, log: list[str], number: int):
    """Rotate a part so its biggest flat cut face (one without pegs) lies on the bed. A part whose cut
    faces all carry pegs stands pegs up instead, and may need supports."""
    m = piece.mesh
    flat = [f for f in piece.faces if not f["pegged"]]
    if flat:
        f = max(flat, key=lambda f: _face_area(m, f["axis"], f["sign"], f["pos"]))
        m.apply_transform(trimesh.geometry.align_vectors(_axis_vec(f["axis"], f["sign"]), [0, 0, -1]))
    elif piece.faces:
        f = piece.faces[0]
        m.apply_transform(trimesh.geometry.align_vectors(_axis_vec(f["axis"], f["sign"]), [0, 0, 1]))
        log.append(f"part {number} prints with its pegs up: it may need supports (dowel holes lie flat instead)")
    lo, hi = m.bounds
    m.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])


def cut_all(mesh: trimesh.Trimesh, cuts: list[tuple[str, float]], kind: str = "none", size: float = 6.0,
            depth: float = 8.0, clearance: float = 0.3, count: int = 2):
    """Cut the part at every plane (`axis`, mm from the low end of the whole part), joining each cut
    with connectors of `kind` (none, dowel, peg, key). Returns (parts laid flat, pins as (diameter, length), log)."""
    if kind not in CONNECTORS:
        raise ValueError(f"connector must be one of {', '.join(CONNECTORS)}")
    if not cuts:
        raise ValueError("no cuts given")
    origin = mesh.bounds[0].copy()
    log: list[str] = []
    pins: list = []
    pieces = [Piece(mesh.copy())]
    for axis, at in cuts:
        if axis not in AXES:
            raise ValueError("axis must be x, y or z")
        i = AXES[axis]
        pos = float(origin[i] + at)
        lo, hi = _bounds(mesh, axis)
        if not (lo < pos < hi):
            raise ValueError(f"cut at {at:g} mm is outside the part (0 to {hi - lo:.1f} mm along {axis})")
        nxt, crossed = [], 0
        for pc in pieces:
            plo, phi = pc.mesh.bounds[:, i]
            if not (plo + 0.01 < pos < phi - 0.01):
                nxt.append(pc)
                continue
            crossed += 1
            low, high = _split(pc.mesh, i, pos)
            pegged = False
            if kind != "none":
                low, high, pegged = _join(low, high, i, pos, pc.mesh, kind, size, depth, clearance, count, log, pins, at)
            nxt.append(Piece(low, pc.faces + [dict(axis=i, sign=1.0, pos=pos, pegged=pegged)]))
            nxt.append(Piece(high, pc.faces + [dict(axis=i, sign=-1.0, pos=pos, pegged=False)]))
        if not crossed:
            raise ValueError(f"cut {axis.upper()} = {at:g} mm touches no part")
        pieces = nxt
    for n, pc in enumerate(pieces, 1):
        _lay_down(pc, log, n)
        log.append(f"part {n}: {'watertight' if pc.mesh.is_watertight else 'NOT watertight'}, "
                   + " × ".join(f"{x:.0f}" for x in pc.mesh.extents) + " mm")
    return [pc.mesh for pc in pieces], pins, log


def pin_meshes(pins: list) -> list[trimesh.Trimesh]:
    """Loose dowel pins, lying on their side (printed along their length they are strongest)."""
    out = []
    for d, length in pins:
        m = trimesh.creation.cylinder(radius=d / 2 - 0.05, height=length, sections=48)
        m.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
        lo = m.bounds[0]
        m.apply_translation([-m.bounds.mean(axis=0)[0], -m.bounds.mean(axis=0)[1], -lo[2]])
        out.append(m)
    return out


def lay_out(parts: list[trimesh.Trimesh], extra: list[trimesh.Trimesh] = ()) -> trimesh.Scene:
    """Every part side by side on the bed (left to right, GAP_MM apart): for the one 3MF that holds all cuts."""
    scene, x = trimesh.Scene(), 0.0
    for n, m in enumerate(list(parts) + list(extra), 1):
        m = m.copy()
        w = m.extents[0]
        m.apply_translation([x + w / 2, 0, 0])
        scene.add_geometry(m, node_name=f"part{n}" if n <= len(parts) else f"pin{n - len(parts)}",
                           geom_name=f"part{n}" if n <= len(parts) else f"pin{n - len(parts)}")
        x += w + GAP_MM
    return scene


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
    return lay_out([bar], pin_meshes([(size, 2 * depth - 1.0)] * 2))


def section_svg(mesh: trimesh.Trimesh, axis: str, at_mm: float) -> str:
    """Outline of the cross-section at `axis = at_mm` as an SVG (mm), the cut face seen from the high side."""
    i = AXES[axis]
    lo, hi = _bounds(mesh, axis)
    origin = np.zeros(3); origin[i] = lo + at_mm
    normal = np.zeros(3); normal[i] = 1
    sec = mesh.section(plane_origin=origin, plane_normal=normal)
    if sec is None:
        raise ValueError("the plane does not touch the part")
    planar, to3d = sec.to_planar()
    b = planar.bounds
    pad = 4
    w, h = (b[1] - b[0]) + 2 * pad
    paths = []
    for poly in planar.polygons_full:
        d = "M" + " L".join(f"{x - b[0][0] + pad:.2f},{b[1][1] - y + pad:.2f}" for x, y in poly.exterior.coords) + " Z"
        for ring in poly.interiors:
            d += " M" + " L".join(f"{x - b[0][0] + pad:.2f},{b[1][1] - y + pad:.2f}" for x, y in ring.coords) + " Z"
        paths.append(d)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}mm" height="{h:.1f}mm" viewBox="0 0 {w:.2f} {h:.2f}">'
            f'<path d="{" ".join(paths)}" fill="#c9a46c" fill-opacity=".5" fill-rule="evenodd" stroke="#333" stroke-width=".4"/></svg>')


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
