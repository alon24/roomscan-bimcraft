"""The printed LiDAR target: synthetic frames of it (photo with the markers, LiDAR depth with a known error) must give that error back."""
import json
import math

import cv2
import numpy as np
import pytest

from app import lidartarget as lt

W, H, FX = 1920, 1440, 1450.0
DW, DH = 256, 192
MARKERS = json.loads('{"40": [[1,1,1,1,1,1],[1,1,1,1,0,1],[1,1,0,0,0,1],[1,1,1,1,0,1],[1,0,1,1,1,1],[1,1,1,1,1,1]],'
                     '"41": [[1,1,1,1,1,1],[1,1,1,0,1,1],[1,0,1,0,1,1],[1,1,1,0,1,1],[1,0,1,1,1,1],[1,1,1,1,1,1]],'
                     '"42": [[1,1,1,1,1,1],[1,1,1,0,0,1],[1,1,1,0,1,1],[1,0,1,1,1,1],[1,0,0,1,1,1],[1,1,1,1,1,1]],'
                     '"43": [[1,1,1,1,1,1],[1,1,1,0,0,1],[1,0,1,1,1,1],[1,0,1,0,0,1],[1,1,1,0,1,1],[1,1,1,1,1,1]],'
                     '"44": [[1,1,1,1,1,1],[1,1,1,0,1,1],[1,1,0,1,1,1],[1,0,0,0,1,1],[1,0,1,1,1,1],[1,1,1,1,1,1]],'
                     '"45": [[1,1,1,1,1,1],[1,1,1,0,1,1],[1,0,0,0,1,1],[1,0,0,0,1,1],[1,0,1,0,0,1],[1,1,1,1,1,1]]}')
BLACK_EXTRA = 0.004       # the fake LiDAR reads black surfaces 4 mm long


def test_marker_patterns_match_opencv():
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for i, g in MARKERS.items():
        assert ((cv2.aruco.generateImageMarker(d, int(i), 6) < 128).astype(int) == np.array(g)).all()


def _marker_image(i: int, side: float, ppm: float) -> np.ndarray:
    """Marker `i` with a one-cell white margin round it, as seen from above (x right, +y up), `ppm` pixels per mm."""
    g = np.pad(np.array(MARKERS[str(i)]), 1)                         # 8 x 8 cells, the outer ring white
    cell = int(round(side / 6 * ppm))
    return np.kron(np.where(g == 1, 20, 235), np.ones((cell, cell))).astype(np.uint8)


