import json
import os
import subprocess
from pathlib import Path

import pytest

from app import convert, geometry as g

FIX = Path(__file__).parent / "fixtures" / "synthetic_room.json"


@pytest.fixture
def scan():
    return json.loads(FIX.read_text())


def close(p, q, tol=1.0):
    return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol


def test_wall_endpoints(scan):
    a, b = g.wall_endpoints(scan["walls"][0])
    assert close(a, (0, 0)) and close(b, (4200, 0))
    a, b = g.wall_endpoints(scan["walls"][1])
    assert close(a, (4200, 0)) and close(b, (4200, 3600))


def test_flat_transform_accepted(scan):
    w = scan["walls"][0]
    w["transform"] = [v for col in w["transform"] for v in col]
    assert close(g.wall_endpoints(w)[1], (4200, 0))


def test_snap_merges_close_endpoints_only():
    s = g.snap_endpoints([((0, 0), (1000, 0)), ((1040, 30), (1040, 900)), ((1300, 0), (1300, 900))], 100)
    assert s[0][1] == s[1][0] and s[0][1] != s[2][0]


def test_loop_closing(scan):
    script, log = convert.convert(scan)
    first = script.splitlines()[0]
    assert first.startswith("wall ")
    args = json.loads(first.split(" ", 1)[1])
    assert args["closed"] is True and len(args["points"]) == 4 and args["height"] == 2500


def test_short_wall_dropped(scan):
    scan["walls"][3]["dimensions"][0] = 0.1
    _, log = convert.convert(scan)
    assert any("dropped short wall" in l for l in log)


def test_door_projected_onto_wall(scan):
    script, _ = convert.convert(scan)
    door = next(l for l in script.splitlines() if l.startswith("door "))
    at = json.loads(door.split(" ", 1)[1])["at"]
    assert close(at, (1800, 0))  # fixture centre is 50 mm off the wall line


def test_window_on_wall_with_sill(scan):
    script, _ = convert.convert(scan)
    win = json.loads(next(l for l in script.splitlines() if l.startswith("window ")).split(" ", 1)[1])
    assert close(win["at"], (4200, 1500))
    assert win["sill"] == 900  # centre y 1.5 m - 0.6 m half height


def test_floor_and_room_emitted(scan):
    script, _ = convert.convert(scan)
    assert "floor {}" in script.splitlines()
    room = json.loads(next(l for l in script.splitlines() if l.startswith("room ")).split(" ", 1)[1])
    assert close(room["point"], (2100, 1800))


def test_square_loop_fixes_skewed_rectangle():
    skew = [(0, 0), (4200, 40), (4180, 3600), (-10, 3580)]
    sq = g.square_loop(skew, 5)
    for i in range(4):
        a, b, c = sq[i - 1], sq[i], sq[(i + 1) % 4]
        dot = (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])
        assert abs(dot) < 1e-3


def test_square_off_by_default(scan):
    _, log = convert.convert(scan)
    assert not any("squared" in l for l in log)
    _, log = convert.convert(scan, square_deg=5)
    assert any("squared" in l for l in log)


TABLE = {"identifier": "T1", "category": {"table": {}}, "dimensions": [1.4, 0.74, 0.8],
         "transform": [[0, 0, -1, 0], [0, 1, 0, 0], [1, 0, 0, 0], [2.0, 0.37, -1.8, 1]]}


def _lines(script, name):
    return [json.loads(l.split(" ", 1)[1]) for l in script.splitlines() if l.startswith(name + " ")]


def test_door_and_window_sized_from_scan(scan):
    script, _ = convert.convert(scan)
    types = {t["name"]: t for t in _lines(script, "type")}
    assert types["Door 900x2000"] == {"category": "door", "name": "Door 900x2000", "width": 900, "height": 2000}
    assert types["Window 1200x1200"]["width"] == 1200
    assert _lines(script, "door")[0]["type"] == "Door 900x2000"
    assert script.index("type ") < script.index("door ")


def test_furniture_box_per_object(scan):
    scan["objects"] = [TABLE, dict(TABLE, identifier="T2")]
    script, log = convert.convert(scan)
    fams = _lines(script, "family")
    assert [f["name"] for f in fams] == ["Scanned Table"]  # one family per category
    comp = _lines(script, "component")
    assert len(comp) == 2
    assert comp[0]["params"] == {"Width": 1400, "Depth": 800, "Height": 740}
    assert close(comp[0]["at"], (2000, 1800))
    assert comp[0]["angle"] == 90.0 and comp[0]["offset"] == 0
    assert "2 furniture objects" in log


