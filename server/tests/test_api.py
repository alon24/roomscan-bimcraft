import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main

FIX = Path(__file__).parent / "fixtures" / "synthetic_room.json"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    main.hub.live = None
    main.hub.viewers.clear()
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


def test_live_frames_and_scan_events_reach_viewers(client):
    import json
    room = json.loads(FIX.read_text())
    with client.websocket_connect("/ws/events") as ws:
        assert client.post("/api/live", json=room).json()["viewers"] == 1
        msg = ws.receive_json()
        assert msg["type"] == "live" and len(msg["room"]["walls"]) == 4
        sid = client.post("/api/scans", files={"json": ("s.json", FIX.read_bytes())}).json()["id"]
        msg = ws.receive_json()
        assert msg["type"] == "scan" and msg["id"] == sid
        assert ws.receive_json() == {"type": "live_end"}


def test_late_viewer_gets_last_live_frame(client):
    import json
    client.post("/api/live", json=json.loads(FIX.read_text()))
    with client.websocket_connect("/ws/events") as ws:
        assert ws.receive_json()["type"] == "live"


def test_room_upload_with_photos(client):
    manifest = b'[{"file":"p001.jpg","transform":[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]}]'
    files = [("json", ("scan.json", FIX.read_bytes())),
             ("photos", ("p001.jpg", b"\xff\xd8jpeg1")), ("photos", ("../evil.png", b"x")),
             ("photos_meta", ("photos.json", manifest))]
    sid = client.post("/api/scans", files=files).json()["id"]
    assert client.get(f"/api/scans/{sid}").json()["photo_count"] == 1
    assert client.get(f"/api/scans/{sid}/photos/p001.jpg").content == b"\xff\xd8jpeg1"
    assert client.get(f"/api/scans/{sid}/photos.json").json()[0]["file"] == "p001.jpg"
    assert client.get(f"/api/scans/{sid}/photos/evil.png").status_code == 404
    assert client.get(f"/api/scans/{sid}/photos/..%2Fscan.json").status_code == 404


def test_photo_index_written_for_room_with_photos(client):
    poses = [{"file": "p001.jpg", "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [2.1, 1.25, 0, 1]],
              "intrinsics": [[900, 0, 0], [0, 900, 0], [640, 360, 1]], "width": 1280, "height": 720}]
    import json
    files = [("json", ("scan.json", FIX.read_bytes())), ("photos", ("p001.jpg", b"\xff\xd8x")),
             ("photos_meta", ("photos.json", json.dumps({"version": 2, "photos": poses}).encode()))]
    sid = client.post("/api/scans", files=files).json()["id"]
    idx = client.get(f"/api/scans/{sid}/photo_index.json").json()
    assert len(idx) >= 1 and all(v["photos"][0]["photo"] == "p001.jpg" for v in idx.values())
