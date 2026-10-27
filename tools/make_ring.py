#!/usr/bin/env python3
"""Design data for the turntable marker ring (see docs/turntable-ring.md).

Writes docs/ring/ring.json (the contract the scan server reads), docs/ring/black_cells.json (every black rectangle,
as polygons in ring coordinates, for the CAD model), docs/ring/ring_preview.svg and a PNG, then checks the design by
rendering it and detecting the markers with OpenCV.
Run:  server/.venv/bin/python tools/make_ring.py
"""
import json
import math
from pathlib import Path

import cv2
import numpy as np

OUT = Path(__file__).resolve().parent.parent / "docs" / "ring"
N = 18                  # markers
ID0 = 0                 # first dictionary id
MARKER = 22.0           # mm, outer edge of the marker's black border
R_CENTRE = 81.0         # mm, marker centres on this radius
RING_ID, RING_OD = 128.0, 196.0   # mm, inner and outer diameter of the ring
STAGE_D = 127.0         # mm, the disc the part stands on (0.5 mm radial clearance inside the ring)
T_BASE, T_RAISED = 2.6, 0.4       # mm, white base and the raised black/orange layer
DICT = cv2.aruco.DICT_4X4_50
CELLS = 6               # 4x4 bits plus the 1-cell black border on each side


def bits(marker_id: int) -> np.ndarray:
    """6x6 matrix, 1 = black, as printed with the marker upright."""
    d = cv2.aruco.getPredefinedDictionary(DICT)
    img = cv2.aruco.generateImageMarker(d, marker_id, CELLS, borderBits=1)  # CELLS pixels: one per cell
    return (img < 128).astype(int)


def frame(i: int) -> dict:
    """Marker i in ring coordinates: centre, 'up' (radially outward) and 'right' (clockwise seen from above) unit vectors."""
    phi = math.radians(i * 360.0 / N)
    u = np.array([math.cos(phi), math.sin(phi)])
    r = np.array([math.sin(phi), -math.cos(phi)])
    return {"phi": phi, "c": R_CENTRE * u, "u": u, "r": r}


def corners(i: int) -> list[list[float]]:
    f, h = frame(i), MARKER / 2
    return [(f["c"] + h * (-f["r"] + f["u"])).tolist(), (f["c"] + h * (f["r"] + f["u"])).tolist(),
            (f["c"] + h * (f["r"] - f["u"])).tolist(), (f["c"] + h * (-f["r"] - f["u"])).tolist()]   # TL TR BR BL, as OpenCV reports


def black_polygons(i: int) -> list[list[list[float]]]:
    """Black cells of marker i as polygons (horizontal runs of cells merged), ring coordinates in mm."""
    f, cell, h = frame(i), MARKER / CELLS, MARKER / 2
    b = bits(ID0 + i)
    polys = []
    for row in range(CELLS):
        col = 0
        while col < CELLS:
            if not b[row, col]:
                col += 1
                continue
            end = col
            while end + 1 < CELLS and b[row, end + 1]:
                end += 1
            x0, x1 = -h + col * cell, -h + (end + 1) * cell       # along 'right'
            y0, y1 = h - (row + 1) * cell, h - row * cell          # along 'up'
            polys.append([(f["c"] + x * f["r"] + y * f["u"]).round(4).tolist() for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))])
            col = end + 1
    return polys


def svg() -> str:
    s = 2.0   # px per mm
    cx = cy = RING_OD / 2 * s + 10
    def pt(p): return f"{cx + p[0] * s:.2f},{cy - p[1] * s:.2f}"
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{2 * cx:.0f}" height="{2 * cy:.0f}" viewBox="0 0 {2 * cx:.0f} {2 * cy:.0f}">',
           f'<rect width="100%" height="100%" fill="#ddd"/>',
           f'<circle cx="{cx}" cy="{cy}" r="{RING_OD / 2 * s}" fill="#fff"/><circle cx="{cx}" cy="{cy}" r="{RING_ID / 2 * s}" fill="#ddd"/>',
           f'<circle cx="{cx}" cy="{cy}" r="{STAGE_D / 2 * s}" fill="#fff"/>']
    for i in range(N):
        for poly in black_polygons(i):
            out.append(f'<polygon points="{" ".join(pt(p) for p in poly)}" fill="#000"/>')
    # orange: rim ticks every 5 degrees (long every 90), index triangle at marker 0, inner guide circle
    for k in range(72):
        a = math.radians(5 * k)
        r0, r1 = (92.5, 97.5) if k % 18 == 0 else (94.5, 97.5)
        w = 0.5
        d = np.array([math.cos(a), math.sin(a)]); n = np.array([-d[1], d[0]])
        poly = [r0 * d - w * n, r1 * d - w * n, r1 * d + w * n, r0 * d + w * n]
        out.append(f'<polygon points="{" ".join(pt(p) for p in poly)}" fill="#f60"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{65.0 * s}" fill="none" stroke="#f60" stroke-width="{1.0 * s}"/>')
    tri = [(66.0, 0), (69.5, 2.5), (69.5, -2.5)]
    out.append(f'<polygon points="{" ".join(pt(p) for p in tri)}" fill="#f60"/>')
    out.append("</svg>")
    return "\n".join(out)


