"""Compare a scanned model with a reference mesh (for example the CAD part it should be): line the reference up on the scan, then
measure how far every point of the scan is from the reference surface."""
import numpy as np
import trimesh
from scipy.spatial import cKDTree

# the 24 ways to turn a part so its axes stay axes (the reference may lie in any of them compared with the scan)
def _cube_rotations() -> list[np.ndarray]:
    out = []
    for perm in ([0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0], [2, 0, 1], [2, 1, 0]):
        for signs in np.ndindex(2, 2, 2):
            m = np.zeros((3, 3))
            for row, (col, s) in enumerate(zip(perm, signs)):
                m[row, col] = 1 - 2 * s
            if np.linalg.det(m) > 0:
                out.append(m)
    return out


def _rot_z(deg: float) -> np.ndarray:
    a = np.radians(deg)
    return np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])


def align(ref: trimesh.Trimesh, scan: trimesh.Trimesh, seed: int = 0) -> tuple[np.ndarray, float]:
    """4x4 matrix that moves `ref` onto `scan`, and the RMS distance after it. Tries every axis-aligned turn of the reference and
    every 10 degrees about Z, keeps the best few by distance, then refines them with ICP."""
    a = scan.sample(3000, seed=seed)
    b = ref.sample(3000, seed=seed + 1)
    b_icp = ref.sample(4000, seed=seed + 2)
    ca, cb = a.mean(axis=0), b.mean(axis=0)
    tree = cKDTree(a - ca)
    cands = []
    for R in _cube_rotations():
        for z in range(0, 90, 10):              # the cube turns already hold the quarter turns about Z
            M = _rot_z(z) @ R
            d, _ = tree.query((b - cb) @ M.T)
            cands.append((float(np.sqrt((d ** 2).mean())), M))
    cands.sort(key=lambda c: c[0])
    best = (np.inf, None)
    for _, M in cands[:6]:
        T = np.eye(4); T[:3, :3] = M; T[:3, 3] = ca - M @ cb
        try:
            mat, _, _ = trimesh.registration.icp(b_icp, a, initial=T, threshold=1e-6, max_iterations=60, scale=False)
        except Exception:
            mat = T
        moved = trimesh.transform_points(b, mat)
        d, _ = tree.query(moved - ca)
        rms = float(np.sqrt((d ** 2).mean()))
        if rms < best[0]:
            best = (rms, mat)
    return best[1], best[0]


def compare(scan: trimesh.Trimesh, ref: trimesh.Trimesh) -> dict:
    ref = ref.copy()
    mat, _ = align(ref, scan)
    ref.apply_transform(mat)
    pts = ref.sample(250000)
    d, _ = cKDTree(pts).query(scan.vertices)
    sign = np.ones(len(d))
    try:                                                      # + when the scan is outside the reference (too fat), - when inside
        if ref.is_watertight:
            sign = np.where(ref.contains(scan.vertices), -1.0, 1.0)
    except Exception:
        pass
    dev = d * sign
    return {"rms_mm": round(float(np.sqrt((d ** 2).mean())), 3), "mean_mm": round(float(d.mean()), 3), "p95_mm": round(float(np.percentile(d, 95)), 3),
            "max_mm": round(float(d.max()), 3), "signed_mean_mm": round(float(dev.mean()), 3), "signed": bool(ref.is_watertight),
            "volume_ratio": round(float(scan.volume / ref.volume), 3) if scan.is_watertight and ref.is_watertight and ref.volume else None,
            "scan_size_mm": [round(float(x), 1) for x in scan.extents], "ref_size_mm": [round(float(x), 1) for x in ref.extents],
            "deviation_mm": np.round(dev, 3).tolist()}
