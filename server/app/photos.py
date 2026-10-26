"""Correlate capture photos with RoomPlan surfaces: which photo sees which wall/door/window, and where.

Conventions (ARKit): world is right-handed, Y up, metres, the same frame as the scan's surface transforms.
Camera space: +X right, +Y up, looking down -Z. Image pixels: origin top-left, u right, v down,
for the unrotated sensor image (landscape) of size width x height; intrinsics are for that image size.
"""
import math

import numpy as np

KINDS = (("walls", "wall"), ("doors", "door"), ("windows", "window"), ("openings", "opening"))


def _mat(t) -> np.ndarray:
    t = np.array(t, dtype=float)
    return t.reshape(4, 4).T if t.size == 16 and t.ndim == 1 else t.T  # columns -> row-major matrix


def corners(surface: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """World-space corners (4x3), centre and unit normal of a RoomPlan surface (local Z = normal)."""
    m = _mat(surface["transform"])
    w, h = surface["dimensions"][0], surface["dimensions"][1]
    pts = [m @ np.array([sx * w / 2, sy * h / 2, 0, 1]) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    return np.array([p[:3] for p in pts]), m[:3, 3], m[:3, 2] / (np.linalg.norm(m[:3, 2]) or 1)


def project(photo: dict, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pixel coordinates (Nx2) and camera-space depth along the view direction (N, positive = in front)."""
    cam = np.linalg.inv(_mat(photo["transform"]))
    k = np.array(photo["intrinsics"], dtype=float).T  # columns -> row-major 3x3
    pc = (cam @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]
    depth = -pc[:, 2]
    safe = np.where(depth > 1e-6, depth, 1e-6)
    u = k[0, 0] * pc[:, 0] / safe + k[0, 2]
    v = k[1, 2] - k[1, 1] * pc[:, 1] / safe
    return np.c_[u, v], depth


def coverage(photo: dict, surface: dict, grid: int = 5) -> dict | None:
    """How much of the surface this photo sees. None if it sees none of it."""
    cs, centre, normal = corners(surface)
    # sample the surface on a grid so partial views count, not just corners
    a, b = cs[1] - cs[0], cs[3] - cs[0]
    samples = np.array([cs[0] + a * i / (grid - 1) + b * j / (grid - 1) for i in range(grid) for j in range(grid)])
    uv, depth = project(photo, samples)
    w, h = photo["width"], photo["height"]
    inside = (depth > 0.05) & (uv[:, 0] >= 0) & (uv[:, 0] <= w) & (uv[:, 1] >= 0) & (uv[:, 1] <= h)
    frac = float(inside.mean())
    if frac == 0:
        return None
    cam_pos = _mat(photo["transform"])[:3, 3]
    view = centre - cam_pos
    dist = float(np.linalg.norm(view))
    incidence = math.degrees(math.acos(min(1.0, abs(float(np.dot(view / (dist or 1), normal))))))
    cuv, cdepth = project(photo, cs)
    return {"photo": photo["file"], "coverage": round(frac, 3), "distance_m": round(dist, 3),
            "incidence_deg": round(incidence, 1),
            "corners_px": [[round(float(x), 1), round(float(y), 1)] for x, y in cuv],
            "corners_in_front": bool((cdepth > 0).all())}


def index_photos(scan: dict, manifest: dict | list) -> dict:
    """{surface id: {kind, photos: [best first]}} for every surface any photo sees.

    Rank: more of the surface visible first, then head-on (low incidence), then closer.
    """
    photos = manifest["photos"] if isinstance(manifest, dict) else manifest
    out = {}
    for key, kind in KINDS:
        for s in scan.get(key) or []:
            hits = [c for p in photos if (c := coverage(p, s))]
            hits.sort(key=lambda c: (-c["coverage"], c["incidence_deg"], c["distance_m"]))
            if hits:
                out[s["identifier"]] = {"kind": kind, "photos": hits}
    return out
