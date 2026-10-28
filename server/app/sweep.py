"""A room scanned from one spot: sit (or stand) in the middle and sweep the phone round.

Every frame has a photo, a LiDAR depth map and the camera's pose. Instead of fusing them into a volume (which needs the phone to
travel and leaves holes where it never looked), the depth is fused into ONE spherical depth image around the spot you stood on:
for every direction (azimuth, elevation) the median range seen by any frame. That image is the room as seen from the middle:
walls, floor and ceiling, with furniture in front of them. It is meshed (cut where the depth jumps), snapped flat where it
lies on a wall/floor/ceiling, coloured from the 360 panorama, and measured: distance to the walls, floor and ceiling and the
room's width, length and height."""
import json
import math
from pathlib import Path

import numpy as np
import trimesh
from scipy import ndimage as ndi

from . import frames, meshscan
from .photos import _mat

RANGE = (0.15, 6.0)           # metres of LiDAR depth we trust
SWEEP_SPREAD_M = 0.35         # all cameras within this of their middle: a sweep from one spot
SPHERE = (720, 360)           # spherical depth image: 0.5 degree cells (width, height)


# ---- which scans are sweeps ----

def depth_photos(d: Path) -> list[dict]:
    return [p for p in frames.manifest(d) if p.get("depth_file") and (d / "photos" / p["depth_file"]).exists()
            and (d / "photos" / p["file"]).exists()]


def cam_positions(photos: list[dict]) -> np.ndarray:
    return np.array([_mat(p["transform"])[:3, 3] for p in photos], dtype=float)


def centre_of(photos: list[dict]) -> np.ndarray:
    return np.median(cam_positions(photos), axis=0)


def is_sweep(d: Path, meta: dict) -> bool:
    """The phone says so, or every camera stayed within ~35 cm of the middle of the cameras (you turned on the spot)."""
    phone = meta.get("phone") or {}
    if phone.get("sweep") is False:          # the scan says it is an ordinary room walk, whatever the camera positions look like
        return False
    if phone.get("sweep") or str(phone.get("scan type", "")).lower().startswith("sweep"):
        return True
    photos = depth_photos(d)
    if len(photos) < 8:
        return False
    pos = cam_positions(photos)
    return float(np.linalg.norm(pos - np.median(pos, axis=0), axis=1).max()) <= SWEEP_SPREAD_M


# ---- the spherical grid. azimuth 0 = looking along -Z (ARKit's start heading), +90 = +X; elevation +90 = up ----

def dirs_grid(w: int, h: int) -> np.ndarray:
    """Unit direction (h, w, 3) of every cell centre of an equirectangular image."""
    az = ((np.arange(w) + 0.5) / w - 0.5) * 2 * np.pi
    el = (0.5 - (np.arange(h) + 0.5) / h) * np.pi
    ce = np.cos(el)[:, None]
    return np.stack([ce * np.sin(az)[None, :], np.broadcast_to(np.sin(el)[:, None], (h, w)), -ce * np.cos(az)[None, :]], axis=-1).astype(np.float32)


