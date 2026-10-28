import json
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


def _upload(client, name="A", project=None):
    data = {"name": name}
    if project:
        data["project"] = project
    return client.post("/api/scans", files={"json": ("s.json", FIX.read_bytes())}, data=data).json()["id"]


def test_rename_and_project(client):
    sid = _upload(client)
    r = client.patch(f"/api/scans/{sid}", json={"name": "  Kitchen ", "project": "House"})
    assert r.json()["name"] == "Kitchen" and r.json()["project"] == "House"
    assert client.patch(f"/api/scans/{sid}", json={"name": ""}).status_code == 422
    assert client.get("/api/scans").json()[0]["project"] == "House"


def test_delete(client):
    sid = _upload(client)
    assert client.delete(f"/api/scans/{sid}").json() == {"deleted": sid}
    assert client.get(f"/api/scans/{sid}").status_code == 404
    assert client.delete(f"/api/scans/{sid}").status_code == 404


def test_bundle_roundtrip_import(client):
    import io, zipfile
    sid = _upload(client, "Bedroom")
    z = client.get(f"/api/scans/{sid}/bundle.zip")
    assert z.status_code == 200 and "attachment" in z.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert f"{sid}/scan.json" in names and f"{sid}/meta.json" in names
    r = client.post("/api/import", files={"file": ("b.zip", z.content)}, data={"project": "Restored"})
    new = r.json()["imported"][0]
    assert new["id"] != sid and new["name"] == "Bedroom"
    meta = client.get(f"/api/scans/{new['id']}").json()
    assert meta["project"] == "Restored" and meta["imported_from"] == sid
    assert client.get(f"/api/scans/{new['id']}/room.ifc").status_code in (200, 404)  # depends on CLI availability
    assert client.get(f"/api/scans/{new['id']}/scan.json").status_code == 200


def test_export_project_filters(client):
    import io, zipfile
    a = _upload(client, "A", "P1")
    _upload(client, "B", "P2")
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/export.zip", params={"project": "P1"}).content))
    assert {n.split("/")[0] for n in z.namelist() if "/" in n} == {a}
    assert client.get("/api/export.zip", params={"project": "nope"}).status_code == 404


def test_import_ignores_unsafe_and_unknown_members(client):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x/meta.json", json.dumps({"id": "x", "name": "Evil", "created_at": 1}))
        z.writestr("x/../../escape.txt", "no")
        z.writestr("x/evil.sh", "no")
        z.writestr("x/photos/../up.jpg", "no")
        z.writestr("x/photos/ok.jpg", "yes")
    r = client.post("/api/import", files={"file": ("e.zip", buf.getvalue())})
    nid = r.json()["imported"][0]["id"]
    assert client.get(f"/api/scans/{nid}/photos/ok.jpg").content == b"yes"
    assert client.get(f"/api/scans/{nid}/evil.sh").status_code == 404
    assert client.post("/api/import", files={"file": ("n.zip", b"junk")}).status_code == 422


def test_project_lifecycle(client):
    assert client.post("/api/projects", json={"name": "Home"}).json() == {"name": "Home"}
    assert client.post("/api/projects", json={"name": "a/b"}).status_code == 422
    kitchen = _upload(client, "Kitchen", "Home")
    _upload(client, "Shed", "Garden")  # a project that exists only because a scan names it
    assert {p["name"]: p["scans"] for p in client.get("/api/projects").json()} == {"Home": 1, "Garden": 1}
    # rename moves every room with it
    client.patch("/api/projects/Home", json={"name": "My House"})
    assert client.get(f"/api/scans/{kitchen}").json()["project"] == "My House"
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert "Home" not in names and "My House" in names
    # delete keeps the rooms by default
    client.delete("/api/projects/My House")
    assert client.get(f"/api/scans/{kitchen}").json()["project"] == ""
    # ...or removes them on request
    client.delete("/api/projects/Garden", params={"delete_scans": True})
    assert [m["name"] for m in client.get("/api/scans").json()] == ["Kitchen"]


def test_move_room_between_projects(client):
    sid = _upload(client, "Bath", "Upstairs")
    client.patch(f"/api/scans/{sid}", json={"project": "Downstairs"})
    assert {p["name"]: p["scans"] for p in client.get("/api/projects").json()} == {"Downstairs": 1}


def test_issue_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    c = TestClient(main.app)
    r = c.post("/api/issues", json={"tab": "mesh", "message": "No surfaces", "log": ["a", "b"]})
    assert r.status_code == 200
    items = c.get("/api/issues").json()
    assert items[0]["message"] == "No surfaces" and items[0]["log"] == ["a", "b"]
    assert c.delete(f"/api/issues/{items[0]['id']}").status_code == 200
    assert c.get("/api/issues").json() == []
    assert c.delete("/api/issues/nope").status_code == 404


def test_export_only_the_chosen_scans(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    c = TestClient(main.app)
    ids = []
    for n in range(3):
        (tmp_path / "s.json").write_text(json.dumps({"walls": []}))
        ids.append(c.post("/api/scans", data={"name": f"s{n}"}, files={"json": ("scan.json", open(tmp_path / "s.json", "rb"))}).json()["id"])
    import io, zipfile
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/export.zip?ids={ids[0]},{ids[2]}").content))
    assert {n.split("/")[0] for n in z.namelist() if "/" in n} == {ids[0], ids[2]}


def test_manual_upload_of_a_ready_made_model(client):
    import io
    import trimesh
    stl = trimesh.creation.box(extents=(30, 20, 10)).export(file_type="stl")
    r = client.post("/api/scans", data={"kind": "object", "name": "My box"}, files={"model": ("box.stl", io.BytesIO(stl))})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    sid = r.json()["id"]
    assert client.get(f"/api/scans/{sid}/model.stl").status_code == 200
    assert client.get(f"/api/scans/{sid}/print").json()["size_mm"] == [30.0, 20.0, 10.0]
    re = client.post(f"/api/scans/{sid}/convert?scale=2").json()    # a re-convert starts from the kept source
    assert re["status"] == "ok" and client.get(f"/api/scans/{sid}/print").json()["size_mm"] == [60.0, 40.0, 20.0]
    assert client.post("/api/scans", data={"kind": "object"}, files={"model": ("x.txt", io.BytesIO(b"hi"))}).status_code == 422
    assert client.post("/api/scans", data={"kind": "object"}, files={"model": ("bad.stl", io.BytesIO(b"nope"))}).json()["status"] == "error"
