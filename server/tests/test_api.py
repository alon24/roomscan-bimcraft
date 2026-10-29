import io
import json
import os
import zipfile
from pathlib import Path

import pytest
import trimesh
from fastapi.testclient import TestClient

from app import main

FIX = Path(__file__).parent / "fixtures" / "synthetic_room.json"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    main.hub.live = None
    main.hub.viewers.clear()
    return TestClient(main.app)


def _upload(client, name="A", project=None):
    data = {"name": name, **({"project": project} if project else {})}
    return client.post("/api/scans", files={"json": ("s.json", FIX.read_bytes())}, data=data).json()["id"]


def _zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _members(content: bytes) -> set[str]:
    return {n.split("/")[0] for n in zipfile.ZipFile(io.BytesIO(content)).namelist() if "/" in n}


def test_upload_list_get(client):
    r = client.post("/api/scans", files={"json": ("scan.json", FIX.read_bytes())}, data={"name": "Test"})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert [m["id"] for m in client.get("/api/scans").json()] == [sid]
    assert client.get(f"/api/scans/{sid}").json()["name"] == "Test"
    assert client.get(f"/api/scans/{sid}/script.txt").text.startswith("wall ")
    assert client.get("/api/scans/nope").status_code == 404
    assert client.get(f"/api/scans/{sid}/settings.py").status_code == 404     # only known artifact names are served


@pytest.mark.skipif(not os.environ.get("BIMCRAFT_CLI"), reason="BIMCRAFT_CLI not set")
def test_full_conversion(client):
    sid = client.post("/api/scans", files={"json": ("s.json", FIX.read_bytes())}).json()["id"]
    assert client.get(f"/api/scans/{sid}").json()["status"] == "ok"
    assert client.post(f"/api/scans/{sid}/convert").json()["status"] == "ok"
    assert client.get(f"/api/scans/{sid}/room.ifc").status_code == 200


def test_object_upload_and_rescale(client, tmp_path):
    from test_objects import make_usdz
    usdz = make_usdz(tmp_path / "o.usdz")
    sid = client.post("/api/scans", files={"usdz": ("scan.usdz", usdz.read_bytes())}, data={"name": "Part", "kind": "object"}).json()["id"]
    meta = client.get(f"/api/scans/{sid}").json()
    assert meta["status"] == "ok" and meta["kind"] == "object" and meta["printable"]
    assert client.get(f"/api/scans/{sid}/report.json").json()["size_mm"] == [30.0, 30.0, 30.0]
    client.post(f"/api/scans/{sid}/convert?scale=2")
    assert client.get(f"/api/scans/{sid}/report.json").json()["size_mm"] == [60.0, 60.0, 60.0]


def test_manual_upload_of_a_ready_made_model(client):
    stl = trimesh.creation.box(extents=(30, 20, 10)).export(file_type="stl")
    r = client.post("/api/scans", data={"kind": "object", "name": "My box"}, files={"model": ("box.stl", io.BytesIO(stl))})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    sid = r.json()["id"]
    assert client.get(f"/api/scans/{sid}/print").json()["size_mm"] == [30.0, 20.0, 10.0]
    re = client.post(f"/api/scans/{sid}/convert?scale=2").json()    # a re-convert starts from the kept source
    assert re["status"] == "ok" and client.get(f"/api/scans/{sid}/print").json()["size_mm"] == [60.0, 40.0, 20.0]
    assert client.post("/api/scans", data={"kind": "object"}).status_code == 422
    assert client.post("/api/scans", data={"kind": "object"}, files={"model": ("x.txt", io.BytesIO(b"hi"))}).status_code == 422
    assert client.post("/api/scans", data={"kind": "object"}, files={"model": ("bad.stl", io.BytesIO(b"nope"))}).json()["status"] == "error"


def test_live_frames_and_scan_events_reach_viewers(client):
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
    client.post("/api/live", json=json.loads(FIX.read_text()))
    with client.websocket_connect("/ws/events") as ws:
        assert ws.receive_json()["type"] == "live"


