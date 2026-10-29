"""Live guided capture (docs/live-guide.md): coverage engine, WebSocket protocol, finish, offline completion, patching a scan."""
import asyncio
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))
import live_replay  # noqa: E402
import liveroom  # noqa: E402

from app import coverage, live, main, registration  # noqa: E402
from app.photos import _mat  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    (tmp_path / "scans").mkdir()
    monkeypatch.setattr(live, "MAP_EVERY", 0.2)
    live.SESSIONS.clear()
    main.hub.live = None
    main.hub.viewers.clear()
    main.hub.live_maps = {}
    with TestClient(main.app) as c:
        yield c
    for s in list(live.SESSIONS.values()):
        s.jobs.put(None)
    live.SESSIONS.clear()


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Ray-traced frames are the slow part of these tests: render each set once per module (the tests only read them)."""
    cache = {}

    def get(name, specs, **kw):
        if name not in cache:
            d = tmp_path_factory.mktemp(name)
            photos = liveroom.make_frames(d, specs, **kw)
            (d / "photos.json").write_text(json.dumps({"version": 2, "photos": photos}))
            cache[name] = (d, photos)
        return cache[name]
    return get


def sweep_source(rendered):
    return rendered("sweep", liveroom.specs_sweep(yaw_step=30))


def send_frames(ws, d, entries, first=1):
    """Send frames and return (acks in order, other messages that arrived meanwhile)."""
    acks, others = [], []
    for i, e in enumerate(entries):
        ws.send_bytes(live_replay.pack_frame(d, e, first + i))
        while True:
            m = ws.receive_json()
            if m["type"] == "ack":
                acks.append(m["n"])
                break
            others.append(m)
    return acks, others


def hello(ws, resume_after=0):
    """Say hello and read the server's cumulative ack (the number of frames it already holds)."""
    ws.send_json({"type": "hello", "resume_after": resume_after, "app": "test"})
    m = wait_message(ws, "ack")
    return m["n"]


