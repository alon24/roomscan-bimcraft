"""The rule: a fix is stored as a fix, and rebuilding can never undo it."""
import json
import math

import pytest

from app import elements, layout, walk

ROOM = [[0.0, 0.0], [6.0, 0.0], [6.0, 4.0], [0.0, 4.0]]


def click(pid, x, y, kind="wall"):
    return {"id": pid, "x": x, "y": 0.0, "z": -y, "kind": kind, "tag": kind}


def room(d, points=(), edited=False, turn=0.0, openings=()):
    (d / "layout.json").write_text(json.dumps({"vertices": [layout._rotate(p, turn) for p in ROOM], "open": [False] * 4, "openings": list(openings), "height_m": 2.5, "turn_deg": turn, "edited": edited, "floor_y": -1.4, "ceiling_y": 1.1}))
    (d / "walk_result.json").write_text(json.dumps({"points": list(points), "floor_y": -1.4, "ceiling_y": 1.1}))
    (d / "walk_points.json").write_text(json.dumps([dict(p) for p in points]))
    return elements.view(d)


def edit(d, pid, **change):
    before = elements.snapshot(d, points=True)
    result = json.loads((d / "walk_result.json").read_text())
    raw = json.loads((d / "walk_points.json").read_text())
    for group in (result["points"], raw):
        next(p for p in group if p["id"] == pid).update(change)
    (d / "walk_result.json").write_text(json.dumps(result))
    (d / "walk_points.json").write_text(json.dumps(raw))
    elements.point_changed(d, "edit_point", {"id": pid}, before, pid)


def test_a_plan_keeps_its_shape_when_it_gets_a_model(tmp_path):
    for turn in (0.0, 36.937):
        v = room(tmp_path, [click("a", 1, 0.05)], edited=True, turn=turn)
        assert v["vertices"] == [layout._rotate(p, turn) for p in ROOM]
        assert len(v["walls"]) == 4 and v["walls"][0]["points"] == ["a"] and v["walls"][0]["pinned"]
        (tmp_path / "model.json").unlink()


def test_a_wall_nobody_fixed_follows_its_points_and_a_confirmed_one_does_not(tmp_path):
    v = room(tmp_path, [click("a", 1, 0.0), click("b", 5, 0.0), click("c", 6, 1), click("d", 6, 3)])
    south, east = v["walls"][0]["id"], v["walls"][1]["id"]
    elements.act(tmp_path, "confirm_wall", {"wall": east})
    edit(tmp_path, "a", z=-0.2)          # the south wall's point moved by 20 cm
    edit(tmp_path, "b", z=-0.2)
    edit(tmp_path, "c", x=6.5)           # the confirmed east wall's point moved
    edit(tmp_path, "d", x=6.5)
    got = elements.view(tmp_path)["vertices"]
    assert got[0][1] == pytest.approx(0.2, abs=0.01) and got[1][1] == pytest.approx(0.2, abs=0.01)
    assert got[1][0] == pytest.approx(6.0, abs=0.01) and got[2][0] == pytest.approx(6.0, abs=0.01)
    assert elements.view(tmp_path)["walls"][0]["id"] == south


def test_making_the_walls_again_never_moves_a_confirmed_wall(tmp_path, monkeypatch):
    v = room(tmp_path, [click("a", 1, 0.0), click("b", 5, 0.0)])
    elements.act(tmp_path, "confirm_wall", {"wall": v["walls"][1]["id"]})
    monkeypatch.setattr(walk, "build", lambda *_: pytest.fail("a confirmed wall must not be rebuilt"))
    out = elements.propose(tmp_path, [])
    assert out["kept_confirmed"] == 1
    assert elements.view(tmp_path)["vertices"][1] == [6.0, 0.0]
    assert elements.locked(tmp_path)


