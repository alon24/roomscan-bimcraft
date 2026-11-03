"""The room as things you can confirm: walls, doors, windows and open stretches, each resting on the points that show it.

Evidence (photos, LiDAR) -> points (clicks) -> elements (this file) -> outputs (layout.json, BIMCraft, Blender).
The rule: a fix is stored as a fix, and rebuilding can never undo it. A confirmed wall is never moved by a rebuild, and the plan's corners are worked
out from the walls, so dragging a corner moves walls rather than something a rebuild would overwrite. Every change is an action in a log, so it can be
undone in a later session and the page, a phone or an AI can all make the same changes.

Model coordinates are plan x, y before the drawing turn: x = world x, y = -world z. model.json holds the model, layout.json stays what everything else reads."""
import json
import math
import time
import uuid
from pathlib import Path

import numpy as np

from . import layout as layout_mod
from . import walk as walk_mod

WALL_KINDS = ("corner", "floor edge", "ceiling edge", "wall")
ASSIGN_NEAR = 0.25       # metres: a point this close to a wall rests on it
NEAR_LOOK = 1.0          # points this close are offered for a wall
PARALLEL = math.sin(math.radians(10))       # walls closer than this in angle don't meet in a corner
STEP_MIN = 0.05          # parallel neighbours further apart than this need a short wall joining them
MIXED_OFF = 0.12         # a wall's points further than this from its line may be two surfaces
SPREAD_MIN = 0.3         # points closer together than this can't give a wall's direction
TINY = 0.15
LOG_KEEP = 300
FAR_CORNER = 6.0         # a crossing further than this from where the corner was is no corner


def _path(d: Path) -> Path:
    return d / "model.json"


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _points(d: Path) -> list[dict]:
    f = d / "walk_result.json"
    return json.loads(f.read_text()).get("points", []) if f.exists() else []


def _xy(p: dict) -> list[float]:
    return [float(p["x"]), -float(p["z"])]


def _unit(a, b) -> np.ndarray:
    v = np.subtract(b, a, dtype=float)
    return v / (np.linalg.norm(v) or 1e-9)


def _proj(q, line) -> list[float]:
    p, u = np.asarray(line[0], float), _unit(*line)
    return (p + u * float((np.asarray(q, float) - p) @ u)).tolist()


def _off(q, line) -> float:
    p, u = np.asarray(line[0], float), _unit(*line)
    r = np.asarray(q, float) - p
    return abs(float(r[0] * u[1] - r[1] * u[0]))


def _on_segment(q, a, b) -> tuple[float, float]:
    """Distance to the stretch a to b, and how far along it."""
    p, u = np.asarray(a, float), _unit(a, b)
    L = float(np.linalg.norm(np.subtract(b, a, dtype=float)))
    t = max(0.0, min(L, float((np.asarray(q, float) - p) @ u)))
    return float(np.linalg.norm(np.asarray(q, float) - (p + u * t))), t


def _corners(walls: list[dict], off: np.ndarray | None = None) -> list[list[float]]:
    """Corner i joins wall i-1 to wall i: where their lines cross, or half way between their ends when they run side by side."""
    n = len(walls)
    off = np.zeros(n) if off is None else off
    lines = []
    for w, o in zip(walls, off):
        u = _unit(*w["line"])
        shift = np.array([-u[1], u[0]]) * o
        lines.append((np.asarray(w["line"][0], float) + shift, u))
    out = []
    for i in range(n):
        (p, u), (q, v) = lines[i - 1], lines[i]
        was = (np.asarray(walls[i - 1]["b"], float) + np.asarray(walls[i]["a"], float)) / 2
        cross = u[0] * v[1] - u[1] * v[0]
        c = None
        if abs(cross) > PARALLEL:
            t = ((q[0] - p[0]) * v[1] - (q[1] - p[1]) * v[0]) / cross
            c = p + u * t
            if np.linalg.norm(c - was) > FAR_CORNER:
                c = None
        if c is None:
            end = p + u * float((np.asarray(walls[i - 1]["b"], float) - p) @ u)
            start = q + v * float((np.asarray(walls[i]["a"], float) - q) @ v)
            c = (end + start) / 2
        out.append([float(c[0]), float(c[1])])
    return out


