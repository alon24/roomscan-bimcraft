"""Line a new capture session up with an old scan of the same room.

Every ARKit session has its own world, but gravity is shared: Y is up in both. So the new session differs from the old one by a turn
about Y and a shift in X, Y and Z (4 numbers). They are found in two steps:
  1. a search over the turn: for every 3 degrees, the top-down wall/furniture density of the new points is correlated with the old
     one (FFT) to give the best shift for that turn;
  2. the best two turns that are clearly different are refined with ICP restricted to those 4 numbers.
Two candidates that fit about equally well (a symmetric empty room, the same view from two sides) are reported as ambiguous
instead of guessed."""
import math

import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree

from . import frames
from .meshscan import _depth_map
from .photos import _mat

STEP_DEG = 3
GRID = 0.05
MAX_RMS = 0.08        # metres: a good fit is a few cm (LiDAR noise); worse than this is not the same room
MIN_INLIERS = 0.35    # share of points within 10 cm of the other cloud
AMBIGUOUS = 1.10      # another clearly different turn that fits within 10% as well as the best one: do not guess


def cloud(d, photos: list[dict], max_frames: int = 60, per_frame: int = 600, seed: int = 0) -> np.ndarray:
    """World points (N, 3) from the depth of up to `max_frames` frames spread over the list."""
    rng = np.random.default_rng(seed)
    use = [p for p in photos if p.get("depth_file") and (d / "photos" / p["depth_file"]).exists()]
    if len(use) > max_frames:
        use = [use[i] for i in np.linspace(0, len(use) - 1, max_frames).astype(int)]
    out = []
    for p in use:
        depth = _depth_map(d, p)
        if depth is None:
            continue
        P = frames.world_points(p, depth).reshape(-1, 3)
        dd = depth.reshape(-1)
        ok = np.isfinite(dd) & (dd > 0.2) & (dd < 5.0) & np.isfinite(P).all(axis=1)
        P = P[ok]
        if len(P) > per_frame:
            P = P[rng.choice(len(P), per_frame, replace=False)]
        out.append(P)
    return np.concatenate(out) if out else np.zeros((0, 3))


def _levels(P: np.ndarray) -> tuple[float, float]:
    return float(np.percentile(P[:, 1], 2)), float(np.percentile(P[:, 1], 98))


