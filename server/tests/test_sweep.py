import json
import shutil

import numpy as np
import pytest
from PIL import Image

import boxroom
from app import panorama, sweep

# the room: walls at +x 2.3, -x 1.7, +z 1.8, -z 1.2 from the camera spot, floor 1.3 below it, ceiling 1.3 above it
# (the first frame looks along -Z, so front = the yellow -z wall, right = the red +x wall)


@pytest.fixture(scope="module")
def scan(tmp_path_factory):
    d = tmp_path_factory.mktemp("sweep")
    boxroom.make_scan(d)
    return d


def test_a_phone_turning_on_the_spot_is_a_sweep(scan):
    assert sweep.is_sweep(scan, {})
    assert sweep.is_sweep(scan, {"phone": {"scan type": "Free scan"}})                 # the spread rule still applies to older scans
    assert not sweep.is_sweep(scan, {"phone": {"sweep": False}})                      # an explicit "no" wins
    assert sweep.is_sweep(scan, {"phone": {"scan type": "Sweep from one spot"}})


def test_room_numbers_within_two_percent(scan):
    photos = sweep.depth_photos(scan)
    c = sweep.centre_of(photos)
    D, used = sweep.fuse_depth_sphere(scan, photos, c)
    assert used == len(photos) and np.isfinite(D).mean() > 0.9
    room, D2 = sweep.measure(D, c, sweep._heading(photos[0]))
    truth = {"width_m": 4.0, "length_m": 3.0, "height_m": 2.6}
    for k, v in truth.items():
        assert room[k] == pytest.approx(v, rel=0.02), (k, room)
    # distances from the centre of the cameras, which is within a few cm of (0, 1.3, 0)
    assert room["walls"]["right"]["wall_m"] == pytest.approx(2.3 - c[0], abs=0.06)
    assert room["walls"]["left"]["wall_m"] == pytest.approx(1.7 + c[0], abs=0.06)
    assert room["walls"]["front"]["wall_m"] == pytest.approx(1.2 + c[2], abs=0.06)
    assert room["walls"]["back"]["wall_m"] == pytest.approx(1.8 - c[2], abs=0.06)
    assert room["floor"]["wall_m"] == pytest.approx(c[1], abs=0.06) and room["ceiling"]["wall_m"] == pytest.approx(2.6 - c[1], abs=0.06)
    assert room["coverage"] > 0.9


def test_panorama_has_the_right_colours_at_the_right_azimuths(scan):
    j = panorama.ensure(scan, width=1536)
    assert j["coverage"] > 0.9 and j["from_one_spot"]
    img = np.asarray(Image.open(scan / "panorama.jpg").convert("RGB")).astype(int)
    H, W, _ = img.shape

    def at(az_deg, el_deg):
        return img[int((0.5 - el_deg / 180) * H), int((az_deg / 360 + 0.5) * W) % W]

    for az, rgb in ((0, (225, 210, 50)), (90, (200, 40, 40)), (-90, (40, 170, 60)), (180, (50, 70, 210))):    # -z yellow, +x red, -x green, +z blue
        assert np.abs(at(az, 0) - rgb).max() < 30, (az, at(az, 0))
    assert np.abs(at(30, -60) - (130, 95, 60)).max() < 30       # floor
    assert np.abs(at(30, 60) - (240, 240, 240)).max() < 30      # ceiling


def test_sweep_conversion_makes_a_coloured_flat_room(scan, tmp_path):
    d = tmp_path / "s"
    shutil.copytree(scan, d)
    ok, log = sweep.convert_sweep(d)
    assert ok, log
    room = json.loads((d / "room.json").read_text())
    assert room["width_m"] == pytest.approx(4.0, rel=0.02) and room["height_m"] == pytest.approx(2.6, rel=0.02)
    rep = json.loads((d / "report.json").read_text())
    assert rep["faces"] > 20000 and rep["coloured"] > 0.9
    import trimesh
    m = trimesh.load(d / "colored.ply", process=False)
    # snapped flat: nearly every vertex lies on one of the six planes of the box
    v = m.vertices
    on = np.zeros(len(v), bool)
    for axis, lo, hi in ((0, -1.7, 2.3), (1, 0.0, 2.6), (2, -1.2, 1.8)):
        on |= np.isclose(v[:, axis], lo, atol=0.08) | np.isclose(v[:, axis], hi, atol=0.08)       # 8 cm: the backdrop shell sits 1.5% further out
    assert on.mean() > 0.9, on.mean()
    assert (d / "mesh.bin").exists() and (d / "panorama.jpg").exists() and not (d / "texturing.json").exists()


