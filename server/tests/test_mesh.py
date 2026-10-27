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
