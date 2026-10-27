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


def test_snap_merges_close_endpoints():
    walls = [((0, 0), (1000, 0)), ((1040, 30), (1040, 900))]
    s = g.snap_endpoints(walls, 100)
    assert s[0][1] == s[1][0]


def test_snap_keeps_distant_endpoints():
    s = g.snap_endpoints([((0, 0), (1000, 0)), ((1300, 0), (1300, 900))], 100)
    assert s[0][1] != s[1][0]


def test_loop_closing(scan):
    script, log = convert.convert(scan)
    first = script.splitlines()[0]
    assert first.startswith("wall ")
    args = json.loads(first.split(" ", 1)[1])
    assert args["closed"] is True and len(args["points"]) == 4 and args["height"] == 2500


def test_open_walls_not_closed(scan):
    scan["walls"].pop()
    script, log = convert.convert(scan)
    assert sum(l.startswith("wall ") for l in script.splitlines()) == 3
    assert not any(json.loads(l.split(" ", 1)[1]).get("closed") for l in script.splitlines() if l.startswith("wall "))


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


@pytest.mark.skipif(not os.environ.get("BIMCRAFT_CLI"), reason="BIMCRAFT_CLI not set")
def test_bimcraft_cli_accepts_script(scan, tmp_path):
    script, _ = convert.convert(scan)
    p = tmp_path / "script.txt"
    p.write_text(script)
    ok, out = convert.run_cli(p, tmp_path)
    assert ok, out
    assert (tmp_path / "room.bimcraft").exists() and (tmp_path / "room.ifc").exists()
    info = subprocess.run([os.environ["BIMCRAFT_CLI"], "info", str(tmp_path / "room.bimcraft")],
                          capture_output=True, text=True)
    assert "wall" in info.stdout.lower()


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


@pytest.mark.skipif(not os.environ.get("BIMCRAFT_CLI"), reason="BIMCRAFT_CLI not set")
def test_bimcraft_cli_accepts_furniture(scan, tmp_path):
    scan["objects"] = [TABLE, dict(TABLE, category={"bed": {}}, identifier="B1")]
    p = tmp_path / "script.txt"
    p.write_text(convert.convert(scan)[0])
    ok, out = convert.run_cli(p, tmp_path)
    assert ok, out