def to_cell(dirs: np.ndarray, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """Column and row of the equirectangular cell each unit direction falls in."""
    az = np.arctan2(dirs[:, 0], -dirs[:, 2])
    el = np.arcsin(np.clip(dirs[:, 1], -1, 1))
    u = np.floor((az / (2 * np.pi) + 0.5) * w).astype(np.int64) % w
    v = np.clip(np.floor((0.5 - el / np.pi) * h).astype(np.int64), 0, h - 1)
    return u, v


def clean_depth(dm: np.ndarray) -> np.ndarray:
    """Depth with the range limited and the 'flying pixels' that LiDAR invents along depth edges removed (set to 0)."""
    dm = dm.astype(np.float32).copy()
    dm[~((dm > RANGE[0]) & (dm < RANGE[1]))] = 0
    flying = (ndi.maximum_filter(dm, 3) - ndi.minimum_filter(dm, 3)) > (0.05 * dm + 0.02)
    dm[flying] = 0
    return dm


def fuse_depth_sphere(d: Path, photos: list[dict], centre: np.ndarray, size: tuple[int, int] = SPHERE,
                      max_frames: int = 240) -> tuple[np.ndarray, int]:
    """Spherical depth image about `centre`: per cell, the median distance (m) to the surface over every frame that saw it, NaN where
    no frame did. Isolated spikes are dropped and one-cell gaps closed. Also returns how many frames were used."""
    w, h = size
    step = max(1, math.ceil(len(photos) / max_frames))
    bins, rng, used = [], [], 0
    for ph in photos[::step]:
        dm = meshscan._depth_map(d, ph)
        if dm is None:
            continue
        dm = clean_depth(dm)
        ok = dm > 0
        if not ok.any():
            continue
        p = frames.world_points(ph, dm)[ok] - centre
        r = np.linalg.norm(p, axis=1)
        good = r > 0.05
        u, v = to_cell((p[good] / r[good, None]).astype(np.float32), w, h)
        bins.append(v * w + u)
        rng.append(r[good].astype(np.float32))
        used += 1
    if not bins:
        return np.full((h, w), np.nan, np.float32), 0
    bins, rng = np.concatenate(bins), np.concatenate(rng)
    order = np.lexsort((rng, bins))                      # sorted by cell, then by range: the median is the middle one
    bins, rng = bins[order], rng[order]
    first = np.r_[0, np.nonzero(np.diff(bins))[0] + 1]
    count = np.diff(np.r_[first, len(bins)])
    out = np.full(w * h, np.nan, np.float32)
    out[bins[first]] = rng[first + count // 2]
    D = out.reshape(h, w)
    return _tidy(D), used


def _tidy(D: np.ndarray, spike: float = 0.06, close: float = 4.0) -> np.ndarray:
    """Drop cells that disagree with their neighbours (a stray reading), then fill gaps up to `close` cells wide from the nearest value."""
    valid = np.isfinite(D)
    if not valid.any():
        return D
    dist, (iy, ix) = ndi.distance_transform_edt(~valid, return_indices=True)
    filled = D[iy, ix]
    pad = 4                                    # the image wraps round at the sides: pad with the other side so the seam is not special
    med = ndi.median_filter(np.pad(filled, ((0, 0), (pad, pad)), mode="wrap"), size=5, mode="nearest")[:, pad:-pad]
    D = D.copy()
    D[valid & (np.abs(D - med) > spike * med + 0.05)] = np.nan
    valid = np.isfinite(D)
    if not valid.any():
        return D
    dist, (iy, ix) = ndi.distance_transform_edt(~valid, return_indices=True)
    fill = (~valid) & (dist <= close)
    D[fill] = D[iy, ix][fill]
    return D


def points_of(D: np.ndarray, centre: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """World points (h, w, 3) of the spherical depth image, and the unit directions."""
    h, w = D.shape
    dirs = dirs_grid(w, h)
    return centre + dirs * D[..., None], dirs


# ---- measuring the room from the middle ----

def _mode(values: np.ndarray, width: float = 0.05) -> float | None:
    """The distance most surface points agree on: centre of the densest `width` window."""
    if len(values) < 150:
        return None
    v = np.sort(values)
    k = np.searchsorted(v, v + width)
    i = int(np.argmax(k - np.arange(len(v))))
    return float(np.median(v[i:k[i]]))


def _manhattan_angle(P: np.ndarray, dirs: np.ndarray, centre: np.ndarray) -> float:
    """Angle (radians) of the room's walls about the vertical: from the directions the wall surfaces face, folded into quarter turns."""
    h, w, _ = P.shape
    rel = P - centre
    dU = np.roll(rel, -1, axis=1) - np.roll(rel, 1, axis=1)
    dV = np.vstack([rel[1:2] - rel[:1], rel[2:] - rel[:-2], rel[-1:] - rel[-2:-1]])
    n = np.cross(dU, dV)
    ln = np.linalg.norm(n, axis=2)
    ok = np.isfinite(ln) & (ln > 1e-9)
    n = np.where(ok[..., None], n / np.where(ok, ln, 1)[..., None], 0)
    n = np.where((np.einsum("hwc,hwc->hw", n, dirs) > 0)[..., None], -n, n)       # facing the middle
    el = np.degrees(np.arcsin(np.clip(dirs[..., 1], -1, 1)))
    sel = ok & np.isfinite(rel[..., 0]) & (np.abs(el) < 40) & (np.abs(n[..., 1]) < 0.35)
    if sel.sum() < 100:
        return 0.0
    a = np.arctan2(n[sel][:, 2], n[sel][:, 0])
    return float(np.angle(np.exp(4j * a).sum()) / 4)


def measure(D: np.ndarray, centre: np.ndarray, heading_az: float = 0.0, snap: bool = True) -> tuple[dict, np.ndarray]:
    """Distances from the middle to the walls, floor and ceiling, plus the room's size. Returns (numbers, depth with the surfaces that lie
    on those walls/floor/ceiling snapped flat). `heading_az` is the azimuth (radians) of the first frame: the wall it faces is 'front'."""
    P, dirs = points_of(D, centre)
    h, w = D.shape
    valid = np.isfinite(D)
    rel = np.where(valid[..., None], P - centre, 0)
    el = np.degrees(np.arcsin(np.clip(dirs[..., 1], -1, 1)))
    theta = _manhattan_angle(P, dirs, centre)
    axes = {k: np.array([math.cos(theta + a), 0.0, math.sin(theta + a)]) for k, a in (("e1", 0), ("e2", math.pi / 2), ("-e1", math.pi), ("-e2", 3 * math.pi / 2))}
    # name the walls from the heading of the first frame: front = the one it faced
    head = np.array([math.sin(heading_az), 0.0, -math.cos(heading_az)])
    front = max(axes, key=lambda k: float(axes[k] @ head))
    order = [front]
    for _ in range(3):   # right of a heading (seen from above, Y up) is cross(heading, up)
        prev = axes[order[-1]]
        right = np.cross(prev, [0, 1, 0])
        order.append(max((k for k in axes if k not in order), key=lambda k: float(axes[k] @ right)))
    names = dict(zip(order, ["front", "right", "back", "left"]))

    horiz = np.hypot(dirs[..., 0], dirs[..., 2]) + 1e-9
    planes, out = [], {"walls": {}}
    for k, axis in axes.items():
        cos_az = (dirs[..., 0] * axis[0] + dirs[..., 2] * axis[2]) / horiz
        sel = valid & (np.abs(el) < 30) & (cos_az > math.cos(math.radians(22)))
        s = (rel[sel] @ axis)
        info = {"wall_m": None, "nearest_m": None, "axis": [round(float(x), 4) for x in axis]}
        if len(s):
            info["nearest_m"] = round(float(np.percentile(s, 4)), 3)
        wall = _mode(s)
        if wall is not None:
            info["wall_m"] = round(wall, 3)
            planes.append((axis, wall))
        out["walls"][names[k]] = info
    for key, sign, mask in (("floor", -1.0, valid & (el < -35)), ("ceiling", 1.0, valid & (el > 35))):
        s = sign * rel[mask][:, 1]
        info = {"wall_m": None, "nearest_m": round(float(np.percentile(s, 4)), 3) if len(s) else None}
        m = _mode(s)
        if m is not None:
            info["wall_m"] = round(m, 3)
            planes.append((np.array([0.0, sign, 0.0]), m))
        out[key] = info
    wl = {n: v["wall_m"] for n, v in out["walls"].items()}
    if wl["left"] and wl["right"]:
        out["width_m"] = round(wl["left"] + wl["right"], 3)
    if wl["front"] and wl["back"]:
        out["length_m"] = round(wl["front"] + wl["back"], 3)
    if out["floor"]["wall_m"] and out["ceiling"]["wall_m"]:
        out["height_m"] = round(out["floor"]["wall_m"] + out["ceiling"]["wall_m"], 3)
    out.update(centre=[round(float(x), 3) for x in centre], heading_deg=round(math.degrees(heading_az), 1), wall_angle_deg=round(math.degrees(theta) % 90, 1),
               coverage=round(float(valid.mean()), 3), height_of_phone_above_floor_m=out["floor"]["wall_m"])
    D2 = D
    if snap and planes:        # where the surface lies within 4 cm of a found wall/floor/ceiling, put it exactly on it: flat, clean walls
        D2 = D.copy()
        best = np.full(D.shape, np.inf, np.float32)
        for n, off in planes:
            ndir = dirs @ n.astype(np.float32)
            dist = np.abs(rel @ n.astype(np.float32) - off)
            hit = valid & (ndir > 0.25) & (dist < 0.04) & (dist < best)
            D2[hit] = off / ndir[hit]
            best[hit] = dist[hit]
        out["snapped_share"] = round(float(np.isfinite(best).mean()), 3)
    return out, D2


# ---- the mesh ----

def depth_mesh(D: np.ndarray, centre: np.ndarray, colours: np.ndarray | None = None, jump: float = 1.16) -> trimesh.Trimesh | None:
    """The spherical depth image as a surface seen from the middle: a grid of vertices, with the triangles left out where the depth
    jumps (an object's edge against the wall behind it), so the two are not joined by a stretched sheet."""
    h, w = D.shape
    valid = np.isfinite(D)
    P, dirs = points_of(D, centre)
    vid = np.arange(h * w).reshape(h, w)
    a, b = vid[:-1, :], np.roll(vid, -1, axis=1)[:-1, :]
    c, e = vid[1:, :], np.roll(vid, -1, axis=1)[1:, :]
    cell = np.stack([D[:-1, :], np.roll(D, -1, axis=1)[:-1, :], D[1:, :], np.roll(D, -1, axis=1)[1:, :]])
    fin = np.isfinite(cell)
    ok = fin.all(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        ok &= (np.max(np.where(fin, cell, -np.inf), axis=0) / np.maximum(np.min(np.where(fin, cell, np.inf), axis=0), 1e-6)) < jump
    ok &= np.abs(dirs[:-1, :, 1]) < 0.9995           # the poles collapse to a point
    a, b, c, e = a[ok], b[ok], c[ok], e[ok]
    if len(a) == 0:
        return None
    faces = np.vstack([np.c_[a, c, b], np.c_[b, c, e]])
    verts = P.reshape(-1, 3)
    m = trimesh.Trimesh(np.nan_to_num(verts), faces, process=False)
    n = m.face_normals
    toward = (centre - m.triangles_center)
    if float(np.einsum("ij,ij->i", n, toward).mean()) < 0:      # wind the triangles to face the middle, where you look from
        m.faces = m.faces[:, ::-1]
    m.update_faces(m.nondegenerate_faces())
    if colours is not None:
        m.visual = trimesh.visual.ColorVisuals(m, vertex_colors=np.c_[colours.reshape(-1, 3), np.full(h * w, 255, np.uint8)])
    m.remove_unreferenced_vertices()
    return m


def convert_sweep(d: Path, scale: float = 1.0) -> tuple[bool, str]:
    """Sweep-from-one-spot room: depth panorama -> measurements, flat clean surface, panorama colours. Writes colored.ply, mesh.bin,
    points.bin, report.json, room.json and panorama.jpg/json. Returns (ok, log)."""
    from . import panorama
    photos = depth_photos(d)
    if len(photos) < 5:
        return False, "a sweep needs at least 5 frames with LiDAR depth"
    centre = centre_of(photos)
    D, used = fuse_depth_sphere(d, photos, centre)
    cover = float(np.isfinite(D).mean())
    if cover < 0.05:
        return False, "the depth frames covered almost none of the sphere"
    heading = _heading(photos[0])
    room, D2 = measure(D, centre, heading)
    pano = panorama.ensure(d, width=1536, centre=centre, depth=D2, force=True)
    img = pano["_image"]
    ph, pw = img.shape[:2]
    h, w = D2.shape
    cu = (np.arange(w) * pw // w)[None, :].repeat(h, 0)
    cv_ = (np.arange(h) * ph // h)[:, None].repeat(w, 1)
    col = img[cv_, cu]
    mesh = depth_mesh(D2, centre, col)
    if mesh is None or len(mesh.faces) == 0:
        return False, "the depth panorama could not be turned into a surface"
    if scale != 1.0:
        mesh.apply_scale(scale)
    rgb = np.asarray(mesh.visual.vertex_colors)[:, :3]
    (d / "mesh.bin").write_bytes(meshscan.pack_preview(mesh, rgb))
    mesh.export(d / "colored.ply")
    P, _ = points_of(D2, centre)
    ok = np.isfinite(D2)
    (d / "points.bin").write_bytes(meshscan.pack_points((P[ok] * scale).astype(np.float32), col[ok]))
    for stale in ("tri_photo.bin", "texturing.json"):    # an earlier conversion's photo-texture plan no longer matches the triangles
        try:
            (d / stale).unlink()
        except FileNotFoundError:
            pass
    if scale != 1.0:
        for k in ("width_m", "length_m", "height_m"):
            if k in room:
                room[k] = round(room[k] * scale, 3)
    room.update(frames_used=used, panorama_coverage=pano["coverage"], depth_coverage=round(cover, 3))
    (d / "room.json").write_text(json.dumps(room, indent=1))
    rep = {"vertices": int(len(mesh.vertices)), "faces": int(len(mesh.faces)), "points": int(ok.sum()),
           "size_m": [round(float(x), 2) for x in mesh.extents], "coloured": pano["coverage"], "photos": len(photos)}
    (d / "report.json").write_text(json.dumps(rep))
    meshscan.write_settings(d, mode="sweep from one spot (depth panorama)", frames_used=used, surface_method="spherical depth image",
                            depth_range=f"{RANGE[0]} to {RANGE[1]} m", surface="median depth per direction, flat walls snapped, coloured from the 360 panorama",
                            photos=len(photos), scale=scale)
    size = " x ".join(f"{room[k]:g}" for k in ("width_m", "length_m", "height_m") if k in room)
    return True, (f"sweep from one spot: {used} frames fused into a depth panorama ({round(cover * 100)}% of directions), {len(mesh.faces)} triangles, "
                  f"{round(pano['coverage'] * 100)}% coloured from the panorama" + (f", room about {size} m" if size else ""))


def _heading(photo: dict) -> float:
    """Azimuth (radians, 0 = -Z, +90 = +X) the camera looks along."""
    f = -_mat(photo["transform"])[:3, 2]
    return float(math.atan2(f[0], -f[2]))
