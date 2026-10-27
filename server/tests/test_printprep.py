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
    parts, log = pp.cut(m, axis, 10)
    assert len(parts) == 2
    assert sum(p.volume for p in parts) == pytest.approx(m.volume, rel=1e-6)
    assert all(p.is_watertight for p in parts)
    assert all(p.bounds[0][2] == pytest.approx(0, abs=1e-6) for p in parts)


def test_cut_low_part_is_the_smaller_side():
    parts, _ = pp.cut(box(), "x", 10)   # 10 mm from the low end of a 40 mm part
    assert parts[0].volume == pytest.approx(10 * 30 * 20) and parts[1].volume == pytest.approx(30 * 30 * 20)


def test_cut_outside_the_part_is_refused():
    with pytest.raises(ValueError):
        pp.cut(box(), "x", 99)


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
    r = c.post("/api/scans/abc/cut", json={"axis": "z", "at": 150}).json()
    assert r["files"] == ["model_part1.stl", "model_part2.stl", "model_parts.3mf"]
    assert c.get("/api/scans/abc/model_part2.stl").status_code == 200
    assert c.post("/api/scans/abc/cut", json={"axis": "z", "at": 999}).status_code == 400
    assert c.get("/api/scans/abc/section.svg?axis=x&at=20").text.startswith("<svg")
    assert c.get("/api/scans/abc/colours").status_code == 404   # no colour information
    main._convert  # a re-convert clears stale parts
    for f in main.PRINT_PREP:
        (d / f).unlink(missing_ok=True)
