import io

import numpy as np
import pytest
import trimesh
from PIL import Image

from app import printprep as pp


def box(size=(40, 30, 20)):
    m = trimesh.creation.box(extents=size)
    m.apply_translation([0, 0, size[2] / 2])  # sits on the bed, like model.stl
    return m


@pytest.mark.parametrize("axis", ["x", "y", "z"])
def test_cut_halves_add_up_and_lie_on_the_bed(axis):
    m = box()
    parts, pins, log = pp.cut_all(m, [(axis, 10)])
    assert len(parts) == 2 and not pins
    assert sum(p.volume for p in parts) == pytest.approx(m.volume, rel=1e-6)
    assert all(p.is_watertight for p in parts)
    assert all(p.bounds[0][2] == pytest.approx(0, abs=1e-6) for p in parts)


def test_cut_low_part_is_the_smaller_side():
    parts, _, _ = pp.cut_all(box(), [("x", 10)])   # 10 mm from the low end of a 40 mm part
    assert sorted(round(p.volume) for p in parts) == [10 * 30 * 20, 30 * 30 * 20]


def test_several_cuts_make_every_part_and_keep_the_volume():
    m = box((60, 40, 100))
    parts, _, log = pp.cut_all(m, [("z", 30), ("z", 70), ("x", 30)])
    assert len(parts) == 6
    assert sum(p.volume for p in parts) == pytest.approx(m.volume, rel=1e-6)
    assert all(p.is_watertight and p.bounds[0][2] == pytest.approx(0, abs=1e-6) for p in parts)


@pytest.mark.parametrize("kind", ["dowel", "peg", "key"])
def test_connectors_take_and_add_material_where_expected(kind):
    m = box((60, 40, 100))
    parts, pins, log = pp.cut_all(m, [("z", 50)], kind, size=6, depth=8, count=2)
    assert all(p.is_watertight for p in parts)
    delta = sum(p.volume for p in parts) - m.volume
    if kind == "dowel":
        assert delta < 0 and len(pins) == 2           # two holes in each half, two loose pins
    else:
        assert not pins and abs(delta) < 400           # a peg added to one half, a slightly bigger socket cut from the other
    assert any("2 " in l for l in log)


def test_pegged_part_stands_pegs_up_and_dowel_parts_lie_flat():
    m = box((60, 40, 100))
    pegs, _, log = pp.cut_all(m, [("z", 50)], "peg")
    assert max(p.extents[2] for p in pegs) == pytest.approx(58, abs=0.1) and any("pegs up" in l for l in log)
    flat, _, log = pp.cut_all(m, [("z", 50)], "dowel")
    assert all(p.extents[2] == pytest.approx(50, abs=0.1) for p in flat) and not any("pegs up" in l for l in log)


def test_connector_too_big_for_the_cut_face_is_skipped_not_fatal():
    parts, pins, log = pp.cut_all(box((12, 12, 40)), [("z", 20)], "dowel", size=10)
    assert len(parts) == 2 and not pins and any("too small" in l for l in log)


def test_one_3mf_holds_every_part_and_pin():
    parts, pins, _ = pp.cut_all(box((60, 40, 100)), [("z", 30), ("z", 70)], "dowel")
    scene = pp.lay_out(parts, pp.pin_meshes(pins))
    assert len(scene.geometry) == 3 + len(pins)
    xs = sorted(g.bounds[0][0] for g in scene.geometry.values())
    assert all(b > a for a, b in zip(xs, xs[1:]))      # side by side, none on top of another


def test_section_svg_has_outline_and_hole():
    ring = trimesh.creation.annulus(r_min=5, r_max=10, height=20)
    svg = pp.section_svg(ring, "z", 10)
    assert svg.startswith("<svg") and svg.count(" M") == 1   # outer ring plus one inner ring


def test_two_colour_texture_gives_two_suggestions_and_ignores_unused_texture():
    img = np.zeros((10, 10, 3), np.uint8)
    img[:, :5] = (200, 40, 40)      # red left half
    img[:, 5:] = (40, 90, 190)      # blue right half
    img[:2, :] = (0, 0, 0)          # black strip nothing points at
    buf = io.BytesIO(); Image.fromarray(img).save(buf, "PNG")
    pos, uv = [], []
    for u in (0.25, 0.75):
        pos += [0, 0, 0, 1, 0, 0, 0, 1, 0]
        uv += [u, 0.5, u, 0.5, u, 0.5]
    cols = pp.textured_colours({"positions": pos, "uvs": uv}, buf.getvalue(), k=3)
    assert {c["filament"] for c in cols} == {"Red", "Blue"}
    assert sum(c["percent"] for c in cols) == pytest.approx(100, abs=0.5)


