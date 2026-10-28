"""Live guided capture (docs/live-guide.md): coverage engine, WebSocket protocol, finish, offline completion, patching a scan."""
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


def sweep_source(tmp_path, name="src", pos=(0.0, 1.3, 0.0), yaw_step=30, ts0=1000.0):
    d = tmp_path / name
    photos = liveroom.make_frames(d, liveroom.specs_sweep(pos=pos, yaw_step=yaw_step), ts0=ts0)
    (d / "photos.json").write_text(json.dumps({"version": 2, "photos": photos}))
    return d, photos


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


# ------------------------------------------------------------------ the coverage engine

def feed(engine, d, entries, upto=None):
    for e in entries[:upto]:
        depth = np.fromfile(d / "photos" / e["depth_file"], dtype="<f4").reshape(e["depth_height"], e["depth_width"])
        engine.add_frame(e, depth)


def look_azimuth(need):
    return math.degrees(math.atan2(need["look"][0], -need["look"][2]))


def test_sweep_needs_point_at_what_is_unseen_and_percent_rises(tmp_path):
    d, photos = sweep_source(tmp_path)
    eng = coverage.CoverageEngine("sweep")
    n = len(photos)
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


def test_sweep_gap_points_at_the_unseen_azimuth(tmp_path):
    d, photos = sweep_source(tmp_path)
    eng = coverage.CoverageEngine("sweep")
    seen = [e for e in photos if True]
    # walls band only for azimuths 0..120 degrees: frames at pitch 0 with yaw 0..120
    level = [e for e, (y, p, _) in zip(photos, liveroom.specs_sweep(yaw_step=30)) if p == 0 and y <= 120]
    feed(eng, d, level)
    m = eng.next_message(force=True)
    wall = [x for x in m["needs"] if x["kind"] == "gap"]
    assert wall
    az = look_azimuth(wall[0]) % 360
    assert not (0 <= az <= 150 or az >= 355), az                           # points away from the 0..120 degrees already covered


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


def test_needs_keep_their_ids_and_order_when_nothing_changes(tmp_path):
    d, photos = sweep_source(tmp_path)
    eng = coverage.CoverageEngine("sweep")
    feed(eng, d, photos[:14])
    a = eng.next_message(force=True)
    b = eng.next_message(force=True)
    c = eng.next_message(force=True)
    ids = [n["id"] for n in a["needs"]]
    assert ids and ids == [n["id"] for n in b["needs"]] == [n["id"] for n in c["needs"]]
    assert [n["target"] for n in a["needs"]] == [n["target"] for n in c["needs"]]


def test_a_satisfied_need_goes_away_after_a_few_updates(tmp_path):
    d, photos = sweep_source(tmp_path)
    eng = coverage.CoverageEngine("sweep")
    feed(eng, d, photos[:12])
    first = eng.next_message(force=True)
    assert any(n["kind"] == "ceiling" for n in first["needs"])
    feed(eng, d, photos[12:])
    for _ in range(4):
        last = eng.next_message(force=True)
    assert not any(n["kind"] == "ceiling" for n in last["needs"])


# ------------------------------------------------------------------ the WebSocket

def test_live_roundtrip_ack_resume_finish_equals_batch(client, tmp_path):
    d, photos = sweep_source(tmp_path)
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
        for _ in range(60):                                                       # the last frames' map
            m = ws.receive_json()
            if m["type"] == "map":
                maps.append(m)
                if m["percent"] >= 80:
                    break
        assert maps and maps[-1]["percent"] >= 80 and maps[-1]["grid"]["w"] > 0
        assert [m["seq"] for m in maps] == sorted(m["seq"] for m in maps)

        stored = json.loads((main.DATA / sid / "photos.json").read_text())["photos"]
        assert len(stored) == len(photos)
        assert len({p["file"] for p in stored}) == len(photos) and len({p["timestamp"] for p in stored}) == len(photos)
        assert all((main.DATA / sid / "photos" / p["file"]).exists() and (main.DATA / sid / "photos" / p["depth_file"]).exists() for p in stored)

        ws.send_json({"type": "finish", "info": {"scan type": "Sweep from one spot", "sweep": True}})
        st = wait_message(ws, "state")
        assert st["status"] == "building"
        assert wait_status(client, sid) == "ok", client.get(f"/api/scans/{sid}").json()["log"]
        done = wait_message(ws, "state")
        assert done["status"] == "ready"
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


