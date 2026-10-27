"""Fixed phone, spinning plate, LiDAR depth only (no photogrammetry, so a plain white part works).

Every frame sees the part from the same side, but the part has turned a bit more. If we know how fast the plate turns and
where its axis is, each frame's points can be turned back into the part's own frame and the views add up to the whole part.
The speed is found by trying speeds and keeping the one where frames taken a moment apart line up best; the axis starts at
the middle of the traced outline and is nudged the same way.
"""
import json
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image
from scipy import ndimage as ndi
from scipy.spatial import cKDTree

from . import frames, meshscan


def _frame_points(d: Path, ph: dict, fp: dict) -> tuple[np.ndarray, np.ndarray] | None:
    """World points and RGB of the part (inside the traced outline) in one frame."""
    dm = meshscan._depth_map(d, ph)
    img_path = d / "photos" / ph["file"]
    if dm is None or not img_path.exists():
        return None
    ok = (dm > 0.1) & (dm < 5) & ((ndi.maximum_filter(dm, 3) - ndi.minimum_filter(dm, 3)) < (0.04 * dm + 0.01))
    pts = frames.world_points(ph, dm)
    poly = np.array(fp["polygon"], dtype=float)
    ok &= meshscan._inside(pts[..., [0, 2]].reshape(-1, 2), poly, 1.15).reshape(dm.shape)
    ok &= (pts[..., 1] >= fp["base_y"] - 0.01) & (pts[..., 1] <= fp["base_y"] + fp["height"] + 0.02)
    if ok.sum() < 150:
        return None
    img = np.asarray(Image.open(img_path).convert("RGB").resize((dm.shape[1], dm.shape[0]), Image.BILINEAR))
    return pts[ok], img[ok]


def _turn(p: np.ndarray, centre: np.ndarray, angle: float) -> np.ndarray:
    """Rotate points about the vertical (Y) axis through `centre` (x, z) by `angle` radians."""
    c, s = np.cos(angle), np.sin(angle)
    x, z = p[:, 0] - centre[0], p[:, 2] - centre[1]
    return np.c_[centre[0] + c * x + s * z, p[:, 1], centre[1] - s * x + c * z]


def _score(sample: list[tuple[float, np.ndarray]], pairs: list[tuple[int, int]], omega: float, centre: np.ndarray) -> float:
    """How badly frames taken a moment apart disagree once turned back by `omega` rad/s. Lower is better."""
    total = 0.0
    for a, b in pairs:
        ta, pa = sample[a]
        tb, pb = sample[b]
        ra, rb = _turn(pa, centre, -omega * ta), _turn(pb, centre, -omega * tb)
        d, _ = cKDTree(rb).query(ra)
        total += float(np.mean(np.minimum(d, 0.03)))     # a few stray points must not decide it
    return total / max(1, len(pairs))


def fuse(d: Path, scale: float = 1.0) -> tuple[bool, str]:
    fp_path = d / "footprint.json"
    if not fp_path.exists():
        return False, "A turntable scan needs the traced outline of the plate (Guided mode: Trace outline)."
    fp = json.loads(fp_path.read_text())
    got = []
    for ph in frames.manifest(d):
        r = _frame_points(d, ph, fp)
        if r is not None:
            got.append((float(ph["timestamp"]), r[0], r[1]))
    if len(got) < 6:
        return False, f"Only {len(got)} frames showed the part. Trace the outline of the plate and keep the phone still."
    got.sort(key=lambda g: g[0])
    t0 = got[0][0]
    rng = np.random.default_rng(0)
    sample = []
    for t, p, _ in got:
        idx = rng.choice(len(p), size=min(len(p), 700), replace=False)
        sample.append((t - t0, p[idx]))
    gap = max(1, len(got) // 6)
    pairs = [(k, k + gap) for k in range(0, len(got) - gap, max(1, len(got) // 10))][:10]
    centre = np.array(fp["polygon"], dtype=float).mean(axis=0)

    # 1) speed: coarse search over both directions, then fine
    best = (1e9, 0.0)
    for w in np.concatenate([np.arange(0.02, 1.5, 0.02), -np.arange(0.02, 1.5, 0.02)]):
        sc = _score(sample, pairs, float(w), centre)
        best = min(best, (sc, float(w)))
    for w in np.arange(best[1] - 0.02, best[1] + 0.0201, 0.002):
        sc = _score(sample, pairs, float(w), centre)
        best = min(best, (sc, float(w)))
    omega = best[1]
    # 2) axis: small search around the middle of the outline
    bc, bs = centre, best[0]
    for dx in np.arange(-0.04, 0.0401, 0.01):
        for dz in np.arange(-0.04, 0.0401, 0.01):
            c2 = centre + (dx, dz)
            sc = _score(sample, pairs, omega, c2)
            if sc < bs:
                bc, bs = c2, sc
    # 3) put every frame back into the part's own frame and merge
    pts = np.vstack([_turn(p, bc, -omega * (t - t0)) for t, p, _ in got])
    cols = np.vstack([c for _, _, c in got])
    cell = np.floor(pts / 0.004).astype(np.int64)
    _, first = np.unique(cell, axis=0, return_index=True)
    pts, cols = pts[first], cols[first]
    # the base is the part's own footprint (outline of everything seen), not the whole plate
    from scipy.spatial import ConvexHull
    hull = pts[:, [0, 2]][ConvexHull(pts[:, [0, 2]]).vertices]
    fp2 = {"polygon": hull.tolist(), "base_y": fp["base_y"], "height": fp["height"]}
    cloud = trimesh.PointCloud(pts, colors=np.c_[cols, np.full(len(cols), 255, dtype=np.uint8)])
    ok, log = meshscan.convert_cloud(d, cloud, scale, footprint=fp2)
    turns = abs(omega) * (got[-1][0] - t0) / (2 * np.pi)
    return ok, (f"turntable: {len(got)} frames, plate speed {abs(omega) * 60 / (2 * np.pi):.1f} rpm "
                f"({turns:.1f} turns), fit error {bs * 1000:.1f} mm; {log}")
