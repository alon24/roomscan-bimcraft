import json
import sys
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from app import main

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import make_ring  # noqa: E402


def test_ring_scan_builds_the_part_from_a_moving_phone(tmp_path, monkeypatch):
    """Phone walks round a ring with a 50 x 60 mm cylinder on it; the ring markers put every frame in one frame."""
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    px_mm = 4.0
    tex = make_ring.render_png(px_mm)
    S, c = tex.shape[0], tex.shape[0] / 2
    fx = fy = 1000.0
    W, H, cx, cy = 1280, 960, 640.0, 480.0
    rng = np.random.default_rng(5)
    th = rng.uniform(0, 2 * np.pi, 30000)
    z = rng.uniform(0, 60, 30000)
    side = np.c_[25 * np.cos(th), 25 * np.sin(th), z]                                   # ring frame, mm
    r = 25 * np.sqrt(rng.uniform(0, 1, 6000)); a = rng.uniform(0, 2 * np.pi, 6000)
    top = np.c_[r * np.cos(a), r * np.sin(a), np.full(6000, 60.0)]
    part = np.vstack([side, top])
    to_world = lambda q: np.c_[q[:, 0], q[:, 2], -q[:, 1]] / 1000.0                     # ring (z up) -> world (y up), metres

    def project(M, pts):
        cam = (np.linalg.inv(M) @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]
        depth = -cam[:, 2]
        return np.c_[fx * cam[:, 0] / depth + cx, cy - fy * cam[:, 1] / depth], depth

    photos, files = [], []
    (tmp_path / "scans").mkdir(parents=True, exist_ok=True)
    for k in range(12):
        az = np.radians(k * 30)
        pos = np.array([0.38 * np.cos(az), 0.26, 0.38 * np.sin(az)])
        f = -pos / np.linalg.norm(pos)
        right = np.cross(f, [0, 1, 0]); right /= np.linalg.norm(right)
        up = np.cross(right, f)
        M = np.eye(4); M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = right, up, -f, pos
        # the photo: the ring seen in perspective
        corners_ring = np.array([[(0 - c) / px_mm, (c - 0) / px_mm, 0], [(S - c) / px_mm, (c - 0) / px_mm, 0],
                                 [(S - c) / px_mm, (c - S) / px_mm, 0], [(0 - c) / px_mm, (c - S) / px_mm, 0]])
        dst, _ = project(M, to_world(corners_ring))
        Hm = cv2.getPerspectiveTransform(np.float32([[0, 0], [S, 0], [S, S], [0, S]]), np.float32(dst))
        img = cv2.warpPerspective(tex, Hm, (W, H), flags=cv2.INTER_AREA, borderValue=170)
        name = f"p{k + 1:03d}"
        Image.fromarray(img).convert("RGB").save(tmp_path / f"{name}.jpg", quality=95)
        # the depth map: only the part is in it
        dw, dh = 640, 480
        uv, depth = project(M, to_world(part))
        u, v = (uv[:, 0] * dw / W).astype(int), (uv[:, 1] * dh / H).astype(int)
        ok = (u >= 0) & (u < dw) & (v >= 0) & (v < dh)
        dm = np.full((dh, dw), np.inf)
        np.minimum.at(dm, (v[ok], u[ok]), depth[ok])
        dm[np.isinf(dm)] = 0
        dm = cv2.morphologyEx(dm.astype(np.float32), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)) * (dm > 0) if False else dm
        dm.astype("<f4").tofile(tmp_path / f"{name}.depth")
        photos.append({"file": f"{name}.jpg", "timestamp": k * 1.0, "width": W, "height": H, "depth_file": f"{name}.depth",
                       "depth_width": dw, "depth_height": dh,
                       "transform": [M[:3, 0].tolist() + [0], M[:3, 1].tolist() + [0], M[:3, 2].tolist() + [0], pos.tolist() + [1]],
                       "intrinsics": [[fx, 0, 0], [0, fy, 0], [cx, cy, 1]]})
        files += [("photos", (f"{name}.jpg", open(tmp_path / f"{name}.jpg", "rb"), "image/jpeg")),
                  ("photos", (f"{name}.depth", open(tmp_path / f"{name}.depth", "rb"), "application/octet-stream"))]
    (tmp_path / "photos.json").write_text(json.dumps({"version": 2, "photos": photos}))
    client = TestClient(main.app)
    r = client.post("/api/scans", data={"kind": "mesh", "ring_scan": "true"},
                    files=files + [("photos_meta", ("photos.json", open(tmp_path / "photos.json", "rb")))])
    meta = client.get(f"/api/scans/{r.json()['id']}").json()
    assert meta["status"] == "ok", meta["log"]
    assert "ring: 12 frames used" in meta["log"], meta["log"]
    size = sorted(client.get(f"/api/scans/{r.json()['id']}/report.json").json()["size_mm"])
    assert 38 < size[0] < 70 and 38 < size[1] < 70 and 50 < size[2] < 80, (size, meta["log"])    # the 50 x 50 x 60 mm part