def test_no_coloured_triangles_is_a_plain_no_colours_error():
    buf = io.BytesIO(); Image.new("RGB", (4, 4), (200, 40, 40)).save(buf, "PNG")
    with pytest.raises(pp.NoColours, match="no coloured"):
        pp.textured_colours({"positions": [0.0] * 9, "uvs": [0.0] * 6}, buf.getvalue(), k=2)   # one triangle with no area


def test_routes_cut_section_colours(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "DATA", tmp_path)
    d = tmp_path / "abc"; d.mkdir()
    (d / "meta.json").write_text(json.dumps({"id": "abc", "kind": "object", "created_at": 1, "status": "ok"}))
    box((40, 30, 300)).export(d / "model.stl")
    c = TestClient(main.app)
    info = c.get("/api/scans/abc/print").json()
    assert info["suggested_cut"]["axis"] == "z"
    r = c.post("/api/scans/abc/cut", json={"cuts": [{"axis": "z", "at": 100}, {"axis": "z", "at": 200}],
                                           "connector": {"type": "dowel", "diameter": 6, "depth": 8, "count": 1}}).json()
    assert r["parts"] == 3 and r["pins"] == 2 and r["files"][0] == "model_parts.3mf"
    assert {"model_part3.stl", "dowels.stl"} <= set(r["files"])
    assert c.get("/api/scans/abc/model_part3.stl").status_code == 200
    assert c.get("/api/scans/abc/model_parts.3mf").status_code == 200
    assert len(c.get("/api/scans/abc/print/mesh.json").json()["faces"]) == 36
    r = c.post("/api/scans/abc/cut", json={"cuts": [{"axis": "z", "at": 150}]}).json()   # a new cut replaces the old parts
    assert r["parts"] == 2 and c.get("/api/scans/abc/model_part3.stl").status_code == 404
    assert c.post("/api/scans/abc/cut", json={"cuts": [{"axis": "z", "at": 999}]}).status_code == 400
    assert c.get("/api/scans/abc/section.svg?axis=x&at=20").text.startswith("<svg")
    assert c.get("/api/scans/abc/colours").status_code == 404   # no colour information
    box((10, 10, 10)).export(d / "colored.ply")                  # a PLY without vertex colours: no made-up 'Light grey'
    r = c.get("/api/scans/abc/colours")
    assert r.status_code == 422 and "no colours" in r.json()["detail"], r.text
    assert c.get("/api/scans/abc/model_colour.3mf").status_code == 422
    (d / "colored.ply").unlink()
    main._clear_print_prep(d)   # what a re-convert does: the parts no longer match model.stl
    assert not list(d.glob("model_part*")) and not (d / "dowels.stl").exists() and (d / "model.stl").exists()


def test_tolerance_bar_has_a_hole_per_clearance_and_matching_pins():
    import io
    scene = pp.tolerance_test(6.0, 8.0)
    names = list(scene.geometry)
    assert len(names) == 3                                         # the bar and two pins
    bar = scene.geometry[names[0]]
    assert bar.is_watertight and abs(bar.bounds[0][2]) < 1e-6
    # five blind holes: removing them leaves less volume than the solid bar
    solid = bar.extents[0] * bar.extents[1] * bar.extents[2]
    removed = solid - bar.volume
    holes = sum(np.pi * (3 + c) ** 2 * 8 for c in pp.CLEARANCE_STEPS)
    assert holes * 0.9 < removed < holes * 1.2, (removed, holes)
    assert all(scene.geometry[n].is_watertight for n in names)


def test_fit_route_compares_with_a_reference(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "DATA", tmp_path)
    d = tmp_path / "abc"
    d.mkdir()
    box = trimesh.creation.box(extents=(40, 25, 15))
    box.apply_translation([0, 0, 7.5])
    box.export(d / "model.stl")
    (d / "meta.json").write_text('{"id":"abc","kind":"mesh"}')
    ref = io.BytesIO(); trimesh.creation.box(extents=(40, 25, 15)).export(ref, file_type="stl")
    r = TestClient(main.app).post("/api/scans/abc/fit", files={"reference": ("ref.stl", ref.getvalue())})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rms_mm"] < 0.3 and len(body["deviation_mm"]) * 3 == len(body["vertices"])
    assert TestClient(main.app).post("/api/scans/abc/fit", files={"reference": ("ref.txt", b"x")}).status_code == 400


