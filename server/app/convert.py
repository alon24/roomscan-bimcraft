"""RoomPlan CapturedRoom JSON -> BIMCraft script."""
import json
import os
import statistics
import subprocess
from pathlib import Path

from shapely.geometry import MultiPoint, Polygon

from . import geometry as g

MIN_WALL_MM = 150.0
SNAP_MM = 100.0
SQUARE_DEG = float(os.environ.get("SQUARE_CORNERS_DEG", "0"))  # 0 = off


def _cmd(name: str, args: dict) -> str:
    return f"{name} {json.dumps(args, separators=(',', ':'))}"


def _r(p: g.Point) -> list[int]:
    return [round(p[0]), round(p[1])]


def _mm(m: float) -> int:
    """Metres to mm, rounded to 10 mm so near-identical sizes share one type."""
    return round(m * 100) * 10


def _box_family(name: str) -> str:
    """A furniture family that is a plain box, its size set per component."""
    w, d = "Width/2", "Depth/2"
    return _cmd("family", {
        "name": name, "category": "furniture", "types": {"Scanned": {}},
        "parameters": [{"name": n, "kind": "length", "instance": True, "value": 500} for n in ("Width", "Depth", "Height")],
        "forms": [{"plane": "xy", "profile": [[f"-{w}", f"-{d}"], [w, f"-{d}"], [w, d], [f"-{w}", d]], "from": 0, "to": "Height"}],
    })


def furniture(scan: dict, level: float, log: list[str]) -> list[str]:
    """RoomPlan objects (bed, table, sofa…) as boxes of their scanned size, one family per category."""
    lines, families = [], set()
    for o in scan.get("objects") or []:
        name = "Scanned " + g.category(o).replace("_", " ").title()
        if name not in families:
            families.add(name)
            lines.append(_box_family(name))
        wx, hy, dz = o["dimensions"]
        lines.append(_cmd("component", {
            "family": name, "at": _r(g.centre_plan(o)), "angle": round(g.plan_angle(o), 1),
            "offset": max(0, round((g.centre(o)[1] - hy / 2 - level) * 1000)),
            "params": {"Width": _mm(wx), "Depth": _mm(dz), "Height": _mm(hy)},
        }))
    if families:
        log.append(f"{len(lines) - len(families)} furniture objects")
    return lines


def convert(scan: dict, snap_mm: float = SNAP_MM, square_deg: float | None = None) -> tuple[str, list[str]]:
    """Return (script text, log lines)."""
    log: list[str] = []
    walls = scan.get("walls") or []
    if not walls and scan.get("objects"):
        log.append("no walls found; furniture only")
        return "\n".join(furniture(scan, g.floor_level(scan), log)) + "\n", log
    if not walls:
        raise ValueError("No walls were found in this scan. RoomPlan needs to see the floor-to-ceiling edges and the corners: stand back, scan slowly, light the room, and avoid close-ups of one plain wall. Delete this one and scan again.")

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
    pairs = [(w, s) for (w, _, _), s in zip(kept, snapped) if s[0] != s[1]]   # BIMCraft refuses a 0 mm wall
    if len(pairs) < len(kept):
        log.append(f"dropped {len(kept) - len(pairs)} wall(s) whose ends snapped together")
    if not pairs:
        raise ValueError("all walls shorter than minimum")
    snapped = [s for _, s in pairs]
    height_mm = round(statistics.median(w["dimensions"][1] for w, _ in pairs) * 1000)
    lines = []

    loop = g.chain_loop(snapped)
    if loop and Polygon(loop).exterior.is_ccw:
        loop.reverse()   # BIMCraft puts a wall's exterior on its left, so walls drawn clockwise face out
    sq = SQUARE_DEG if square_deg is None else square_deg
    if loop and sq > 0:
        squared = g.square_loop(loop, sq)
        moved = dict(zip(loop, squared))   # doors and windows go on the squared walls, not where the walls were
        snapped = [(moved[a], moved[b]) for a, b in snapped]
        loop = squared
        log.append(f"squared corners (tolerance {sq}°)")
    if loop:
        lines.append(_cmd("wall", {"points": [_r(p) for p in loop], "closed": True, "height": height_mm}))
        log.append(f"closed loop of {len(loop)} walls")
    else:
        log.append("walls do not form a single closed loop; emitting one wall per segment")
        for a, b in snapped:
            lines.append(_cmd("wall", {"points": [_r(a), _r(b)], "height": height_mm}))

    by_id = {w.get("identifier"): s for (w, _), s in zip(pairs, snapped)}
    level = g.floor_level(scan)

    types = set()
    for kind, key in (("door", "doors"), ("window", "windows")):
        for s in scan.get(key) or []:
            c = g.centre_plan(s)
            host = by_id.get(s.get("parentIdentifier"))
            cands = [host] if host else snapped
            at = min((g.project_on_segment(c, a, b) for a, b in cands), key=lambda q: g.dist(q, c))
            w, h = _mm(s["dimensions"][0]), _mm(s["dimensions"][1])
            tname = f"{kind.title()} {w}x{h}"
            if tname not in types:
                types.add(tname)
                lines.append(_cmd("type", {"category": kind, "name": tname, "width": w, "height": h}))
            args = {"at": _r(at), "type": tname}
            if kind == "window":
                sill = round((g.centre(s)[1] - s["dimensions"][1] / 2 - level) * 1000)
                if sill > 0:
                    args["sill"] = sill
            lines.append(_cmd(kind, args))

    if loop:
        lines.append(_cmd("room", {"point": _r(Polygon(loop).representative_point().coords[0]), "name": "Room"}))
        lines.append("floor {}")
    else:   # BIMCraft finds no room inside open walls, and a floor needs an outline: the hull round the walls
        hull = MultiPoint([p for seg in snapped for p in seg]).convex_hull
        if hull.geom_type == "Polygon":
            lines.append(_cmd("floor", {"boundary": [_r(p) for p in hull.exterior.coords[:-1]]}))
        log.append("no room: the walls do not enclose one")
    lines += furniture(scan, level, log)
    return "\n".join(lines) + "\n", log


def run_cli(script: Path, outdir: Path, timeout: int = 60) -> tuple[bool, str]:
    """Run bimcraft-cli; return (ok, combined stdout/stderr)."""
    cli = os.environ.get("BIMCRAFT_CLI", "bimcraft-cli")
    cmd = [cli, "run", "--script", str(script)]
    for name in ("room.bimcraft", "room.ifc", "plan.png"):
        cmd += ["--save", str(outdir / name)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"{type(e).__name__}: {e}"
    return p.returncode == 0, p.stdout + p.stderr
