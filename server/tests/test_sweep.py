import json

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


def test_sweep_conversion_makes_a_coloured_flat_room(tmp_path):
    d = tmp_path / "s"
    boxroom.make_scan(d)
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


def test_routes_serve_the_panorama_and_the_room_numbers(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "DATA", tmp_path)
    d = tmp_path / "abc123abc123"
    boxroom.make_scan(d)
    (d / "meta.json").write_text(json.dumps({"id": "abc123abc123", "name": "Room", "kind": "mesh", "status": "ok", "phone": {"sweep": True}}))
    c = TestClient(main.app)
    r = c.get("/api/scans/abc123abc123/panorama.json?width=1024")
    assert r.status_code == 200 and r.json()["coverage"] > 0.9 and "_image" not in r.json()
    r = c.get("/api/scans/abc123abc123/panorama.jpg?width=1024")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert c.get("/api/scans/abc123abc123/panorama.json?width=100").status_code == 400
    r = c.post("/api/scans/abc123abc123/convert")
    assert r.status_code == 200, r.text
    room = c.get("/api/scans/abc123abc123/room.json").json()
    assert room["width_m"] == pytest.approx(4.0, rel=0.02) and "sweep from one spot" in r.json()["log"]