def test_room_upload_with_photos(client):
    poses = [{"file": "p001.jpg", "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [2.1, 1.25, 0, 1]],
              "intrinsics": [[900, 0, 0], [0, 900, 0], [640, 360, 1]], "width": 1280, "height": 720}]
    files = [("json", ("scan.json", FIX.read_bytes())),
             ("photos", ("p001.jpg", b"\xff\xd8jpeg1")), ("photos", ("../evil.png", b"x")),
             ("photos_meta", ("photos.json", json.dumps({"version": 2, "photos": poses}).encode()))]
    sid = client.post("/api/scans", files=files).json()["id"]
    assert client.get(f"/api/scans/{sid}").json()["photo_count"] == 1
    assert client.get(f"/api/scans/{sid}/photos/p001.jpg").content == b"\xff\xd8jpeg1"
    assert client.get(f"/api/scans/{sid}/photos/evil.png").status_code == 404
    assert client.get(f"/api/scans/{sid}/photos/..%2Fscan.json").status_code == 404
    idx = client.get(f"/api/scans/{sid}/photo_index.json").json()
    assert len(idx) >= 1 and all(v["photos"][0]["photo"] == "p001.jpg" for v in idx.values())


def test_rename_and_project(client):
    sid = _upload(client)
    r = client.patch(f"/api/scans/{sid}", json={"name": "  Kitchen ", "project": "House"})
    assert r.json()["name"] == "Kitchen" and r.json()["project"] == "House"
    assert client.patch(f"/api/scans/{sid}", json={"name": ""}).status_code == 422
    assert client.patch(f"/api/scans/{sid}", json={"project": "a/b"}).status_code == 422   # /api/projects/a/b could never be reached
    client.patch(f"/api/scans/{sid}", json={"project": "Downstairs"})
    assert {p["name"]: p["scans"] for p in client.get("/api/projects").json()} == {"Downstairs": 1}


def test_tags_are_cleaned_and_kept(client):
    sid = _upload(client)
    assert client.patch(f"/api/scans/{sid}", json={"tags": ["#Gear ", "gear", "", "bracket"]}).json()["tags"] == ["Gear", "bracket", "gear"]
    assert client.get("/api/scans").json()[0]["tags"] == ["Gear", "bracket", "gear"]


def test_delete(client):
    sid = _upload(client)
    assert client.delete(f"/api/scans/{sid}").json() == {"deleted": sid}
    assert client.get(f"/api/scans/{sid}").status_code == 404
    assert client.delete(f"/api/scans/{sid}").status_code == 404


def test_bundle_roundtrip_import(client):
    sid = _upload(client, "Bedroom")
    z = client.get(f"/api/scans/{sid}/bundle.zip")
    assert z.status_code == 200 and "attachment" in z.headers["content-disposition"]
    assert {f"{sid}/scan.json", f"{sid}/meta.json"} <= set(zipfile.ZipFile(io.BytesIO(z.content)).namelist())
    new = client.post("/api/import", files={"file": ("b.zip", z.content)}, data={"project": "Restored"}).json()["imported"][0]
    assert new["id"] != sid and new["name"] == "Bedroom"
    meta = client.get(f"/api/scans/{new['id']}").json()
    assert meta["project"] == "Restored" and meta["imported_from"] == sid
    assert client.get(f"/api/scans/{new['id']}/scan.json").status_code == 200


def test_export_filters_by_project_and_ids(client):
    a, b, c = _upload(client, "A", "P1"), _upload(client, "B", "P2"), _upload(client, "C", "P2")
    assert _members(client.get("/api/export.zip", params={"project": "P1"}).content) == {a}
    assert _members(client.get(f"/api/export.zip?ids={a},{c}").content) == {a, c}
    assert client.get("/api/export.zip", params={"project": "nope"}).status_code == 404


def test_import_ignores_unsafe_and_unknown_members(client):
    r = client.post("/api/import", files={"file": ("e.zip", _zip({
        "x/meta.json": json.dumps({"id": "x", "name": "Evil", "created_at": 1}),
        "x/../../escape.txt": "no", "x/evil.sh": "no", "x/photos/../up.jpg": "no",
        "x/photos/ok.jpg": "yes", "x/masks/phone.json": '["ok.jpg"]'}))})
    nid = r.json()["imported"][0]["id"]
    d = main.DATA / nid
    assert client.get(f"/api/scans/{nid}/photos/ok.jpg").content == b"yes"
    assert (d / "masks" / "phone.json").exists()        # the phone's outlines: rebuilds keep them over auto-detected ones
    assert sorted(p.name for p in d.iterdir()) == ["masks", "meta.json", "photos"]
    assert client.post("/api/import", files={"file": ("n.zip", b"junk")}).status_code == 422
    # a meta.json that is not an object is not a scan, and leaves nothing behind to break the list
    assert client.post("/api/import", files={"file": ("l.zip", _zip({"y/meta.json": "[1, 2]"}))}).status_code == 422
    assert [m["id"] for m in client.get("/api/scans").json()] == [nid]