def test_furniture_only_without_walls(scan):
    scan["walls"], scan["floors"] = [], []
    scan["objects"] = [TABLE]
    script, log = convert.convert(scan)
    assert not _lines(script, "wall") and len(_lines(script, "component")) == 1


def wall(a, b, height=2.5):
    """A RoomPlan wall between two plan points (mm)."""
    (x0, z0), (x1, z1) = (a[0] / 1000, -a[1] / 1000), (b[0] / 1000, -b[1] / 1000)
    n = ((x1 - x0) ** 2 + (z1 - z0) ** 2) ** 0.5
    ax = [(x1 - x0) / n, 0, (z1 - z0) / n, 0]
    return {"identifier": f"W{a}{b}", "dimensions": [n, height, 0],
            "transform": [ax, [0, 1, 0, 0], [-ax[2], 0, ax[0], 0], [(x0 + x1) / 2, height / 2, (z0 + z1) / 2, 1]]}


def walls(*pts):
    return [wall(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def test_l_shaped_room_point_is_inside_and_loop_runs_clockwise(scan):
    # the corners' average (2333, 2333) is outside an L: BIMCraft refuses a room there and the whole script fails
    scan["walls"] = walls((0, 0), (0, 6000), (1000, 6000), (1000, 1000), (6000, 1000), (6000, 0))
    script, _ = convert.convert(scan)
    pts = _lines(script, "wall")[0]["points"]
    room = _lines(script, "room")[0]["point"]
    assert room[0] < 1000 or room[1] < 1000
    assert sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1])) < 0   # clockwise: walls face out


def test_open_walls_get_a_floor_but_no_room(scan):
    scan["walls"].pop()
    script, log = convert.convert(scan)
    assert len(_lines(script, "wall")) == 3 and not any(w.get("closed") for w in _lines(script, "wall"))
    assert not _lines(script, "room") and len(_lines(script, "floor")[0]["boundary"]) == 4
    assert any("no room" in l for l in log)


def test_squared_walls_keep_their_doors(scan):
    # skewed so far that a door left on the unsquared wall would be off the squared one
    scan["walls"] = walls((0, 0), (0, 3600), (4200, 3900), (4200, 0))
    script, _ = convert.convert(scan, square_deg=6)
    loop = _lines(script, "wall")[0]["points"]
    door = _lines(script, "door")[0]["at"]
    segs = list(zip(loop, loop[1:] + loop[:1]))
    assert min(g.dist(door, g.project_on_segment(door, a, b)) for a, b in segs) < 1


def test_walls_whose_ends_snap_together_are_dropped(scan):
    scan["walls"].insert(1, wall((4200, -90), (4200, 90)))   # 180 mm: kept, but both ends snap to the corner
    script, log = convert.convert(scan)
    assert any("snapped together" in l for l in log) and _lines(script, "wall")[0]["closed"]


@pytest.mark.skipif(not os.environ.get("BIMCRAFT_CLI"), reason="BIMCRAFT_CLI not set")
@pytest.mark.parametrize("variant", ["box", "furniture", "l_shape", "open", "squared"])
def test_bimcraft_cli_accepts_script(scan, tmp_path, variant):
    square = None
    if variant == "furniture":
        scan["objects"] = [TABLE, dict(TABLE, category={"bed": {}}, identifier="B1")]
    elif variant == "l_shape":
        scan["walls"] = walls((0, 0), (0, 6000), (1000, 6000), (1000, 1000), (6000, 1000), (6000, 0))
    elif variant == "open":
        scan["walls"].pop()
    elif variant == "squared":
        scan["walls"], square = walls((0, 0), (0, 3600), (4200, 3900), (4200, 0)), 6
    p = tmp_path / "script.txt"
    p.write_text(convert.convert(scan, square_deg=square)[0])
    ok, out = convert.run_cli(p, tmp_path)
    assert ok, out
    if variant == "box":
        info = subprocess.run([os.environ["BIMCRAFT_CLI"], "info", str(tmp_path / "room.bimcraft")], capture_output=True, text=True)
        assert (tmp_path / "room.ifc").exists() and "wall" in info.stdout.lower()
