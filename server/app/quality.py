"""How good a ring scan is, and what to add: from the camera's place around the ring in every frame (the same fit and limits
the build uses), the angles covered, the speed the tray turned at, the distance, and a score with tips."""
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

from . import frames, ring

FAST, IDEAL, SLOW = 30.0, 20.0, 5.0          # degrees a second: the same limits as the phone's live check
NEAR, FAR = 180.0, 420.0                      # camera distance from the ring centre, mm
MAX_ERR, MIN_MARKERS = 3.0, 4                 # a frame is used only with a fit like this (as in ring.fuse)
BINS = 36                                     # 10 degree sectors round the part
BANDS = (("low", 0, 32), ("middle", 32, 50), ("high", 50, 91))   # the same edges as the phone's dial (GuidedScan.lowMax / midMax)


def _poses(d: Path) -> list[dict]:
    cfg = ring.config(d)
    det = ring.detector()
    rows = []
    for ph in frames.manifest(d):
        path = d / "photos" / ph["file"]
        if not path.exists():
            continue
        row = {"file": ph["file"], "t": float(ph.get("timestamp", 0.0)), "ok": False}
        pose = ring.ring_pose(np.asarray(Image.open(path).convert("L")), ph, cfg, det)
        if pose is not None:
            R, t, n, err = pose
            if err <= MAX_ERR and n >= MIN_MARKERS:
                c = -R.T @ t                                    # camera centre in the ring frame, mm
                dist = float(np.linalg.norm(c))
                row.update(ok=True, az=math.degrees(math.atan2(c[1], c[0])) % 360, elev=math.degrees(math.asin(max(-1, min(1, c[2] / dist)))),
                           dist=dist, err=float(err), markers=int(n))
        rows.append(row)
    return rows


