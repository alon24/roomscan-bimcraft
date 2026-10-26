"""Object Capture USDZ -> printable mesh (STL / 3MF). Output units: millimetres, Z up, sitting on z=0."""
import json
from pathlib import Path

import numpy as np
import trimesh
from pxr import Usd, UsdGeom


def load_usdz(path: Path) -> trimesh.Trimesh:
    """Merge every UsdGeom.Mesh in the stage into one triangle mesh, in mm and Z-up."""
    stage = Usd.Stage.Open(str(path))
    if stage is None:
        raise ValueError("cannot open USD stage")
    mm = (UsdGeom.GetStageMetersPerUnit(stage) or 1.0) * 1000.0
    cache = UsdGeom.XformCache()
    verts, faces, base = [], [], 0
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Mesh):
            continue
        mesh = UsdGeom.Mesh(prim)
        pts = np.array(mesh.GetPointsAttr().Get() or [], dtype=float)
        counts = mesh.GetFaceVertexCountsAttr().Get()
        idx = mesh.GetFaceVertexIndicesAttr().Get()
        if len(pts) == 0 or not counts or not idx:
            continue
        m = np.array(cache.GetLocalToWorldTransform(prim))  # row-vector convention
        pts = (np.c_[pts, np.ones(len(pts))] @ m)[:, :3]
        i = 0
        for c in counts:  # fan-triangulate polygons
            for k in range(1, c - 1):
                faces.append([base + idx[i], base + idx[i + k], base + idx[i + k + 1]])
            i += c
        verts.append(pts)
        base += len(pts)
    if not faces:
        raise ValueError("USDZ contains no mesh geometry")
    v = np.vstack(verts) * mm
    if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y:  # (x, y, z) -> (x, -z, y)
        v = np.c_[v[:, 0], -v[:, 2], v[:, 1]]
    return trimesh.Trimesh(v, np.array(faces), process=False)


def clean(mesh: trimesh.Trimesh, scale: float = 1.0) -> tuple[trimesh.Trimesh, list[str]]:
    """Make the mesh printable where possible: keep the largest piece, close holes, centre on the bed."""
    log: list[str] = []
    mesh = mesh.copy()
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    parts = mesh.split(only_watertight=False)
    if len(parts) > 1:
        mesh = max(parts, key=lambda p: len(p.faces))
        log.append(f"kept largest of {len(parts)} pieces (dropped {len(parts) - 1})")
    if not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
        log.append("filled holes: " + ("now watertight" if mesh.is_watertight else "still not watertight"))
    trimesh.repair.fix_normals(mesh)
    if scale != 1.0:
        mesh.apply_scale(scale)
        log.append(f"scaled by {scale}")
    lo, hi = mesh.bounds
    mesh.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return mesh, log


def report(mesh: trimesh.Trimesh) -> dict:
    return {
        "watertight": bool(mesh.is_watertight),
        "volume_mm3": round(float(mesh.volume), 1) if mesh.is_watertight else None,
        "size_mm": [round(float(x), 2) for x in mesh.extents],
        "faces": int(len(mesh.faces)),
    }


def convert_object(src: Path, outdir: Path, scale: float = 1.0) -> tuple[bool, str]:
    """Write model.stl, model.3mf and preview.json into outdir. Returns (ok, log)."""
    try:
        mesh, log = clean(load_usdz(src), scale)
        mesh.export(outdir / "model.stl")
        mesh.export(outdir / "model.3mf")
        (outdir / "preview.json").write_text(json.dumps({
            "vertices": np.round(mesh.vertices, 2).ravel().tolist(),
            "faces": mesh.faces.ravel().tolist()}))
        rep = report(mesh)
        log.append(json.dumps(rep))
        (outdir / "report.json").write_text(json.dumps(rep))
        return rep["watertight"], "\n".join(log) + ("" if rep["watertight"] else "\nWARNING: mesh is not watertight; slicer may need repair")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