def render_png(px_per_mm: float = 10.0) -> np.ndarray:
    size = int(RING_OD * px_per_mm + 40)
    img = np.full((size, size), 235, dtype=np.uint8)
    c = size / 2
    d = cv2.aruco.getPredefinedDictionary(DICT)
    side = int(round(MARKER * px_per_mm / CELLS)) * CELLS
    cv2.circle(img, (int(c), int(c)), int(RING_OD / 2 * px_per_mm), 255, -1)
    cv2.circle(img, (int(c), int(c)), int(RING_ID / 2 * px_per_mm), 235, -1)
    for i in range(N):
        m = cv2.aruco.generateImageMarker(d, ID0 + i, side, borderBits=1)
        f = frame(i)
        # image axes: x right, y down. 'up' of the marker is the radial direction; draw by an affine map of the upright marker
        centre = np.array([c + f["c"][0] * px_per_mm, c - f["c"][1] * px_per_mm])
        ux, uy = f["u"][0], -f["u"][1]
        rx, ry = f["r"][0], -f["r"][1]
        A = np.array([[rx, -ux], [ry, -uy]])  # marker image x -> right, marker image y(down) -> -up
        M = np.c_[A, centre - A @ np.array([side / 2, side / 2])]
        warped = cv2.warpAffine(m, M, img.shape[::-1], flags=cv2.INTER_NEAREST, borderValue=255)
        mask = cv2.warpAffine(np.full_like(m, 255), M, img.shape[::-1], flags=cv2.INTER_NEAREST)
        img[mask > 0] = warped[mask > 0]
    return img


def verify() -> str:
    px = 10.0
    img = render_png(px)
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(DICT), cv2.aruco.DetectorParameters())
    found, ids, _ = det.detectMarkers(img)
    ids = sorted(int(i) for i in (ids.ravel() if ids is not None else []))
    assert ids == list(range(ID0, ID0 + N)), f"detected {ids}"
    size = img.shape[0] / 2
    worst = 0.0
    for c, mid in zip(found, [int(i) for i in cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(DICT)).detectMarkers(img)[1].ravel()]):
        want = np.array(corners(mid - ID0))
        got = (c.reshape(4, 2) - size) / px * np.array([1, -1])
        worst = max(worst, float(np.abs(want - got).max()))
    assert worst < 0.5, f"corner order or position off by {worst} mm"
    cv2.imwrite(str(OUT / "ring_preview.png"), img)
    return f"all {N} markers detected, corners agree within {worst:.2f} mm"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ring = {
        "dictionary": "DICT_4X4_50", "marker_ids": list(range(ID0, ID0 + N)), "count": N,
        "marker_size_mm": MARKER, "centre_radius_mm": R_CENTRE, "ring_inner_d_mm": RING_ID, "ring_outer_d_mm": RING_OD,
        "stage_d_mm": STAGE_D, "base_thickness_mm": T_BASE, "raised_mm": T_RAISED,
        "frame": "ring frame: origin at the ring centre, Z up out of the ring surface (the white surface is Z=0), "
                 "marker i centre at angle i*360/count, counter-clockwise seen from above, X axis through marker 0",
        "marker_up": "radially outward; 'right' is clockwise seen from above",
        "corner_order": "TL, TR, BR, BL of the outer edge of the black border (OpenCV order)",
        "corners_mm": {str(ID0 + i): corners(i) for i in range(N)},
    }
    (OUT / "ring.json").write_text(json.dumps(ring, indent=1))
    (OUT / "black_cells.json").write_text(json.dumps({
        "note": "Every black rectangle as 4 corner points [x, y] in mm in ring coordinates (extrude 0.4 mm on top of the white base).",
        "markers": {str(ID0 + i): {"bits_6x6": bits(ID0 + i).tolist(), "polygons": black_polygons(i)} for i in range(N)}}, indent=1))
    (OUT / "ring_preview.svg").write_text(svg())
    print(verify())
    print("polygons per marker:", [len(black_polygons(i)) for i in range(N)])


if __name__ == "__main__":
    main()
