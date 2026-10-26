import json

import numpy as np
import pytest
import trimesh
from pxr import Gf, Usd, UsdGeom, UsdUtils

from app import objects


def make_usdz(path, cube_m=0.03, extra_speck=False, y_up=True, open_top=False):
    """A cube of cube_m metres (usdz default metersPerUnit=1) as a USDZ, placed away from the origin."""
    usda = path.with_suffix(".usda")
    stage = Usd.Stage.CreateNew(str(usda))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y if y_up else UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    box = trimesh.creation.box(extents=[cube_m] * 3)
    faces = box.faces[:-2] if open_top else box.faces
    parts = [(box.vertices, faces)]
    if extra_speck:
        s = trimesh.creation.box(extents=[0.001] * 3)
        parts.append((s.vertices + 0.5, s.faces))
    for n, (v, f) in enumerate(parts):
        m = UsdGeom.Mesh.Define(stage, f"/Root/m{n}")
        m.GetPointsAttr().Set([Gf.Vec3f(*map(float, p)) for p in v])
        m.GetFaceVertexCountsAttr().Set([3] * len(f))
        m.GetFaceVertexIndicesAttr().Set([int(i) for i in np.asarray(f).ravel()])
        UsdGeom.Xformable(m).AddTranslateOp().Set(Gf.Vec3d(1, 2, 3))
    stage.GetRootLayer().Save()
    UsdUtils.CreateNewUsdzPackage(str(usda), str(path))
    return path


def test_load_units_and_transform(tmp_path):
    mesh = objects.load_usdz(make_usdz(tmp_path / "a.usdz"))
    assert np.allclose(mesh.extents, [30, 30, 30], atol=0.01)  # 0.03 m -> 30 mm
    assert mesh.is_watertight


def test_y_up_converted_to_z_up(tmp_path):
    p = make_usdz(tmp_path / "a.usdz", y_up=True)
    m = objects.load_usdz(p)
    # translate (1,2,3) m in Y-up -> (x, -z, y) = (1000, -3000, 2000) mm centre
    assert np.allclose(m.bounds.mean(axis=0), [1000, -3000, 2000], atol=0.01)


def test_clean_drops_speck_and_sits_on_bed(tmp_path):
    m, log = objects.clean(objects.load_usdz(make_usdz(tmp_path / "a.usdz", extra_speck=True)))
    assert any("kept largest" in l for l in log)
    assert np.isclose(m.bounds[0][2], 0) and np.allclose(m.bounds.mean(axis=0)[:2], 0)
    assert np.allclose(m.extents, 30, atol=0.01)


def test_clean_fills_hole(tmp_path):
    m, log = objects.clean(objects.load_usdz(make_usdz(tmp_path / "a.usdz", open_top=True)))
    assert m.is_watertight


def test_scale(tmp_path):
    m, _ = objects.clean(objects.load_usdz(make_usdz(tmp_path / "a.usdz")), scale=2)
    assert np.allclose(m.extents, 60, atol=0.01)


def test_convert_object_writes_files(tmp_path):
    ok, log = objects.convert_object(make_usdz(tmp_path / "a.usdz"), tmp_path)
    assert ok, log
    for f in ("model.stl", "model.3mf", "preview.json", "report.json"):
        assert (tmp_path / f).stat().st_size > 0
    assert trimesh.load(tmp_path / "model.stl").is_watertight
    assert json.loads((tmp_path / "report.json").read_text())["size_mm"] == [30.0, 30.0, 30.0]


def test_garbage_input_fails_cleanly(tmp_path):
    bad = tmp_path / "bad.usdz"
    bad.write_bytes(b"not a usdz")
    ok, log = objects.convert_object(bad, tmp_path)
    assert not ok and log
