"""Cuts on any plane (the Orca-style dialog): tilted planes, chosen connector spots and shapes, keep/discard/flip, the preview."""
import json

import numpy as np
import pytest
import trimesh

from app import printprep as pp


def box(size=(60, 40, 100)):
    m = trimesh.creation.box(extents=size)
    m.apply_translation([0, 0, size[2] / 2])  # sits on the bed, like model.stl
    return m


TILT = (np.array([0.0, 0.0, 50.0]), np.array([1.0, 0.0, 1.0]) / np.sqrt(2))


def plane(points=None, normal=None):
    return dict(origin=TILT[0], normal=TILT[1] if normal is None else normal, points=points)


def test_tilted_plane_gives_two_watertight_parts_that_add_up():
    m = box()
    parts, pins, log, views = pp.cut_planes(m, [plane()])
    assert len(parts) == 2 and not pins
    assert all(p.is_watertight for p in parts)
    assert sum(p.volume for p in parts) == pytest.approx(m.volume, rel=0.01)
    assert [s for s, _ in views] == ["lower", "upper"]
    # views stay in place: together they are the original box
    assert sum(v.volume for _, v in views) == pytest.approx(m.volume, rel=0.01)


def test_preview_gives_the_cut_face_in_plane_coordinates_and_auto_spots():
    m = box()
    r = pp.cut_preview(m, *TILT, kind="dowel", size=6, count=2)
    assert len(r["polygons"]) == 1 and not r["polygons"][0]["holes"]
    assert r["area_mm2"] == pytest.approx(40 * 60 * np.sqrt(2), rel=1e-3)   # 40 wide, 60*sqrt(2) long on the slant
    u, v, n = (np.array(r[k]) for k in ("u", "v", "normal"))
    assert abs(u @ v) < 1e-6 and abs(u @ n) < 1e-6 and np.linalg.norm(u) == pytest.approx(1)
    assert len(r["points"]) == 2 and r["fits"] and r["max_size_mm"] > 20
    o = np.array(r["origin"])
    for pu, pv in r["points"]:                                   # every spot is on the plane and inside the box
        p = o + pu * u + pv * v
        assert abs((p - o) @ n) < 1e-6 and np.all(np.abs(p[:2]) <= [30, 20]) and 0 <= p[2] <= 100
    assert pp.cut_preview(m, *TILT, kind="none")["points"] == []


def test_chosen_spots_are_used_and_spots_too_close_to_the_edge_are_skipped():
    m = box()
    parts, pins, log, _ = pp.cut_planes(m, [plane(points=[(0.0, 0.0), (10.0, 5.0), (-8.0, -5.0)])], "dowel", size=6, depth=6)
    assert len(pins) == 3 and all(p.is_watertight for p in parts)
    assert any("3 dowel hole pairs" in l for l in log)
    parts, pins, log, _ = pp.cut_planes(m, [plane(points=[(0.0, 0.0), (500.0, 500.0)])], "dowel", size=6)
    assert len(pins) == 1 and any("skipped" in l for l in log)
    with pytest.raises(ValueError, match="does not fit"):
        pp.cut_planes(m, [plane(points=[(500.0, 500.0)])], "dowel", size=6)


@pytest.mark.parametrize("shape", ["circle", "square", "triangle", "hexagon"])
def test_every_shape_cuts_holes_and_pins_that_match(shape):
    m = box()
    parts, pins, log, _ = pp.cut_planes(m, [plane()], "dowel", shape=shape, size=8, depth=6, count=1)
    assert all(p.is_watertight for p in parts) and len(pins) == 1 and pins[0][2] == shape
    assert sum(p.volume for p in parts) < m.volume
    loose = pp.pin_meshes(pins)
    assert len(loose) == 1 and loose[0].is_watertight
    parts, pins, log, _ = pp.cut_planes(m, [plane()], "peg", shape=shape, size=8, depth=6, count=1)
    assert all(p.is_watertight for p in parts) and not pins


def test_keep_discard_and_flip():
    m = box()
    parts, _, log, views = pp.cut_planes(m, [plane()], keep_upper=False)
    assert len(parts) == 1 and [s for s, _ in views] == ["lower"]
    parts, _, log, views = pp.cut_planes(m, [plane()], keep_lower=False)
    assert len(parts) == 1 and [s for s, _ in views] == ["upper"]
    with pytest.raises(ValueError, match="both parts are discarded"):
        pp.cut_planes(m, [plane()], keep_upper=False, keep_lower=False)
    # connectors need both parts
    _, pins, log, _ = pp.cut_planes(m, [plane()], "dowel", keep_upper=False)
    assert not pins and any("discarded" in l for l in log)
    # flipped: the cut face (the big slanted face) ends up on top instead of on the bed
    def top_face(part):
        up = (part.face_normals[:, 2] > 0.99) & (part.triangles_center[:, 2] > part.bounds[1][2] - 0.1)
        return part.area_faces[up].sum()
    flat, _, _, _ = pp.cut_planes(m, [plane()], keep_lower=False)
    turned, _, log, _ = pp.cut_planes(m, [plane()], keep_lower=False, flip_upper=True)
    assert top_face(turned[0]) == pytest.approx(40 * 60 * np.sqrt(2), rel=0.01) and top_face(flat[0]) < 1
    assert any("turned over" in l for l in log)
    with pytest.raises(ValueError, match="one cut plane"):
        pp.cut_planes(m, [plane(), dict(origin=[0, 0, 80.0], normal=[0, 0, 1.0], points=None)], flip_upper=True)


