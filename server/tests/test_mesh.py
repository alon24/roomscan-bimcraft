import json

import numpy as np
import trimesh
from fastapi.testclient import TestClient
from PIL import Image

from app import main

PHOTO = {"file": "p001.jpg", "timestamp": 0, "width": 100, "height": 100,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 2, 1]],  # camera at z=2, looking down -Z
         "intrinsics": [[100, 0, 0], [0, 100, 0], [50, 50, 1]]}


def plane_ply(path):
    v = np.array([[-.5, -.5, 0], [.5, -.5, 0], [.5, .5, 0], [-.5, .5, 0]])
    trimesh.Trimesh(v, [[0, 1, 2], [0, 2, 3]], process=False).export(path)


def test_mesh_scan_is_coloured_from_the_photo(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    plane_ply(tmp_path / "m.ply")
    Image.new("RGB", (100, 100), (200, 30, 30)).save(tmp_path / "p001.jpg")
    manifest = tmp_path / "photos.json"
    manifest.write_text(json.dumps({"version": 2, "photos": [PHOTO]}))
    c = TestClient(main.app)
    r = c.post("/api/scans", data={"kind": "mesh", "name": "wall"},
               files=[("mesh", ("scan.ply", open(tmp_path / "m.ply", "rb"))),
                      ("photos", ("p001.jpg", open(tmp_path / "p001.jpg", "rb"), "image/jpeg")),
                      ("photos_meta", ("photos.json", open(manifest, "rb")))])
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    sid = r.json()["id"]
    rep = c.get(f"/api/scans/{sid}/report.json").json()
    assert rep["vertices"] == 4 and rep["faces"] == 2 and rep["coloured"] == 1.0
    raw = c.get(f"/api/scans/{sid}/mesh.bin").content
    rgb = np.frombuffer(raw[4 * 12:4 * 12 + 12], dtype=np.uint8).reshape(4, 3)
    assert (abs(rgb.astype(int) - [200, 30, 30]).max() <= 6)


def test_mesh_without_photos_is_grey(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    plane_ply(tmp_path / "m.ply")
    c = TestClient(main.app)
    r = c.post("/api/scans", data={"kind": "mesh"}, files=[("mesh", ("scan.ply", open(tmp_path / "m.ply", "rb")))])
    assert r.json()["status"] == "ok"
    assert c.get(f"/api/scans/{r.json()['id']}/report.json").json()["coloured"] == 0.0


def test_mesh_kind_needs_a_mesh(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    assert TestClient(main.app).post("/api/scans", data={"kind": "mesh"}).status_code == 422


def test_point_cloud_scan_is_kept_and_meshed(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    rng = np.random.default_rng(1)
    v = rng.normal(size=(4000, 3))
    v = 0.5 * v / np.linalg.norm(v, axis=1)[:, None]
    trimesh.PointCloud(v, colors=np.tile([10, 200, 30, 255], (len(v), 1)).astype(np.uint8)).export(tmp_path / "c.ply")
    c = TestClient(main.app)
    r = c.post("/api/scans", data={"kind": "mesh"}, files=[("mesh", ("scan.ply", open(tmp_path / "c.ply", "rb")))])
    assert r.json()["status"] == "ok", r.text
    rep = c.get(f"/api/scans/{r.json()['id']}/report.json").json()
    assert rep["points"] == 4000
    assert len(c.get(f"/api/scans/{r.json()['id']}/points.bin").content) == 4000 * 15


def test_traced_outline_crops_and_closes_the_base(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    rng = np.random.default_rng(2)
    r, h, n = 0.05, 0.08, 6000
    th = rng.uniform(0, 2 * np.pi, n)
    side = np.c_[r * np.cos(th), rng.uniform(0, h, n), r * np.sin(th)]            # side of a cylinder on the table (y = 0)
    top = rng.uniform(-1, 1, (n // 3, 2)); top = top[np.linalg.norm(top, axis=1) < 1] * r
    top = np.c_[top[:, 0], np.full(len(top), h), top[:, 1]]
    junk = rng.uniform(-0.5, 0.5, (500, 3)) + [1.0, 0.1, 0]                       # the rest of the room, outside the outline
    pts = np.vstack([side, top, junk])
    trimesh.PointCloud(pts, colors=np.tile([200, 100, 50, 255], (len(pts), 1)).astype(np.uint8)).export(tmp_path / "c.ply")
    poly = [[r * 1.1 * np.cos(a), r * 1.1 * np.sin(a)] for a in np.linspace(0, 2 * np.pi, 16, endpoint=False)]
    (tmp_path / "fp.json").write_text(json.dumps({"polygon": poly, "base_y": 0.0, "height": h}))
    c = TestClient(main.app)
    res = c.post("/api/scans", data={"kind": "mesh"}, files=[("mesh", ("scan.ply", open(tmp_path / "c.ply", "rb"))),
                                                               ("footprint", ("footprint.json", open(tmp_path / "fp.json", "rb")))])
    assert res.json()["status"] == "ok", res.text
    rep = c.get(f"/api/scans/{res.json()['id']}/report.json").json()
    assert rep["stl"] is True and rep["faces"] > 100
    assert max(rep["size_mm"][:2]) < 130 and 70 < rep["size_mm"][2] < 95           # the 100 x 80 mm part, not the junk 1 m away
    assert c.get(f"/api/scans/{res.json()['id']}/model.stl").status_code == 200


def _frame_scan(tmp_path, monkeypatch):
    """A flat wall 2 m away seen by one frame with depth, plus a traced outline around the middle of it."""
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    plane_ply(tmp_path / "m.ply")
    Image.new("RGB", (100, 100), (30, 160, 30)).save(tmp_path / "p001.jpg")
    np.full((100, 100), 2.0, dtype="<f4").tofile(tmp_path / "p001.depth")
    photo = {**PHOTO, "intrinsics": [[50, 0, 0], [0, 50, 0], [50, 50, 1]], "depth_file": "p001.depth", "depth_width": 100, "depth_height": 100}
    (tmp_path / "photos.json").write_text(json.dumps({"version": 2, "photos": [photo]}))
    (tmp_path / "fp.json").write_text(json.dumps({"polygon": [[-.3, -.3], [.3, -.3], [.3, .3], [-.3, .3]], "base_y": -0.3, "height": 0.6}))
    c = TestClient(main.app)
    r = c.post("/api/scans", data={"kind": "mesh"}, files=[
        ("mesh", ("scan.ply", open(tmp_path / "m.ply", "rb"))), ("footprint", ("footprint.json", open(tmp_path / "fp.json", "rb"))),
        ("photos", ("p001.jpg", open(tmp_path / "p001.jpg", "rb"), "image/jpeg")),
        ("photos", ("p001.depth", open(tmp_path / "p001.depth", "rb"), "application/octet-stream")),
        ("photos_meta", ("photos.json", open(tmp_path / "photos.json", "rb")))])
    assert r.json()["status"] == "ok", r.text
    return c, r.json()["id"]


def test_frames_get_an_automatic_mask_from_depth_and_outline(tmp_path, monkeypatch):
    c, sid = _frame_scan(tmp_path, monkeypatch)
    frames = c.get(f"/api/scans/{sid}/frames").json()
    assert frames[0]["has_depth"] and frames[0]["has_mask"] and not frames[0]["edited"]
    m = np.asarray(Image.open(__import__("io").BytesIO(c.get(f"/api/scans/{sid}/masks/p001.png").content)).convert("L")) > 127
    assert m[50, 50] and not m[5, 5] and not m[95, 95]
    assert 150 < m.sum() < 400            # about the 16 x 16 pixels the outline covers, not the whole picture


def test_corrected_mask_is_kept_and_used_to_rebuild(tmp_path, monkeypatch):
    c, sid = _frame_scan(tmp_path, monkeypatch)
    rgba = np.zeros((100, 100, 4), dtype=np.uint8)
    rgba[40:60, 20:80] = (255, 0, 0, 200)             # the user paints a wider strip
    buf = __import__("io").BytesIO()
    Image.fromarray(rgba).save(buf, "PNG")
    assert c.put(f"/api/scans/{sid}/masks/p001.png", content=buf.getvalue()).status_code == 200
    assert c.get(f"/api/scans/{sid}/frames").json()[0]["edited"] is True
    meta = c.post(f"/api/scans/{sid}/rebuild").json()
    assert meta["status"] == "ok" and "fused 1 frames" in meta["log"]
    rep = c.get(f"/api/scans/{sid}/report.json").json()
    assert rep["points"] > 200                          # the painted strip, one point per 5 mm cell


def test_turntable_fusion_recovers_the_whole_part_from_one_side(tmp_path, monkeypatch):
    """A lopsided part on a plate turning at 0.3 rad/s, seen by a phone that never moves: the fused model must be the whole part."""
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    rng = np.random.default_rng(3)

    def box(lo, hi, n):  # points on the surface of an axis-aligned box
        lo, hi = np.array(lo, float), np.array(hi, float)
        p = rng.uniform(lo, hi, (n, 3))
        face = rng.integers(0, 6, n)
        for i in range(n):
            ax, side = face[i] // 2, face[i] % 2
            p[i, ax] = hi[ax] if side else lo[ax]
        return p
    part = np.vstack([box([-.06, 0, -.03], [.06, .06, .03], 6000), box([.02, .06, -.03], [.06, .14, .03], 3000)])  # an L, 12 x 14 x 6 cm
    w, h, fx, fy, omega = 96, 72, 90.0, 90.0, 0.3
    photos = []
    (tmp_path / "scans").mkdir(parents=True, exist_ok=True)
    files = []
    for k in range(30):
        t = k * 0.4
        c, s = np.cos(omega * t), np.sin(omega * t)
        p = np.c_[c * part[:, 0] + s * part[:, 2], part[:, 1], -s * part[:, 0] + c * part[:, 2]]   # the plate turns about Y
        z = 0.6 - p[:, 2]                                                                           # camera at z=0.6 looking down -Z
        u = (fx * p[:, 0] / z + w / 2).astype(int)
        v = (h / 2 - fy * (p[:, 1] - 0.07) / z).astype(int)                                          # camera height 0.07
        ok = (u >= 0) & (u < w) & (v >= 0) & (v < h) & (z > 0.1)
        depth = np.full((h, w), np.inf)
        np.minimum.at(depth, (v[ok], u[ok]), z[ok])
        depth[np.isinf(depth)] = 0
        name = f"p{k + 1:03d}"
        depth.astype("<f4").tofile(tmp_path / f"{name}.depth")
        Image.new("RGB", (w, h), (120, 120, 120)).save(tmp_path / f"{name}.jpg")
        photos.append({"file": f"{name}.jpg", "timestamp": t, "width": w, "height": h, "depth_file": f"{name}.depth",
                       "depth_width": w, "depth_height": h,
                       "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0.07, 0.6, 1]],
                       "intrinsics": [[fx, 0, 0], [0, fy, 0], [w / 2, h / 2, 1]]})
        files += [("photos", (f"{name}.jpg", open(tmp_path / f"{name}.jpg", "rb"), "image/jpeg")),
                  ("photos", (f"{name}.depth", open(tmp_path / f"{name}.depth", "rb"), "application/octet-stream"))]
    (tmp_path / "photos.json").write_text(json.dumps({"version": 2, "photos": photos}))
    (tmp_path / "fp.json").write_text(json.dumps({"polygon": [[-.12, -.12], [.12, -.12], [.12, .12], [-.12, .12]], "base_y": -0.01, "height": 0.2}))
    r = TestClient(main.app).post("/api/scans", data={"kind": "mesh", "turntable_scan": "true"},
                                  files=files + [("footprint", ("footprint.json", open(tmp_path / "fp.json", "rb"))),
                                                 ("photos_meta", ("photos.json", open(tmp_path / "photos.json", "rb")))])
    assert r.json()["status"] == "ok", r.text
    meta = TestClient(main.app).get(f"/api/scans/{r.json()['id']}").json()
    assert "turntable:" in meta["log"]
    rep = TestClient(main.app).get(f"/api/scans/{r.json()['id']}/report.json").json()
    ext = sorted(rep["size_mm"])
    assert 45 < ext[0] < 95 and 105 < ext[1] < 150 and 125 < ext[2] < 170, (rep["size_mm"], meta["log"])   # the 60 x 120 x 140 mm part
