import json
import sys
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from app import main

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import make_ring  # noqa: E402


def test_ring_scan_builds_the_part_from_a_moving_phone(tmp_path, monkeypatch):
    """Phone walks round a ring with a 50 x 60 mm cylinder on it; the ring markers put every frame in one frame."""
    monkeypatch.setattr(main, "DATA", tmp_path / "scans")
    px_mm = 4.0
    tex = make_ring.render_png(px_mm)
    S, c = tex.shape[0], tex.shape[0] / 2
    fx = fy = 1000.0
    W, H, cx, cy = 1280, 960, 640.0, 480.0
    rng = np.random.default_rng(5)
    th = rng.uniform(0, 2 * np.pi, 30000)
    z = rng.uniform(0, 60, 30000)
    side = np.c_[25 * np.cos(th), 25 * np.sin(th), z]                                   # ring frame, mm
    r = 25 * np.sqrt(rng.uniform(0, 1, 6000)); a = rng.uniform(0, 2 * np.pi, 6000)
    top = np.c_[r * np.cos(a), r * np.sin(a), np.full(6000, 60.0)]
    part = np.vstack([side, top])
    to_world = lambda q: np.c_[q[:, 0], q[:, 2], -q[:, 1]] / 1000.0                     # ring (z up) -> world (y up), metres

    def project(M, pts):
        cam = (np.linalg.inv(M) @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]
        depth = -cam[:, 2]
        return np.c_[fx * cam[:, 0] / depth + cx, cy - fy * cam[:, 1] / depth], depth

    photos, files = [], []
    (tmp_path / "scans").mkdir(parents=True, exist_ok=True)
    for k in range(12):
        az = np.radians(k * 30)
        pos = np.array([0.38 * np.cos(az), 0.26, 0.38 * np.sin(az)])
        f = -pos / np.linalg.norm(pos)
        right = np.cross(f, [0, 1, 0]); right /= np.linalg.norm(right)
        up = np.cross(right, f)
        M = np.eye(4); M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = right, up, -f, pos
        # the photo: the ring seen in perspective
        corners_ring = np.array([[(0 - c) / px_mm, (c - 0) / px_mm, 0], [(S - c) / px_mm, (c - 0) / px_mm, 0],
                                 [(S - c) / px_mm, (c - S) / px_mm, 0], [(0 - c) / px_mm, (c - S) / px_mm, 0]])
        dst, _ = project(M, to_world(corners_ring))
        Hm = cv2.getPerspectiveTransform(np.float32([[0, 0], [S, 0], [S, S], [0, S]]), np.float32(dst))
        img = cv2.warpPerspective(tex, Hm, (W, H), flags=cv2.INTER_AREA, borderValue=170)
        name = f"p{k + 1:03d}"
        Image.fromarray(img).convert("RGB").save(tmp_path / f"{name}.jpg", quality=95)
        # the depth map: only the part is in it
        dw, dh = 640, 480
        uv, depth = project(M, to_world(part))
        u, v = (uv[:, 0] * dw / W).astype(int), (uv[:, 1] * dh / H).astype(int)
        ok = (u >= 0) & (u < dw) & (v >= 0) & (v < dh)
        dm = np.full((dh, dw), np.inf)
        np.minimum.at(dm, (v[ok], u[ok]), depth[ok])
        dm[np.isinf(dm)] = 0
        dm = cv2.morphologyEx(dm.astype(np.float32), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)) * (dm > 0) if False else dm
        dm.astype("<f4").tofile(tmp_path / f"{name}.depth")
        photos.append({"file": f"{name}.jpg", "timestamp": k * 1.0, "width": W, "height": H, "depth_file": f"{name}.depth",
                       "depth_width": dw, "depth_height": dh,
                       "transform": [M[:3, 0].tolist() + [0], M[:3, 1].tolist() + [0], M[:3, 2].tolist() + [0], pos.tolist() + [1]],
                       "intrinsics": [[fx, 0, 0], [0, fy, 0], [cx, cy, 1]]})
        files += [("photos", (f"{name}.jpg", open(tmp_path / f"{name}.jpg", "rb"), "image/jpeg")),
                  ("photos", (f"{name}.depth", open(tmp_path / f"{name}.depth", "rb"), "application/octet-stream"))]
    (tmp_path / "photos.json").write_text(json.dumps({"version": 2, "photos": photos}))
    client = TestClient(main.app)
    r = client.post("/api/scans", data={"kind": "mesh", "ring_scan": "true"},
                    files=files + [("photos_meta", ("photos.json", open(tmp_path / "photos.json", "rb")))])
    meta = client.get(f"/api/scans/{r.json()['id']}").json()
    assert meta["status"] == "ok", meta["log"]
    assert "ring: 12 frames used" in meta["log"], meta["log"]
    size = sorted(client.get(f"/api/scans/{r.json()['id']}/report.json").json()["size_mm"])
    assert 38 < size[0] < 70 and 38 < size[1] < 70 and 50 < size[2] < 80, (size, meta["log"])    # the 50 x 50 x 60 mm part