def _solve(walls: list[dict]) -> tuple[np.ndarray, float]:
    """Tape lengths: each wall may slide across its own direction, as little as it can, confirmed walls least, so typed lengths come true. Returns the slides and
    the biggest length it couldn't reach."""
    n = len(walls)
    locked = [i for i, w in enumerate(walls) if w.get("length_m")]
    off = np.zeros(n)
    if not locked or n < 3:
        return off, 0.0
    def lengths(o: np.ndarray) -> np.ndarray:
        c = _corners(walls, o)
        return np.array([float((np.subtract(c[(i + 1) % n], c[i])) @ _unit(*walls[i]["line"])) for i in locked])
    want = np.array([float(walls[i]["length_m"]) for i in locked])
    give = np.array([0.02 if w.get("confirmed") else 0.2 if w.get("pinned") else 1.0 for w in walls])     # how readily it moves
    for _ in range(3):
        now = lengths(off)
        J = np.zeros((len(locked), n))
        for k in range(n):
            nudge = off.copy()
            nudge[k] += 0.05
            J[:, k] = (lengths(nudge) - now) / 0.05
        A = np.vstack([J * 100, np.diag(1 / np.sqrt(give))])
        b = np.concatenate([(want - now) * 100, -off / np.sqrt(give)])
        off = off + np.linalg.lstsq(A, b, rcond=None)[0]
    return off, float(np.abs(lengths(off) - want).max())


def _fit(w: dict, by_id: dict) -> None:
    """A wall nobody has fixed follows its points."""
    if w.get("confirmed") or w.get("pinned"):
        return
    pts = [_xy(by_id[i]) for i in w["points"] if i in by_id]
    if len(pts) < 2:
        return
    P = np.array(pts)
    c = P.mean(axis=0)
    if np.linalg.norm(P - c, axis=1).max() * 2 < SPREAD_MIN:
        return
    u = np.linalg.svd(P - c)[2][0]
    if u @ _unit(*w["line"]) < 0:
        u = -u
    w["line"] = [c.tolist(), (c + u).tolist()]


def _rest_points(walls: list[dict], points: list[dict]) -> None:
    for p in points:
        if p.get("kind") not in WALL_KINDS or any(p["id"] in w["points"] for w in walls):
            continue
        q = _xy(p)
        best = min(walls, key=lambda w: _on_segment(q, w["a"], w["b"])[0], default=None)
        if best is not None and _on_segment(q, best["a"], best["b"])[0] <= ASSIGN_NEAR:
            best["points"].append(p["id"])


def from_layout(lay: dict, points: list[dict]) -> dict:
    """A model for a plan that has none. A plan someone has corrected keeps its walls where they are."""
    turn = float(lay.get("turn_deg") or 0)
    v = [layout_mod._rotate(p, -turn) for p in lay["vertices"]]
    flags = lay.get("open") or [False] * len(v)
    walls = []
    for i, (a, b) in enumerate(zip(v, v[1:] + v[:1])):
        walls.append({"id": _new_id(), "a": a, "b": b, "line": [a, b], "open": bool(flags[i]) if i < len(flags) else False, "confirmed": False, "pinned": bool(lay.get("edited")), "points": [], "length_m": None})
    _rest_points(walls, points)
    return {"version": 1, "walls": walls}


def load(d: Path) -> dict:
    """The model, with anything changed behind its back, such as joining a scan, taken in."""
    lay = layout_mod.load(d)
    f = _path(d)
    if not f.exists():
        return from_layout(lay, _points(d)) if lay["vertices"] else {"version": 1, "walls": []}
    m = json.loads(f.read_text())
    if not _same(m, lay):
        _take_plan(m, lay)
    return m


def _plan(m: dict, lay: dict) -> list[list[float]]:
    turn = float(lay.get("turn_deg") or 0)
    off, _ = _solve(m["walls"])
    return [layout_mod._rotate(c, turn) for c in _corners(m["walls"], off)]


def _same(m: dict, lay: dict) -> bool:
    v = lay["vertices"]
    if len(v) != len(m["walls"]):
        return False
    if not v:
        return True
    return all(math.dist(a, b) < 0.012 for a, b in zip(v, _plan(m, lay))) and [bool(x) for x in lay.get("open", [])] == [bool(w["open"]) for w in m["walls"]]