def test_bad_frames_do_not_break_the_connection(client, tmp_path):
    d, photos = sweep_source(tmp_path, yaw_step=60)
    start = client.post("/api/live", json={"kind": "room", "name": "bad"}).json()
    with client.websocket_connect(start["ws"]) as ws:
        hello(ws)
        ws.send_bytes(b"\x00\x00\x00\x05nope")
        assert wait_message(ws, "error")["message"].startswith("bad frame")
        ws.send_text("{not json")
        assert wait_message(ws, "error")
        ws.send_json({"type": "dance"})
        assert "unknown" in wait_message(ws, "error")["message"]
        acks, _ = send_frames(ws, d, photos[:2])
        assert acks == [1, 2]


def test_unknown_scan_and_wrong_kind(client):
    assert client.post("/api/live", json={"kind": "ring"}).status_code == 200      # not a live kind: it is the legacy RoomPlan push
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/live/doesnotexist"):
            pass
    assert client.post("/api/live", json={"kind": "room", "resume_scan": "nope"}).status_code == 404


def test_legacy_roomplan_live_post_still_works(client):
    r = client.post("/api/live", json={"walls": [], "doors": []})
    assert r.status_code == 200 and "viewers" in r.json()


def test_capturing_scans_do_not_break_the_other_routes(client):
    sid = client.post("/api/live", json={"kind": "room", "name": "empty"}).json()["scan_id"]
    assert client.get("/api/scans").json()[0]["status"] == "capturing"
    assert client.get(f"/api/scans/{sid}").status_code == 200
    assert client.get(f"/api/scans/{sid}/print").status_code == 404
    assert client.get(f"/api/scans/{sid}/quality").status_code == 404
    assert client.post(f"/api/scans/{sid}/convert").status_code == 409
    for path in ("panorama.json", "frames", "room.json", "report.json", "features"):
        assert client.get(f"/api/scans/{sid}/{path}").status_code < 500, path


def test_janitor_builds_a_capture_nobody_touched(client, tmp_path):
    d, photos = sweep_source(tmp_path)
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


def test_a_live_scan_that_lost_the_connection_is_completed_with_the_missing_frames(client, tmp_path):
    d, photos = sweep_source(tmp_path)
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

def patch_frames(tmp_path, yaw=137.0, shift=(1.3, 0.2, -0.7)):
    """Session A: a sweep from near the middle. Session B (a different ARKit world): a few turns from another spot."""
    a_dir, a = sweep_source(tmp_path, "a", pos=(-0.2, 1.3, 0.1), yaw_step=30, ts0=1000.0)
    Tno = liveroom.yaw_shift(yaw, shift)                          # new <- old
    b_specs = [(y, p, np.array([0.9, 1.3, -0.4])) for p in (-30, 10, 45) for y in range(0, 360, 36)]
    b_dir = tmp_path / "b"
    b = liveroom.make_frames(b_dir, b_specs, to_new=Tno, start=1, ts0=5000.0, seed=11)
    (b_dir / "photos.json").write_text(json.dumps({"version": 2, "photos": b}))
    return a_dir, a, b_dir, b, b_specs, Tno