def test_dragging_a_corner_moves_the_walls_and_keeps_what_is_known_about_them(tmp_path):
    v = room(tmp_path)
    ids = [w["id"] for w in v["walls"]]
    elements.act(tmp_path, "confirm_wall", {"wall": ids[2]})
    moved = [[0.0, 0.0], [6.5, 0.0], [6.5, 4.0], [0.0, 4.0]]
    lay = {"vertices": moved, "open": [False] * 4, "openings": [], "height_m": 2.5, "turn_deg": 0.0, "edited": True}
    got = elements.put_plan(tmp_path, lay)
    assert got["vertices"] == moved
    assert [w["id"] for w in got["walls"]] == ids and got["walls"][2]["confirmed"]
    m = json.loads((tmp_path / "model.json").read_text())
    assert m["walls"][1]["line"][0] == [6.5, 0.0]


def test_a_stretch_moved_back_gets_a_short_wall_joining_it(tmp_path):
    """Wall 5's case: one line through two surfaces. Split it, move one part."""
    v = room(tmp_path)
    north = v["walls"][2]["id"]
    got = elements.act(tmp_path, "split_wall", {"wall": north, "at": [3.0, -4.0]})
    assert len(got["walls"]) == 5 and got["vertices"][2] == [6.0, 4.0]
    kitchen = got["walls"][3]["id"]
    got = elements.act(tmp_path, "move_wall", {"wall": kitchen, "a": [2.5, -3.0], "b": [0.5, -3.0]})   # given backwards: the wall's direction is kept
    assert len(got["walls"]) == 6
    assert sorted(map(tuple, got["vertices"])) == sorted(map(tuple, [[0.0, 0.0], [6.0, 0.0], [6.0, 4.0], [3.0, 4.0], [3.0, 3.0], [0.0, 3.0]]))
    assert got["problem"] is None


def test_a_wall_can_be_open_or_not_a_wall_and_a_room_keeps_three(tmp_path):
    v = room(tmp_path)
    got = elements.act(tmp_path, "set_open", {"wall": v["walls"][1]["id"]})
    assert got["open"] == [False, True, False, False]
    with pytest.raises(ValueError):
        elements.act(tmp_path, "split_wall", {"wall": v["walls"][0]["id"], "at": [0.05, 0.0]})
    got = elements.act(tmp_path, "not_a_wall", {"wall": v["walls"][0]["id"]})
    assert len(got["walls"]) == 3
    with pytest.raises(ValueError):
        elements.act(tmp_path, "not_a_wall", {"wall": got["walls"][0]["id"]})


def test_a_taped_length_comes_true_and_moves_a_confirmed_wall_least(tmp_path):
    v = room(tmp_path)
    ids = [w["id"] for w in v["walls"]]
    elements.act(tmp_path, "confirm_wall", {"wall": ids[3]})       # the west wall stays
    got = elements.act(tmp_path, "set_length", {"wall": ids[0], "m": 5.95})
    assert math.dist(got["vertices"][0], got["vertices"][1]) == pytest.approx(5.95, abs=0.005)
    assert got["vertices"][0][0] == pytest.approx(0.0, abs=0.01) and got["vertices"][1][0] == pytest.approx(5.95, abs=0.01)
    assert not got["checks"]
    got = elements.act(tmp_path, "set_length", {"wall": ids[2], "m": 5.0})    # the opposite wall: both can't be true
    assert any("don't all fit" in c["text"] for c in got["checks"])
    got = elements.act(tmp_path, "set_length", {"wall": ids[2], "m": None})
    assert not got["checks"]


def test_undo_works_in_a_later_session_and_also_for_points(tmp_path):
    v = room(tmp_path, [click("a", 1, 0.0), click("b", 5, 0.0)])
    start = elements.view(tmp_path)["vertices"]
    elements.act(tmp_path, "set_open", {"wall": v["walls"][0]["id"]})
    edit(tmp_path, "a", z=-0.3)
    assert elements.view(tmp_path)["undo"] >= 2
    after = elements.undo(tmp_path)
    assert json.loads((tmp_path / "walk_result.json").read_text())["points"][0]["z"] == 0
    assert after["open"][0] is True
    after = elements.undo(tmp_path)
    assert after["open"][0] is False and after["vertices"] == start
    assert "set_open" in (tmp_path / "edits_history.jsonl").read_text()     # the record stays


