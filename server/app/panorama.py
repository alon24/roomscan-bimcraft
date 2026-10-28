"""A 360 degree panorama (equirectangular JPEG) from the frames of a scan.

Every frame has a photo, the camera's pose and intrinsics. Standing in one spot and turning, the photos only differ by rotation, so
each panorama pixel is just "which photo sees this direction, and where". If the phone also moved a little, the per-direction depth
(a spherical depth image about the chosen centre) is used to look up the right pixel with the right parallax, and a photo that has
something nearer in front of that spot is not used for it. Photos are blended with feathered edges and matched in brightness and colour
(each frame gets a gain found from how it disagrees with the others where they overlap). Directions no photo sees stay neutral grey."""
import json
import math
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import frames, meshscan, sweep
from .photos import _mat

NEUTRAL = 128
_locks: dict[str, threading.Lock] = {}


def _lock(d: Path) -> threading.Lock:
    return _locks.setdefault(str(d), threading.Lock())


def key_for(d: Path, width: int) -> str:
    st = (d / "photos.json").stat()
    return f"{st.st_mtime_ns}:{st.st_size}:{width}"


def usable_photos(d: Path) -> list[dict]:
    return [p for p in frames.manifest(d) if (d / "photos" / p["file"]).exists() and p.get("transform") and p.get("intrinsics")]


def select_frames(d: Path, photos: list[dict], max_frames: int = 70) -> list[dict]:
    """Keep the sharpest one or two frames for every 12 degree patch of view direction, at most `max_frames` in all: a 0.3 s sweep makes
    hundreds of near-identical frames, and blur from a moving phone would soften the panorama."""
    rows = []
    for ph in photos:
        g = cv2.imread(str(d / "photos" / ph["file"]), cv2.IMREAD_GRAYSCALE)
        if g is None:
            continue
        sharp = float(cv2.Laplacian(cv2.resize(g, (320, 240), interpolation=cv2.INTER_AREA), cv2.CV_32F).var())
        f = -_mat(ph["transform"])[:3, 2]
        cell = (int((math.atan2(f[0], -f[2]) + math.pi) / (2 * math.pi) * 30), int((math.asin(max(-1.0, min(1.0, f[1]))) + math.pi / 2) / math.pi * 15))
        rows.append((cell, sharp, ph))
    keep, by = [], {}
    for cell, sharp, ph in rows:
        by.setdefault(cell, []).append((sharp, ph))
    for cell, lst in by.items():
        lst.sort(key=lambda x: -x[0])
        keep += lst[:2]
    keep.sort(key=lambda x: -x[0])
    return [ph for _, ph in keep[:max_frames]]


def _samples(ph: dict, dirs: np.ndarray, rad: np.ndarray, centre: np.ndarray, dm: np.ndarray | None):
    """For one frame: which panorama pixels it sees (flat indices), where in the photo (u, v) and how much to trust it (feather weight)."""
    M = _mat(ph["transform"])
    Rm, t = M[:3, :3].astype(np.float32), M[:3, 3].astype(np.float32)
    k = np.array(ph["intrinsics"], dtype=float)
    fx, fy, cx, cy = float(k[0][0]), float(k[1][1]), float(k[2][0]), float(k[2][1])
    w_, h_ = float(ph["width"]), float(ph["height"])
    half = min(1.45, math.atan2(math.hypot(w_ / 2, h_ / 2), min(fx, fy)) + 0.12)
    fwd = -Rm[:, 2]
    sel = np.nonzero(dirs @ fwd > math.cos(half))[0]
    if len(sel) == 0:
        return sel, None, None, None
    pts = centre.astype(np.float32) + dirs[sel] * rad[sel, None]
    pc = (pts - t) @ Rm                          # camera space: R^T (p - t)
    z = -pc[:, 2]
    ok = z > 0.1
    zz = np.where(ok, z, 1.0)
    u = fx * pc[:, 0] / zz + cx
    v = cy - fy * pc[:, 1] / zz
    ok &= (u >= 1) & (u < w_ - 2) & (v >= 1) & (v < h_ - 2)
    if dm is not None:                            # something nearer in front of that spot: this photo cannot see it
        dh, dw = dm.shape
        seen = dm[np.clip((v * dh / h_).astype(int), 0, dh - 1), np.clip((u * dw / w_).astype(int), 0, dw - 1)]
        ok &= ~((seen > 0.1) & (z > seen + 0.25))
    fu, fv = np.minimum(u, w_ - 1 - u) / (0.25 * w_), np.minimum(v, h_ - 1 - v) / (0.25 * h_)
    wgt = np.clip(np.minimum(fu, fv), 0, 1) ** 2 + 1e-3
    return sel[ok], u[ok].astype(np.float32), v[ok].astype(np.float32), wgt[ok].astype(np.float32)


