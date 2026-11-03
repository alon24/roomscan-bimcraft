import os
import json
import math
import os
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


def test_marks_that_disagree_do_not_replace_the_height(tmp_path):
    import json
    room = {"height_m": 2.57, "floor": {"wall_m": 1.5}, "ceiling": {"wall_m": 1.07}}
    marks = [{"kind": "floor", "x": 0, "y": y, "z": 0} for y in (-1.39, -1.29)] + [{"kind": "ceiling", "x": 0, "y": 1.12, "z": 0}]
    (tmp_path / "marks.json").write_text(json.dumps({"sessions": {"a": marks}}))
    sweep.apply_marks(room, tmp_path)
    assert room["height_m"] == 2.57 and room["marks"]["used"] is False and room["marks"]["height_m"] == pytest.approx(2.46, abs=0.01)


def _wall(x, z, nx, nz):
    return {"kind": "wall", "x": x, "y": 0.0, "z": z, "nx": nx, "nz": nz}


def test_wall_marks_give_the_distances_between_opposite_walls_at_any_turn():
    import math
    for turn in (0.0, 0.6):
        c, s = math.cos(turn), math.sin(turn)
        rot = lambda x, z: (c * x - s * z, s * x + c * z)
        def mark(x, z, nx, nz):
            return _wall(*rot(x, z), *rot(nx, nz))
        marks = [mark(2.0, 0.5, -1, 0), mark(2.02, -0.5, -1, 0), mark(-3.95, 0.2, 1, 0),           # 5.96 across
                 mark(0.3, 3.0, 0, -1), mark(0.1, -3.6, 0, 1)]                                     # 6.6 the other way
        out = sweep.wall_sizes(marks)
        assert sorted(round(v, 2) for v in out["sizes"]) == [5.96, 6.6] and out["spread"] == pytest.approx(0.02, abs=0.001)


def test_wall_marks_are_used_when_they_agree_and_taken_back(tmp_path):
    import json
    room = {"width_m": 5.72, "length_m": 6.66, "height_m": 2.57, "floor": {"wall_m": 1.5}, "ceiling": {"wall_m": 1.07}}
    write = lambda s: (tmp_path / "marks.json").write_text(json.dumps({"sessions": s}))
    write({"a": [_wall(2.0, 0, -1, 0), _wall(-3.95, 0, 1, 0), _wall(0, 3.0, 0, -1), _wall(0, -3.6, 0, 1)]})
    sweep.apply_marks(room, tmp_path)
    assert room["width_m"] == pytest.approx(5.95) and room["length_m"] == pytest.approx(6.6) and room["wall_marks"]["used"]
    write({"a": [_wall(2.0, 0, -1, 0), _wall(2.3, 1, -1, 0), _wall(-3.95, 0, 1, 0)]})          # 30 cm apart: not one flat wall
    sweep.apply_marks(room, tmp_path)
    assert room["width_m"] == 5.72 and not room["wall_marks"]["used"]
    write({})
    sweep.apply_marks(room, tmp_path)
    assert room["width_m"] == 5.72 and room["length_m"] == 6.66 and "wall_marks" not in room