def test_two_planes_and_a_plane_that_misses():
    m = box()
    parts, _, _, views = pp.cut_planes(m, [plane(), dict(origin=[0, 0, 30.0], normal=[0, 0, 1.0], points=None)])
    assert len(parts) == 4 and sum(p.volume for p in parts) == pytest.approx(m.volume, rel=0.01)
    with pytest.raises(ValueError, match="misses the part"):
        pp.cut_planes(m, [dict(origin=[0, 0, 400.0], normal=[0, 0, 1.0], points=None)])
    with pytest.raises(ValueError, match="no direction"):
        pp.cut_planes(m, [dict(origin=[0, 0, 50.0], normal=[0, 0, 0.0], points=None)])


def test_routes_preview_and_plane_cut(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "DATA", tmp_path)
    d = tmp_path / "abc"; d.mkdir()
    (d / "meta.json").write_text(json.dumps({"id": "abc", "kind": "object", "created_at": 1, "status": "ok"}))
    box().export(d / "model.stl")
    c = TestClient(main.app)
    pl = {"origin": [0, 0, 50], "normal": [1, 0, 1]}
    r = c.post("/api/scans/abc/cut/preview", json={"plane": pl, "connector": {"type": "dowel", "size": 6, "count": 2}})
    assert r.status_code == 200
    pv = r.json()
    assert len(pv["polygons"]) == 1 and len(pv["points"]) == 2 and len(pv["u"]) == 3
    # the spots the preview showed (edited: one moved) go back to the cut unchanged
    spots = [pv["points"][0], [0.0, 3.0]]
    r = c.post("/api/scans/abc/cut/plane", json={"planes": [{**pl, "connectors": spots}], "connector": {"type": "dowel", "shape": "hexagon", "size": 6, "depth": 6}})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["parts"] == 2 and j["pins"] == 2 and j["files"][0] == "model_parts.3mf" and {"dowels.stl", "model_part2.stl"} <= set(j["files"])
    assert [v["side"] for v in j["views"]] == ["lower", "upper"] and len(j["views"][0]["faces"]) % 3 == 0
    assert c.get("/api/scans/abc/model_part2.stl").status_code == 200
    # discard one side: a single part, the old part 2 file is gone
    r = c.post("/api/scans/abc/cut/plane", json={"planes": [pl], "connector": {"type": "none"}, "keep": {"upper": False, "lower": True}})
    assert r.status_code == 200 and r.json()["parts"] == 1 and c.get("/api/scans/abc/model_part2.stl").status_code == 404
    # clear refusals
    r = c.post("/api/scans/abc/cut/plane", json={"planes": [{"origin": [0, 0, 500], "normal": [0, 0, 1]}], "connector": {"type": "none"}})
    assert r.status_code == 400 and "misses" in r.json()["detail"]
    sliver = {"origin": [30, 0, 95], "normal": [1, 0, 1]}   # a 40 x 7 mm cut face near the corner
    r = c.post("/api/scans/abc/cut/plane", json={"planes": [sliver], "connector": {"type": "dowel", "size": 6}})
    assert r.status_code == 400 and "does not fit" in r.json()["detail"]
    assert c.post("/api/scans/abc/cut/plane", json={"planes": [sliver], "connector": {"type": "none"}}).status_code == 200
    r = c.post("/api/scans/abc/cut/preview", json={"plane": {"origin": [0, 0, 500], "normal": [0, 0, 1]}})
    assert r.status_code == 400 and "does not touch" in r.json()["detail"]
    r = c.post("/api/scans/abc/cut/plane", json={"planes": [pl], "keep": {"upper": False, "lower": False}})
    assert r.status_code == 400
    r = c.post("/api/scans/abc/cut/plane", json={"planes": [{"origin": [0, 0], "normal": [0, 0, 1]}]})
    assert r.status_code == 422


def test_a_cut_through_many_islands_gets_a_bounded_number_of_connectors():
    import time
    cols = [trimesh.creation.box(extents=(12, 12, 40)) for _ in range(36)]
    for i, c in enumerate(cols):                       # a 6 x 6 forest of posts: the cut face has 36 islands
        c.apply_translation([(i % 6) * 20.0, (i // 6) * 20.0, 20.0])
    m = trimesh.util.concatenate(cols)
    assert m.is_watertight
    polys, _ = pp.section_polygons(m, [0, 0, 20.0], [0, 0, 1.0])
    assert len(polys) == 36
    t = time.monotonic()
    parts, pins, log, _ = pp.cut_planes(m, [dict(origin=[0, 0, 20.0], normal=[0, 0, 1.0], points=None)], "dowel", size=4, depth=4, count=2)
    assert len(pins) <= pp.MAX_FACE_CONNECTORS and len(pins) >= pp.MAX_ISLANDS
    assert any("36 separate pieces" in line for line in log), log
    assert time.monotonic() - t < 60