def _ring_photos(tmp_path, marker, radius, n=6):
    """Photos + depth of the ring plane itself, for a ring printed with the given marker size and marker-centre radius."""
    make_ring.MARKER, make_ring.R_CENTRE = marker, radius
    px_mm = 4.0
    tex = make_ring.render_png(px_mm)
    S, c = tex.shape[0], tex.shape[0] / 2
    fx = fy = 1000.0
    W, H, cx, cy = 1280, 960, 640.0, 480.0
    rng = np.random.default_rng(7)
    rr = np.sqrt(rng.uniform((make_ring.RING_ID / 2) ** 2, (make_ring.RING_OD / 2) ** 2, 80000)); a = rng.uniform(0, 2 * np.pi, 80000)
    plane = np.c_[rr * np.cos(a), rr * np.sin(a), np.zeros(80000)]
    to_world = lambda q: np.c_[q[:, 0], q[:, 2], -q[:, 1]] / 1000.0
    photos, files = [], []
    (tmp_path / "w").mkdir(exist_ok=True)

    def project(M, pts):
        cam = (np.linalg.inv(M) @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]
        depth = -cam[:, 2]
        return np.c_[fx * cam[:, 0] / depth + cx, cy - fy * cam[:, 1] / depth], depth
    for k in range(n):
        az = np.radians(k * 60)
        pos = np.array([0.33 * np.cos(az), 0.25, 0.33 * np.sin(az)])
        f = -pos / np.linalg.norm(pos)
        right = np.cross(f, [0, 1, 0]); right /= np.linalg.norm(right)
        M = np.eye(4); M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = right, np.cross(right, f), -f, pos
        src = np.float32([[0, 0], [S, 0], [S, S], [0, S]])
        ring_c = np.array([[(0 - c) / px_mm, c / px_mm, 0], [(S - c) / px_mm, c / px_mm, 0], [(S - c) / px_mm, (c - S) / px_mm, 0], [(0 - c) / px_mm, (c - S) / px_mm, 0]])
        dst, _ = project(M, to_world(ring_c))
        img = cv2.warpPerspective(tex, cv2.getPerspectiveTransform(src, np.float32(dst)), (W, H), flags=cv2.INTER_AREA, borderValue=170)
        name = f"p{k + 1:03d}"
        Image.fromarray(img).convert("RGB").save(tmp_path / f"{name}.jpg", quality=95)
        uv, depth = project(M, to_world(plane))
        u, v = (uv[:, 0] / 2).astype(int), (uv[:, 1] / 2).astype(int)
        ok = (u >= 0) & (u < 640) & (v >= 0) & (v < 480)
        dm = np.full((480, 640), np.inf); np.minimum.at(dm, (v[ok], u[ok]), depth[ok]); dm[np.isinf(dm)] = 0
        dm.astype("<f4").tofile(tmp_path / f"{name}.depth")
        photos.append({"file": f"{name}.jpg", "timestamp": k, "width": W, "height": H, "depth_file": f"{name}.depth", "depth_width": 640, "depth_height": 480,
                       "transform": [M[:3, 0].tolist() + [0], M[:3, 1].tolist() + [0], M[:3, 2].tolist() + [0], pos.tolist() + [1]],
                       "intrinsics": [[fx, 0, 0], [0, fy, 0], [cx, cy, 1]]})
        files += [("photos", (f"{name}.jpg", open(tmp_path / f"{name}.jpg", "rb"), "image/jpeg")),
                  ("photos", (f"{name}.depth", open(tmp_path / f"{name}.depth", "rb"), "application/octet-stream"))]
    (tmp_path / "photos.json").write_text(json.dumps({"version": 2, "photos": photos}))
    files.append(("photos_meta", ("photos.json", open(tmp_path / "photos.json", "rb"))))
    return files