def _colours(img: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Bilinear colour of the photo at (u, v) for every sample. OpenCV's remap wants a 2D map no taller than 32767 rows, so lay them out 2048 wide."""
    n = len(u)
    rows = -(-n // 2048)
    pad = rows * 2048 - n
    mu, mv = np.pad(u, (0, pad)).reshape(rows, 2048), np.pad(v, (0, pad)).reshape(rows, 2048)
    return cv2.remap(img, mu, mv, cv2.INTER_LINEAR).reshape(-1, 3)[:n].astype(np.float32)


def _radius(depth: np.ndarray | None, w: int, h: int) -> np.ndarray:
    """Distance of the surface along every panorama direction (m): from the spherical depth, 3 m where there is none."""
    if depth is None or not np.isfinite(depth).any():
        return np.full(w * h, 3.0, np.float32)
    dh, dw = depth.shape
    r = depth[(np.arange(h) * dh // h)[:, None], (np.arange(w) * dw // w)[None, :]]
    r = np.where(np.isfinite(r), r, np.nanmedian(depth))
    return r.reshape(-1).astype(np.float32)


def _gains(low: list, npix: int, sweeps: int = 3) -> np.ndarray:
    """Per-frame brightness/colour gain (n, 3): each frame is nudged toward what the other frames show where they overlap."""
    n = len(low)
    g = np.ones((n, 3), np.float32)
    S = np.zeros((npix, 3), np.float32)
    Wt = np.zeros(npix, np.float32)
    for (idx, col, wg) in low:
        S[idx] += col * wg[:, None]
        Wt[idx] += wg
    for _ in range(sweeps):
        for k, (idx, col, wg) in enumerate(low):
            rest_w = Wt[idx] - wg
            m = rest_w > 0.3
            if m.sum() < 400:
                continue
            others = (S[idx[m]] - col[m] * wg[m, None] * g[k]) / rest_w[m, None]
            new = np.median(others / np.maximum(col[m], 4.0), axis=0)
            new = np.clip(new, 0.6, 1.7)
            S[idx] += col * wg[:, None] * (new - g[k])
            g[k] = new
    g /= np.exp(np.log(g).mean(axis=0))
    return g


def render(d: Path, photos: list[dict], centre: np.ndarray, depth: np.ndarray | None, width: int = 3072,
           max_frames: int = 70) -> tuple[np.ndarray, float, int]:
    """The panorama image (uint8 H x W x 3), the share of directions covered, and how many frames were used."""
    chosen = select_frames(d, photos, max_frames)
    if not chosen:
        raise ValueError("no usable photos")
    imgs = {}

    def load(ph):
        return np.asarray(Image.open(d / "photos" / ph["file"]).convert("RGB"))

    def occluder(ph):
        return meshscan._depth_map(d, ph) if ph.get("depth_file") else None

    wl = min(1024, width)
    hl = wl // 2
    dl = sweep.dirs_grid(wl, hl).reshape(-1, 3)
    rl = _radius(depth, wl, hl)
    low = []
    for ph in chosen:
        idx, u, v, wg = _samples(ph, dl, rl, centre, occluder(ph))
        if len(idx) == 0:
            low.append((idx, np.zeros((0, 3), np.float32), np.zeros(0, np.float32)))
            continue
        low.append((idx, _colours(load(ph), u, v), wg))
    gains = _gains(low, wl * hl)
    del low

    W, H = width, width // 2
    df = sweep.dirs_grid(W, H).reshape(-1, 3)
    rf = _radius(depth, W, H)
    acc = np.zeros((H * W, 3), np.float32)
    wt = np.zeros(H * W, np.float32)
    used = 0
    for k, ph in enumerate(chosen):
        idx, u, v, wg = _samples(ph, df, rf, centre, occluder(ph))
        if len(idx) == 0:
            continue
        col = _colours(load(ph), u, v) * gains[k]
        acc[idx] += col * wg[:, None]
        wt[idx] += wg
        used += 1
    seen = wt > 0.02
    out = np.full((H * W, 3), NEUTRAL, np.float32)
    out[seen] = acc[seen] / wt[seen, None]
    return np.clip(out, 0, 255).astype(np.uint8).reshape(H, W, 3), float(seen.mean()), used


def ensure(d: Path, width: int = 3072, centre: np.ndarray | None = None, depth: np.ndarray | None = None, force: bool = False) -> dict:
    """Build (or reuse) panorama.jpg and panorama.json for this scan. Returns the json; a freshly built one also carries `_image`."""
    with _lock(d):
        pj, pimg = d / "panorama.json", d / "panorama.jpg"
        if not (d / "photos.json").exists():
            raise ValueError("this scan has no photos")
        key = key_for(d, width)
        if not force and pj.exists() and pimg.exists():
            j = json.loads(pj.read_text())
            if j.get("key") == key:
                return j
        photos = usable_photos(d)
        if len(photos) < 2:
            raise ValueError("a panorama needs at least 2 photos with poses")
        if centre is None:
            centre = sweep.centre_of(photos)
        if depth is None:
            dp = sweep.depth_photos(d)
            if len(dp) >= 3:
                depth, _ = sweep.fuse_depth_sphere(d, dp, centre)
        img, cover, used = render(d, photos, centre, depth, width)
        Image.fromarray(img).save(pimg, quality=90, optimize=True)
        spread = float(np.linalg.norm(sweep.cam_positions(photos) - centre, axis=1).max())
        j = {"key": key, "width": img.shape[1], "height": img.shape[0], "coverage": round(cover, 3),
             "centre": [round(float(x), 3) for x in centre], "frames_used": used, "frames": len(photos),
             "camera_spread_m": round(spread, 3), "from_one_spot": spread <= sweep.SWEEP_SPREAD_M}
        pj.write_text(json.dumps(j))
        return {**j, "_image": img}