def test_saying_which_points_make_a_wall(tmp_path):
    v = room(tmp_path, [click("a", 1, 0.9), click("b", 5, 0.9)], edited=True)
    south = v["walls"][0]
    assert v["loose"] == ["a", "b"] and "a" in south["near"]
    elements.act(tmp_path, "assign_point", {"point": "a", "wall": south["id"]})
    got = elements.act(tmp_path, "assign_point", {"point": "b", "wall": south["id"]})
    assert got["vertices"][0][1] == pytest.approx(0.9, abs=0.01) and not got["walls"][0]["pinned"]
    got = elements.act(tmp_path, "assign_point", {"point": "b", "wall": None})
    assert "b" in got["loose"] and "b" not in got["walls"][0]["points"]


def test_a_door_point_is_not_a_wall_point(tmp_path):
    v = room(tmp_path, [click("a", 1, 0.0), click("b", 5, 0.0)])
    assert "a" in v["walls"][0]["points"]
    edit(tmp_path, "a", kind="door")
    got = elements.view(tmp_path)
    assert "a" not in got["walls"][0]["points"]
    assert [o["kind"] for o in got["openings"]] == ["door"]
    edit(tmp_path, "a", kind="wall")
    assert elements.view(tmp_path)["openings"] == [] and "a" in elements.view(tmp_path)["walls"][0]["points"]


def test_joining_a_scan_behind_the_models_back_is_taken_in(tmp_path):
    v = room(tmp_path)
    grown = json.loads((tmp_path / "layout.json").read_text())
    grown["vertices"] = [[0.0, 0.0], [6.0, 0.0], [6.0, 5.0], [0.0, 5.0]]
    (tmp_path / "layout.json").write_text(json.dumps(grown))
    got = elements.view(tmp_path)
    assert got["walls"][0]["id"] == v["walls"][0]["id"] and got["vertices"] == grown["vertices"]


def test_an_unknown_action_is_refused(tmp_path):
    room(tmp_path)
    with pytest.raises(ValueError):
        elements.act(tmp_path, "nonsense", {})


def test_every_button_on_the_layout_page_does_something():
    """A rewrite once dropped Undo, Height, Build and Back to the scan's walls without anyone noticing."""
    import re
    from pathlib import Path
    page = (Path(__file__).parent.parent / "app/static/layout.html").read_text()
    html, script = page.split("<script>", 1)
    ids = re.findall(r'<button\b[^>]*\bid="(\w+)"', html) + ["height"]
    dead = [i for i in ids if not re.search(r'\$\("%s"\)\.(onclick|onchange|oninput)\b|\["%s",' % (i, i), script)]
    assert not dead, dead


def door(x, y, **more):
    return {"kind": "door", "at": [x, y], "width": 0.9, "marked": True, **more}


def test_a_door_set_by_hand_moves_with_its_wall(tmp_path):
    d = door(3.0, 0.0, ends=[[2.55, 0.0], [3.45, 0.0]], corners=[[2.55, 0.0, 0.0], [3.45, 0.0, 0.0], [2.55, 2.0, 0.0], [3.45, 2.0, 0.0]])
    v = room(tmp_path, [click("a", 1, 0.0), click("b", 5, 0.0)], openings=[d])
    elements.act(tmp_path, "move_wall", {"wall": v["walls"][0]["id"], "a": [0, -0.3], "b": [6, -0.3]})
    o = elements.view(tmp_path)["openings"][0]
    assert o["at"] == pytest.approx([3.0, 0.3], abs=0.01)
    assert o["ends"][0][1] == pytest.approx(-0.3, abs=0.01) and o["corners"][2][1] == 2.0 and o["corners"][2][2] == pytest.approx(-0.3, abs=0.01)
    elements.undo(tmp_path)
    assert elements.view(tmp_path)["openings"][0]["at"] == pytest.approx([3.0, 0.0], abs=0.001)