def _gaps(hit: np.ndarray, min_bins: int = 2) -> list[list[int]]:
    """Runs of empty sectors (circular) as [from_deg, to_deg], at least min_bins wide."""
    if hit.all():
        return []
    if not hit.any():
        return [[0, 360]]
    start = int(np.argmax(hit))                                 # begin at a covered sector so a run never wraps
    order = [(start + i) % BINS for i in range(BINS)]
    out, run = [], []
    for b in order + [None]:
        if b is not None and not hit[b]:
            run.append(b)
            continue
        if len(run) >= min_bins:
            out.append([run[0] * 360 // BINS, (run[-1] + 1) * 360 // BINS])
        run = []
    return out


def analyse(d: Path) -> dict:
    rows = _poses(d)
    ok = [r for r in rows if r["ok"]]
    out: dict = {"frames": len(rows), "usable": len(ok)}
    if len(ok) < 3:
        out.update(score=0, grade="poor", tips=[f"The ring was read well in only {len(ok)} of {len(rows)} frames. Keep 4 or more markers in "
                                                   "view, in focus and without glare."], az_hits=[0] * BINS, gaps=[[0, 360]], points=[])
        return out
    az = np.array([r["az"] for r in ok]); el = np.array([r["elev"] for r in ok]); dist = np.array([r["dist"] for r in ok])
    hits = np.bincount((az // (360 / BINS)).astype(int) % BINS, minlength=BINS)
    covered = hits > 0
    gaps = _gaps(covered)
    # speed: the turn between neighbouring usable frames (bad fits can flip the angle, so jumps over 150 degrees a second are ignored)
    rates = []
    for a, b in zip(ok, ok[1:]):
        dt = b["t"] - a["t"]
        if 0.05 < dt <= 1.5:
            da = abs((b["az"] - a["az"] + 180) % 360 - 180)
            if da / dt < 150:
                rates.append(da / dt)
    rates = np.array(rates)
    bands = {name: int(((el >= lo) & (el < hi)).sum()) for name, lo, hi in BANDS}
    near_far = float(((dist < NEAR) | (dist > FAR)).mean())
    out.update(
        az_hits=hits.tolist(), gaps=gaps, elevation={"min": round(float(el.min()), 1), "max": round(float(el.max()), 1), "median": round(float(np.median(el)), 1)},
        bands=bands, distance_mm={"min": round(float(dist.min())), "median": round(float(np.median(dist))), "max": round(float(dist.max()))},
        mean_markers=round(float(np.mean([r["markers"] for r in ok])), 1), mean_error_px=round(float(np.mean([r["err"] for r in ok])), 2),
        points=[[round(r["az"], 1), round(r["elev"], 1)] for r in ok])
    fast = slow = 0.0
    if len(rates):
        fast, slow = round(float((rates > FAST).mean()), 3), round(float((rates < SLOW).mean()), 3)
        out["speed"] = {"median": round(float(np.median(rates)), 1), "p90": round(float(np.percentile(rates, 90)), 1),
                        "too_fast": fast, "too_slow": slow}
    tips = []
    # score: 30 frames read, 30 angles covered, 20 heights, 20 steady speed
    s_frames = 30 * len(ok) / max(len(rows), 1)
    s_az = 30 * covered.mean() * (0.5 if gaps and max(g[1] - g[0] for g in gaps) >= 60 else 1.0)
    used_bands = sum(1 for n in bands.values() if n >= 3)
    s_el = {0: 0, 1: 8, 2: 15, 3: 20}[used_bands]
    s_speed = 20 * (1 - min(1.0, fast * 1.5)) * (1 - min(1.0, slow * 0.5))
    score = round(s_frames + s_az + s_el + s_speed - 10 * near_far)
    if len(ok) < 0.9 * len(rows):
        tips.append(f"{len(rows) - len(ok)} of {len(rows)} frames could not be used (ring not read well). Keep 4 or more markers in view and in focus.")
    for g in gaps:
        tips.append(f"No frames between {g[0]}° and {g[1]}° round the ring: turn the tray slowly through that part, or scan again.")
    for name, lo, hi in BANDS:
        if bands[name] < 3:
            if name == "low":
                tips.append("Add a low pass: lower the phone until the angle on screen reads under 32° and turn the tray once more. Sides and overhangs are thin without it.")
            elif name == "high":
                tips.append("Add a pass from above (over 50°). The top of the part has few views.")
            else:
                tips.append("There are few frames at a middle angle (32° to 50°), the best one for the whole shape.")
    if fast > 0.1:
        tips.append(f"{round(fast * 100)}% of the time the tray turned faster than {FAST:.0f}°/s: slower gives sharper photos.")
    if slow > 0.4:
        tips.append(f"The tray stood still or crept for {round(slow * 100)}% of the time. A steady turn of about 15°/s is enough.")
    if near_far > 0.2:
        tips.append(f"The phone was closer than {NEAR / 10:.0f} cm or farther than {FAR / 10:.0f} cm in {round(near_far * 100)}% of the frames.")
    score = max(0, min(100, score))
    out["score"] = score
    out["grade"] = "good" if score >= 80 else ("fair" if score >= 55 else "poor")
    out["tips"] = tips or ["Nothing to add: the whole ring was covered at several heights at a steady speed."]
    return out


VERSION = 1                                   # bump when analyse() changes, so old reports are made again


def _key(d: Path) -> dict:
    """Everything analyse() reads: the frame list, the photos (added, removed or replaced by a re-convert) and the ring
    dimensions (a new calibration changes every pose)."""
    def stamp(p: Path):
        try:
            st = p.stat()
            return [st.st_mtime_ns, st.st_size]
        except OSError:
            return None
    cfg = json.dumps(ring.config(d), sort_keys=True)
    return {"v": VERSION, "frames": len(frames.manifest(d)), "photos_json": stamp(d / "photos.json"),
            "photos_dir": stamp(d / "photos"), "ring": hashlib.sha1(cfg.encode()).hexdigest()}


def cached(d: Path) -> dict:
    p = d / "quality.json"
    key = _key(d)
    if p.exists():
        try:
            c = json.loads(p.read_text())
        except ValueError:
            c = {}
        if c.get("cache_key") == key:
            return c
    c = analyse(d)
    c["cache_key"] = key
    p.write_text(json.dumps(c))
    return c