def _take_plan(m: dict, lay: dict, points: list[dict] | None = None) -> None:
    """The plan was changed directly, by dragging a corner or tidying: each stretch finds the wall it was and that wall moves, keeping what is known about it."""
    turn = float(lay.get("turn_deg") or 0)
    v = [layout_mod._rotate(p, -turn) for p in lay["vertices"]]
    flags = lay.get("open") or [False] * len(v)
    old = list(m["walls"])
    was = _corners(old, _solve(old)[0]) if old else []
    n_old = len(old)
    ends = {id(w): (was[i], was[(i + 1) % n_old]) for i, w in enumerate(old)}
    walls, used = [], set()
    for i, (a, b) in enumerate(zip(v, v[1:] + v[:1])):
        best, cost = None, 1.0
        if math.dist(a, b) > 1e-6:
            u, mid = _unit(a, b), [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
            for w in old:
                if id(w) in used:
                    continue
                x, y = ends[id(w)]
                if math.dist(x, y) < 1e-6 or abs(float(u @ _unit(x, y))) < math.cos(math.radians(25)):
                    continue
                c = _on_segment(mid, x, y)[0]
                if c < cost:
                    best, cost = w, c
        if best is None:
            w = {"id": _new_id(), "a": a, "b": b, "line": [a, b], "open": False, "confirmed": False, "pinned": True, "points": [], "length_m": None}
        else:
            used.add(id(best))
            w = best
            x, y = ends[id(best)]
            moved = math.dist(a, x) > 0.012 or math.dist(b, y) > 0.012
            if moved:
                w.update(line=[a, b], pinned=True)
                if w.get("length_m") and abs(math.dist(a, b) - w["length_m"]) > 0.012:
                    w["length_m"] = None       # dragged to another length: the tape no longer says this
            w.update(a=a, b=b)
        w["open"] = bool(flags[i]) if i < len(flags) else False
        walls.append(w)
    m["walls"] = walls
    _rest_points(m["walls"], points or [])


def _write(d: Path, m: dict, lay: dict) -> dict:
    """The outputs follow from the walls."""
    walls = m["walls"]
    if len(walls) >= 3:
        base = _corners(walls)
        for i, w in enumerate(walls):
            w["a"], w["b"] = base[i], base[(i + 1) % len(walls)]
        turn = float(lay.get("turn_deg") or 0)
        off, m["tape_off"] = _solve(walls)
        lay["vertices"] = [layout_mod._rotate(c, turn) for c in _corners(walls, off)]
        lay["open"] = [bool(w["open"]) for w in walls]
    if any(w.get("confirmed") or w.get("pinned") for w in walls) or any(o.get("marked") for o in lay.get("openings", [])):
        lay["edited"] = True
    _path(d).write_text(json.dumps(m))
    (d / "layout.json").write_text(json.dumps(lay))
    return lay


# ---- the log of actions

def _log(d: Path) -> Path:
    return d / "edits.jsonl"


def snapshot(d: Path, points: bool = False) -> dict:
    out = {}
    for name, key in (("model.json", "model"), ("layout.json", "layout")) + ((("walk_points.json", "walk_points"), ("walk_result.json", "walk_result")) if points else ()):
        f = d / name
        out[key] = json.loads(f.read_text()) if f.exists() else None
    return out


def record(d: Path, op: str, args: dict, before: dict) -> None:
    """For undo, and a record of every fix kept for good in edits_history.jsonl."""
    with (d / "edits_history.jsonl").open("a") as h:
        h.write(json.dumps({"t": time.time(), "op": op, "args": args}) + "\n")
    f = _log(d)
    lines = f.read_text().splitlines() if f.exists() else []
    lines.append(json.dumps({"t": time.time(), "op": op, "before": before}))
    f.write_text("\n".join(lines[-LOG_KEEP:]) + "\n")


def undo_count(d: Path) -> int:
    f = _log(d)
    return len(f.read_text().splitlines()) if f.exists() else 0


def undo(d: Path) -> dict:
    f = _log(d)
    lines = f.read_text().splitlines() if f.exists() else []
    if not lines:
        raise ValueError("there is nothing to undo")
    last = json.loads(lines[-1])
    for key, name in (("model", "model.json"), ("layout", "layout.json"), ("walk_points", "walk_points.json"), ("walk_result", "walk_result.json")):
        if key not in last["before"]:
            continue
        target = d / name
        if last["before"][key] is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(json.dumps(last["before"][key]))
    f.write_text("\n".join(lines[:-1]) + ("\n" if len(lines) > 1 else ""))
    with (d / "edits_history.jsonl").open("a") as h:
        h.write(json.dumps({"t": time.time(), "op": "undo", "args": {"undid": last["op"]}}) + "\n")
    return view(d)


# ---- what the page sees

def _problems(m: dict, lay: dict, points: list[dict]) -> list[dict]:
    out = []
    v, walls = lay["vertices"], m["walls"]
    n = len(v)
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            a, b, c, e = v[i], v[(i + 1) % n], v[j], v[(j + 1) % n]
            if side(a, b, c) * side(a, b, e) < 0 and side(c, e, a) * side(c, e, b) < 0:
                out.append({"level": "block", "text": f"Walls {i + 1} and {j + 1} cross.", "wall": i})
    for i in range(n):
        if math.dist(v[i], v[(i + 1) % n]) < TINY:
            out.append({"level": "warn", "text": f"Wall {i + 1} is very short: it may not be a real wall.", "wall": i})
    if m.get("tape_off", 0) > 0.01:
        out.append({"level": "warn", "text": f"Your tape lengths don't all fit together: up to {m['tape_off'] * 100:.0f} cm out. Check them."})
    for i, w in enumerate(walls):
        if not w.get("confirmed"):
            continue
        by = {p["id"]: p for p in points}
        far = [p for p in (by.get(x) for x in w["points"]) if p and _off(_xy(p), w["line"]) > MIXED_OFF]
        if far:
            out.append({"level": "warn", "text": f"Wall {i + 1} is confirmed, but {len(far)} of its points are away from it.", "wall": i})
    return out


def view(d: Path) -> dict:
    lay = layout_mod.load(d)
    m = load(d)
    if not _path(d).exists() and m["walls"]:
        _write(d, m, lay)
        lay = layout_mod.load(d)
    points = _points(d)
    by = {p["id"]: p for p in points}
    placed = {i for w in m["walls"] for i in w["points"]}
    walls = []
    for w in m["walls"]:
        off = [_off(_xy(by[i]), w["line"]) for i in w["points"] if i in by]
        near = []
        for p in points:
            if p["id"] in w["points"] or p.get("kind") not in WALL_KINDS:
                continue
            if _on_segment(_xy(p), w["a"], w["b"])[0] <= NEAR_LOOK:
                near.append(p["id"])
        walls.append({"id": w["id"], "confirmed": bool(w.get("confirmed")), "pinned": bool(w.get("pinned")), "open": bool(w["open"]), "length_m": w.get("length_m"), "points": w["points"], "near": near,
                      "mixed": bool(off) and max(off) > MIXED_OFF})
    loose = [p["id"] for p in points if p.get("kind") in WALL_KINDS and p["id"] not in placed]
    return {"vertices": lay["vertices"], "open": lay.get("open", []), "openings": lay.get("openings", []), "height_m": lay.get("height_m"), "turn_deg": lay.get("turn_deg", 0), "walls": walls,
            "loose": loose, "checks": _problems(m, lay, points), "undo": undo_count(d), "problem": layout_mod.problem(lay)}


# ---- actions

def _wall(m: dict, wid: str) -> dict:
    w = next((x for x in m["walls"] if x["id"] == wid), None)
    if w is None:
        raise ValueError("there is no such wall")
    return w


def _world(q) -> list[float]:
    return [float(q[0]), -float(q[1])]


def _regen_openings(d: Path, lay: dict, m: dict, points: list[dict]) -> None:
    """Doors and windows from clicks follow the clicks. Ones set by hand stay, and win over what clicks make in the same place."""
    f = d / "walk_result.json"
    if not f.exists() or len(m["walls"]) < 3:
        return
    floor_y = json.loads(f.read_text()).get("floor_y")
    if floor_y is None:
        return
    turn = float(lay.get("turn_deg") or 0)
    kept = [o for o in lay.get("openings", []) if o.get("marked")]
    v = [w["a"] for w in m["walls"]]
    made = [{**o, "at": layout_mod._rotate(o["at"], turn)} for o in walk_mod.openings_from(points, v, [bool(w["open"]) for w in m["walls"]], floor_y)]
    clear = lambda g: all(math.dist(g["at"], k["at"]) > (g.get("width", 1) + k.get("width", 1)) / 2 for k in kept)
    lay["openings"] = [g for g in made if clear(g)] + kept


def _step_between(first: dict, second: dict, joint) -> dict | None:
    """Two walls side by side at different depths, such as a recess: a short wall joins them."""
    u, v = _unit(*first["line"]), _unit(*second["line"])
    if abs(u[0] * v[1] - u[1] * v[0]) > PARALLEL or _off(second["line"][0], first["line"]) <= STEP_MIN:
        return None
    a, b = _proj(joint, first["line"]), _proj(joint, second["line"])
    return {"id": _new_id(), "a": a, "b": b, "line": [a, b], "open": False, "confirmed": False, "pinned": True, "points": [], "length_m": None}


def act(d: Path, op: str, args: dict) -> dict:
    before = snapshot(d)
    lay = layout_mod.load(d)
    m = load(d)
    points = _points(d)
    by = {p["id"]: p for p in points}
    walls = m["walls"]
    if op == "confirm_wall":
        w = _wall(m, args["wall"])
        w["confirmed"] = bool(args.get("on", True))
    elif op == "confirm_all_walls":
        for w in walls:
            w["confirmed"] = True
    elif op == "set_open":
        _wall(m, args["wall"])["open"] = bool(args.get("on", True))
    elif op == "not_a_wall":
        if len(walls) <= 3:
            raise ValueError("a room needs at least 3 walls")
        walls.remove(_wall(m, args["wall"]))
    elif op == "split_wall":
        w = _wall(m, args["wall"])
        i = walls.index(w)
        u = _unit(w["a"], w["b"])
        L = math.dist(w["a"], w["b"])
        at = _world(args["at"]) if args.get("at") else [(w["a"][0] + w["b"][0]) / 2, (w["a"][1] + w["b"][1]) / 2]
        t = float((np.asarray(at) - np.asarray(w["a"])) @ u)
        if not 0.1 < t < L - 0.1:
            raise ValueError("that's too close to the end of the wall to split it there")
        mid = (np.asarray(w["a"]) + u * t).tolist()
        other = {"id": _new_id(), "a": mid, "b": w["b"], "line": list(w["line"]), "open": w["open"], "confirmed": False, "pinned": True, "points": [], "length_m": None}
        keep = []
        for pid in w["points"]:
            (keep if pid not in by or float((np.asarray(_xy(by[pid])) - np.asarray(w["a"])) @ u) <= t else other["points"]).append(pid)
        w.update(b=mid, points=keep, confirmed=False, pinned=True, length_m=None)
        walls.insert(i + 1, other)
    elif op == "move_wall":
        w = _wall(m, args["wall"])
        a, b = _world(args["a"]), _world(args["b"])
        if math.dist(a, b) < 0.1:
            raise ValueError("those two spots are too close together")
        if float(np.subtract(b, a) @ np.subtract(w["b"], w["a"])) < 0:
            a, b = b, a
        i = walls.index(w)
        old_a, old_b = list(w["a"]), list(w["b"])
        w.update(line=[a, b], pinned=True, length_m=None, a=_proj(old_a, [a, b]), b=_proj(old_b, [a, b]))
        n = len(walls)
        after = _step_between(w, walls[(i + 1) % n], old_b)
        if after:
            walls.insert(i + 1, after)
        before_step = _step_between(walls[i - 1], w, old_a)
        if before_step:
            walls.insert(i, before_step)
    elif op == "assign_point":
        pid = args["point"]
        if pid not in by:
            raise ValueError("there is no such point")
        for w in walls:
            if pid in w["points"]:
                w["points"].remove(pid)
                _fit(w, by)
        if args.get("wall"):
            w = _wall(m, args["wall"])
            w["points"].append(pid)
            if not w.get("confirmed"):
                w["pinned"] = False         # saying which points make a wall hands it back to them
                _fit(w, by)
    elif op == "set_length":
        w = _wall(m, args["wall"])
        L = args.get("m")
        if L is not None and not 0.05 <= float(L) <= 100:
            raise ValueError("a length has to be between 5 cm and 100 m")
        w["length_m"] = float(L) if L is not None else None
    elif op == "confirm_opening":
        o = lay["openings"][int(args["index"])]
        o["marked"] = bool(args.get("on", True))
    else:
        raise ValueError(f"I don't know the action {op}")
    _write(d, m, lay)
    record(d, op, args, before)
    return view(d)


def put_plan(d: Path, lay: dict) -> dict:
    """The page changed the plan directly, so the walls move to match."""
    before = snapshot(d)
    m = json.loads(_path(d).read_text()) if _path(d).exists() else None
    if m is None:
        old = layout_mod.load(d)
        m = from_layout(old, _points(d)) if old["vertices"] else {"version": 1, "walls": []}
    _take_plan(m, lay, _points(d))
    _write(d, m, lay)
    if before.get("layout") != lay:
        record(d, "plan", {}, before)
    return view(d)


def point_changed(d: Path, op: str, args: dict, before: dict, pid: str | None = None) -> None:
    """A point was added, moved, retyped or deleted: walls nobody has fixed follow, and doors and windows from clicks too."""
    lay = layout_mod.load(d)
    if not lay["vertices"]:
        record(d, op, args, before)
        return
    m = load(d)
    points = _points(d)
    by = {p["id"]: p for p in points}
    p = by.get(pid)
    for w in m["walls"]:
        if pid in w["points"]:
            if p is None or p.get("kind") not in WALL_KINDS:
                w["points"].remove(pid)
            _fit(w, by)
    if p is not None and p.get("kind") in WALL_KINDS:
        _rest_points(m["walls"], [p])
        for w in m["walls"]:
            if pid in w["points"]:
                _fit(w, by)
    _regen_openings(d, lay, m, points)
    _write(d, m, lay)
    record(d, op, args, before)


def locked(d: Path) -> bool:
    """Has anyone fixed anything a rebuild could undo?"""
    if not _path(d).exists():
        f = d / "layout.json"
        return bool(f.exists() and json.loads(f.read_text()).get("edited"))
    m = json.loads(_path(d).read_text())
    return any(w.get("confirmed") or w.get("pinned") for w in m["walls"])


def propose(d: Path, clicks: list[dict]) -> dict:
    """Make the walls again from the points, on request. Confirmed walls can't be undone: with any, the others just follow their points. Doors and windows set by hand stay."""
    before = snapshot(d, points=True)
    old_lay = layout_mod.load(d)
    old = load(d)
    kept_open = [o for o in old_lay.get("openings", []) if o.get("marked")]
    confirmed = sum(1 for w in old["walls"] if w.get("confirmed"))
    if confirmed:
        points = _points(d)
        by = {p["id"]: p for p in points}
        for w in old["walls"]:
            w["pinned"] = bool(w.get("confirmed"))
            _fit(w, by)
        _regen_openings(d, old_lay, old, points)
        _write(d, old, old_lay)
        record(d, "propose", {"kept_confirmed": confirmed}, before)
        return {"built": True, "kept_confirmed": confirmed, "outline": len(old["walls"])}
    out = walk_mod.build(d, clicks)
    if not out.get("built"):
        return out
    lay = layout_mod.load(d)
    if kept_open:
        back, turn = -float(old_lay.get("turn_deg") or 0), float(lay.get("turn_deg") or 0)
        kept = [{**o, "at": layout_mod._rotate(layout_mod._rotate(o["at"], back), turn)} for o in kept_open]
        clear = lambda g: all(math.dist(g["at"], k["at"]) > (g.get("width", 1) + k.get("width", 1)) / 2 for k in kept)
        lay["openings"] = [g for g in lay["openings"] if clear(g)] + kept
    m = from_layout({**lay, "edited": False}, _points(d))
    _write(d, m, lay)
    record(d, "propose", {}, before)
    return out


def built_fresh(d: Path) -> None:
    """A scan that nobody has corrected was built again: the model starts over from it."""
    _path(d).unlink(missing_ok=True)


def forget(d: Path) -> None:
    before = snapshot(d)
    (d / "layout.json").unlink(missing_ok=True)
    _path(d).unlink(missing_ok=True)
    record(d, "reset", {}, before)