def test_the_outline_finds_an_l_shaped_room_not_a_box():
    """An L: 6 x 4 m with a 2 x 1.5 m corner missing. Walls are at their true lengths, with an inside corner the box could not show."""
    w, h = 720, 360
    dirs = sweep.dirs_grid(w, h)
    x0, x1, z0, z1, notch_x, notch_z = -3.0, 3.0, -2.0, 2.0, 1.0, 0.5
    t = np.full((h, w), np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        for axis, plane in ((0, x0), (0, x1), (2, z0), (2, z1)):
            s = plane / dirs[..., axis]
            t = np.where((s > 0) & (s < t), s, t)
        hit = dirs * t[..., None]
        cut = (hit[..., 0] > notch_x) & (hit[..., 2] > notch_z)        # the missing corner: its two walls come forward
        for axis, plane, other, lim in ((0, notch_x, 2, notch_z), (2, notch_z, 0, notch_x)):
            s = plane / dirs[..., axis]
            on = (s > 0) & cut & ((dirs[..., other] * s) >= lim)
            t = np.where(on & (s < t), s, t)
    P = dirs * t[..., None]
    P[np.abs(P[..., 1]) > 1.3] = np.nan
    o = sweep.room_outline(P, np.zeros(3), 1.25)
    lengths = sorted(v["length_m"] for v in o["walls"])
    assert len(lengths) == 6, lengths
    assert lengths == pytest.approx([1.5, 2.0, 2.5, 4.0, 4.0, 6.0], abs=0.25)
    assert all(abs(c["angle_deg"] - 90) < 6 for c in o["corners"])


def test_layout_script_builds_in_bimcraft(tmp_path):
    from app import layout
    room = {"height_m": 2.5, "outline": {"walls": [{"a": [0, 0], "b": [4, 0]}, {"a": [4, 0], "b": [4, -3]}, {"a": [4, -3], "b": [0, -3]}, {"a": [0, -3], "b": [0, 0]}]}}
    lay = layout.default(room)
    assert len(lay["vertices"]) == 4 and layout.problem(lay) is None
    text = layout.script({**lay, "openings": [{"kind": "door", "at": [2, 0]}, {"kind": "window", "at": [4, 1.5]}]})
    assert text.splitlines()[0].startswith("wall ") and "door" in text and "window" in text
    assert layout.problem({"vertices": [[0, 0], [1, 1], [1, 0], [0, 1]]}) is not None
    m = layout.blender_model(lay)
    assert m.is_volume or len(m.faces) > 0
    assert abs(m.bounds[1][1] - 2.5) < 1e-6          # height is y in glTF
    if os.environ.get("BIMCRAFT_CLI"):
        ok, log = layout.export(tmp_path, {**lay, "openings": [{"kind": "door", "at": [2, 0]}, {"kind": "window", "at": [4, 1.5]}]}, "Test")
        assert ok, log
        assert (tmp_path / "room.bimcraft").exists() and (tmp_path / "layout.glb").exists()


def test_layout_overlay_lies_where_the_plan_is(tmp_path):
    import cv2
    import trimesh
    from app import layout
    # a 2 x 1 m red floor at y = 0, x from 1 to 3, world z from -2 to -1, so plan y from 1 to 2; and a ceiling that must be taken off
    floor = trimesh.Trimesh([[1, 0, -1], [3, 0, -1], [3, 0, -2], [1, 0, -2]], [[0, 1, 2], [0, 2, 3]], vertex_colors=[[255, 0, 0, 255]] * 4, process=False)
    ceil = trimesh.Trimesh([[1, 3, -1], [3, 3, -1], [3, 3, -2], [1, 3, -2]], [[0, 1, 2], [0, 2, 3]], vertex_colors=[[0, 255, 0, 255]] * 4, process=False)
    # long triangles would be dropped as skins, so cut the floor up small
    floor = floor.subdivide().subdivide().subdivide()
    ceil = ceil.subdivide().subdivide().subdivide()
    floor.visual.vertex_colors = [255, 0, 0, 255]
    ceil.visual.vertex_colors = [0, 255, 0, 255]
    trimesh.util.concatenate([floor, ceil]).export(tmp_path / "colored.ply")
    png, m = layout.overlay(tmp_path, 0.0, 0.0, 2.0)
    assert abs(m["x0"] - 1) < 0.02 and abs(m["x1"] - 3) < 0.03 and abs(m["y1"] - 2) < 0.02 and abs(m["y0"] - 1) < 0.03
    img = cv2.imread(str(png), cv2.IMREAD_UNCHANGED)
    h, w = img.shape[:2]
    assert list(img[h // 2, w // 2]) == [0, 0, 255, 255]     # red, BGRA: the floor, no ceiling


def test_layout_open_stretch_gets_no_wall(tmp_path):
    import numpy as np
    from app import layout
    v = [[0, 0], [0, 3], [4, 3], [4, 0]]       # clockwise; the stretch from (4, 3) to (4, 0) looks into a kitchen
    cells = np.array([[5.5, y] for y in np.arange(0.1, 3, 0.05)] + [[2, 2.9], [0.1, 1.5], [2, 0.1]])
    assert layout.open_edges(v, cells, np.array([2.0, 1.5])) == [False, False, True, False]
    lay = {"vertices": v, "open": [False, False, True, False], "openings": [{"kind": "door", "at": [0, 1]}], "height_m": 2.5}
    text = layout.script(lay)
    assert '"closed"' not in text and "room " not in text and text.count("wall ") == 1
    assert '[[4000, 0], [0, 0], [0, 3000], [4000, 3000]]' in text
    m = layout.blender_model(lay)
    assert m.bounds[1][1] > 2.4
    if os.environ.get("BIMCRAFT_CLI"):
        ok, log = layout.export(tmp_path, lay, "Open")
        assert ok, log
    assert layout.problem({"vertices": v, "open": [True] * 4}) is not None


def test_join_finds_a_known_turn_and_shift():
    import numpy as np
    from app import join
    rng = np.random.default_rng(3)
    # an L-shaped room's walls, so only one fit is right
    walls = [((0, 0), (6, 0)), ((6, 0), (6, 3)), ((6, 3), (3, 3)), ((3, 3), (3, 5)), ((3, 5), (0, 5)), ((0, 5), (0, 0))]
    pts = np.vstack([np.linspace(a, b, int(np.hypot(b[0] - a[0], b[1] - a[1]) / 0.05)) for a, b in walls])
    dst = pts + rng.normal(0, 0.01, pts.shape)
    src = join._turn(pts[rng.random(len(pts)) < 0.7], 73.0) + [1.5, -2.0]
    f = join.fit(src, dst)[0]
    assert min((f["deg"] + 73) % 360, (-f["deg"] - 73) % 360) < 0.5
    back = join.plan_point(src, f)
    assert np.median(np.abs(back - pts[np.argmin(((pts[None] - back[:, None]) ** 2).sum(-1), axis=1)])) < 0.03
    assert join.sure(join.fit(src, dst))


def test_join_matrix_is_the_same_as_the_plan_point():
    import numpy as np
    from app import join
    f = {"deg": 33.0, "tx": 1.0, "ty": -2.0}
    p = [0.7, 1.1, -0.4]
    w = join.world_point(p, f, 0.25)
    q = join.plan_point([p[0], -p[2]], f)[0]
    assert abs(w[0] - q[0]) < 1e-9 and abs(-w[2] - q[1]) < 1e-9 and abs(w[1] - 1.35) < 1e-9


def test_walked_plan_saves_and_takes_a_join(tmp_path):
    import numpy as np
    from app import layout
    corners = [[0, 0], [5, 0], [5, 4], [0, 4]]
    xs = np.arange(0, 5, 0.05)
    zs = np.arange(0, 4, 0.05)
    wall = [[x, 2.0, z] for x in xs for z in (0, 4)] + [[x, 2.0, z] for z in zs for x in (0, 5)]
    d = tmp_path / "plan"
    d.mkdir()
    lay = layout.from_walk(d, {"corners": corners, "open": [False, True, False, False], "floor_y": -0.2, "ceiling_y": 2.4, "cloud": list(np.array(wall).ravel())})
    assert lay["height_m"] == 2.6 and lay["open"][1] and len(layout.points(d)["top"]) > 100
    assert layout.load(d)["floor_y"] == -0.2
    # a nonsense plan is refused
    import pytest
    with pytest.raises(ValueError):
        layout.from_walk(d, {"corners": [[0, 0]], "floor_y": 0, "ceiling_y": 2.5})


def _room_scan(tmp_path, w=5.0, l=4.0, h=2.6, floor=-1.4, step=0.02):
    """A scanned empty room as a mesh file: walls, floor and ceiling as readings. x from 0 to w, world z from 0 to -l, so plan y from 0 to l."""
    import numpy as np
    import trimesh
    xs, zs, ys = np.arange(0, w, step), -np.arange(0, l, step), floor + np.arange(0, h, step)
    grid = lambda a, b: np.array(np.meshgrid(a, b)).reshape(2, -1).T
    parts = [np.c_[g[:, 0], g[:, 1], np.full(len(g), z)] for g, z in ((grid(xs, ys), 0.0), (grid(xs, ys), -l))]
    parts += [np.c_[np.full(len(g), x), g[:, 1], g[:, 0]] for g, x in ((grid(zs, ys), 0.0), (grid(zs, ys), w))]
    parts += [np.c_[g[:, 0], np.full(len(g), y), g[:, 1]] for g, y in ((grid(xs, zs), floor), (grid(xs, zs), floor + h))]
    P = np.vstack(parts) + np.random.default_rng(0).normal(0, 0.002, (sum(map(len, parts)), 3))
    trimesh.PointCloud(P).export(tmp_path / "scan.ply")


def test_walk_works_out_each_click_and_snaps_corners(tmp_path):
    from app import layout, walk
    _room_scan(tmp_path)
    off = 0.06   # clicks are never exactly on the corner
    clicks = [{"id": "a", "x": off, "y": -1.3, "z": -off}, {"id": "b", "x": 5 - off, "y": 1.1, "z": -off}, {"id": "c", "x": 5 - off, "y": -1.3, "z": -4 + off},
              {"id": "mid", "x": 2.5, "y": -1.33, "z": -off},  # a wall meeting the floor, on the way round
              {"id": "d", "x": off, "y": 0.0, "z": -4 + off}, {"id": "door", "x": 2.5, "y": -0.3, "z": -4 + off, "tag": "door"}, {"id": "f", "x": 2.5, "y": -1.4, "z": -2}]
    res = walk.build(tmp_path, clicks)
    assert res["built"] and abs(res["height_m"] - 2.6) < 0.05
    got = {p["id"]: p for p in __import__("json").loads((tmp_path / "walk_result.json").read_text())["points"]}
    assert got["a"]["kind"] == got["b"]["kind"] == got["c"]["kind"] == got["d"]["kind"] == "corner"
    assert got["mid"]["kind"] == "floor edge" and got["door"]["kind"] == "door" and got["f"]["kind"] == "floor"
    assert abs(got["a"]["x"]) < 0.015 and abs(got["a"]["z"]) < 0.015       # 6 cm off when clicked, on the real corner now
    lay = layout.load(tmp_path)
    assert len(lay["vertices"]) == 4 and len(lay["openings"]) == 1 and not any(lay["open"])
    import math
    sides = sorted(math.dist(a, b) for a, b in zip(lay["vertices"], lay["vertices"][1:] + lay["vertices"][:1]))
    assert abs(sides[0] - 4) < 0.03 and abs(sides[-1] - 5) < 0.03
    assert layout.problem(lay) is None and "door" in layout.script(lay)


def test_walk_marks_a_stretch_with_no_readings_open(tmp_path):
    import numpy as np
    from app import walk
    v = [[0.0, 0.0], [5.0, 0.0], [5.0, 4.0], [0.0, 4.0]]
    cells = np.array([[x, 0.0] for x in np.arange(0, 5, 0.05)] + [[x, 4.0] for x in np.arange(0, 5, 0.05)] + [[0.0, y] for y in np.arange(0, 4, 0.05)])
    assert walk._no_wall(v, cells) == [False, True, False, False]


def _shape_scan(tmp_path, corners, h=2.6, floor=-1.4, step=0.02):
    """A scanned room of any outline, plan x, y, as readings of its walls, floor and ceiling."""
    import numpy as np
    import trimesh
    parts = []
    ys = floor + np.arange(0, h, step)
    for a, b in zip(corners, corners[1:] + corners[:1]):
        n = int(np.hypot(b[0] - a[0], b[1] - a[1]) / step)
        line = np.linspace(a, b, n)
        for y in ys:
            parts.append(np.c_[line[:, 0], np.full(n, y), -line[:, 1]])
    xs = np.array(corners, float)
    gx, gy = np.meshgrid(np.arange(xs[:, 0].min(), xs[:, 0].max(), step), np.arange(xs[:, 1].min(), xs[:, 1].max(), step))
    flat = np.c_[gx.ravel(), gy.ravel()]
    for y in (floor, floor + h):
        parts.append(np.c_[flat[:, 0], np.full(len(flat), y), -flat[:, 1]])
    P = np.vstack(parts)
    trimesh.PointCloud(P + np.random.default_rng(0).normal(0, 0.002, P.shape)).export(tmp_path / "scan.ply")


def test_walk_order_of_clicks_does_not_matter_and_missing_corners_are_found(tmp_path):
    import json
    import math
    import random
    from app import layout, walk
    # an L: only points along the walls are clicked, no corner at all, and in a jumbled order
    L = [[0, 0], [6, 0], [6, 3], [3, 3], [3, 5], [0, 5]]
    _shape_scan(tmp_path, L)
    along = []
    for a, b in zip(L, L[1:] + L[:1]):
        for t in (0.3, 0.7):
            along.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
    results = []
    for seed in (1, 2, 3):
        random.Random(seed).shuffle(along)
        clicks = [{"id": str(i), "x": p[0] + 0.03, "y": random.Random(i).choice([-1.3, -0.2, 1.1]), "z": -p[1] - 0.03} for i, p in enumerate(along)]
        assert walk.build(tmp_path, clicks)["built"]
        lay = layout.load(tmp_path)
        assert layout.problem(lay) is None
        t = -lay["turn_deg"]
        v = sorted([round(x, 1), round(y, 1)] for x, y in (layout._rotate(p, t) for p in lay["vertices"]))
        results.append(v)
    assert results[0] == results[1] == results[2]
    assert results[0] == sorted([float(a), float(b)] for a, b in L), results[0]
    assert not any(lay["open"])
    assert math.isclose(layout._area(lay["vertices"]) ** 2, (6 * 3 + 3 * 2) ** 2, rel_tol=0.03)
    assert json.loads((tmp_path / "walk_result.json").read_text())["points"]


def test_walk_flags_points_and_finds_their_photos(tmp_path):
    import numpy as np
    from app import walk
    pts = [{"id": "a", "kind": "wall", "x": 0.0, "y": 1.0, "z": 0.0}, {"id": "b", "kind": "wall", "x": 2.0, "y": 1.0, "z": -2.0}, {"id": "c", "kind": "unknown", "x": 1.0, "y": 1.0, "z": -1.0}, {"id": "d", "kind": "ignore", "x": 9.0, "y": 1.0, "z": -9.0}]
    v = [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]]
    walk.flag_points(pts, v, 0.0, 2.6)
    assert pts[0]["flag"] is None and "Not on the plan" in pts[1]["flag"] and "door or window" in pts[2]["flag"] and pts[3]["flag"] is None
    # a camera at the origin, upright, looking along -z: a point 2 m ahead is in the middle of the picture
    upright = np.eye(4).T.tolist()
    frame = {"file": "p001.jpg", "transform": upright, "intrinsics": [[1000, 0, 0], [0, 1000, 0], [960, 720, 1]], "width": 1920, "height": 1440}
    got = walk.photos_of(tmp_path, [0.0, 0.0, -2.0], [frame])
    assert len(got) == 1 and abs(got[0]["u"] - 0.5) < 1e-3 and abs(got[0]["v"] - 0.5) < 1e-3 and got[0]["away_m"] == 2.0
    assert walk.photos_of(tmp_path, [0.0, 0.0, 2.0], [frame]) == []     # behind the camera
    # higher in the room is higher in the picture
    assert walk.photos_of(tmp_path, [0.0, 0.5, -2.0], [frame])[0]["v"] < 0.5


def test_walk_window_clicks_make_a_window_of_that_size(tmp_path):
    from app import layout, walk
    v = [[0.0, 0.0], [0.0, 4.0], [5.0, 4.0], [5.0, 0.0]]
    click = lambda kind, x, y, z: {"kind": kind, "x": x, "y": y, "z": z}
    # floor at -1.4. A window: bottom and top, left and right. A second far along the wall. A door with one click.
    pts = [click("window", 1.0, -0.5, -0.05), click("window", 1.0, 0.7, -0.03), click("window", 2.4, -0.5, 0.02), click("window", 2.4, 0.7, 0.0), click("window", 4.4, 0.0, 0.0), click("door", 4.95, -0.5, -2.0)]
    got = walk.openings_from(pts, v, [False] * 4, -1.4)
    wins = sorted((o for o in got if o["kind"] == "window"), key=lambda o: o["at"][0])
    assert len(wins) == 2 and len([o for o in got if o["kind"] == "door"]) == 1
    w = wins[0]
    assert abs(w["width"] - 1.4) < 0.01 and abs(w["sill"] - 0.9) < 0.01 and abs(w["head"] - 2.1) < 0.01 and w["width_known"] and w["height_known"]
    assert abs(w["at"][0] - 1.7) < 0.01 and abs(w["at"][1]) < 0.01
    assert not wins[1]["width_known"] and not wins[1]["height_known"]
    door = next(o for o in got if o["kind"] == "door")
    assert door["sill"] == 0 and door["head"] == 2.1 and abs(door["at"][0] - 5) < 0.01
    # they get through into BIMCraft with their own sizes
    lay = {"vertices": v, "open": [False] * 4, "openings": got, "height_m": 2.6}
    assert '"sill": 900' in layout.script(lay)
    if os.environ.get("BIMCRAFT_CLI"):
        ok, log = layout.export(tmp_path, lay, "Sizes")
        assert ok, log


def _frame_for_photo_tests():
    # a camera at (0, 1, 0), upright, looking along -z. 400 x 400, focal 200, centre 200, 200
    return {"file": "a.jpg", "width": 400, "height": 400, "depth_file": "a.bin", "intrinsics": [[200, 0, 0], [0, 200, 0], [200, 200, 1]],
            "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 1, 0, 1]]}


def test_a_spot_in_a_photo_is_placed_from_its_lidar_reading(tmp_path, monkeypatch):
    import numpy as np
    from app import meshscan, walk
    depth = np.full((40, 40), 2.0)
    monkeypatch.setattr(meshscan, "_depth_map", lambda d, ph: depth)
    ph = _frame_for_photo_tests()
    x, y, z = walk.photo_point(tmp_path, ph, 0.75, 0.5)     # half way to the right edge: 1 m right at 2 m
    assert (x, y, z) == pytest.approx((1.0, 1.0, -2.0))
    depth[:] = 0
    assert walk.photo_point(tmp_path, ph, 0.75, 0.5) is None


def test_a_photo_shows_only_the_points_nothing_hides(tmp_path, monkeypatch):
    import numpy as np
    from app import meshscan, walk
    monkeypatch.setattr(meshscan, "_depth_map", lambda d, ph: np.full((40, 40), 2.0))
    pts = [{"id": "front", "x": 0, "y": 1, "z": -2}, {"id": "hidden", "x": 0, "y": 1, "z": -4}, {"id": "behind", "x": 0, "y": 1, "z": 2}, {"id": "aside", "x": 9, "y": 1, "z": -2}]
    assert walk.seen_in(tmp_path, _frame_for_photo_tests(), pts) == ["front"]


def test_making_the_plan_again_keeps_doors_marked_by_hand(tmp_path, monkeypatch):
    from app import layout, main, walk
    d = tmp_path
    square = [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0], [0.0, 3.0]]
    (d / "walk_points.json").write_text("[]")
    (d / "layout.json").write_text(json.dumps({"vertices": square, "open": [False] * 4, "height_m": 2.5, "turn_deg": 90, "openings": [{"kind": "door", "at": [1.0, 0.0], "width": 0.9, "marked": True}, {"kind": "window", "at": [5.0, 5.0]}]}))
    def built(_d, _c):
        (d / "layout.json").write_text(json.dumps({"vertices": square, "open": [False] * 4, "height_m": 2.5, "turn_deg": 0, "openings": [{"kind": "window", "at": layout._rotate(layout._rotate([1.0, 0.0], -90), 0), "width": 1.2}, {"kind": "window", "at": [8.0, 8.0], "width": 1.0}]}))
        return {"built": True}
    monkeypatch.setattr(walk, "build", built)
    main._build_walk(d, force=True)
    got = json.loads((d / "layout.json").read_text())["openings"]
    assert [o["kind"] for o in got] == ["window", "door"] and got[0]["at"] == [8.0, 8.0]
    assert got[1]["at"] == pytest.approx(layout._rotate([1.0, 0.0], -90), abs=1e-3)
    assert got[1]["marked"] is True and layout.validate({"vertices": [[0, 0], [1, 0], [1, 1]], "height_m": 2.5, "openings": got})["openings"][1]["marked"] is True


def test_a_door_stands_at_its_clicks_not_on_the_plan_wall():
    from app import layout, walk
    v = [[0.0, 0.0], [0.0, 4.0], [5.0, 4.0], [5.0, 0.0]]
    click = lambda x, y, z: {"kind": "door", "x": x, "y": y, "z": z}
    # the south wall is at z = 0, the real door is on a slant off it
    got = walk.openings_from([click(1.0, 0.7, -0.1), click(2.0, -1.4, 0.1)], v, [False] * 4, -1.4)[0]
    assert sorted(got["ends"]) == [[1.0, -0.1], [2.0, 0.1]]
    assert got["width"] == pytest.approx(math.hypot(1.0, 0.2), abs=1e-3)
    kept = layout.validate({"vertices": v, "height_m": 2.5, "openings": [got]})["openings"][0]
    assert kept["ends"] == got["ends"]


def test_a_door_keeps_its_four_marked_corners():
    from app import layout
    corners = [[1, -1.4, 0], [2, -1.3, 0.1], [2, 0.7, 0.1], [1, 0.7, 0]]
    door = {"kind": "door", "at": [1.5, 0], "width": 1.0, "sill": 0, "head": 2.1, "corners": corners, "ends": [[1, 0], [2, 0.1]], "marked": True}
    kept = layout.validate({"vertices": [[0, 0], [4, 0], [4, 3]], "height_m": 2.5, "openings": [door]})["openings"][0]
    assert kept["corners"] == corners and kept["marked"] is True
