"""Flat faces, round bosses and round holes found in a scan (RANSAC on the vertices and their normals), with real sizes in mm.
The start of scan-to-CAD: the numbers are what you type into a CAD sketch. It does not build CAD geometry."""
import numpy as np
import trimesh


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12)


def _sample(mesh: trimesh.Trimesh, n: int = 12000) -> tuple[np.ndarray, np.ndarray]:
    pts, fi = trimesh.sample.sample_surface_even(mesh, n)
    return np.asarray(pts), np.asarray(mesh.face_normals[fi])


def find_planes(pts, nrm, tol=0.6, ang=0.9, min_share=0.03, max_planes=6, rng=None):
    rng = rng or np.random.default_rng(0)
    left = np.ones(len(pts), bool)
    out = []
    for _ in range(max_planes):
        idx = np.flatnonzero(left)
        if len(idx) < 50:
            break
        best = None
        for _ in range(300):
            i = rng.choice(idx)
            n, p = nrm[i], pts[i]
            inl = left & (np.abs((pts - p) @ n) < tol) & (nrm @ n > ang)
            if best is None or inl.sum() > best[0].sum():
                best = (inl, n, p)
        inl, n, p = best
        if inl.sum() < min_share * len(pts):
            break
        n = _unit(nrm[inl].mean(axis=0)); d = float(np.mean(pts[inl] @ n))
        out.append({"normal": [round(float(x), 3) for x in n], "offset_mm": round(d, 2), "share": round(float(inl.sum() / len(pts)), 3)})
        left &= ~inl
    return out, left


def find_cylinders(pts, nrm, mask, tol=0.5, min_pts=120, max_cyl=6, rng=None):
    rng = rng or np.random.default_rng(1)
    left = mask.copy()
    out = []
    for _ in range(max_cyl):
        idx = np.flatnonzero(left)
        if len(idx) < min_pts:
            break
        best = None
        for _ in range(900):
            i, j = rng.choice(idx, 2, replace=False)
            axis = np.cross(nrm[i], nrm[j])
            if np.linalg.norm(axis) < 0.3:
                continue
            axis = _unit(axis)
            # a point of the axis: where the two normal lines (through each point) come closest
            A = np.c_[nrm[i], -nrm[j]]
            try:
                t = np.linalg.lstsq(A, pts[j] - pts[i], rcond=None)[0]
            except np.linalg.LinAlgError:
                continue
            c = pts[i] + t[0] * nrm[i]
            r = abs(t[0])
            if not (1.0 < r < 80.0):
                continue
            rel = pts[idx] - c
            radial = rel - np.outer(rel @ axis, axis)
            dist = np.linalg.norm(radial, axis=1)
            nr = np.abs(np.einsum("ij,ij->i", nrm[idx], radial / np.maximum(dist[:, None], 1e-9)))
            inl = (np.abs(dist - r) < tol) & (nr > 0.85)
            if best is None or inl.sum() > best[0].sum():
                best = (inl, axis, c, r)
        if best is None or best[0].sum() < min_pts:
            break
        inl, axis, c, r = best
        sel = idx[inl]
        rel = pts[sel] - c
        along = rel @ axis
        radial = rel - np.outer(along, axis)
        # refine the radius and centre on the inliers
        r = float(np.linalg.norm(radial, axis=1).mean())
        ang = np.degrees(np.arctan2(radial @ _unit(np.cross(axis, [1, 0, 0]) if abs(axis[0]) < .9 else np.cross(axis, [0, 1, 0])),
                                    radial @ _unit(np.cross(_unit(np.cross(axis, [1, 0, 0]) if abs(axis[0]) < .9 else np.cross(axis, [0, 1, 0])), axis))))
        coverage = float(len(set(np.floor((ang + 180) / 30).astype(int))) / 12)
        outward = float(np.mean(np.einsum("ij,ij->i", nrm[sel], radial / np.maximum(np.linalg.norm(radial, axis=1, keepdims=True), 1e-9))))
        if coverage >= 0.5:                                          # a quarter of a round face is not enough to call it a cylinder
            out.append({"kind": "boss" if outward > 0 else "hole", "diameter_mm": round(2 * r, 2), "length_mm": round(float(along.max() - along.min()), 1),
                        "centre_mm": [round(float(x), 1) for x in (c + axis * (along.max() + along.min()) / 2)],
                        "axis": [round(float(x), 3) for x in axis], "coverage": round(coverage, 2)})
        left[sel] = False
    return out


def detect(mesh: trimesh.Trimesh) -> dict:
    pts, nrm = _sample(mesh)
    planes, rest = find_planes(pts, nrm)
    cyl = []
    for c in find_cylinders(pts, nrm, rest):                      # the same round face found twice is one feature
        a, p = np.array(c["axis"]), np.array(c["centre_mm"])
        if any(abs(float(a @ np.array(o["axis"]))) > 0.97 and abs(c["diameter_mm"] - o["diameter_mm"]) < 0.06 * o["diameter_mm"] + 0.5
               and np.linalg.norm(np.cross(p - np.array(o["centre_mm"]), a)) < 3.0 for o in cyl):
            continue
        cyl.append(c)
    names = {"top": (0, 0, 1), "bottom": (0, 0, -1), "front": (0, -1, 0), "back": (0, 1, 0), "left": (-1, 0, 0), "right": (1, 0, 0)}
    for p in planes:
        n = np.array(p["normal"])
        best = max(names, key=lambda k: float(n @ names[k]))
        p["facing"] = best if float(n @ names[best]) > 0.9 else "slanted"
    return {"size_mm": [round(float(x), 1) for x in mesh.extents], "planes": planes, "cylinders": cyl}
