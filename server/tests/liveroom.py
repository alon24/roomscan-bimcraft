"""Synthetic rooms for the live-capture tests: the box of boxroom.py plus a pillar (so the room is not symmetric and a second
capture can be lined up with the first), and a way to render any list of camera poses, also in a turned and shifted world."""
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

import boxroom

PILLAR_LO = np.array([0.9, 0.0, 0.4])
PILLAR_HI = np.array([1.5, 2.6, 1.0])
GREY = (150, 150, 150)


def trace(origin: np.ndarray, dirs: np.ndarray):
    dist, axis, sign = boxroom.trace(origin, dirs)
    inv = 1.0 / np.where(np.abs(dirs) < 1e-9, 1e-9, dirs)
    t1, t2 = (PILLAR_LO - origin) * inv, (PILLAR_HI - origin) * inv
    tmin = np.max(np.minimum(t1, t2), axis=-1)
    tmax = np.min(np.maximum(t1, t2), axis=-1)
    hit = (tmax >= tmin) & (tmin > 0) & (tmin < dist)
    return np.where(hit, tmin, dist), axis, sign, hit


def render(M: np.ndarray, w: int, h: int, fx: float):
    u, v = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    cam = np.stack([(u - w / 2) / fx, -(v - h / 2) / fx, -np.ones_like(u)], axis=-1)
    dirs = cam @ M[:3, :3].T
    dirs /= np.linalg.norm(dirs, axis=-1, keepdims=True)
    dist, axis, sign, hit = trace(M[:3, 3], dirs)
    zdepth = dist * (-(cam[..., 2])) / np.linalg.norm(cam, axis=-1)
    img = np.zeros((h, w, 3), np.uint8)
    for (a, s), c in boxroom.COLOURS.items():
        img[(axis == a) & (sign == s)] = c
    img[hit] = GREY
    return img, zdepth.astype(np.float32)


def specs_sweep(pos=(0.0, 1.3, 0.0), yaw_step: int = 24, pitches=(-40, 0, 40), yaw_range=(0, 360)):
    out = [(y, p, np.array(pos, float)) for p in pitches for y in range(yaw_range[0], yaw_range[1], yaw_step)]
    if yaw_range == (0, 360):
        out += [(0, 88, np.array(pos, float)), (0, -88, np.array(pos, float))]
    return out


def make_frames(d: Path, specs, to_new: np.ndarray | None = None, ts0: float = 1000.0, start: int = 1, w: int = 640, h: int = 480, fx: float = 500.0,
                depth_noise: float = 0.004, seed: int = 5) -> list[dict]:
    """Render (yaw deg, pitch deg, position) camera poses of the room into photos/ and return the manifest entries. `to_new` makes the
    transforms those of a session whose world is `to_new @ (the room's world)` (a different ARKit session)."""
    (d / "photos").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    dw, dh = 256, 192
    photos = []
    for i, (yaw, pitch, pos) in enumerate(specs):
        M = boxroom.pose(math.radians(yaw), math.radians(pitch), np.asarray(pos, float))
        img, _ = render(M, w, h, fx)
        _, depth = render(M, dw, dh, fx * dw / w)
        name = f"p{start + i:03d}"
        Image.fromarray(img).save(d / "photos" / f"{name}.jpg", quality=92)
        (depth + rng.normal(0, depth_noise, depth.shape).astype(np.float32)).astype("<f4").tofile(d / "photos" / f"{name}.depth")
        Mn = M if to_new is None else to_new @ M
        photos.append({"file": f"{name}.jpg", "depth_file": f"{name}.depth", "width": w, "height": h, "depth_width": dw, "depth_height": dh,
                       "intrinsics": [[fx, 0, 0], [0, fx, 0], [w / 2, h / 2, 1]], "transform": Mn.T.tolist(), "timestamp": ts0 + (start + i) * 0.4})
    return photos


def yaw_shift(deg: float, shift=(0.0, 0.0, 0.0)) -> np.ndarray:
    a = math.radians(deg)
    T = np.eye(4)
    T[:3, :3] = [[math.cos(a), 0, math.sin(a)], [0, 1, 0], [-math.sin(a), 0, math.cos(a)]]
    T[:3, 3] = shift
    return T