def test_splitting_a_wall_leaves_its_door_where_it_is_and_a_slid_wall_carries_it(tmp_path):
    v = room(tmp_path, [click("a", 1, 0.0), click("b", 5, 0.0)], openings=[door(4.5, 0.0)])
    elements.act(tmp_path, "split_wall", {"wall": v["walls"][0]["id"], "at": [2.0, 0.0]})
    assert elements.view(tmp_path)["openings"][0]["at"] == pytest.approx([4.5, 0.0], abs=0.001)
    v = elements.view(tmp_path)
    elements.act(tmp_path, "set_length", {"wall": v["walls"][2]["id"], "m": 3.5})    # the east wall slides, the door sits on the south wall
    got = elements.view(tmp_path)
    assert elements._off(got["openings"][0]["at"], [got["vertices"][1], got["vertices"][2]]) < 0.005


def test_dragging_a_corner_carries_the_door_but_dragging_the_door_does_not(tmp_path):
    room(tmp_path, [click("a", 1, 0.0)], openings=[door(3.0, 0.0)])
    lay = json.loads((tmp_path / "layout.json").read_text())
    lay["vertices"][0][1] += 0.4
    lay["vertices"][1][1] += 0.4
    elements.put_plan(tmp_path, lay)
    assert elements.view(tmp_path)["openings"][0]["at"] == pytest.approx([3.0, 0.4], abs=0.02)
    lay = json.loads((tmp_path / "layout.json").read_text())
    lay["openings"][0]["at"] = [2.0, 0.4]
    elements.put_plan(tmp_path, lay)
    assert elements.view(tmp_path)["openings"][0]["at"] == pytest.approx([2.0, 0.4], abs=0.02)


def test_a_turned_plan_carries_its_door(tmp_path):
    turn = 36.937
    d = door(*layout._rotate([3.0, 0.0], turn))
    v = room(tmp_path, [click("a", 1, 0.0)], turn=turn, openings=[d])
    elements.act(tmp_path, "move_wall", {"wall": v["walls"][0]["id"], "a": [0, -0.3], "b": [6, -0.3]})
    o = elements.view(tmp_path)["openings"][0]
    assert o["at"] == pytest.approx(layout._rotate([3.0, 0.3], turn), abs=0.02)


def test_an_opening_can_be_confirmed_and_unconfirmed(tmp_path):
    room(tmp_path, [click("a", 1, 0.0)], openings=[{"kind": "window", "at": [3.0, 0.0], "width": 1.0}])
    assert not elements.view(tmp_path)["openings"][0].get("marked")
    assert elements.act(tmp_path, "confirm_opening", {"index": 0, "on": True})["openings"][0]["marked"]
    assert not elements.act(tmp_path, "confirm_opening", {"index": 0, "on": False})["openings"][0]["marked"]


def test_a_point_is_flagged_off_the_plan_until_a_wall_reaches_it(tmp_path):
    room(tmp_path, [click("a", 1, 0.0), click("far", 3.0, 0.6)])
    flag = lambda: next(p for p in json.loads((tmp_path / "walk_result.json").read_text())["points"] if p["id"] == "far").get("flag")
    elements.act(tmp_path, "confirm_wall", {"wall": elements.view(tmp_path)["walls"][0]["id"]})
    assert flag()
    elements.act(tmp_path, "move_wall", {"wall": elements.view(tmp_path)["walls"][0]["id"], "a": [0, -0.6], "b": [6, -0.6]})
    assert not flag()
