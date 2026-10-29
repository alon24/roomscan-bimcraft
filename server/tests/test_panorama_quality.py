import numpy as np

from app.panorama import frame_quality


def _ph(sharp, x):
    t = np.eye(4)
    t[3, 0] = x          # stored column-major: the 4th row of the nested list is the translation
    return {"_sharp": sharp, "transform": t.tolist()}


def test_sharper_and_nearer_frames_count_more():
    q = frame_quality([_ph(100, 0.0), _ph(50, 0.0), _ph(100, 0.4)], np.zeros(3))
    assert q[0] > q[1] > 0          # blurrier counts less
    assert q[0] > 20 * q[2]         # 40 cm off the centre counts far less
