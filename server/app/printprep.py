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


def cut(mesh: trimesh.Trimesh, axis: str, at_mm: float) -> tuple[list[trimesh.Trimesh], list[str]]:
    """Split at the plane `axis = at_mm` (a position along the part, from its low end), cap both
    cut faces, and lay each half cut-face down on the bed. Returns (parts, log)."""
    if axis not in AXES:
        raise ValueError("axis must be x, y or z")
    i = AXES[axis]
    lo, hi = _bounds(mesh, axis)
    pos = lo + at_mm
    if not (lo < pos < hi):
        raise ValueError(f"cut at {at_mm} mm is outside the part (0 to {hi - lo:.1f} mm along {axis})")
    log = []
    origin = np.zeros(3); origin[i] = pos
    normal = np.zeros(3); normal[i] = 1
    parts = []
    for sign in (-1, 1):  # low half, then high half
        half = mesh.slice_plane(origin, normal * sign, cap=True)
        if len(half.faces) == 0:
            raise ValueError("one side of the cut is empty")
        if not half.is_watertight:
            trimesh.repair.fill_holes(half)
        log.append(f"part {len(parts) + 1}: {'watertight' if half.is_watertight else 'NOT watertight'}")
        # lay it cut face down: that face's outward normal is +axis on the low half, -axis on the high half
        half.apply_transform(trimesh.geometry.align_vectors(normal * (1 if sign == -1 else -1), [0, 0, -1]))
        lo2, hi2 = half.bounds
        half.apply_translation([-(lo2[0] + hi2[0]) / 2, -(lo2[1] + hi2[1]) / 2, -lo2[2]])
        parts.append(half)
    return parts, log


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
