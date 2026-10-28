import json

import cv2
import numpy as np
import pytest

from app import photomeasure as pm

# A flat "table" 10 px per mm: a bank card and a 100 mm bar, photographed from a slant.
S = 10
CARD = (85.6, 53.98)
BAR = ((20.0, 100.0), (120.0, 100.0))   # mm on the table, 100 mm long


def scene(tilt=0.0):
    rng = np.random.default_rng(1)
    table = np.full((1500, 2200, 3), 120, np.uint8)
    table = cv2.add(table, rng.integers(0, 25, table.shape, dtype=np.uint8))
    x, y = 300, 200
    cv2.rectangle(table, (x, y), (x + int(CARD[0] * S), y + int(CARD[1] * S)), (245, 245, 245), -1)
    cv2.line(table, (int(BAR[0][0] * S), int(BAR[0][1] * S)), (int(BAR[1][0] * S), int(BAR[1][1] * S)), (20, 20, 20), 14)
    src = np.float32([[0, 0], [2200, 0], [2200, 1500], [0, 1500]])
    dst = np.float32([[120, 90], [1500 - 60 * tilt, 60 + 40 * tilt], [1480 - 80 * tilt, 1050 - 30 * tilt], [60, 1000]])
    M = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(table, M, (1600, 1150), borderValue=(90, 90, 90)), M


def project(M, mm):
    v = M @ np.array([mm[0] * S, mm[1] * S, 1.0])
    return v[:2] / v[2]


@pytest.mark.parametrize("tilt", [0.0, 0.5, 1.0])
def test_bar_measures_100mm_from_the_card(tilt):
    img, M = scene(tilt)
    quad = pm.detect_reference(img, *pm.REFS["card"])
    assert quad is not None
    H = pm.homography(quad, *pm.REFS["card"])
    d = pm.distance_mm(H, project(M, BAR[0]), project(M, BAR[1]))
    assert d == pytest.approx(100.0, rel=0.02), d


def test_nothing_found_in_a_blank_picture():
    assert pm.detect_reference(np.full((600, 800, 3), 128, np.uint8), *pm.REFS["card"]) is None


def test_hand_placed_corners_give_the_same_scale():
    img, M = scene(0.5)
    corners = [project(M, p) for p in [(30, 20), (30 + CARD[0], 20), (30 + CARD[0], 20 + CARD[1]), (30, 20 + CARD[1])]]
    H = pm.homography(np.array(corners), *pm.REFS["card"])
    assert pm.distance_mm(H, project(M, BAR[0]), project(M, BAR[1])) == pytest.approx(100.0, rel=0.005)


def test_custom_reference_needs_a_size():
    with pytest.raises(ValueError):
        pm.reference_size("custom")
    assert pm.reference_size("custom", 50, 80) == (80, 50)


def test_steep_angle_is_warned_about():
    flat = np.array([[0, 0], [856, 0], [856, 540], [0, 540]], float)
    steep = np.array([[0, 0], [856, 100], [856, 440], [0, 540]], float)
    assert pm.tilt_warning(flat) is None
    assert pm.tilt_warning(steep) is not None


def test_api_detect_homography_and_rectify():
    from fastapi.testclient import TestClient
    from app import main
    img, M = scene(0.5)
    ok, jpg = cv2.imencode(".jpg", img)
    c = TestClient(main.app)
    r = c.post("/api/photo/detect", files={"file": ("p.jpg", jpg.tobytes(), "image/jpeg")}, data={"ref": "card"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["found"] and len(j["corners"]) == 4
    H = np.array(j["H"]).reshape(3, 3)
    assert pm.distance_mm(H, project(M, BAR[0]), project(M, BAR[1])) == pytest.approx(100.0, rel=0.02)

    r2 = c.post("/api/photo/homography", json={"corners": j["corners"], "ref": "card"})
    assert r2.status_code == 200 and len(r2.json()["H"]) == 9

    r3 = c.post("/api/photo/rectify", files={"file": ("p.jpg", jpg.tobytes(), "image/jpeg")},
                data={"corners": json.dumps(j["corners"]), "ref": "card"})
    assert r3.status_code == 200 and r3.headers["content-type"] == "image/png" and float(r3.headers["x-px-per-mm"]) > 0

    assert c.post("/api/photo/detect", files={"file": ("p.jpg", b"not a picture", "image/jpeg")}).status_code == 400
    assert c.post("/api/photo/detect", files={"file": ("p.jpg", jpg.tobytes(), "image/jpeg")}, data={"ref": "custom"}).status_code == 400
