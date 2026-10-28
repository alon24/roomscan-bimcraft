"""A synthetic room scan to test against: a box with known walls, and a phone turning on the spot in it (a little off the exact
middle, like a hand), taking a photo and a LiDAR depth map every few degrees. Same file layout and conventions as a real scan."""
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

# the room in ARKit world axes (Y up): x from -1.7 to 2.3 (4.0 m), z from -1.2 to 1.8 (3.0 m), y from 0 to 2.6 (floor to ceiling)
LO = np.array([-1.7, 0.0, -1.2])
HI = np.array([2.3, 2.6, 1.8])
# colours by surface: +x red, -x green, +z blue, -z yellow, floor brown, ceiling white
COLOURS = {(0, 1): (200, 40, 40), (0, -1): (40, 170, 60), (2, 1): (50, 70, 210), (2, -1): (225, 210, 50), (1, -1): (130, 95, 60), (1, 1): (240, 240, 240)}
CAM = np.array([0.0, 1.3, 0.0])


def pose(yaw: float, pitch: float, pos: np.ndarray) -> np.ndarray:
    """ARKit camera-to-world (4x4): the camera looks down its -Z, +Y is up in the image. yaw 0 looks along -Z, +90 deg along +X."""
    f = np.array([math.cos(pitch) * math.sin(yaw), math.sin(pitch), -math.cos(pitch) * math.cos(yaw)])
    r = np.cross(f, [0, 1, 0])
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    M = np.eye(4)
    M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = r, u, -f, pos
    return M


def trace(origin: np.ndarray, dirs: np.ndarray):
    """Distance along each ray to the room's walls, and which wall (axis, sign) it hits."""
    inv = 1.0 / np.where(np.abs(dirs) < 1e-9, 1e-9, dirs)
    t_hi, t_lo = (HI - origin) * inv, (LO - origin) * inv
    t = np.where(dirs > 0, t_hi, t_lo)               # distance to the wall on the side the ray heads for, per axis
    axis = np.argmin(t, axis=-1)
    dist = np.min(t, axis=-1)
    sign = np.where(np.take_along_axis(dirs, axis[..., None], -1)[..., 0] > 0, 1, -1)
    return dist, axis, sign


def render(M: np.ndarray, w: int, h: int, fx: float):
    u, v = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    cam = np.stack([(u - w / 2) / fx, -(v - h / 2) / fx, -np.ones_like(u)], axis=-1)
    dirs = cam @ M[:3, :3].T
    dirs /= np.linalg.norm(dirs, axis=-1, keepdims=True)
    dist, axis, sign = trace(M[:3, 3], dirs)
    zdepth = dist * (-(cam[..., 2])) / np.linalg.norm(cam, axis=-1)          # depth along the optical axis, as LiDAR reports
    img = np.zeros((h, w, 3), np.uint8)
    for (a, s), c in COLOURS.items():
        img[(axis == a) & (sign == s)] = c
    return img, zdepth.astype(np.float32)


def make_scan(d: Path, yaw_step: int = 24, pitches=(-40, 0, 40), jitter: float = 0.08, seed: int = 3) -> list[dict]:
    """Write photos/, photos.json for a phone turning in the room. Returns the manifest entries."""
    (d / "photos").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    W, H, fx = 640, 480, 500.0
    dw, dh = 256, 192
    photos, n = [], 0
    angles = [(y, p) for p in pitches for y in range(0, 360, yaw_step)] + [(0, 88), (0, -88)]
    for yaw, pit in angles:
        n += 1
        pos = CAM + rng.uniform(-jitter, jitter, 3)
        M = pose(math.radians(yaw), math.radians(pit), pos)
        img, _ = render(M, W, H, fx)
        _, depth = render(M, dw, dh, fx * dw / W)
        name = f"p{n:03d}"
        Image.fromarray(img).save(d / "photos" / f"{name}.jpg", quality=95)
        (depth + rng.normal(0, 0.004, depth.shape).astype(np.float32)).astype("<f4").tofile(d / "photos" / f"{name}.depth")
        photos.append({"file": f"{name}.jpg", "depth_file": f"{name}.depth", "width": W, "height": H, "depth_width": dw, "depth_height": dh,
                       "intrinsics": [[fx, 0, 0], [0, fx, 0], [W / 2, H / 2, 1]], "transform": M.T.tolist(), "timestamp": 1000.0 + n * 0.3})
    (d / "photos.json").write_text(json.dumps({"version": 2, "photos": photos}))
    return photos