def test_project_lifecycle(client):
    assert client.post("/api/projects", json={"name": "Home"}).json() == {"name": "Home"}
    assert client.post("/api/projects", json={"name": "a/b"}).status_code == 422
    client.patch("/api/projects/Home", json={"name": "Home"})          # renaming to the same name once deleted the project
    assert [p["name"] for p in client.get("/api/projects").json()] == ["Home"]
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


def test_bad_input_does_not_break_the_list(client):
    sid = _upload(client)
    for bad in ("nan", "inf", "0", "-1"):   # NaN in meta.json once made every list request fail
        assert client.post(f"/api/scans/{sid}/convert?scale={bad}").status_code == 422
    assert client.post("/api/scans", files={"json": ("s.json", FIX.read_bytes())}, data={"scale": "nan"}).status_code == 422
    for name, text in (("broken", "{half writ"), ("old", '{"id": "old", "name": "no created_at"}')):
        (main.DATA / name).mkdir()
        (main.DATA / name / "meta.json").write_text(text)
    assert {m["id"] for m in client.get("/api/scans").json()} == {sid, "old"}
    assert client.get("/api/projects").status_code == 200


def test_issue_reports(client):
    assert client.post("/api/issues", json={"tab": "mesh", "message": "No surfaces", "log": ["a", "b"]}).status_code == 200
    items = client.get("/api/issues").json()
    assert items[0]["message"] == "No surfaces" and items[0]["log"] == ["a", "b"]
    assert client.delete(f"/api/issues/{items[0]['id']}").status_code == 200
    assert client.get("/api/issues").json() == []
    assert client.delete("/api/issues/nope").status_code == 404


def test_share_link_serves_only_that_model_and_can_be_revoked(client):
    for sid in ("s1", "s2"):
        d = main.DATA / sid
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"id": sid, "name": sid, "created_at": 1, "status": "ok"}))
        trimesh.creation.box(extents=(10, 20, 30)).export(d / "model.stl")
    c = client
    assert c.get("/s/not-a-real-token").status_code == 404
    tok = c.post("/api/scans/s1/share").json()["token"]
    assert c.post("/api/scans/s1/share").json()["token"] == tok                       # the same link every time
    assert "Shared model" in c.get(f"/s/{tok}").text
    m = c.get(f"/s/{tok}/mesh.json").json()
    assert m["name"] == "s1" and len(m["faces"]) == 36
    assert c.get(f"/s/{tok}/model.stl").content[:5] != b""
    assert c.get("/s/" + tok + "x/mesh.json").status_code == 404
    # an imported copy does not answer to the original's link (it would stay open after the original revokes it)
    copy = c.post("/api/import", files={"file": ("b.zip", c.get("/api/scans/s1/bundle.zip").content)}).json()["imported"][0]["id"]
    assert "share" not in c.get(f"/api/scans/{copy}").json()
    assert c.delete("/api/scans/s1/share").json() == {"shared": False}
    assert c.get(f"/s/{tok}/mesh.json").status_code == 404
    assert c.post("/api/scans/zzz/share").status_code == 404


def test_connector_that_does_not_fit_is_a_clear_400(client):
    """cut_plane turns printprep's "too small" log line into a 400: this pins that wording."""
    d = main.DATA / "small"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": "small", "name": "small", "created_at": 1, "status": "ok"}))
    trimesh.creation.box(extents=(8, 8, 20)).export(d / "model.stl")
    r = client.post("/api/scans/small/cut/plane", json={"planes": [{"origin": [0, 0, 0], "normal": [0, 0, 1]}],
                                                        "connector": {"type": "dowel", "size": 10}})
    assert r.status_code == 400 and "does not fit on this cut face" in r.json()["detail"]


def test_broadcast_does_not_wait_for_a_slow_viewer(monkeypatch):
    import asyncio, time

    class Viewer:
        def __init__(self, delay=0.0, fail=False):
            self.delay, self.fail, self.got, self.closed = delay, fail, [], False

        async def send_json(self, msg):
            if self.fail:
                raise RuntimeError("gone")
            await asyncio.sleep(self.delay)
            self.got.append(msg)

        async def close(self):
            self.closed = True

    hub = main.Hub()
    monkeypatch.setattr(main.Hub, "SEND_TIMEOUT", 0.3)
    fast, slow, dead = Viewer(), Viewer(delay=5), Viewer(fail=True)
    hub.viewers |= {fast, slow, dead}
    t = time.monotonic()
    asyncio.run(hub.broadcast({"type": "list"}))
    assert time.monotonic() - t < 1.5                       # not 5 s behind the slow one
    assert fast.got == [{"type": "list"}] and hub.viewers == {fast}
    assert slow.closed and dead.closed
