"""The model as a USDZ in real size, for AR Quick Look on the iPhone ("View in AR": the part on your desk at 1:1)."""
import tempfile
from pathlib import Path

import numpy as np
import trimesh
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, UsdUtils, Vt


def _source(d: Path) -> tuple[trimesh.Trimesh, float, str]:
    """(mesh, metres per unit, up axis). The coloured mesh is in metres, Y up; an uploaded model only has model.stl in mm, Z up."""
    colored, stl = d / "colored.ply", d / "model.stl"
    if colored.exists() and stl.exists():
        m = trimesh.load(colored, process=False)
        if isinstance(m, trimesh.Trimesh) and len(m.faces):
            return m, 1.0, "Y"
    if stl.exists():
        return trimesh.load(stl, process=False), 0.001, "Z"
    raise FileNotFoundError("no model to show in AR")


def make_usdz(d: Path, out: Path) -> Path:
    m, unit, up = _source(d)
    work = Path(tempfile.mkdtemp())
    usdc = work / "model.usdc"
    stage = Usd.Stage.CreateNew(str(usdc))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y if up == "Y" else UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, unit)
    root = UsdGeom.Xform.Define(stage, "/Model")
    stage.SetDefaultPrim(root.GetPrim())
    mesh = UsdGeom.Mesh.Define(stage, "/Model/Mesh")
    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(np.asarray(m.vertices, dtype=np.float32)))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.full(len(m.faces), 3, dtype=np.int32)))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(np.asarray(m.faces, dtype=np.int32).ravel()))
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    mesh.CreateDoubleSidedAttr(True)
    coloured = m.visual.kind == "vertex" and len(m.visual.vertex_colors) == len(m.vertices)
    cols = (np.asarray(m.visual.vertex_colors)[:, :3] / 255.0).astype(np.float32) if coloured else np.tile(np.float32([0.8, 0.8, 0.8]), (len(m.vertices), 1))
    pv = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.vertex)
    pv.Set(Vt.Vec3fArray.FromNumpy(cols))
    mat = UsdShade.Material.Define(stage, "/Model/Look")
    shader = UsdShade.Shader.Define(stage, "/Model/Look/Surface")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.8)
    reader = UsdShade.Shader.Define(stage, "/Model/Look/Colour")
    reader.CreateIdAttr("UsdPrimvarReader_float3")
    reader.CreateInput("varname", Sdf.ValueTypeNames.String).Set("displayColor")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(reader.ConnectableAPI(), "result")
    mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(mat)
    stage.GetRootLayer().Save()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(usdc)), str(out))
    return out
