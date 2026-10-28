import json
from pathlib import Path

from app import photos

SCAN = json.loads((Path(__file__).parent / "fixtures" / "synthetic_room.json").read_text())
K = [[900, 0, 0], [0, 900, 0], [640, 360, 1]]  # fx, fy; principal point (640, 360); columns


def pose(x, y, z, yaw=0.0, name="p.jpg"):
    import math
    c, s = math.cos(yaw), math.sin(yaw)
    return {"file": name, "transform": [[c, 0, -s, 0], [0, 1, 0, 0], [s, 0, c, 0], [x, y, z, 1]],
            "intrinsics": K, "width": 1280, "height": 720}


def wall_by_plan_y(y_mm):
    # fixture walls: 0 -> plan y=0 (z=0), 2 -> plan y=3600 (z=-3.6)
    return SCAN["walls"][0 if y_mm == 0 else 2]


def test_camera_sees_the_wall_ahead_not_behind():
    cam = pose(2.1, 1.25, 0.0)  # at the z=0 wall, looking down -Z toward the far wall at z=-3.6
    assert photos.coverage(cam, wall_by_plan_y(3600))["coverage"] == 1.0
    assert photos.coverage(cam, wall_by_plan_y(0)) is None  # behind the camera
    close = photos.coverage(pose(2.1, 1.25, -1.8), wall_by_plan_y(3600))
    assert 0.2 < close["coverage"] < 1.0  # 4.2 m wall at 1.8 m is wider than the view


def test_wall_centre_projects_near_principal_point():
    cam = pose(2.1, 1.25, -1.8)
    c = photos.coverage(cam, wall_by_plan_y(3600))
    xs = [p[0] for p in c["corners_px"]]
    assert min(xs) < 640 < max(xs) and abs(c["distance_m"] - 1.8) < 1e-6 and c["incidence_deg"] < 1


def test_oblique_view_has_higher_incidence():
    head_on = photos.coverage(pose(2.1, 1.25, -1.8), wall_by_plan_y(3600))
    oblique = photos.coverage(pose(0.3, 1.25, -1.8), wall_by_plan_y(3600))
    assert oblique and oblique["incidence_deg"] > head_on["incidence_deg"] + 10


def test_turned_camera_sees_side_wall():
    # yaw -90 deg: camera looks along +X toward the wall at x=4.2 (window wall)
    cam = pose(2.1, 1.4, -1.8, yaw=-1.5708)
    idx = photos.index_photos(SCAN, [cam])
    side = SCAN["walls"][1]["identifier"]
    assert side in idx and idx[side]["kind"] == "wall"
    assert SCAN["windows"][0]["identifier"] in idx  # the window sits in that wall


def test_index_ranks_best_photo_first_and_accepts_manifest_object():
    near = pose(2.1, 1.25, -1.8, name="near.jpg")
    far_side = pose(0.3, 1.25, -1.8, name="side.jpg")
    idx = photos.index_photos(SCAN, {"version": 2, "photos": [far_side, near]})
    best = idx[SCAN["walls"][2]["identifier"]]["photos"]
    assert best[0]["photo"] == "near.jpg"
