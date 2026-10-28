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


def test_cut_outside_the_part_is_refused():
    with pytest.raises(ValueError):
        pp.cut_all(box(), [("x", 99)])


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


def test_suggest_cut_only_when_it_does_not_fit():
    assert pp.suggest_cut([100, 100, 100]) is None
    s = pp.suggest_cut([100, 100, 400])
    assert s["axis"] == "z" and s["at"] == 200


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


def test_tolerance_route_returns_a_3mf():
    from fastapi.testclient import TestClient
    from app import main
    r = TestClient(main.app).get("/api/print/tolerance.3mf?size=6&depth=8")
    assert r.status_code == 200 and r.content[:2] == b"PK"