def _look_at(cam: np.ndarray, target: np.ndarray) -> np.ndarray:
    """OpenCV camera rotation (rows = camera x right, y down, z forward in plate coordinates)."""
    z = target - cam
    z /= np.linalg.norm(z)
    x = np.cross(z, [0, 0, 1.0]) if abs(z[2]) < 0.99 else np.array([1.0, 0, 0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.stack([x, y, z])


def _frame(d, n, dist_m, tilt_deg, az_deg, a_true, b_true, floor=False, angle_mm_per_deg=0.0):
    """Write photo n, its depth and its manifest entry: the camera `dist_m` from the plate centre, tilted `tilt_deg` from straight
    down. The LiDAR reads `measured` such that true = a_true * measured + b_true."""
    target = np.array([0, 0, lt.PLATE])
    t, az = math.radians(tilt_deg), math.radians(az_deg)
    cam = target + dist_m * 1000 * np.array([math.sin(t) * math.cos(az), math.sin(t) * math.sin(az), math.cos(t)])
    R = _look_at(cam, target)
    tvec = -R @ cam
    K = np.array([[FX, 0, W / 2 - 0.5], [0, FX, H / 2 - 0.5], [0, 0, 1]])      # OpenCV pixel centres; ARKit cx = W/2
    # photo: a white plate, then each marker through the homography of its own plane (pad tops for 44 and 45)
    P = K @ np.c_[R, tvec]

    def warp(img, x0, y_top, size_mm, z):
        ppm = img.shape[1] / size_mm
        S = np.array([[1 / ppm, 0, x0], [0, -1 / ppm, y_top], [0, 0, 1]])        # texture px -> plate mm
        Hm = (P[:, [0, 1, 3]] + np.outer(P[:, 2], [0, 0, z])) @ S
        return (cv2.warpPerspective(img, Hm, (W, H), flags=cv2.INTER_AREA, borderValue=0),
                cv2.warpPerspective(np.full(img.shape, 255, np.uint8), Hm, (W, H), flags=cv2.INTER_NEAREST, borderValue=0))

    photo = np.full((H, W), 120, np.uint8)
    plate, m = warp(np.full((400, 400), 235, np.uint8), -lt.SIZE / 2, lt.SIZE / 2, lt.SIZE, lt.PLATE)
    photo[m > 0] = plate[m > 0]
    for i, (mx, my, mz, side) in lt.MARKERS.items():
        img = _marker_image(i, side, 4.0)
        span = side * 8 / 6
        w_, m = warp(img, mx - span / 2, my + span / 2, span, mz + lt.INLAY)
        photo[m > 0] = w_[m > 0]
    cv2.imwrite(str(d / "photos" / f"p{n:03d}.jpg"), photo)
    # depth: cast every depth pixel's ray against the shape
    fx, cx = FX * DW / W, (W / 2) * DW / W
    u, v = np.meshgrid(np.arange(DW) + 0.5, np.arange(DH) + 0.5)
    rays = np.stack([(u - cx) / fx, (v - cx * DH / DW) / fx, np.ones_like(u)], -1).reshape(-1, 3) @ R
    tz, _, face = lt.cast(cam, rays)
    tf = -cam[2] / rays[:, 2] / 1000
    true = np.where(np.isfinite(tz), tz / 1000, tf if floor else 2.0)   # off the print: the floor it lies on, or far away
    black = np.array(lt.colours())[np.maximum(face, 0) // 8] == "black"
    _, nrm, _ = lt.cast(cam, rays)
    un = rays / np.linalg.norm(rays, axis=1, keepdims=True)
    inc = np.degrees(np.arccos(np.clip(np.abs((un * np.where(np.isfinite(tz)[:, None], nrm, [[0, 0, 1.0]])).sum(1)), 0, 1)))
    meas = ((true - b_true) / a_true + np.where(black & (face >= 0), BLACK_EXTRA, 0) - angle_mm_per_deg * inc / 1000).astype("<f4").reshape(DH, DW)
    meas.tofile(d / "photos" / f"p{n:03d}.depth")
    M = np.eye(4)                                                   # ARKit pose: camera-to-world, camera looks down -Z, +Y up
    M[:3, :3] = R.T @ np.diag([1, -1, -1])
    M[:3, 3] = cam / 1000
    return {"file": f"p{n:03d}.jpg", "depth_file": f"p{n:03d}.depth", "width": W, "height": H, "depth_width": DW, "depth_height": DH,
            "intrinsics": [[FX, 0, 0], [0, FX, 0], [W / 2, H / 2, 1]], "transform": M.T.tolist(), "timestamp": n}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    d = tmp_path_factory.mktemp("lidarcal")
    (d / "photos").mkdir()
    photos = []
    for n, (dist, tilt, az) in enumerate([(0.3, 0, 0), (0.5, 0, 90), (0.5, 30, 0), (0.8, 30, 180), (0.5, 55, 45), (0.8, 0, 0)], 1):
        photos.append(_frame(d, n, dist, tilt, az, 1.02, -0.008))
    (d / "photos.json").write_text(json.dumps({"version": 2, "photos": photos}))
    return d, lt.analyse(d)


def test_the_fit_gives_back_the_lidar_error(run):
    _, res = run
    assert res["frames_used"] == 6, res.get("messages")
    assert res["fit"]["a"] == pytest.approx(1.02, abs=0.004) and res["fit"]["b"] == pytest.approx(-0.008, abs=0.002)
    assert res["fit"]["rms_after_mm"] < 1.0 and res["fit"]["rms_before_mm"] > 2
    assert res["good"]


def test_marker_poses_put_each_frame_at_its_station(run):
    _, res = run
    st = {f["file"]: f["station"] for f in res["frames_detail"]}
    assert st["p001.jpg"] == "d30t0" and st["p003.jpg"] == "d50t30" and st["p004.jpg"] == "d80t30" and st["p005.jpg"] == "d50t55"
    for f in res["frames_detail"]:
        assert len(f["markers"]) >= 2
    assert res["camera"]["tracking_scale"] == pytest.approx(1.0, abs=0.01)
    assert res["camera"]["marker_error_px"] < 1.0


def test_steep_surfaces_are_seen_in_the_angle_table(run):
    _, res = run
    bins = {b["deg"][0] for b in res["by_angle"]["after"]}
    assert 0 in bins and 45 in bins                                  # the flat top straight on, and the 45/60 degree wedges and slanted views


def test_rays_hit_the_shape_where_it_is():
    down = np.array([[0, 0, -1.0]])
    for (x, y), h in lt.PADS:
        t, n, _ = lt.cast(np.array([x, y, 500.0]), down)
        assert t[0] == pytest.approx(500 - lt.PLATE - h - lt.INLAY) and n[0] == pytest.approx([0, 0, 1])   # every pad wears a marker or a colour patch
    t, n, _ = lt.cast(np.array([0, 76.0, 500.0]), down)             # middle of the 15 degree wedge (+y edge)
    assert t[0] == pytest.approx(500 - lt.PLATE - (lt.WEDGE_OUT - 76) * math.tan(math.radians(15)))
    assert math.degrees(math.acos(n[0][2])) == pytest.approx(15)
    t, _, _ = lt.cast(np.array([0, 0.0, 500.0]), down)               # between the pads: the plate
    assert t[0] == pytest.approx(500 - lt.PLATE)
    t, _, _ = lt.cast(np.array([300, 0.0, 500.0]), down)             # off the print
    assert not np.isfinite(t[0])


def test_info_lists_the_stations_for_the_app():
    j = lt.info()
    ids = [s["id"] for s in j["stations"]]
    assert "d30t0" in ids and "d120t55" not in ids and len(ids) == 11
    assert j["marker_ids"] == [40, 41, 42, 43, 44, 45]


def test_markers_on_the_pads_are_used_and_each_colour_is_reported(run):
    _, res = run
    assert any(44 in f["markers"] or 45 in f["markers"] for f in res["frames_detail"])
    col = {c["colour"]: c for c in res["by_colour"]}
    assert col["white"]["bias_mm"] == pytest.approx(0, abs=0.5)
    assert col["black"]["bias_mm"] == pytest.approx(BLACK_EXTRA * 1000 * 1.02, abs=1.0)      # reads long by 4 mm, only black
    assert "blue" in col and "red" in col and abs(col["blue"]["bias_mm"]) < 0.5



def test_the_floor_and_the_angle_error_come_back(tmp_path):
    (tmp_path / "photos").mkdir()
    photos = [_frame(tmp_path, n, dist, tilt, az, 1.0, -0.007, floor=True, angle_mm_per_deg=0.25)
              for n, (dist, tilt, az) in enumerate([(0.3, 0, 0), (0.6, 0, 90), (1.0, 0, 0), (1.3, 0, 0), (0.6, 20, 0), (0.8, 30, 90), (0.8, 40, 0), (0.8, 50, 180)], 1)]
    (tmp_path / "photos.json").write_text(json.dumps({"version": 2, "photos": photos}))
    res = lt.analyse(tmp_path)
    assert res["good"] and res["fit"]["floor_frames"] >= 6
    assert res["fit"]["b"] == pytest.approx(-0.007, abs=0.002)
    table = {r["deg"]: r["m"] for r in res["angle_correction"]}
    assert table[45] == pytest.approx(0.25 * 45 / 1000, abs=0.003)          # reads short at an angle, so the correction adds
    assert "mm" in res["summary"] and res["fix"] is None


def test_a_bad_session_says_what_to_do(tmp_path):
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos.json").write_text(json.dumps({"version": 2, "photos": []}))
    res = lt.analyse(tmp_path)
    assert not res["good"] and res["fix"]


def test_depth_alignment(monkeypatch, tmp_path):
    """Stored depth is turned from the photo: a photo pixel at (x, y) is stored at row x - 32, column 223 - y."""
    from app import meshscan
    monkeypatch.setattr(meshscan, "DEPTH_TURNED", True)
    stored = np.zeros((192, 256), "<f4")
    stored[96 - 32 + 10, 223 - 50] = 3.0                  # photo-grid column 106, row 50
    out = meshscan.align_depth(stored)
    assert out.shape == (192, 256) and out[50, 106] == 3.0
    assert (out[:, :32] == 0).all() and (out[:, 224:] == 0).all()     # the photo's sides have no depth
