import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main

FIX = Path(__file__).parent / "fixtures" / "synthetic_room.json"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    return TestClient(main.app)


def test_upload_list_get(client):
    r = client.post("/api/scans", files={"json": ("scan.json", FIX.read_bytes())}, data={"name": "Test"})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert [m["id"] for m in client.get("/api/scans").json()] == [sid]
    assert client.get(f"/api/scans/{sid}").json()["name"] == "Test"
    assert client.get(f"/api/scans/{sid}/script.txt").text.startswith("wall ")


def test_unknown_scan_404(client):
    assert client.get("/api/scans/nope").status_code == 404
    assert client.get("/api/scans/../etc").status_code in (404, 422)


@pytest.mark.skipif(not os.environ.get("BIMCRAFT_CLI"), reason="BIMCRAFT_CLI not set")
def test_full_conversion(client):
    sid = client.post("/api/scans", files={"json": ("s.json", FIX.read_bytes())}).json()["id"]
    assert client.get(f"/api/scans/{sid}").json()["status"] == "ok"
    assert client.post(f"/api/scans/{sid}/convert").json()["status"] == "ok"
    assert client.get(f"/api/scans/{sid}/room.ifc").status_code == 200


def test_object_upload_and_rescale(client, tmp_path):
    from test_objects import make_usdz
    usdz = make_usdz(tmp_path / "o.usdz")
    r = client.post("/api/scans", files={"usdz": ("scan.usdz", usdz.read_bytes())},
                    data={"name": "Part", "kind": "object"})
    sid = r.json()["id"]
    meta = client.get(f"/api/scans/{sid}").json()
    assert meta["status"] == "ok" and meta["kind"] == "object" and meta["printable"]
    assert client.get(f"/api/scans/{sid}/model.stl").status_code == 200
    assert client.get(f"/api/scans/{sid}/report.json").json()["size_mm"] == [30.0, 30.0, 30.0]
    client.post(f"/api/scans/{sid}/convert?scale=2")
    assert client.get(f"/api/scans/{sid}/report.json").json()["size_mm"] == [60.0, 60.0, 60.0]


def test_object_upload_needs_usdz(client):
    r = client.post("/api/scans", data={"kind": "object"})
    assert r.status_code == 422
