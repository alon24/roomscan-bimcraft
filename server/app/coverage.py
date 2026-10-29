"""Live coverage: while a room is being scanned, what has been seen, how well, and what to capture next.

Every frame's LiDAR depth is dropped into 5 cm cells (how many frames saw the cell, from which of 8 viewing directions, how near).
From those cells comes a top-down 10 cm map for the phone and a short list of "needs" in plain words. Everything is in the frame of
the scan (for a patch scan: the OLD scan's frame; `to_view` turns the result into the phone's own session frame).

Needs v1 (docs/live-guide.md): sweep gaps by the phone dial's sectors (24 x walls/ceiling/floor), holes inside the room outline,
wall cells seen from far away or from too few frames, a missing ceiling/floor, blurry frames. At most 5, stable ids with hysteresis."""
import base64
import hashlib
import math
import threading

import cv2
import numpy as np
from scipy import ndimage as ndi

from . import frames
from .photos import _mat

CELL = 0.05              # integration cell, metres
MAP_CELL = 0.10          # cell of the map sent to the phone
MAX_GRID = 96            # cap of the map's width and height (cells)
RANGE = (0.15, 6.0)      # metres of LiDAR depth we trust
FAR = 3.0                # a wall cell seen only from further than this is "far"
MIN_VIEWS = 3            # fewer frames than this and a wall cell is "thin"
SECTORS = 24             # the dial's sectors of azimuth
REACH_DEG = 25.0         # the dial counts a cell as seen when the camera points this close to its centre
DIAL_PITCH = (0.0, 55.0, -50.0)    # walls, ceiling, floor (same as the phone's SweepCoverage.bandPitch)
OFF = 1 << 20


def _pack(i: np.ndarray) -> np.ndarray:
    i = i.astype(np.int64) + OFF
    return (i[:, 0] << 42) | (i[:, 1] << 21) | i[:, 2]


def _unpack(k: np.ndarray) -> np.ndarray:
    k = np.asarray(k, dtype=np.int64)
    m = (1 << 21) - 1
    return np.stack([(k >> 42) & m, (k >> 21) & m, k & m], axis=1) - OFF


def _dial_dirs() -> np.ndarray:
    """Unit direction (3, SECTORS, 3) of every dial cell centre: ARKit axes (azimuth 0 looks along -Z, +90 along +X)."""
    out = np.zeros((3, SECTORS, 3))
    for b, p in enumerate(DIAL_PITCH):
        for i in range(SECTORS):
            a, e = math.radians((i + 0.5) * 360 / SECTORS), math.radians(p)
            out[b, i] = (math.sin(a) * math.cos(e), math.sin(e), -math.cos(a) * math.cos(e))
    return out


DIAL = _dial_dirs()


def _circular_runs(miss: np.ndarray) -> list[tuple[int, int]]:
    """Stretches of True round a circle as (first index, length); one run if everything is True."""
    n = len(miss)
    if miss.all():
        return [(0, n)]
    out = []
    for i in range(n):
        if miss[i] and not miss[i - 1]:
            length = 0
            while miss[(i + length) % n]:
                length += 1
            out.append((i, length))
    return out


class NeedTracker:
    """Keeps the list of needs steady: a need keeps its id and place until it is satisfied (gone for 3 updates in a row)."""
    GONE_AFTER = 3

    def __init__(self):
        self.active: dict[str, dict] = {}
        self.counter = 0
        self.order = 0

    def update(self, cands: list[dict]) -> list[dict]:
        seen = set()
        for c in cands:
            match = None
            for id_, a in self.active.items():
                n = a["need"]
                if id_ in seen or n["kind"] != c["kind"]:
                    continue
                if c["kind"] in ("blur",) or np.linalg.norm(np.array(n["target"]) - np.array(c["target"])) < 1.2:
                    match = id_
                    break
            if match is None:
                self.counter += 1
                self.order += 1
                id_ = f"{c['kind']}-{self.counter}"
                self.active[id_] = {"need": {**c, "id": id_}, "absent": 0, "order": self.order}
            else:
                id_ = match
                a = self.active[id_]
                a["absent"] = 0
                n = a["need"]
                n["priority"], n["text"] = c["priority"], c["text"]
                if np.linalg.norm(np.array(n["target"]) - np.array(c["target"])) > 0.5:       # keep the place unless it clearly moved
                    n["target"], n["stand"], n["look"] = c["target"], c["stand"], c["look"]
            seen.add(id_)
        for id_ in list(self.active):
            if id_ not in seen:
                self.active[id_]["absent"] += 1
                if self.active[id_]["absent"] >= self.GONE_AFTER:
                    del self.active[id_]
        ordered = sorted(self.active.values(), key=lambda a: (-a["need"]["priority"], a["order"]))
        return [a["need"] for a in ordered[:5]]