def wait_status(client, sid, want=("ok", "error"), timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = client.get(f"/api/scans/{sid}").json()["status"]
        if st in want:
            return st
        time.sleep(0.3)
    raise AssertionError(f"scan {sid} stayed {st}")


def wait_message(ws, kind, limit=200):
    for _ in range(limit):
        m = ws.receive_json()
        if m["type"] == kind:
            return m
    raise AssertionError(f"no {kind} message")


def wait_state(ws, status, limit=200):
    """Skip earlier messages, including the `state: capturing` a hello may still have queued."""
    for _ in range(limit):
        m = ws.receive_json()
        if m["type"] == "state" and m["status"] == status:
            return m
    raise AssertionError(f"no state {status}")


# ------------------------------------------------------------------ the coverage engine

def feed(engine, d, entries, upto=None):
    for e in entries[:upto]:
        depth = np.fromfile(d / "photos" / e["depth_file"], dtype="<f4").reshape(e["depth_height"], e["depth_width"])
        engine.add_frame(e, depth)


def look_azimuth(need):
    return math.degrees(math.atan2(need["look"][0], -need["look"][2]))


def test_sweep_needs_point_at_what_is_unseen_and_percent_rises(rendered):
    d, photos = sweep_source(rendered)
    eng = coverage.CoverageEngine("sweep")
    pcts = []
    # the first third looks only down (pitch -40 at every azimuth): the walls and the ceiling are still unseen
    feed(eng, d, photos[:12])
    m = eng.next_message(force=True)
    pcts.append(m["percent"])
    kinds = {x["kind"] for x in m["needs"]}
    assert {"gap", "ceiling"} & kinds, m["needs"]
    wall = next(x for x in m["needs"] if x["kind"] == "gap")
    assert abs(wall["look"][1]) < 0.2 and wall["stand"] is not None          # walls are straight ahead, not up or down
    assert "wall" in wall["text"]
    feed(eng, d, photos[12:24])
    pcts.append(eng.next_message(force=True)["percent"])
    feed(eng, d, photos[24:])
    m = eng.next_message(force=True)
    pcts.append(m["percent"])
    for _ in range(4):                                                         # a satisfied need stays three updates, then goes
        m = eng.next_message(force=True)
    assert pcts[0] < pcts[1] < pcts[2], pcts
    assert pcts[2] >= 85
    assert not [x for x in m["needs"] if x["priority"] >= 2], m["needs"]
    assert m["grid"]["w"] <= 96 and m["grid"]["h"] <= 96 and len(m["grid"]["data"]) < 8 * 1024
    assert m["you"] is not None


def test_walked_room_needs_point_at_the_unseen_walls(tmp_path):
    d = tmp_path / "walk"
    # a walk along the +x side, looking at the +x wall only
    specs = [(y, p, np.array([0.7, 1.3, z])) for z in (-0.8, -0.3, 0.2, 0.7, 1.2) for y in (70, 90, 110) for p in (-15, 10)]
    photos = liveroom.make_frames(d, specs)
    eng = coverage.CoverageEngine("room")
    pcts = []
    for lo, hi in ((0, 6), (6, 18), (18, len(photos))):
        feed(eng, d, photos[lo:hi])
        pcts.append(eng.next_message(force=True)["percent"])
    assert pcts[0] <= pcts[1] <= pcts[2]
    m = eng.next_message(force=True)
    walls = [x for x in m["needs"] if x["kind"] == "gap"]
    assert walls, m["needs"]
    az = look_azimuth(walls[0]) % 360
    assert not (30 <= az <= 150), (az, walls[0]["text"])                  # the red +x wall (azimuth 90) is the one that WAS seen
    assert m["percent"] < 70, m["percent"]                                 # one wall of four is not a finished room


def test_needs_keep_their_ids_and_order_when_nothing_changes(rendered):
    d, photos = sweep_source(rendered)
    eng = coverage.CoverageEngine("sweep")
    feed(eng, d, photos[:14])
    a = eng.next_message(force=True)
    b = eng.next_message(force=True)
    c = eng.next_message(force=True)
    ids = [n["id"] for n in a["needs"]]
    assert ids and ids == [n["id"] for n in b["needs"]] == [n["id"] for n in c["needs"]]
    assert [n["target"] for n in a["needs"]] == [n["target"] for n in c["needs"]]


# ------------------------------------------------------------------ the WebSocket

def test_live_roundtrip_ack_resume_finish_equals_batch(client, rendered):
    d, photos = sweep_source(rendered)
    r = client.post("/api/live", json={"kind": "sweep", "name": "Live test", "info": {"app": "test"}})
    assert r.status_code == 200, r.text
    start = r.json()
    sid = start["scan_id"]
    assert start["ws"] == f"/ws/live/{sid}" and start["patch"] is False
    meta = client.get(f"/api/scans/{sid}").json()
    assert meta["status"] == "capturing" and meta["kind"] == "mesh" and meta["phone"]["sweep"] is True
    assert [m["id"] for m in client.get("/api/scans").json()] == [sid]

    with client.websocket_connect(start["ws"]) as ws:
        assert hello(ws, 0) == 0
        acks, _ = send_frames(ws, d, photos[:15])
        assert acks == list(range(1, 16))
    # the connection is gone; the phone comes back and resumes from its last ack, resending three frames it was not sure about
    with client.websocket_connect(start["ws"]) as ws:
        assert hello(ws, 15) == 15
        acks, others = send_frames(ws, d, photos[12:], first=13)
        assert acks == [15, 15, 15] + list(range(16, len(photos) + 1))          # duplicates change nothing; the ack stays cumulative
        maps = [m for m in others if m["type"] == "map"]
        live.SESSIONS[sid].jobs.join()                                            # every frame is in the map; hello asks for it now
        ws.send_json({"type": "hello", "resume_after": len(photos), "app": "test"})
        wait_message(ws, "state")
        # a ticker map computed while the last frames were still being integrated can still be on its way (a busy machine): the
        # engine has changed since, so a fresh map always follows it
        for _ in range(5):
            maps.append(wait_message(ws, "map"))
            if maps[-1]["percent"] >= 80:
                break
        assert maps[-1]["percent"] >= 80 and maps[-1]["grid"]["w"] > 0
        assert [m["seq"] for m in maps] == sorted(m["seq"] for m in maps)

        stored = json.loads((main.DATA / sid / "photos.json").read_text())["photos"]
        assert len(stored) == len(photos)
        assert len({p["file"] for p in stored}) == len(photos) and len({p["timestamp"] for p in stored}) == len(photos)
        assert all((main.DATA / sid / "photos" / p["file"]).exists() and (main.DATA / sid / "photos" / p["depth_file"]).exists() for p in stored)

        ws.send_json({"type": "finish", "info": {"scan type": "Sweep from one spot", "sweep": True}})
        wait_state(ws, "building")
        assert wait_status(client, sid) == "ok", client.get(f"/api/scans/{sid}").json()["log"]
        wait_state(ws, "ready")
    live_meta = client.get(f"/api/scans/{sid}").json()
    assert live_meta["photo_count"] == len(photos)
    live_report = json.loads((main.DATA / sid / "report.json").read_text())
    live_room = json.loads((main.DATA / sid / "room.json").read_text())
    assert (main.DATA / sid / "scan.ply").exists() and (main.DATA / sid / "colored.ply").exists()

    # the same frames through the ordinary batch upload give the same model
    files = [("photos", (p["file"], (d / "photos" / p["file"]).read_bytes())) for p in photos]
    files += [("photos", (p["depth_file"], (d / "photos" / p["depth_file"]).read_bytes())) for p in photos]
    files += [("photos_meta", ("photos.json", (d / "photos.json").read_bytes())), ("mesh", ("scan.ply", (main.DATA / sid / "scan.ply").read_bytes()))]
    b = client.post("/api/scans", files=files, data={"name": "batch", "kind": "mesh", "info": json.dumps({"scan type": "Sweep from one spot", "sweep": True})})
    assert b.status_code == 200 and b.json()["status"] == "ok"
    bid = b.json()["id"]
    batch_report = json.loads((main.DATA / bid / "report.json").read_text())
    batch_room = json.loads((main.DATA / bid / "room.json").read_text())
    assert live_report["faces"] == pytest.approx(batch_report["faces"], rel=0.02)
    assert live_report["vertices"] == pytest.approx(batch_report["vertices"], rel=0.02)
    for k in ("width_m", "length_m", "height_m"):
        assert live_room[k] == pytest.approx(batch_room[k], rel=0.02)
    # and the numbers are the synthetic room's
    assert live_room["width_m"] == pytest.approx(4.0, rel=0.03) and live_room["height_m"] == pytest.approx(2.6, rel=0.03)


def test_bad_frames_do_not_break_the_connection(client, rendered):
    d, photos = sweep_source(rendered)
    start = client.post("/api/live", json={"kind": "room", "name": "bad"}).json()
    with client.websocket_connect(start["ws"]) as ws:
        hello(ws)
        ws.send_bytes(b"\x00\x00\x00\x05nope")
        assert wait_message(ws, "error")["message"].startswith("bad frame")
        no_pose = {k: v for k, v in photos[0].items() if k != "transform"}       # would break the build of the whole scan later
        ws.send_bytes(live_replay.pack_frame(d, no_pose, 1))
        assert "transform" in wait_message(ws, "error")["message"]
        assert wait_message(ws, "ack")["n"] == 1                                 # dropped, but counted: a resend could not fix it
        ws.send_text("{not json")
        assert wait_message(ws, "error")
        ws.send_text("[1, 2]")
        assert wait_message(ws, "error")
        ws.send_json({"type": "dance"})
        assert "unknown" in wait_message(ws, "error")["message"]
        acks, _ = send_frames(ws, d, photos[1:3], first=2)
        assert acks == [2, 3]


def test_unknown_scan_and_wrong_kind(client):
    r = client.post("/api/live", json={"kind": "ring", "walls": []})               # not a live kind: it is the legacy RoomPlan push
    assert r.status_code == 200 and "viewers" in r.json()
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/live/doesnotexist"):
            pass
    assert client.post("/api/live", json={"kind": "room", "resume_scan": "nope"}).status_code == 404


def test_capturing_scans_do_not_break_the_other_routes(client):
    sid = client.post("/api/live", json={"kind": "room", "name": "empty"}).json()["scan_id"]
    assert client.get("/api/scans").json()[0]["status"] == "capturing"
    assert client.get(f"/api/scans/{sid}").status_code == 200
    assert client.get(f"/api/scans/{sid}/print").status_code == 404
    assert client.get(f"/api/scans/{sid}/quality").status_code == 404
    assert client.post(f"/api/scans/{sid}/convert").status_code == 409
    assert client.post(f"/api/scans/{sid}/add").status_code == 409                  # patch a capture only once it is built
    assert client.post("/api/live", json={"kind": "room", "resume_scan": sid}).status_code == 409
    for path in ("panorama.json", "frames", "room.json", "report.json", "features"):
        assert client.get(f"/api/scans/{sid}/{path}").status_code < 500, path


def test_janitor_builds_a_capture_nobody_touched(client, rendered):
    d, photos = sweep_source(rendered)
    start = client.post("/api/live", json={"kind": "sweep", "name": "left"}).json()
    sid = start["scan_id"]
    with client.websocket_connect(start["ws"]) as ws:
        hello(ws)
        send_frames(ws, d, photos[:20])
    assert live.janitor_once() == []                                           # just now: still fresh
    done = live.janitor_once(now=time.time() + live.IDLE_S + 5)
    assert done == [sid]
    assert wait_status(client, sid) == "ok"


# ------------------------------------------------------------------ offline completion

def post_frames(client, sid, d, entries, route="frames", data=None):
    files = []
    for e in entries:
        files.append(("photos", (e["file"], (d / "photos" / e["file"]).read_bytes())))
        files.append(("photos", (e["depth_file"], (d / "photos" / e["depth_file"]).read_bytes())))
    files.append(("photos_meta", ("photos.json", json.dumps({"photos": entries}).encode())))
    return client.post(f"/api/scans/{sid}/{route}", files=files, data=data or {})


def test_a_live_scan_that_lost_the_connection_is_completed_with_the_missing_frames(client, rendered):
    d, photos = sweep_source(rendered)
    start = client.post("/api/live", json={"kind": "sweep", "name": "half"}).json()
    sid = start["scan_id"]
    with client.websocket_connect(start["ws"]) as ws:
        hello(ws)
        acks, _ = send_frames(ws, d, photos[:10])
        assert acks[-1] == 10
    r = post_frames(client, sid, d, photos[7:])                                # three it already has (matched by timestamp) and the rest
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["skipped"] == 3 and j["added"] == len(photos) - 10 and j["total"] == len(photos)
    assert client.get(f"/api/scans/{sid}").json()["status"] == "capturing"      # appending does NOT build
    again = post_frames(client, sid, d, photos[7:]).json()                       # idempotent
    assert again["added"] == 0 and again["total"] == len(photos)
    assert client.post(f"/api/scans/{sid}/finish").json()["status"] == "building"
    assert wait_status(client, sid) == "ok"
    assert len(json.loads((main.DATA / sid / "photos.json").read_text())["photos"]) == len(photos)
    assert post_frames(client, sid, d, photos[:2]).status_code == 409           # no longer open


# ------------------------------------------------------------------ patching a scan

B_SPECS = [(y, p, np.array([0.9, 1.3, -0.4])) for p in (-30, 10, 45) for y in range(0, 360, 36)]


def session_a(rendered):
    """Session A: a sweep from near the middle of the room."""
    return rendered("a", liveroom.specs_sweep(pos=(-0.2, 1.3, 0.1), yaw_step=30))


def patch_frames(rendered, yaw=137.0, shift=(1.3, 0.2, -0.7)):
    """Session A, and session B: a few turns from another spot, in a different ARKit world (turned by `yaw`, moved by `shift`).
    The world only changes the transforms, so B's pictures are rendered once and its transforms rewritten per test."""
    a_dir, a = session_a(rendered)
    Tno = liveroom.yaw_shift(yaw, shift)                          # new <- old
    b_dir, b_room = rendered("b", B_SPECS, ts0=5000.0, seed=11)
    b = [{**e, "transform": (Tno @ _mat(e["transform"])).T.tolist()} for e in b_room]
    return a_dir, a, b_dir, b, B_SPECS, Tno


def test_a_symmetric_empty_room_is_reported_as_ambiguous(tmp_path):
    import boxroom
    d = tmp_path / "sym"
    a = boxroom.make_scan(d / "a")
    b = boxroom.make_scan(d / "b", yaw_step=36, seed=8)
    (d / "photos").mkdir(exist_ok=True)
    import shutil
    for p in a:
        shutil.copy(d / "a" / "photos" / p["file"], d / "photos" / p["file"]); shutil.copy(d / "a" / "photos" / p["depth_file"], d / "photos" / p["depth_file"])
    b2 = []
    for p in b:
        shutil.copy(d / "b" / "photos" / p["file"], d / "photos" / f"b{p['file']}"); shutil.copy(d / "b" / "photos" / p["depth_file"], d / "photos" / f"b{p['depth_file']}")
        b2.append({**p, "file": f"b{p['file']}", "depth_file": f"b{p['depth_file']}"})
    res = registration.register(d, a, b2)
    assert not res["ok"] and res.get("ambiguous"), res
    assert "distinctive" in res["message"]


def test_patch_over_the_socket_aligns_rewrites_and_rebuilds(client, rendered):
    a_dir, a, b_dir, b, b_specs, Tno = patch_frames(rendered)
    start = client.post("/api/live", json={"kind": "sweep", "name": "base"}).json()
    sid = start["scan_id"]
    with client.websocket_connect(start["ws"]) as ws:
        hello(ws)
        send_frames(ws, a_dir, a)
        ws.send_json({"type": "finish", "info": {"sweep": True, "scan type": "Sweep from one spot"}})
        wait_state(ws, "building")          # leaving the block cancels the server side at once
    assert wait_status(client, sid) == "ok"
    before = json.loads((main.DATA / sid / "photos.json").read_text())["photos"]
    assert len(before) == len(a)

    patch = client.post("/api/live", json={"kind": "sweep", "resume_scan": sid}).json()
    assert patch["scan_id"] == sid and patch["patch"] is True and patch["session"] == 2
    assert client.get(f"/api/scans/{sid}").json()["status"] == "capturing"
    with client.websocket_connect(patch["ws"]) as ws:
        hello(ws)
        acks, others = send_frames(ws, b_dir, b)
        assert acks == list(range(1, len(b) + 1))
        al = [m for m in others if m["type"] == "aligned"]
        while not [m for m in al if m["ok"]]:
            al.append(wait_message(ws, "aligned"))
        ok = [m for m in al if m["ok"]][0]
        assert ok["rms_mm"] < 60
        ws.send_json({"type": "hello", "resume_after": len(b), "app": "test"})     # a map now (maps are only sent when something changed)
        you = wait_message(ws, "map")["you"]                                      # in the phone's OWN frame, not the old scan's
        assert np.linalg.norm(np.array(you) - (Tno @ np.r_[b_specs[-1][2], 1])[:3]) < 0.05, you
        sessions = json.loads((main.DATA / sid / "sessions.json").read_text())["sessions"]
        s2 = next(s for s in sessions if s["index"] == 2)
        T = np.array(s2["transform"]).reshape(4, 4)
        E = T @ Tno
        assert np.linalg.norm(E[:3, 3]) < 0.03 and abs(registration.yaw_of(E)) < 1.0
        stored = json.loads((main.DATA / sid / "photos.json").read_text())["photos"]
        assert len(stored) == len(a) + len(b)
        # a new frame's camera is where it really stood, in the OLD frame; its raw transform is kept in sessions.json
        newest = [p for p in stored if p.get("session") == 2]
        assert len(newest) == len(b)
        for p, (yaw, pitch, pos) in zip(newest, b_specs):
            assert np.linalg.norm(_mat(p["transform"])[:3, 3] - pos) < 0.03
        assert len(s2["frames"]) == len(b)
        ws.send_json({"type": "finish"})
        wait_state(ws, "building")          # leaving the block cancels the server side at once
    assert wait_status(client, sid) == "ok"
    assert len(json.loads((main.DATA / sid / "photos.json").read_text())["photos"]) == len(a) + len(b)


def test_add_route_aligns_stores_as_a_session_and_rebuilds(client, rendered):
    a_dir, a, b_dir, b, b_specs, Tno = patch_frames(rendered, yaw=212.0, shift=(-0.4, 0.1, 0.9))
    files = [("photos", (p["file"], (a_dir / "photos" / p["file"]).read_bytes())) for p in a]
    files += [("photos", (p["depth_file"], (a_dir / "photos" / p["depth_file"]).read_bytes())) for p in a]
    files += [("photos_meta", ("photos.json", (a_dir / "photos.json").read_bytes())), ("mesh", ("scan.ply", b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n"))]
    sid = client.post("/api/scans", files=files, data={"name": "base", "kind": "mesh", "info": json.dumps({"sweep": True, "scan type": "Sweep from one spot"})}).json()["id"]
    r = post_frames(client, sid, b_dir, b, route="add")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["aligned"] is True and j["rms_mm"] < 60 and j["added"] == len(b), j
    sessions = json.loads((main.DATA / sid / "sessions.json").read_text())["sessions"]
    s2 = next(s for s in sessions if s["index"] == 2)
    E = np.array(s2["transform"]).reshape(4, 4) @ Tno
    assert np.linalg.norm(E[:3, 3]) < 0.03 and abs(registration.yaw_of(E)) < 1.0
    assert client.get(f"/api/scans/{sid}").json()["status"] == "ok"
    assert len(json.loads((main.DATA / sid / "photos.json").read_text())["photos"]) == len(a) + len(b)


def test_add_route_leaves_the_old_scan_alone_when_it_cannot_match(client, rendered, tmp_path):
    a_dir, a = session_a(rendered)
    files = [("photos", (p["file"], (a_dir / "photos" / p["file"]).read_bytes())) for p in a]
    files += [("photos", (p["depth_file"], (a_dir / "photos" / p["depth_file"]).read_bytes())) for p in a]
    files += [("photos_meta", ("photos.json", (a_dir / "photos.json").read_bytes())), ("mesh", ("scan.ply", b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n"))]
    sid = client.post("/api/scans", files=files, data={"name": "base", "kind": "mesh", "info": json.dumps({"sweep": True})}).json()["id"]
    n_before = len(json.loads((main.DATA / sid / "photos.json").read_text())["photos"])
    ceiling = [(0, 88, np.array([0.9, 1.3, -0.4])), (40, 88, np.array([0.9, 1.3, -0.4])), (80, 88, np.array([0.9, 1.3, -0.4])), (120, 88, np.array([0.9, 1.3, -0.4]))]
    c_dir = tmp_path / "c"
    c = liveroom.make_frames(c_dir, ceiling, to_new=liveroom.yaw_shift(137.0, (1.3, 0.2, -0.7)), ts0=9000.0)
    r = post_frames(client, sid, c_dir, c, route="add")
    assert r.status_code == 200
    j = r.json()
    assert j["aligned"] is False and j["message"]
    assert len(json.loads((main.DATA / sid / "photos.json").read_text())["photos"]) == n_before
    assert client.get(f"/api/scans/{sid}").json()["status"] == "ok"
    # a live patch session that is left without a frame: the janitor puts the old status back and the old model stays
    assert client.post("/api/live", json={"kind": "sweep", "resume_scan": sid}).json()["patch"] is True
    assert live.janitor_once(now=time.time() + live.IDLE_S + 5) == [sid]
    assert wait_status(client, sid) == "ok"
    assert len(json.loads((main.DATA / sid / "photos.json").read_text())["photos"]) == n_before


def test_a_gap_across_north_is_one_need_and_ceiling_and_floor_get_one_each(tmp_path):
    d = tmp_path / "seam"
    # level views from azimuth 60 to 300 only: the walls across 300..360..60 (through north) are missing; nothing up or down
    specs = [(y, 0, np.array([0.0, 1.3, 0.0])) for y in range(60, 301, 20)]
    photos = liveroom.make_frames(d, specs)
    eng = coverage.CoverageEngine("sweep")
    feed(eng, d, photos)
    m = eng.next_message(force=True)
    kinds = [n["kind"] for n in m["needs"]]
    assert kinds.count("gap") == 1 and kinds.count("ceiling") == 1 and kinds.count("floor") == 1, m["needs"]
    az = look_azimuth(next(n for n in m["needs"] if n["kind"] == "gap")) % 360
    assert az < 25 or az > 335, az                                        # straight across north, not the far side of the circle


def test_needs_words_do_not_change_when_the_phone_frame_is_turned(tmp_path):
    """Patch mode: the map is sent in the phone's own (turned) frame. Left and right must stay where they are: the words are
    relative to the first frame's heading, which turns with everything else (a past sign error swapped them)."""
    d = tmp_path / "turn"
    photos = liveroom.make_frames(d, [(y, 0, np.array([0.0, 1.3, 0.0])) for y in range(0, 181, 30)], w=160, h=120, fx=125.0)
    texts = []
    for turn in (None, liveroom.yaw_shift(90.0, (0.4, 0.0, -0.3))):
        eng = coverage.CoverageEngine("sweep")
        feed(eng, d, photos)
        texts.append(sorted(n["text"] for n in eng.next_message(turn, force=True)["needs"]))
    assert any("to the left" in t or "to the right" in t for t in texts[0]), texts[0]
    assert texts[0] == texts[1]


class _RecordingSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        await asyncio.sleep(0)               # a real send yields to the loop
        self.sent.append(obj)


def test_maps_go_out_in_seq_order_when_two_senders_race(tmp_path):
    """The ticker and a hello (or two sockets) both ask for a map: the one computed first used to get the lower seq but could be
    sent second. Now computing, numbering and sending are one step per session, so seq follows send order."""
    sess = live.Session(tmp_path, "race", index=1, patch=False, engine=False)
    calls, busy = [], []

    def slow_then_fast(force=False):
        calls.append(force)
        busy.append(1)
        assert len(busy) == 1, "two maps computed at the same time"
        time.sleep(0.3 if len(calls) == 1 else 0.0)          # the first map takes longer to compute
        busy.pop()
        return {"type": "map", "call": len(calls)}

    sess.next_map = slow_then_fast
    ws = _RecordingSocket()

    async def race():
        await asyncio.gather(live.send_map(ws, sess, force=True), live.send_map(ws, sess), live.send_map(ws, sess, force=True))

    asyncio.run(race())
    maps = [m for m in ws.sent if m["type"] == "map"]
    assert [m["seq"] for m in maps] == [1, 2, 3]
    assert [m["call"] for m in maps] == [1, 2, 3]            # and the content is in the order it was computed


def test_deleting_a_scan_during_capture_closes_the_session(client, rendered):
    from starlette.websockets import WebSocketDisconnect
    d, photos = sweep_source(rendered)
    start = client.post("/api/live", json={"kind": "sweep", "name": "doomed"}).json()
    sid = start["scan_id"]
    with client.websocket_connect(start["ws"]) as ws:
        hello(ws)
        send_frames(ws, d, photos[:4])
        sess = live.SESSIONS[sid]
        assert client.delete(f"/api/scans/{sid}").status_code == 200
        m = wait_state(ws, "error")
        assert "deleted" in m["message"]
        with pytest.raises(WebSocketDisconnect) as gone:
            for _ in range(50):
                ws.receive_json()
        assert gone.value.code == 4410
    assert sid not in live.SESSIONS and sess.engine is None and sess.deleted
    assert not (main.DATA / sid).exists()
    with pytest.raises(ValueError):                                                # a late store is refused, nothing is recreated
        sess.store_frame(5, photos[4], b"x", b"")
    with pytest.raises(Exception):
        with client.websocket_connect(start["ws"]):
            pass
    assert post_frames(client, sid, d, photos[4:6]).status_code == 404
    assert not (main.DATA / sid).exists()


def test_delete_all_closes_every_live_session(client, rendered):
    d, photos = sweep_source(rendered)
    a = client.post("/api/live", json={"kind": "room", "name": "a"}).json()
    b = client.post("/api/live", json={"kind": "room", "name": "b"}).json()
    with client.websocket_connect(a["ws"]) as ws:
        hello(ws)
        send_frames(ws, d, photos[:2])
        r = client.request("DELETE", "/api/scans?all=true", json={"confirm": "DELETE ALL"})
        assert r.status_code == 200 and r.json()["deleted"] == 2
        assert "deleted" in wait_state(ws, "error")["message"]
    assert not live.SESSIONS and not list(main.DATA.iterdir())
    assert client.post(f"/api/scans/{b['scan_id']}/finish").status_code == 404