def test_routes_serve_the_panorama_and_the_room_numbers(scan, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "DATA", tmp_path)
    d = tmp_path / "abc123abc123"
    shutil.copytree(scan, d)
    (d / "panorama.json").unlink(missing_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": "abc123abc123", "name": "Room", "kind": "mesh", "status": "ok", "phone": {"sweep": True}}))
    c = TestClient(main.app)
    r = c.get("/api/scans/abc123abc123/panorama.json?width=1024")
    assert r.status_code == 200 and r.json()["coverage"] > 0.9 and "_image" not in r.json()
    r = c.get("/api/scans/abc123abc123/panorama.jpg?width=1024")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert c.get("/api/scans/abc123abc123/panorama.json?width=100").status_code == 400


def test_depth_pixels_land_on_the_walls_with_the_pixel_centre_convention():
    """The renderers cast rays through pixel centres (i + 0.5) with the principal point at w / 2, like ARKit's intrinsics
    (frames.world_points). Unprojecting the noise-free depth must give points on the room's walls; with whole-pixel coordinates
    they were off by half a depth pixel (about 4 mm at 2 m here)."""
    import math
    from app import frames
    W, H, fx, dw, dh = 640, 480, 500.0, 256, 192
    worst = 0.0
    for yaw, pitch in ((0, 0), (37, 20), (130, -35), (250, 10)):
        M = boxroom.pose(math.radians(yaw), math.radians(pitch), boxroom.CAM + np.array([0.05, -0.02, 0.03]))
        _, depth = boxroom.render(M, dw, dh, fx * dw / W)
        photo = {"width": W, "height": H, "intrinsics": [[fx, 0, 0], [0, fx, 0], [W / 2, H / 2, 1]], "transform": M.T.tolist()}
        P = frames.world_points(photo, depth).reshape(-1, 3)
        off = np.minimum(np.abs(P - boxroom.LO), np.abs(P - boxroom.HI)).min(axis=1)      # distance to the nearest wall plane
        worst = max(worst, float(off.max()))
    assert worst < 1e-4, worst


def _room_with_desk_depth(desk_y: float = 0.85):
    """Spherical depth from the camera spot of the box room, with a desk top (0.45 m below the phone, in front and to the sides,
    like sitting at it) and a wardrobe front 0.4 m before the back wall: near things that hold more points than the floor and wall."""
    w, h = sweep.SPHERE
    dirs = sweep.dirs_grid(w, h).reshape(-1, 3)
    dist, _, _ = boxroom.trace(boxroom.CAM, dirs)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (desk_y - boxroom.CAM[1]) / dirs[:, 1]
        p = boxroom.CAM + dirs * t[:, None]
        on = (t > 0) & (np.abs(p[:, 0]) < 1.4) & (p[:, 2] > -1.2) & (p[:, 2] < 0.3)
        dist = np.where(on & (t < dist), t, dist)
        t = (1.4 - boxroom.CAM[2]) / dirs[:, 2]
        p = boxroom.CAM + dirs * t[:, None]
        on = (t > 0) & (p[:, 0] > -1.0) & (p[:, 0] < 0.4) & (p[:, 1] < 2.2)
        dist = np.where(on & (t < dist), t, dist)
    return dist.reshape(h, w).astype(np.float32)


def test_floor_and_walls_are_the_biggest_flat_surfaces_not_the_desk_or_wardrobe():
    D = _room_with_desk_depth()
    room, _ = sweep.measure(D, boxroom.CAM)
    assert room["floor"]["wall_m"] == pytest.approx(1.3, abs=0.04), room["floor"]      # not the desk at 0.45
    assert room["ceiling"]["wall_m"] == pytest.approx(1.3, abs=0.04)
    assert room["walls"]["back"]["wall_m"] == pytest.approx(1.8, abs=0.04), room["walls"]["back"]    # behind the wardrobe at 1.4
    assert room["height_m"] == pytest.approx(2.6, abs=0.05)


def test_a_sweep_uses_the_spot_most_frames_were_taken_from():
    rng = np.random.default_rng(1)
    here = [{"file": f"a{i}.jpg", "transform": boxroom.pose(i, 0, boxroom.CAM + rng.uniform(-0.1, 0.1, 3)).T.tolist()} for i in range(20)]
    there = [{"file": f"b{i}.jpg", "transform": boxroom.pose(i, 0, boxroom.CAM + [1.2, 0, 0]).T.tolist()} for i in range(8)]
    assert sweep.main_spot(there[:4] + here + there[4:]) == here
    assert sweep.main_spot(here) == here


def test_frames_pointed_at_you_are_left_out(tmp_path):
    (tmp_path / "photos").mkdir()
    near = np.full((192, 256), 0.45, "<f4"); near[:40] = 2.5           # mostly your lap
    far = np.full((192, 256), 2.0, "<f4"); far[150:] = 0.5              # the room, with your knee at the bottom
    near.tofile(tmp_path / "photos" / "n.depth"); far.tofile(tmp_path / "photos" / "f.depth")
    ph = {"depth_width": 256, "depth_height": 192}
    assert sweep.looks_at_you(tmp_path, dict(ph, depth_file="n.depth"))
    assert not sweep.looks_at_you(tmp_path, dict(ph, depth_file="f.depth"))


def test_frames_whose_height_drifted_are_put_back(scan):
    """ARKit's height for the phone drifts during a sweep: frames that see the same floor or ceiling are brought back together."""
    photos = sweep.sweep_frames(scan, sweep.depth_photos(scan))[:24]
    bad = []
    for n, ph in enumerate(photos):
        T = np.array(ph["transform"], float)
        (T.__setitem__((3, 1), T[3, 1] + 0.15) if n % 2 else None) if T.ndim == 2 else T.__setitem__(13, T[13] + (0.15 if n % 2 else 0))
        bad.append({**ph, "transform": T.tolist()})
    fixed, moved = sweep.refine_positions(scan, bad)
    assert moved > 0.03
    y = lambda p: sweep._mat(p["transform"])[1, 3]
    gap_before = abs(np.mean([y(p) for p in bad[1::2]]) - np.mean([y(p) for p in bad[::2]]))
    gap_after = abs(np.mean([y(p) for p in fixed[1::2]]) - np.mean([y(p) for p in fixed[::2]]))
    assert gap_before > 0.1 and gap_after < gap_before / 2


def test_marked_points_set_the_height_and_can_be_taken_back(tmp_path):
    import json
    room = {"height_m": 2.76, "floor": {"wall_m": 1.5}, "ceiling": {"wall_m": 1.26}}
    write = lambda s: (tmp_path / "marks.json").write_text(json.dumps({"sessions": s}))
    def m(kind, y):
        return {"kind": kind, "x": 0, "y": y, "z": 0}
    write({"a": [m("floor", -1.5), m("floor", -1.52), m("floor", -1.5), m("ceiling", 1.07), m("ceiling", 1.05)], "b": [m("ceiling", 9.0)]})   # b: its own world, no floor
    sweep.apply_marks(room, tmp_path)
    assert room["height_m"] == pytest.approx(2.56, abs=0.001) and room["height_auto_m"] == 2.76
    assert room["ceiling"]["wall_m"] == pytest.approx(1.06, abs=0.001) and room["marks"]["ceiling"] == 3
    write({})
    sweep.apply_marks(room, tmp_path)
    assert room["height_m"] == 2.76 and room["ceiling"]["wall_m"] == 1.26 and "marks" not in room


def test_a_phone_adding_to_a_sweep_is_told_where_the_spot_is_in_its_own_coordinates(tmp_path):
    import json
    from app import live
    (tmp_path / "room.json").write_text(json.dumps({"centre": [1.0, 0.0, 2.0], "floor": {"wall_m": 1.5}}))
    T = np.eye(4)
    T[:3, 3] = [0.5, 0.25, -1.0]                 # the phone's world to the scan's
    out = live.spot_for_phone(tmp_path, T)
    assert out["spot"] == pytest.approx([0.5, -0.25, 3.0]) and out["floor_y"] == pytest.approx(-1.75)
    assert live.spot_for_phone(tmp_path / "nothing", T) == {}


def test_the_dial_shows_what_the_scan_has_in_the_phones_own_directions(tmp_path, monkeypatch):
    """Depth only on the side with +x in the scan. A phone turned 90 degrees from the scan sees that side a quarter turn round."""
    w, h = 720, 360
    D = np.where(sweep.dirs_grid(w, h)[..., 0] > 0, 2.0, np.nan)
    monkeypatch.setattr(sweep, "sweep_frames", lambda d, p: [{}])
    monkeypatch.setattr(sweep, "depth_photos", lambda d: [{}])
    monkeypatch.setattr(sweep, "fuse_depth_sphere", lambda d, p, c: (D, 1))
    same = sweep.rings_seen(tmp_path, np.eye(3), np.zeros(3))
    assert len(same["rings"]) == 72
    assert same["rings"][6] and not same["rings"][18]         # sector 6 is at azimuth 90 degrees (+x), sector 18 at 270
    c, s = 0.0, 1.0
    turned = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])     # the phone's +z is the scan's +x
    other = sweep.rings_seen(tmp_path, turned, np.zeros(3))
    assert other["rings"][0] != same["rings"][0] or other["rings"][6] != same["rings"][6]
    assert sum(other["rings"][:24]) == pytest.approx(sum(same["rings"][:24]), abs=2)
