"""Transforms, wall endpoints and projections. Units: RoomPlan metres in, plan millimetres out."""
import math

Point = tuple[float, float]


def matrix(surface: dict) -> list[list[float]]:
    """Return the 4x4 transform as four columns. Accepts nested columns or a flat 16-list."""
    t = surface["transform"]
    if len(t) == 16:
        t = [t[i * 4:(i + 1) * 4] for i in range(4)]
    return t


def to_plan(x: float, z: float) -> Point:
    return (x * 1000.0, -z * 1000.0)


def centre(surface: dict) -> tuple[float, float, float]:
    c = matrix(surface)[3]
    return (c[0], c[1], c[2])


def centre_plan(surface: dict) -> Point:
    c = centre(surface)
    return to_plan(c[0], c[2])


def wall_endpoints(surface: dict) -> tuple[Point, Point]:
    """Plan-space (mm) endpoints from the centre and local X axis, projected onto the floor."""
    col0 = matrix(surface)[0]
    cx, _, cz = centre(surface)
    ax, az = col0[0], col0[2]
    n = math.hypot(ax, az) or 1.0
    ax, az = ax / n, az / n
    half = surface["dimensions"][0] / 2
    return (to_plan(cx - ax * half, cz - az * half),
            to_plan(cx + ax * half, cz + az * half))


def project_on_segment(p: Point, a: Point, b: Point) -> Point:
    """Closest point to p on segment a-b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    len2 = dx * dx + dy * dy
    if len2 == 0:
        return a
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2))
    return (a[0] + t * dx, a[1] + t * dy)


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def floor_level(scan: dict) -> float:
    """World Y of the floor: min of floor surfaces, else min of wall bottoms."""
    floors = scan.get("floors") or []
    if floors:
        return min(centre(f)[1] for f in floors)
    return min(centre(w)[1] - w["dimensions"][1] / 2 for w in scan["walls"])


def snap_endpoints(walls: list[tuple[Point, Point]], tol: float = 100.0) -> list[tuple[Point, Point]]:
    """Merge endpoints within tol mm into one shared node (cluster centroid)."""
    pts = [p for w in walls for p in w]
    clusters: list[list[Point]] = []
    for p in pts:
        for c in clusters:
            if dist(p, c[0]) <= tol:
                c.append(p)
                break
        else:
            clusters.append([p])
    node = {}
    for c in clusters:
        m = (sum(p[0] for p in c) / len(c), sum(p[1] for p in c) / len(c))
        for p in c:
            node[p] = m
    return [(node[a], node[b]) for a, b in walls]


def chain_loop(walls: list[tuple[Point, Point]]) -> list[Point] | None:
    """If the (snapped) walls form one closed loop, return its ordered corner points."""
    if len(walls) < 3:
        return None
    remaining = list(walls)
    a, b = remaining.pop(0)
    pts = [a, b]
    while remaining:
        end = pts[-1]
        for i, (p, q) in enumerate(remaining):
            if p == end:
                pts.append(q)
            elif q == end:
                pts.append(p)
            else:
                continue
            remaining.pop(i)
            break
        else:
            return None
    return pts[:-1] if pts[-1] == pts[0] else None
