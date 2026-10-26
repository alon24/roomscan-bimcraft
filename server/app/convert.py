"""RoomPlan CapturedRoom JSON -> BIMCraft script."""
import json
import os
import statistics
import subprocess
from pathlib import Path

from . import geometry as g

MIN_WALL_MM = 150.0
SNAP_MM = 100.0


def _cmd(name: str, args: dict) -> str:
    return f"{name} {json.dumps(args, separators=(',', ':'))}"


def _r(p: g.Point) -> list[int]:
    return [round(p[0]), round(p[1])]


def convert(scan: dict, snap_mm: float = SNAP_MM) -> tuple[str, list[str]]:
    """Return (script text, log lines)."""
    log: list[str] = []
    walls = scan.get("walls") or []
    if not walls:
        raise ValueError("scan has no walls")

    raw = [(w, *g.wall_endpoints(w)) for w in walls]
    kept = []
    for w, a, b in raw:
        if g.dist(a, b) < MIN_WALL_MM:
            log.append(f"dropped short wall {w.get('identifier')} ({g.dist(a, b):.0f} mm)")
        else:
            kept.append((w, a, b))
    if not kept:
        raise ValueError("all walls shorter than minimum")

    snapped = g.snap_endpoints([(a, b) for _, a, b in kept], snap_mm)
    height_mm = round(statistics.median(w["dimensions"][1] for w, _, _ in kept) * 1000)
    lines = []

    loop = g.chain_loop(snapped)
    if loop:
        lines.append(_cmd("wall", {"points": [_r(p) for p in loop], "closed": True, "height": height_mm}))
        log.append(f"closed loop of {len(loop)} walls")
    else:
        log.append("walls do not form a single closed loop; emitting one wall per segment")
        for a, b in snapped:
            lines.append(_cmd("wall", {"points": [_r(a), _r(b)], "height": height_mm}))

    by_id = {w["identifier"]: (a, b) for (w, _, _), (a, b) in zip(kept, snapped)}
    segs = list(by_id.values())
    level = g.floor_level(scan)

    for kind, key in (("door", "doors"), ("window", "windows")):
        for s in scan.get(key) or []:
            c = g.centre_plan(s)
            host = by_id.get(s.get("parentIdentifier"))
            cands = [host] if host else segs
            at = min((g.project_on_segment(c, a, b) for a, b in cands), key=lambda q: g.dist(q, c))
            args = {"at": _r(at)}
            if kind == "window":
                sill = round((g.centre(s)[1] - s["dimensions"][1] / 2 - level) * 1000)
                if sill > 0:
                    args["sill"] = sill
            lines.append(_cmd(kind, args))

    pts = loop or [p for seg in snapped for p in seg]
    mid = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    lines.append(_cmd("room", {"point": _r(mid), "name": "Room"}))
    lines.append("floor {}")
    return "\n".join(lines) + "\n", log


def run_cli(script: Path, outdir: Path, timeout: int = 60) -> tuple[bool, str]:
    """Run bimcraft-cli; return (ok, combined stdout/stderr)."""
    cli = os.environ.get("BIMCRAFT_CLI", "bimcraft-cli")
    cmd = [cli, "run", "--script", str(script)]
    for name in ("room.bimcraft", "room.ifc", "plan.png"):
        cmd += ["--save", str(outdir / name)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"{type(e).__name__}: {e}"
    return p.returncode == 0, p.stdout + p.stderr