def test_autofit_cuts_a_long_part_to_the_bed_and_estimates():
    m = trimesh.creation.box(extents=(230, 40, 30))
    m.apply_translation(-m.bounds[0])
    cuts = pp.auto_cuts([230, 40, 30], [100, 100, 100])
    assert [c[0] for c in cuts] == ["x", "x"] and abs(cuts[0][1] - 230 / 3) < 1e-6
    e = pp.estimate(m)
    assert 0 < e["grams"] < m.volume * 0.00124 and e["supports"] == "none" and e["hours"] > 0
    arch = trimesh.creation.box(extents=(40, 40, 10)); arch.apply_translation([0, 0, 25])     # a floating slab: its underside hangs over
    assert pp.estimate(arch)["supports"] == "yes"


def test_usdz_is_real_size_and_passes_the_arkit_check(tmp_path):
    from pxr import Usd, UsdGeom, UsdUtils
    from app import usdzout
    m = trimesh.creation.box(extents=(0.05, 0.1, 0.03)); m.apply_translation([0, 0.05, 0])
    m.visual.vertex_colors = np.tile(np.array([200, 30, 30, 255], dtype=np.uint8), (len(m.vertices), 1))
    m.export(tmp_path / "colored.ply")
    trimesh.creation.box(extents=(50, 100, 30)).export(tmp_path / "model.stl")
    out = usdzout.make_usdz(tmp_path, tmp_path / "model.usdz")
    stage = Usd.Stage.Open(str(out))
    assert UsdGeom.GetStageMetersPerUnit(stage) == 1.0 and UsdGeom.GetStageUpAxis(stage) == "Y"
    pts = np.array(UsdGeom.Mesh(stage.GetPrimAtPath("/Model/Mesh")).GetPointsAttr().Get())
    assert abs(np.ptp(pts[:, 1]) - 0.1) < 1e-4
    import zipfile
    from pxr import UsdShade
    z = zipfile.ZipFile(out)
    assert z.namelist()[0].endswith(".usdc") and all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())   # Quick Look wants it stored, not compressed
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/Model/Mesh"))
    assert UsdShade.MaterialBindingAPI(mesh).ComputeBoundMaterial()[0]
    assert len(UsdGeom.PrimvarsAPI(mesh).GetPrimvar("displayColor").Get()) == len(pts)


def test_colour_3mf_gives_every_triangle_a_filament(tmp_path):
    import re
    import zipfile
    box = trimesh.creation.box(extents=(20, 20, 20))
    box.export(tmp_path / "model.stl")
    v = box.vertices
    c = trimesh.Trimesh(np.c_[v[:, 0], v[:, 2], -v[:, 1]] / 1000.0, box.faces, process=False)      # metres, Y up, as the scans' colored.ply
    c.visual.vertex_colors = np.where(v[:, [2]] > 0, [200, 40, 40, 255], [30, 30, 30, 255]).astype(np.uint8)
    c.export(tmp_path / "colored.ply")
    data, used = pp.colour_3mf(tmp_path, 2)
    names = {u["filament"] for u in used}
    assert names == {"Red", "Black"} and abs(sum(u["percent"] for u in used) - 100) < 0.5
    xml = zipfile.ZipFile(io.BytesIO(data)).read("3D/3dmodel.model").decode()
    assert len(re.findall(r"<triangle ", xml)) == len(box.faces) and xml.count("<base ") == 2
    back = trimesh.load(io.BytesIO(data), file_type="3mf")                      # still a normal 3MF to every other reader
    assert abs(back.bounds[1][0] - 10) < 1e-3


def test_features_route_and_scan_to_scan_fit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "DATA", tmp_path)
    for sid, scale in (("a1", 1.0), ("b2", 1.04)):
        d = tmp_path / sid
        d.mkdir()
        (d / "meta.json").write_text('{"id":"%s","kind":"mesh"}' % sid)
        bar = trimesh.creation.box(extents=(60, 30, 20))
        bar.apply_scale(scale); bar.apply_translation(-bar.bounds[0]); bar.export(d / "model.stl")
    c = TestClient(main.app)
    f = c.get("/api/scans/a1/features").json()
    assert {p["facing"] for p in f["planes"]} >= {"top", "bottom"} and (tmp_path / "a1" / "features.json").exists()
    r = c.post("/api/scans/b2/fit", data={"against": "a1"})
    assert r.status_code == 200 and r.json()["signed_mean_mm"] > 0.3          # the second scan is 4% bigger
    assert c.post("/api/scans/b2/fit", data={"against": "nope"}).status_code == 404
