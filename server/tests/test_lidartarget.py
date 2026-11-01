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
                     '"43": [[1,1,1,1,1,1],[1,1,1,0,0,1],[1,0,1,1,1,1],[1,0,1,0,0,1],[1,1,1,0,1,1],[1,1,1,1,1,1]]}')


def test_marker_patterns_match_opencv():
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for i, g in MARKERS.items():
        assert ((cv2.aruco.generateImageMarker(d, int(i), 6) < 128).astype(int) == np.array(g)).all()


def _top_texture(px_per_mm: float = 4.0) -> np.ndarray:
    """The plate's top as seen from above: white with the black markers, x right, +y up."""
    n = int(lt.SIZE * px_per_mm)
    img = np.full((n, n), 235, np.uint8)
    cell = lt.MARKER / 6
    for i, (cx, cy) in lt.MARKERS.items():
        g = MARKERS[str(i)]
        for r in range(6):
            for c in range(6):
                if g[r][c]:
                    x0 = cx - lt.MARKER / 2 + c * cell
                    y1 = cy + lt.MARKER / 2 - r * cell
                    u0, v0 = int(round((x0 + lt.SIZE / 2) * px_per_mm)), int(round((lt.SIZE / 2 - y1) * px_per_mm))
                    img[v0:v0 + int(round(cell * px_per_mm)), u0:u0 + int(round(cell * px_per_mm))] = 20
    return img


def _look_at(cam: np.ndarray, target: np.ndarray) -> np.ndarray:
    """OpenCV camera rotation (rows = camera x right, y down, z forward in plate coordinates)."""
    z = target - cam
    z /= np.linalg.norm(z)
    x = np.cross(z, [0, 0, 1.0]) if abs(z[2]) < 0.99 else np.array([1.0, 0, 0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.stack([x, y, z])


def _frame(d, n, dist_m, tilt_deg, az_deg, a_true, b_true, tex):
    """Write photo n, its depth and its manifest entry: the camera `dist_m` from the plate centre, tilted `tilt_deg` from straight
    down. The LiDAR reads `measured` such that true = a_true * measured + b_true."""
    target = np.array([0, 0, lt.PLATE])
    t, az = math.radians(tilt_deg), math.radians(az_deg)
    cam = target + dist_m * 1000 * np.array([math.sin(t) * math.cos(az), math.sin(t) * math.sin(az), math.cos(t)])
    R = _look_at(cam, target)
    tvec = -R @ cam
    K = np.array([[FX, 0, W / 2 - 0.5], [0, FX, H / 2 - 0.5], [0, 0, 1]])      # OpenCV pixel centres; ARKit cx = W/2
    # photo: the plate top through the homography of the plane z = plate + inlay
    z = lt.PLATE + lt.INLAY
    ppm = 4.0
    S = np.array([[1 / ppm, 0, -lt.SIZE / 2], [0, -1 / ppm, lt.SIZE / 2], [0, 0, 1]])     # texture px -> plate mm
    P = K @ np.c_[R, tvec]
    Hm = P[:, [0, 1, 3]] + np.outer(P[:, 2], [0, 0, z])
    Hm = Hm @ S
    img = cv2.warpPerspective(tex, Hm, (W, H), flags=cv2.INTER_AREA, borderValue=120)
    cv2.imwrite(str(d / "photos" / f"p{n:03d}.jpg"), img)
    # depth: cast every depth pixel's ray against the shape
    fx, cx = FX * DW / W, (W / 2) * DW / W
    u, v = np.meshgrid(np.arange(DW) + 0.5, np.arange(DH) + 0.5)
    rays = np.stack([(u - cx) / fx, (v - cx * DH / DW) / fx, np.ones_like(u)], -1).reshape(-1, 3) @ R
    tz, _, _ = lt.cast(cam, rays)
    true = np.where(np.isfinite(tz), tz / 1000, 2.0)                # off the print: a floor 2 m away
    meas = ((true - b_true) / a_true).astype("<f4").reshape(DH, DW)
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
    tex = _top_texture()
    photos = []
    for n, (dist, tilt, az) in enumerate([(0.3, 0, 0), (0.5, 0, 90), (0.5, 30, 0), (0.8, 30, 180), (0.5, 55, 45), (0.8, 0, 0)], 1):
        photos.append(_frame(d, n, dist, tilt, az, 1.02, -0.008, tex))
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
        assert t[0] == pytest.approx(500 - lt.PLATE - h) and n[0] == pytest.approx([0, 0, 1])
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
    assert j["marker_ids"] == [40, 41, 42, 43]