def test_ring_calibration_accepts_the_right_sizes_and_warns_on_wrong_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    (tmp_path / "scans").mkdir()
    saved = (make_ring.MARKER, make_ring.R_CENTRE)
    try:
        files = _ring_photos(tmp_path, 21.0, 80.0)        # a ring printed 4.5% small
        c = TestClient(main.app)
        good = c.post("/api/ring/calibrate", data={"marker_mm": "21.0", "centre_distance_mm": "160.0"}, files=files).json()
        assert good["good"] and good["saved"] and good["photos_good"] >= 5 and good["error_px"] < 1.5, good
        assert 0.96 < good["lidar_scale"] < 1.04, good
        info = c.get("/api/ring").json()
        assert info["calibrated"] and info["marker_size_mm"] == 21.0 and info["centre_radius_mm"] == 80.0
        files = _ring_photos(tmp_path, 21.0, 80.0)
        bad = c.post("/api/ring/calibrate", data={"marker_mm": "24.0", "centre_distance_mm": "176.0", "save": "false"}, files=files).json()
        assert not bad["good"] and bad["lidar_scale"] < 0.95 and any("smaller" in m or "bigger" in m for m in bad["messages"]), bad
        assert c.delete("/api/ring/calibration").status_code == 200 and not c.get("/api/ring").json()["calibrated"]
    finally:
        make_ring.MARKER, make_ring.R_CENTRE = saved


def test_ring_readings_from_calipers_are_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    (tmp_path / "scans").mkdir()
    c = TestClient(main.app)
    r = c.post("/api/ring/measured", data={"marker_mm": "22.2", "across_mm": "183.38"}).json()
    assert r["saved"] and abs(r["centre_radius_mm"] - 80.59) < 0.01
    info = c.get("/api/ring").json()
    assert info["calibrated"] and info["marker_size_mm"] == 22.2
    assert c.post("/api/ring/measured", data={"marker_mm": "5", "across_mm": "183"}).status_code == 422


def test_ring_scan_with_outlines_carves_the_true_shape(tmp_path, monkeypatch):
    """Same moving phone and 50 x 50 x 60 mm cylinder, now with the part's outline in each photo: the carved shape is exact."""
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    px_mm = 4.0
    tex = make_ring.render_png(px_mm)
    S, c = tex.shape[0], tex.shape[0] / 2
    fx = fy = 1000.0
    W, H, cx, cy = 1280, 960, 640.0, 480.0
    rng = np.random.default_rng(5)
    th = rng.uniform(0, 2 * np.pi, 60000); z = rng.uniform(0, 60, 60000)
    r = 25 * np.sqrt(rng.uniform(0, 1, 12000)); a = rng.uniform(0, 2 * np.pi, 12000)
    part = np.vstack([np.c_[25 * np.cos(th), 25 * np.sin(th), z], np.c_[r * np.cos(a), r * np.sin(a), np.full(12000, 60.0)]])
    to_world = lambda q: np.c_[q[:, 0], q[:, 2], -q[:, 1]] / 1000.0

    def project(M, pts):
        cam = (np.linalg.inv(M) @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]
        depth = -cam[:, 2]
        return np.c_[fx * cam[:, 0] / depth + cx, cy - fy * cam[:, 1] / depth], depth
    photos, files = [], []
    (tmp_path / "scans").mkdir(parents=True, exist_ok=True)
    for k in range(12):
        az = np.radians(k * 30)
        low = k % 2 == 1                                      # two passes: one looking down, one nearly level with the part's top
        pos = np.array([0.38 * np.cos(az), 0.10 if low else 0.26, 0.38 * np.sin(az)])
        f = -pos / np.linalg.norm(pos)
        right = np.cross(f, [0, 1, 0]); right /= np.linalg.norm(right)
        M = np.eye(4); M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = right, np.cross(right, f), -f, pos
        ring_c = np.array([[(0 - c) / px_mm, c / px_mm, 0], [(S - c) / px_mm, c / px_mm, 0], [(S - c) / px_mm, (c - S) / px_mm, 0], [(0 - c) / px_mm, (c - S) / px_mm, 0]])
        dst, _ = project(M, to_world(ring_c))
        img = cv2.warpPerspective(tex, cv2.getPerspectiveTransform(np.float32([[0, 0], [S, 0], [S, S], [0, S]]), np.float32(dst)), (W, H), flags=cv2.INTER_AREA, borderValue=170)
        name = f"p{k + 1:03d}"
        Image.fromarray(img).convert("RGB").save(tmp_path / f"{name}.jpg", quality=95)
        uv, depth = project(M, to_world(part))
        dm = np.full((480, 640), np.inf)
        u2, v2 = (uv[:, 0] / 2).astype(int), (uv[:, 1] / 2).astype(int)
        ok = (u2 >= 0) & (u2 < 640) & (v2 >= 0) & (v2 < 480)
        np.minimum.at(dm, (v2[ok], u2[ok]), depth[ok]); dm[np.isinf(dm)] = 0
        dm.astype("<f4").tofile(tmp_path / f"{name}.depth")
        mask = np.zeros((H, W), np.uint8)                       # the outline: every pixel the part covers
        ok = (uv[:, 0] >= 0) & (uv[:, 0] < W) & (uv[:, 1] >= 0) & (uv[:, 1] < H)
        mask[uv[ok, 1].astype(int), uv[ok, 0].astype(int)] = 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        Image.fromarray(mask).save(tmp_path / f"{name}.png")
        photos.append({"file": f"{name}.jpg", "timestamp": k, "width": W, "height": H, "depth_file": f"{name}.depth", "depth_width": 640, "depth_height": 480,
                       "transform": [M[:3, 0].tolist() + [0], M[:3, 1].tolist() + [0], M[:3, 2].tolist() + [0], pos.tolist() + [1]],
                       "intrinsics": [[fx, 0, 0], [0, fy, 0], [cx, cy, 1]]})
        files += [("photos", (f"{name}.jpg", open(tmp_path / f"{name}.jpg", "rb"), "image/jpeg")),
                  ("photos", (f"{name}.depth", open(tmp_path / f"{name}.depth", "rb"), "application/octet-stream")),
                  ("masks", (f"{name}.png", open(tmp_path / f"{name}.png", "rb"), "image/png"))]
    (tmp_path / "photos.json").write_text(json.dumps({"version": 2, "photos": photos}))
    client = TestClient(main.app)
    r = client.post("/api/scans", data={"kind": "mesh", "ring_scan": "true"},
                    files=files + [("photos_meta", ("photos.json", open(tmp_path / "photos.json", "rb")))])
    meta = client.get(f"/api/scans/{r.json()['id']}").json()
    assert meta["status"] == "ok" and "outline carving from 12 photos" in meta["log"], meta["log"]
    size = sorted(client.get(f"/api/scans/{r.json()['id']}/report.json").json()["size_mm"])
    assert 46 < size[0] < 56 and 46 < size[1] < 56 and 55 < size[2] < 66, (size, meta["log"])   # 50 x 50 x 60 mm, within a few mm