def _raster(xz: np.ndarray, lo: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    ix = np.floor((xz[:, 0] - lo[0]) / GRID).astype(int)
    iz = np.floor((xz[:, 1] - lo[1]) / GRID).astype(int)
    ok = (ix >= 0) & (ix < shape[1]) & (iz >= 0) & (iz < shape[0])
    img = np.zeros(shape)
    np.add.at(img, (iz[ok], ix[ok]), 1.0)
    img = gaussian_filter(img, 2.0)
    return img / (np.linalg.norm(img) + 1e-9)


def _rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _wall_band(P: np.ndarray) -> np.ndarray:
    lo, hi = _levels(P)
    m = (P[:, 1] > lo + 0.3) & (P[:, 1] < hi - 0.2)
    return P[m] if m.sum() > 300 else P


def _candidates(old: np.ndarray, new: np.ndarray, top: int = 6):
    """Best (score, yaw, shift_xz) from the correlation search, distinct turns only."""
    A, B = _wall_band(old), _wall_band(new)
    ca = A[:, [0, 2]].mean(axis=0)
    pad = 2.5
    lo = A[:, [0, 2]].min(axis=0) - pad             # the new points are moved onto the old middle, so the old bounds serve both
    hi = A[:, [0, 2]].max(axis=0) + pad
    shape = (int(math.ceil((hi[1] - lo[1]) / GRID)), int(math.ceil((hi[0] - lo[0]) / GRID)))
    shape = (min(shape[0], 600), min(shape[1], 600))
    ra = _raster(A[:, [0, 2]], lo, shape)
    FA = np.fft.rfft2(ra)
    cb = B[:, [0, 2]].mean(axis=0)
    res = []
    for deg in range(0, 360, STEP_DEG):
        a = math.radians(deg)
        R = _rot_y(a)
        Bx = (B - np.array([cb[0], 0, cb[1]])) @ R.T            # new points turned about their own middle
        # put the middle of the new cloud on the middle of the old one, then correlate for the shift
        xz = Bx[:, [0, 2]] + ca
        rb = _raster(xz, lo, shape)
        corr = np.fft.irfft2(FA * np.conj(np.fft.rfft2(rb)), s=shape)
        iz, ix = np.unravel_index(int(np.argmax(corr)), corr.shape)
        iz = iz - shape[0] if iz > shape[0] // 2 else iz
        ix = ix - shape[1] if ix > shape[1] // 2 else ix
        res.append((float(corr.max()), deg, np.array([ix * GRID, iz * GRID]), np.array([cb[0], cb[1]]), ca))
    res.sort(key=lambda r: -r[0])
    picked = []
    for r in res:
        if all(min(abs(r[1] - q[1]), 360 - abs(r[1] - q[1])) > 20 for q in picked):
            picked.append(r)
        if len(picked) >= top:
            break
    return picked


def _transform(yaw: float, shift_xz: np.ndarray, cb: np.ndarray, ca: np.ndarray, dy: float) -> np.ndarray:
    """4x4 old<-new: turn the new points about their own middle, move that middle to the old middle, then the found shift."""
    R = _rot_y(yaw)
    T = np.eye(4)
    T[:3, :3] = R
    mid_new = np.array([cb[0], 0, cb[1]])
    T[:3, 3] = np.array([ca[0] + shift_xz[0], dy, ca[1] + shift_xz[1]]) - R @ mid_new
    return T


def _icp(old_tree: cKDTree, old: np.ndarray, new: np.ndarray, T: np.ndarray, iters: int = 40, trim: float = 0.75):
    """Refine T (turn about Y + shift) with trimmed point-to-point ICP. Returns (T, rms, inlier share)."""
    rms, inl = np.inf, 0.0
    for _ in range(iters):
        P = new @ T[:3, :3].T + T[:3, 3]
        d, j = old_tree.query(P)
        keep = d <= np.quantile(d, trim)
        p, q = P[keep], old[j[keep]]
        pc, qc = p.mean(axis=0), q.mean(axis=0)
        # 2D Procrustes for the turn about Y, plain mean difference for the height
        a = p[:, [0, 2]] - pc[[0, 2]]
        b = q[:, [0, 2]] - qc[[0, 2]]
        H = a.T @ b
        ang = math.atan2(H[0, 1] - H[1, 0], H[0, 0] + H[1, 1])      # rotation taking a to b in the (x, z) plane
        # (x, z) rotation by ang corresponds to a Y-rotation of -ang with R_y(a) = [[c,0,s],[0,1,0],[-s,0,c]]
        dR = _rot_y(-ang)
        shift = qc - dR @ pc
        step = np.eye(4)
        step[:3, :3] = dR
        step[:3, 3] = shift
        T = step @ T
        rms_new = float(np.sqrt((d[keep] ** 2).mean()))
        inl = float((d < 0.10).mean())
        if abs(rms - rms_new) < 1e-5:
            rms = rms_new
            break
        rms = rms_new
    return T, rms, inl


def register(d, old_photos: list[dict], new_photos: list[dict]) -> dict:
    """Find T (old <- new). Returns {ok, T (4x4), rms_mm, inliers, message}. The new photos' own transforms are in the new session frame."""
    old = cloud(d, old_photos, seed=1)
    new = cloud(d, new_photos, seed=2)
    if len(old) < 2000 or len(new) < 1000:
        return {"ok": False, "message": "Not enough depth yet to recognise the room: film a wall and a corner you already scanned, slowly.", "rms_mm": None}
    lo_o, _ = _levels(old)
    lo_n, _ = _levels(new)
    dy = lo_o - lo_n
    cands = _candidates(old, new)
    tree = cKDTree(old)
    fitted = []
    sub = new[np.random.default_rng(3).choice(len(new), min(len(new), 12000), replace=False)]
    for score, deg, shift, cb, ca in cands:
        T0 = _transform(math.radians(deg), shift, cb, ca, dy)
        T, rms, inl = _icp(tree, old, sub, T0)
        d, _ = tree.query(sub @ T[:3, :3].T + T[:3, 3])
        tm = float(np.minimum(d, 0.10).mean())            # truncated mean distance: rewards matches, punishes parts that disagree
        fitted.append((tm, rms, inl, T))
    fitted.sort(key=lambda f: f[0])
    best = fitted[0]
    yaw = yaw_of(best[3])
    for other in fitted[1:]:
        dyaw = abs((yaw_of(other[3]) - yaw + 180) % 360 - 180)
        close = other[0] < best[0] * AMBIGUOUS or (other[2] >= 0.95 * best[2] and other[0] < best[0] * 1.25)    # about equally good, or both near-perfect
        if dyaw > 30 and close and other[2] > MIN_INLIERS:
            return {"ok": False, "ambiguous": True, "rms_mm": round(best[1] * 1000, 1),
                    "message": "The room looks the same from two directions, so I cannot tell where you are. Film something distinctive (a door, a window, furniture) that you scanned before."}
    _, rms, inl, T = best
    if rms > MAX_RMS or inl < MIN_INLIERS:
        return {"ok": False, "rms_mm": round(rms * 1000, 1), "inliers": round(inl, 2),
                "message": "I could not match this to the first scan yet. Film an area you already scanned (walls and corners), slowly, for a few seconds."}
    return {"ok": True, "T": T, "rms_mm": round(rms * 1000, 1), "inliers": round(inl, 2),
            "message": f"Matched to the first scan (error {rms * 1000:.0f} mm)."}


def apply(photos: list[dict], T: np.ndarray) -> list[dict]:
    """Photo entries with their transforms moved into the old frame (the phone's column-major layout is kept)."""
    out = []
    for p in photos:
        q = dict(p)
        M = T @ _mat(p["transform"])
        q["transform"] = M.T.tolist()
        out.append(q)
    return out


def yaw_of(T: np.ndarray) -> float:
    return math.degrees(math.atan2(T[0, 2], T[0, 0]))
