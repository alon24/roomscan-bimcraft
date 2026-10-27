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