def test_outline_that_includes_the_plate_is_cleaned_with_the_phones_separate_things(tmp_path, monkeypatch):
    """Like above, but the phone's outline also covers the plate and ring (as Vision often does). With the separate things it
    found (part = 1, plate and ring = 2) the plate is left out and the shape is still exact."""
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    px_mm = 4.0
    tex = make_ring.render_png(px_mm)
    S, c = tex.shape[0], tex.shape[0] / 2
    fx = fy = 1000.0
    W, H, cx, cy = 1280, 960, 640.0, 480.0
    rng = np.random.default_rng(5)
    th = rng.uniform(0, 2 * np.pi, 60000); z = rng.uniform(0, 60, 60000)
    r = 25 * np.sqrt(rng.uniform(0, 1, 12000)); a = rng.uniform(0, 2 * np.pi, 12000)
    part = np.vstack([np.c_[25 * np.cos(th), 25 * np.sin(th), z], np.c_[r * np.cos(a), r * np.sin(a), np.full(12000, 60.0)]])
    to_world = lambda q: np.c_[q[:, 0], q[:, 2], -q[:, 1]] / 1000.0

    def project(M, pts):
        cam = (np.linalg.inv(M) @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]
        depth = -cam[:, 2]
        return np.c_[fx * cam[:, 0] / depth + cx, cy - fy * cam[:, 1] / depth], depth
    photos, files = [], []
    (tmp_path / "scans").mkdir(parents=True, exist_ok=True)
    for k in range(12):
        az = np.radians(k * 30)
        low = k % 2 == 1                                      # two passes: one looking down, one nearly level with the part's top
        pos = np.array([0.38 * np.cos(az), 0.10 if low else 0.26, 0.38 * np.sin(az)])
        f = -pos / np.linalg.norm(pos)
        right = np.cross(f, [0, 1, 0]); right /= np.linalg.norm(right)
        M = np.eye(4); M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = right, np.cross(right, f), -f, pos
        ring_c = np.array([[(0 - c) / px_mm, c / px_mm, 0], [(S - c) / px_mm, c / px_mm, 0], [(S - c) / px_mm, (c - S) / px_mm, 0], [(0 - c) / px_mm, (c - S) / px_mm, 0]])
        dst, _ = project(M, to_world(ring_c))
        img = cv2.warpPerspective(tex, cv2.getPerspectiveTransform(np.float32([[0, 0], [S, 0], [S, S], [0, S]]), np.float32(dst)), (W, H), flags=cv2.INTER_AREA, borderValue=170)
        name = f"p{k + 1:03d}"
        Image.fromarray(img).convert("RGB").save(tmp_path / f"{name}.jpg", quality=95)
        uv, depth = project(M, to_world(part))
        dm = np.full((480, 640), np.inf)
        u2, v2 = (uv[:, 0] / 2).astype(int), (uv[:, 1] / 2).astype(int)
        ok = (u2 >= 0) & (u2 < 640) & (v2 >= 0) & (v2 < 480)
        np.minimum.at(dm, (v2[ok], u2[ok]), depth[ok]); dm[np.isinf(dm)] = 0
        dm.astype("<f4").tofile(tmp_path / f"{name}.depth")
        mask = np.zeros((H, W), np.uint8)                       # the outline: every pixel the part covers
        ok = (uv[:, 0] >= 0) & (uv[:, 0] < W) & (uv[:, 1] >= 0) & (uv[:, 1] < H)
        mask[uv[ok, 1].astype(int), uv[ok, 0].astype(int)] = 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        disc = np.zeros((H, W), np.uint8)                       # the plate and ring as the phone would also outline them
        a = np.linspace(0, 2 * np.pi, 90)
        rim, _ = project(M, to_world(np.c_[98 * np.cos(a), 98 * np.sin(a), np.zeros(90)]))
        cv2.fillPoly(disc, [rim.astype(np.int32)], 255)
        labels = np.where(mask > 0, 1, np.where(disc > 0, 2, 0)).astype(np.uint8)
        Image.fromarray(np.maximum(mask, disc)).save(tmp_path / f"{name}.png")
        Image.fromarray(labels).save(tmp_path / f"{name}-labels.png")
        photos.append({"file": f"{name}.jpg", "timestamp": k, "width": W, "height": H, "depth_file": f"{name}.depth", "depth_width": 640, "depth_height": 480,
                       "transform": [M[:3, 0].tolist() + [0], M[:3, 1].tolist() + [0], M[:3, 2].tolist() + [0], pos.tolist() + [1]],
                       "intrinsics": [[fx, 0, 0], [0, fy, 0], [cx, cy, 1]]})
        files += [("photos", (f"{name}.jpg", open(tmp_path / f"{name}.jpg", "rb"), "image/jpeg")),
                  ("photos", (f"{name}.depth", open(tmp_path / f"{name}.depth", "rb"), "application/octet-stream")),
                  ("masks", (f"{name}.png", open(tmp_path / f"{name}.png", "rb"), "image/png")),
                  ("masks", (f"{name}-labels.png", open(tmp_path / f"{name}-labels.png", "rb"), "image/png"))]
    (tmp_path / "photos.json").write_text(json.dumps({"version": 2, "photos": photos}))
    client = TestClient(main.app)
    r = client.post("/api/scans", data={"kind": "mesh", "ring_scan": "true"},
                    files=files + [("photos_meta", ("photos.json", open(tmp_path / "photos.json", "rb")))])
    meta = client.get(f"/api/scans/{r.json()['id']}").json()
    assert meta["status"] == "ok" and "outline carving from 12 photos" in meta["log"], meta["log"]
    size = sorted(client.get(f"/api/scans/{r.json()['id']}/report.json").json()["size_mm"])
    assert 46 < size[0] < 56 and 46 < size[1] < 56 and 55 < size[2] < 66, (size, meta["log"])   # 50 x 50 x 60 mm, within a few mm


def test_quality_report_finds_the_gaps_and_the_missing_low_pass(tmp_path):
    import shutil
    from app import quality
    _ring_photos(tmp_path, make_ring.MARKER, make_ring.R_CENTRE, n=4)       # frames at 0, 60, 120, 180 degrees, one a second, one height
    d = tmp_path / "scan"
    (d / "photos").mkdir(parents=True)
    for f in tmp_path.glob("p*.jpg"):
        shutil.copy(f, d / "photos" / f.name)
    shutil.copy(tmp_path / "photos.json", d / "photos.json")
    q = quality.analyse(d)
    assert q["usable"] == 4 and q["frames"] == 4
    assert any(g[0] >= 180 or g[1] > 180 for g in q["gaps"]), q["gaps"]       # nothing between 180 and 360 degrees
    assert any("low pass" in t for t in q["tips"])                            # all frames at one height
    assert q["grade"] in ("poor", "fair") and q["score"] < 80
    assert 50 < q["speed"]["median"] < 70                                     # 60 degrees a second in the synthetic scan
