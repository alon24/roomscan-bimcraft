"""Fit one scan onto another from what both saw, so sweeps and a plan walked on the phone end up in one drawing.

Every capture has its own coordinates, but gravity is shared, so only a turn and a shift are unknown. The fit uses the wall readings seen from above
(layout_points.json: metres, x = world x, y = -world z). A cross-correlation tries every turn; the best few are then refined. A nearly square room can fit
well in more than one way, so the other good fits are kept and the page can step to them."""
import math

import numpy as np
from scipy.optimize import minimize
from scipy.signal import fftconvolve
from scipy.spatial import cKDTree

RASTER = 0.1           # metres
STEP_DEG = 5
KEEP = 4               # fits kept to choose between
NEAR = 0.07            # metres: a reading this near the other scan's agrees with it
SURE = 1.25            # the best fit must beat the next by this much to count as certain


def _turn(p: np.ndarray, deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return p @ np.array([[c, s], [-s, c]])


def _raster(p: np.ndarray, lo: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    r = np.zeros(shape, np.float32)
    i = ((p - lo) / RASTER).astype(int)
    keep = (i >= 0).all(axis=1) & (i[:, 0] < shape[0]) & (i[:, 1] < shape[1])
    r[i[keep, 0], i[keep, 1]] = 1
    return r


def _refine(src: np.ndarray, tree: cKDTree, dst: np.ndarray, deg: float, t: np.ndarray) -> tuple[float, np.ndarray]:
    """Trimmed closest points in 2D, closing in from 30 to 5 cm."""
    for near in (0.3, 0.3, 0.2, 0.2, 0.15, 0.15, 0.1, 0.1, 0.1, 0.1, 0.07, 0.07, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05):
        q = _turn(src, deg) + t
        d, j = tree.query(q)
        ok = d < near
        if ok.sum() < 20:
            break
        a, b = src[ok], dst[j[ok]]
        ca, cb = a.mean(axis=0), b.mean(axis=0)
        A, B = a - ca, b - cb
        deg = math.degrees(math.atan2((A[:, 0] * B[:, 1] - A[:, 1] * B[:, 0]).sum(), (A * B).sum()))
        t = cb - _turn(ca[None], deg)[0]
    return deg, t


def _polish(src: np.ndarray, tree: cKDTree, deg: float, t: np.ndarray) -> tuple[float, np.ndarray]:
    """Closest points stall on a grid of readings, so finish by maximising how tightly src lies on dst directly."""
    def cost(v: np.ndarray) -> float:
        d = tree.query(_turn(src, v[0]) + v[1:])[0]
        return -float(np.exp(-(d / 0.03) ** 2).sum())
    r = minimize(cost, [deg, t[0], t[1]], method="Nelder-Mead", options={"initial_simplex": np.array([[deg, t[0], t[1]], [deg + 1, t[0], t[1]], [deg, t[0] + 0.03, t[1]], [deg, t[0], t[1] + 0.03]]), "xatol": 0.01, "fatol": 0.5, "maxiter": 120})
    return float(r.x[0]), r.x[1:]


def fit(src: np.ndarray, dst: np.ndarray) -> list[dict]:
    """Ways to lay src on dst, best first: p' = R(deg) p + (tx, ty). `agree` is how many src readings land on dst, `share` the same over all of src."""
    tree = cKDTree(dst)
    lo_d = dst.min(axis=0)
    D = _raster(dst, lo_d, tuple(((dst.max(axis=0) - lo_d) / RASTER).astype(int) + 2))
    # one cell of give, so a small error in the turn still scores
    D = np.maximum.reduce([D, np.roll(D, 1, 0), np.roll(D, -1, 0), np.roll(D, 1, 1), np.roll(D, -1, 1)])
    coarse = []
    for deg in range(0, 360, STEP_DEG):
        q = _turn(src, deg)
        lo_s = q.min(axis=0)
        S = _raster(q, lo_s, tuple(((q.max(axis=0) - lo_s) / RASTER).astype(int) + 2))
        c = fftconvolve(D, S[::-1, ::-1], mode="full")
        i, j = np.unravel_index(np.argmax(c), c.shape)
        shift = np.array([i - (S.shape[0] - 1), j - (S.shape[1] - 1)]) * RASTER
        coarse.append((float(c[i, j]), deg, lo_d + shift - lo_s))
    coarse.sort(key=lambda x: -x[0])
    out: list[dict] = []
    tried: list[int] = []
    for _, deg, t in coarse:
        if any(min((deg - o) % 360, (o - deg) % 360) < 15 for o in tried):
            continue
        tried.append(deg)
        d2, t2 = _polish(src, tree, *_refine(src, tree, dst, float(deg), t))
        agree = int((tree.query(_turn(src, d2) + t2)[0] < NEAR).sum())
        out.append({"deg": round(d2 % 360, 3), "tx": round(float(t2[0]), 4), "ty": round(float(t2[1]), 4), "agree": agree, "share": round(agree / len(src), 3)})
        if len(tried) >= KEEP * 2:
            break
    out.sort(key=lambda f: -f["agree"])
    return out[:KEEP]


def sure(fits: list[dict]) -> bool:
    return len(fits) < 2 or fits[0]["agree"] >= SURE * max(fits[1]["agree"], 1)


def plan_point(p: list[float] | np.ndarray, f: dict) -> np.ndarray:
    """A point of the source scan's plan (before any drawing turn) in the host's plan."""
    return _turn(np.asarray(p, float).reshape(-1, 2), f["deg"]) + [f["tx"], f["ty"]]


def matrix(f: dict, dy: float) -> list[float]:
    """The same fit as a 4 x 4 world transform, column by column (three.js order). World y is up, plan y = -world z."""
    c, s = math.cos(math.radians(f["deg"])), math.sin(math.radians(f["deg"]))
    return [c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, f["tx"], dy, -f["ty"], 1]


def world_point(p: list[float], f: dict, dy: float) -> list[float]:
    m = matrix(f, dy)
    return [m[0] * p[0] + m[8] * p[2] + m[12], p[1] + dy, m[2] * p[0] + m[10] * p[2] + m[14]]
