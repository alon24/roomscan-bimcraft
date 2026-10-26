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