def test_registration_recovers_a_random_turn_and_shift(tmp_path):
    a_dir, a, b_dir, b, specs, Tno = patch_frames(tmp_path, yaw=-58.0, shift=(-0.9, 0.15, 1.2))
    both = tmp_path / "both"
    (both / "photos").mkdir(parents=True)
    import shutil
    for p in a:
        shutil.copy(a_dir / "photos" / p["file"], both / "photos" / p["file"]); shutil.copy(a_dir / "photos" / p["depth_file"], both / "photos" / p["depth_file"])
    for p in b:
        shutil.copy(b_dir / "photos" / p["file"], both / "photos" / f"b{p['file']}"); shutil.copy(b_dir / "photos" / p["depth_file"], both / "photos" / f"b{p['depth_file']}")
    b2 = [{**p, "file": f"b{p['file']}", "depth_file": f"b{p['depth_file']}"} for p in b]
    res = registration.register(both, a, b2)
    assert res["ok"], res
    E = res["T"] @ Tno                                               # should be the identity
    assert np.linalg.norm(E[:3, 3]) < 0.03 and abs(registration.yaw_of(E)) < 1.0
    assert res["rms_mm"] < 60


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


def test_patch_over_the_socket_aligns_rewrites_and_rebuilds(client, tmp_path):
    a_dir, a, b_dir, b, b_specs, Tno = patch_frames(tmp_path)
    start = client.post("/api/live", json={"kind": "sweep", "name": "base"}).json()
    sid = start["scan_id"]
    with client.websocket_connect(start["ws"]) as ws:
        hello(ws)
        send_frames(ws, a_dir, a)
        ws.send_json({"type": "finish", "info": {"sweep": True, "scan type": "Sweep from one spot"}})
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
        # a map in the NEW session's own frame: "you" is where the phone is in ITS world
        last = None
        for _ in range(60):
            m = ws.receive_json()
            if m["type"] == "map":
                last = m
                if m["you"] is not None:
                    break
        assert last is not None
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
            from app.photos import _mat
            assert np.linalg.norm(_mat(p["transform"])[:3, 3] - pos) < 0.03
        assert len(s2["frames"]) == len(b)
        ws.send_json({"type": "finish"})
    assert wait_status(client, sid) == "ok"
    assert len(json.loads((main.DATA / sid / "photos.json").read_text())["photos"]) == len(a) + len(b)


def test_add_route_aligns_stores_as_a_session_and_rebuilds(client, tmp_path):
    a_dir, a, b_dir, b, b_specs, Tno = patch_frames(tmp_path, yaw=212.0, shift=(-0.4, 0.1, 0.9))
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


def test_add_route_leaves_the_old_scan_alone_when_it_cannot_match(client, tmp_path):
    a_dir, a, b_dir, b, b_specs, Tno = patch_frames(tmp_path)
    files = [("photos", (p["file"], (a_dir / "photos" / p["file"]).read_bytes())) for p in a]
    files += [("photos", (p["depth_file"], (a_dir / "photos" / p["depth_file"]).read_bytes())) for p in a]
    files += [("photos_meta", ("photos.json", (a_dir / "photos.json").read_bytes())), ("mesh", ("scan.ply", b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n"))]
    sid = client.post("/api/scans", files=files, data={"name": "base", "kind": "mesh", "info": json.dumps({"sweep": True})}).json()["id"]
    n_before = len(json.loads((main.DATA / sid / "photos.json").read_text())["photos"])
    ceiling = [(0, 88, np.array([0.9, 1.3, -0.4])), (40, 88, np.array([0.9, 1.3, -0.4])), (80, 88, np.array([0.9, 1.3, -0.4])), (120, 88, np.array([0.9, 1.3, -0.4]))]
    c_dir = tmp_path / "c"
    c = liveroom.make_frames(c_dir, ceiling, to_new=Tno, ts0=9000.0)
    r = post_frames(client, sid, c_dir, c, route="add")
    assert r.status_code == 200
    j = r.json()
    assert j["aligned"] is False and j["message"]
    assert len(json.loads((main.DATA / sid / "photos.json").read_text())["photos"]) == n_before
    assert client.get(f"/api/scans/{sid}").json()["status"] == "ok"