class CoverageEngine:
    def __init__(self, kind: str = "room"):
        self.kind = kind                      # "room" | "sweep"
        self.lock = threading.RLock()
        self.map_lock = threading.Lock()      # one map at a time: the tracker, seq and hash are shared by the ticker and a hello
        self.index: dict[int, int] = {}
        self.keys: list[int] = []
        self.views: list[int] = []
        self.sect: list[int] = []
        self.near: list[float] = []
        self.free: set[int] = set()           # 10 cm columns seen across (packed ix, iz)
        self.cams: list[np.ndarray] = []
        self.pose: np.ndarray | None = None   # latest camera position
        self.heading0: float | None = None    # azimuth of the first frame: "front" in the texts
        self.dial = np.zeros((3, SECTORS), bool)
        self.sharp: list[float] = []
        self.frames = 0
        self.pct = 0.0
        self.tracker = NeedTracker()
        self.dirty = True
        self.last_hash = ""
        self.seq = 0
        self.to_view: np.ndarray | None = None   # engine frame -> the phone's frame (patch mode)

    # ---- input ----

    def add_frame(self, photo: dict, depth: np.ndarray | None, pre: np.ndarray | None = None, sharpness: float | None = None) -> None:
        """Drop one frame in. `pre` moves the photo's own frame into the engine's (identity for the first session)."""
        T = _mat(photo["transform"])
        if pre is not None:
            T = pre @ T
        cam = T[:3, 3].copy()
        fwd = -T[:3, 2]
        with self.lock:
            self.frames += 1
            self.cams.append(cam)
            self.pose = cam
            if sharpness is not None:
                self.sharp.append(float(sharpness))
            self._dial(fwd)
            if self.heading0 is None:
                self.heading0 = math.atan2(fwd[0], -fwd[2])
            self.dirty = True
        if depth is None:
            return
        P = frames.world_points(photo, depth)[::2, ::2].reshape(-1, 3)
        d = depth[::2, ::2].reshape(-1)
        ok = np.isfinite(d) & (d > RANGE[0]) & (d < RANGE[1]) & np.isfinite(P).all(axis=1)
        if not ok.any():
            return
        P, d = P[ok], d[ok]
        if pre is not None:
            P = P @ pre[:3, :3].T + pre[:3, 3]
        v = cam - P
        dist = np.linalg.norm(v, axis=1)
        sector = (((np.arctan2(v[:, 0], v[:, 2]) + math.pi) / (2 * math.pi)) * 8).astype(int) % 8
        keys = _pack(np.floor(P / CELL))
        uk, inv = np.unique(keys, return_inverse=True)
        bits = np.zeros(len(uk), np.uint8)
        np.bitwise_or.at(bits, inv, (1 << sector).astype(np.uint8))
        near = np.full(len(uk), np.inf)
        np.minimum.at(near, inv, dist)
        free = self._free_columns(P, cam)
        with self.lock:
            for k, b, n in zip(uk.tolist(), bits.tolist(), near.tolist()):
                i = self.index.get(k)
                if i is None:
                    self.index[k] = len(self.keys)
                    self.keys.append(k); self.views.append(1); self.sect.append(b); self.near.append(n)
                else:
                    self.views[i] += 1; self.sect[i] |= b
                    if n < self.near[i]:
                        self.near[i] = n
            self.free |= free
            self.dirty = True

    def set_pose(self, transform) -> None:
        T = _mat(transform)
        with self.lock:
            self.pose = T[:3, 3].copy()
            self._dial(-T[:3, 2])
            self.dirty = True

    def _dial(self, fwd: np.ndarray) -> None:
        f = fwd / (np.linalg.norm(fwd) + 1e-9)
        hit = DIAL @ f >= math.cos(math.radians(REACH_DEG))
        self.dial |= hit

    @staticmethod
    def _free_columns(P: np.ndarray, cam: np.ndarray) -> set[int]:
        """Columns between the camera and the nearest surface at about camera height, in 72 directions: floor the phone looked across."""
        rel = P - cam
        h = np.hypot(rel[:, 0], rel[:, 2])
        band = (np.abs(rel[:, 1]) < 0.6) & (h > 0.3)
        if not band.any():
            return set()
        az = (np.arctan2(rel[band, 0], rel[band, 2]) + math.pi) / (2 * math.pi)
        b = (az * 72).astype(int) % 72
        r = np.full(72, np.inf)
        np.minimum.at(r, b, h[band])
        out = set()
        for i in np.nonzero(np.isfinite(r))[0]:
            a = (i + 0.5) / 72 * 2 * math.pi - math.pi
            dx, dz = math.sin(a), math.cos(a)
            steps = np.arange(0.2, r[i] * 0.9, MAP_CELL)
            ix = np.floor((cam[0] + dx * steps) / MAP_CELL).astype(np.int64)
            iz = np.floor((cam[2] + dz * steps) / MAP_CELL).astype(np.int64)
            out.update(((ix + OFF) << 21 | (iz + OFF)).tolist())
        return out

    # ---- output ----

    def _snapshot(self):
        with self.lock:
            n = len(self.keys)
            return (np.array(self.keys, dtype=np.int64), np.array(self.views, dtype=np.int32), np.array(self.sect, dtype=np.uint8),
                    np.array(self.near, dtype=np.float32), np.array(list(self.free), dtype=np.int64), np.array(self.cams) if self.cams else np.zeros((0, 3)),
                    None if self.pose is None else self.pose.copy(), self.dial.copy(), list(self.sharp), n)

    def build_map(self, to_view: np.ndarray | None = None) -> dict | None:
        """The `map` message body (without seq/type), or None if nothing is known yet. `to_view` expresses it in the phone's frame."""
        keys, views, sect, near, free, cams, pose, dial, sharp, n = self._snapshot()
        if n == 0 and pose is None:
            return None
        M = to_view if to_view is not None else np.eye(4)
        R, t = M[:3, :3], M[:3, 3]
        cells = (_unpack(keys) + 0.5) * CELL if n else np.zeros((0, 3))
        cells = cells @ R.T + t
        cams_v = cams @ R.T + t if len(cams) else cams
        pose_v = None if pose is None else R @ pose + t
        good_cell = (views >= MIN_VIEWS) & (near <= FAR) if n else np.zeros(0, bool)

        # levels: floor and ceiling from the cells themselves
        floor_y = ceil_y = None
        if n >= 300:
            floor_y, ceil_y = float(np.percentile(cells[:, 1], 3)), float(np.percentile(cells[:, 1], 97))
            if ceil_y - floor_y < 1.6:
                ceil_y = None
        wall = np.ones(n, bool)
        isfloor = np.zeros(n, bool)
        isceil = np.zeros(n, bool)
        if floor_y is not None:
            isfloor = cells[:, 1] < floor_y + 0.25
            wall &= ~isfloor
        if ceil_y is not None:
            isceil = cells[:, 1] > ceil_y - 0.25
            wall &= ~isceil

        # grid bounds in the view frame
        pts_xz = [cells[:, [0, 2]]] if n else []
        if len(cams_v):
            pts_xz.append(cams_v[:, [0, 2]])
        allxz = np.concatenate(pts_xz) if pts_xz else np.zeros((1, 2))
        lo, hi = allxz.min(axis=0) - 0.3, allxz.max(axis=0) + 0.3
        span = hi - lo
        size = np.minimum(MAX_GRID, np.ceil(span / MAP_CELL)).astype(int)
        big = span / MAP_CELL > MAX_GRID
        if big.any():                    # too big for the map: centre the capped axis on where the phone mostly was
            c = np.median(cams_v[:, [0, 2]], axis=0) if len(cams_v) else allxz.mean(axis=0)
            lo = np.where(big, c - size * MAP_CELL / 2, lo)
        w, h = max(int(size[0]), 8), max(int(size[1]), 8)
        x0, z0 = float(round(lo[0], 2)), float(round(lo[1], 2))

        def col(xz: np.ndarray):
            ix = np.floor((xz[:, 0] - x0) / MAP_CELL).astype(int)
            iz = np.floor((xz[:, 1] - z0) / MAP_CELL).astype(int)
            ok = (ix >= 0) & (ix < w) & (iz >= 0) & (iz < h)
            return ix, iz, ok

        seen = np.zeros((h, w), bool)
        wall_n = np.zeros((h, w)); wall_good = np.zeros((h, w)); wall_far = np.zeros((h, w)); wall_few = np.zeros((h, w))
        oth_n = np.zeros((h, w)); oth_good = np.zeros((h, w))
        ceil_seen = np.zeros((h, w), bool); floor_seen = np.zeros((h, w), bool)
        if n:
            ix, iz, ok = col(cells[:, [0, 2]])
            for sel, tn, tg in ((wall, wall_n, wall_good), (~wall, oth_n, oth_good)):
                m = ok & sel
                np.add.at(tn, (iz[m], ix[m]), 1)
                np.add.at(tg, (iz[m], ix[m]), good_cell[m].astype(float))
            m = ok & wall
            np.add.at(wall_far, (iz[m], ix[m]), (near[m] > FAR).astype(float))
            np.add.at(wall_few, (iz[m], ix[m]), (views[m] < MIN_VIEWS).astype(float))
            seen[iz[ok], ix[ok]] = True
            ceil_seen[iz[ok & isceil], ix[ok & isceil]] = True
            floor_seen[iz[ok & isfloor], ix[ok & isfloor]] = True
        freeg = np.zeros((h, w), bool)
        if len(free):
            fx = (free >> 21) - OFF
            fz = (free & ((1 << 21) - 1)) - OFF
            xz = np.stack([(fx + 0.5) * MAP_CELL, (fz + 0.5) * MAP_CELL], axis=1)
            xz = xz @ R[[0, 2]][:, [0, 2]].T + t[[0, 2]]
            ix, iz, ok = col(xz)
            freeg[iz[ok], ix[ok]] = True
        # quality of a column: its wall cells if it has any, else its floor/ceiling cells
        wq = np.where(wall_n > 0, wall_good / np.maximum(wall_n, 1), np.where(oth_n > 0, oth_good / np.maximum(oth_n, 1), 0))
        good = seen & (wq >= 0.5)
        thin = seen & ~good
        grid = np.zeros((h, w), np.uint8)
        grid[freeg & ~seen] = 3
        grid[thin] = 1
        grid[good] = 2

        # room outline: convex hull of what was seen and where the phone was; unseen cells inside it are gaps
        hull = np.zeros((h, w), np.uint8)
        pts = np.argwhere(seen | freeg)
        if len(pts) >= 3:
            cv2.fillConvexPoly(hull, cv2.convexHull(pts[:, ::-1].astype(np.int32)), 1)
        hull = ndi.binary_erosion(hull.astype(bool), iterations=1) if hull.sum() > 20 else hull.astype(bool)
        holes = hull & ~seen & ~freeg

        ntotal = int(good.sum() + thin.sum() + holes.sum())
        share = dial.mean(axis=1)
        if self.kind == "sweep":
            pct = 100 * (0.5 * share[0] + 0.25 * share[1] + 0.25 * share[2])
        else:   # a walked room: how much of the outline is seen well, held down by the directions the phone never faced
            pct = 100 * (0.4 * (good.sum() + 0.5 * thin.sum()) / max(ntotal, 1) + 0.6 * min(1.0, share[0] / 0.9))
        with self.lock:
            self.pct = max(self.pct, float(pct)) if self.frames > 1 else float(pct)
            pct = self.pct

        # ---- needs ----
        cands: list[dict] = []
        cam_y = float(np.median(cams_v[:, 1])) if len(cams_v) else (floor_y or 0) + 1.4
        stand0 = np.median(cams_v, axis=0) if len(cams_v) else np.zeros(3)
        h0 = self.heading0 or 0.0
        hv = R @ np.array([math.sin(h0), 0.0, -math.cos(h0)])              # the first frame's heading, in the view frame
        heading0 = math.atan2(hv[0], -hv[2])
        you_xz = pose_v[[0, 2]] if pose_v is not None else stand0[[0, 2]]

        def xz_of(ix: float, iz: float) -> np.ndarray:
            return np.array([x0 + (ix + 0.5) * MAP_CELL, z0 + (iz + 0.5) * MAP_CELL])

        def rel_word(dx: float, dz: float) -> str:
            a = (math.degrees(math.atan2(dx, -dz) - heading0) + 540) % 360 - 180        # 0 = where the first frame looked
            return "ahead" if abs(a) < 45 else "to the right" if 45 <= a < 135 else "to the left" if -135 <= a <= -45 else "behind you"

        def mk(kind, text, target, stand, priority):
            target = np.array(target, float)
            stand_a = None if stand is None else np.array(stand, float)
            look = None
            if stand_a is not None:
                v = target - stand_a
                look = (v / (np.linalg.norm(v) + 1e-9)).round(3).tolist()
            cands.append({"kind": kind, "text": text, "target": target.round(2).tolist(),
                          "stand": None if stand_a is None else stand_a.round(2).tolist(), "look": look, "priority": priority})

        here = stand0 if self.kind == "sweep" or pose_v is None else pose_v     # a walked scan: from where the phone is now
        if len(cams_v):
            bands = range(3) if self.kind == "sweep" else ((0,) if self.frames >= 4 and dial[0].mean() < 0.85 else ())
            for b in bands:
                if dial[b].mean() >= (0.95 if self.kind == "sweep" else 0.85):
                    continue
                runs = _circular_runs(~dial[b])                      # (first sector, how many) of every stretch not seen, seam included
                if b > 0:                                            # one need for the ceiling and one for the floor: the biggest stretch
                    runs = [max(runs, key=lambda r: r[1])]
                for start, length in runs:
                    az = ((start + length / 2) * 360 / SECTORS % 360) * math.pi / 180
                    el = math.radians(DIAL_PITCH[b])
                    look_w = np.array([math.sin(az) * math.cos(el), math.sin(el), -math.cos(az) * math.cos(el)])
                    look_v = R @ look_w
                    rel = rel_word(look_v[0], look_v[2])
                    whole = length >= SECTORS * 0.6
                    if b == 0:
                        txt = f"The walls {rel} are not seen yet. Turn that way slowly and film them."
                    elif b == 1:
                        txt = "The ceiling is not seen yet. Tilt up and sweep it." if whole else f"The ceiling {rel} is not seen yet. Tilt up and film it."
                    else:
                        txt = "The floor is not seen yet. Tilt down and sweep it." if whole else f"The floor {rel} is not seen yet. Tilt down and film it."
                    mk("gap" if b == 0 else ("ceiling" if b == 1 else "floor"), txt, here + look_v * 2.0, here, 3 if b == 0 else 2)
        if self.kind != "sweep":
            # holes inside the room outline
            if holes.any():
                lab, k = ndi.label(ndi.binary_opening(holes, iterations=1) if holes.sum() > 40 else holes)
                for j in range(1, k + 1):
                    m = lab == j
                    if m.sum() < 4:
                        continue
                    cz, cx = ndi.center_of_mass(m)
                    c = xz_of(cx, cz)
                    d = c - you_xz
                    dist = float(np.hypot(*d))
                    stand_xz = c - d / max(dist, 1e-6) * min(1.5, max(dist - 0.3, 0)) if dist > 0.3 else c
                    mk("gap", f"There is a gap in the scan {rel_word(*(c - stand0[[0, 2]]))}. Walk toward it and film it for a few seconds.",
                       [c[0], cam_y, c[1]], [stand_xz[0], cam_y, stand_xz[1]], 3 if m.sum() >= 15 else 2)
            # wall cells seen from too far away or too few times
            weak = (wall_n > 0) & ((wall_far / np.maximum(wall_n, 1) > 0.5) | (wall_few / np.maximum(wall_n, 1) > 0.5))
            if weak.any():
                lab, k = ndi.label(ndi.binary_dilation(weak, iterations=2))
                for j in range(1, k + 1):
                    m = (lab == j) & weak
                    if m.sum() < 6:
                        continue
                    cz, cx = ndi.center_of_mass(m)
                    c = xz_of(cx, cz)
                    far = float(wall_far[m].sum() / max(wall_n[m].sum(), 1)) > 0.5
                    inward = stand0[[0, 2]] - c
                    inward = inward / max(np.hypot(*inward), 1e-6)
                    st = c + inward * 1.5
                    word = rel_word(*(c - stand0[[0, 2]]))
                    mk("far" if far else "thin",
                       (f"The wall {word} is only seen from far away. Walk closer (about 1.5 m) and film it for 3 seconds."
                        if far else f"The wall {word} was seen from only one or two angles. Film it again from a different spot."),
                       [c[0], cam_y, c[1]], [st[0], cam_y, st[1]], 2)
            seen_cols = int(seen.sum())
            if ceil_y is not None and seen_cols >= 30 and ceil_seen.sum() / seen_cols < 0.3:
                mk("ceiling", "The ceiling has hardly been seen. Tilt up and film it.", [stand0[0], ceil_y, stand0[2]], [stand0[0], cam_y, stand0[2]], 2)
            if floor_y is not None and seen_cols >= 30 and floor_seen.sum() / seen_cols < 0.3:
                mk("floor", "The floor has hardly been seen. Tilt down and film it.", [stand0[0], floor_y, stand0[2]], [stand0[0], cam_y, stand0[2]], 2)
        if len(sharp) >= 12:
            ref, recent = float(np.median(sharp)), float(np.median(sharp[-6:]))
            if recent < 0.45 * ref:
                tgt = (pose_v if pose_v is not None else stand0) + np.array([0, 0, -1.0])
                mk("blur", "The last pictures are blurry. Move the phone more slowly.", tgt, None, 1)
        needs = self.tracker.update(cands)
        # The list above holds a satisfied need for a few updates so the phone's markers do not flicker; the summary and "done" only
        # count what is still missing right now.
        current = [n_ for n_ in needs if self.tracker.active[n_["id"]]["absent"] == 0]

        done = pct >= 90 and not any(n_["priority"] >= 2 for n_ in current)
        if done:
            summary = f"{int(pct)}% covered. Everything looks complete: tap Stop."
        elif current:
            summary = f"{int(pct)}% covered. Next: {current[0]['text']}"
        else:
            summary = f"{int(pct)}% covered. Keep going."
        return {"percent": int(round(pct)), "summary": summary,
                "grid": {"x0": x0, "z0": z0, "cell": MAP_CELL, "w": w, "h": h, "data": base64.b64encode(grid.tobytes()).decode()},
                "you": None if pose_v is None else [round(float(v), 2) for v in pose_v], "needs": needs}

    def next_message(self, to_view: np.ndarray | None = None, force: bool = False) -> dict | None:
        """A `map` message if something changed since the last one, else None."""
        if not force and not self.dirty:
            return None
        with self.map_lock:
            with self.lock:
                self.dirty = False
            body = self.build_map(to_view)
            if body is None:
                return None
            sig = hashlib.md5(repr((body["percent"], body["grid"]["data"], [n["id"] for n in body["needs"]], [n["text"] for n in body["needs"]],
                                    None if body["you"] is None else [round(v, 1) for v in body["you"]])).encode()).hexdigest()
            if sig == self.last_hash and not force:
                return None
            self.last_hash = sig
            self.seq += 1
            return {"type": "map", "seq": self.seq, **body}
