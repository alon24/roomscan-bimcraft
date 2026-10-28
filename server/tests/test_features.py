import numpy as np
import trimesh

from app import features


def _part():
    box = trimesh.creation.box(extents=(60, 40, 20))
    hole = trimesh.creation.cylinder(radius=4, height=40, sections=64)
    hole.apply_translation([-15, 0, 0])
    boss = trimesh.creation.cylinder(radius=7, height=12, sections=64)
    boss.apply_translation([18, 5, 16])
    out = trimesh.boolean.difference([box, hole], engine="manifold")
    return trimesh.boolean.union([out, boss], engine="manifold")


def test_finds_the_flat_faces_the_hole_and_the_boss():
    res = features.detect(_part())
    facing = {p["facing"] for p in res["planes"]}
    assert {"top", "bottom"} <= facing
    holes = [c for c in res["cylinders"] if c["kind"] == "hole" and abs(c["diameter_mm"] - 8) < 0.5]
    bosses = [c for c in res["cylinders"] if c["kind"] == "boss" and abs(c["diameter_mm"] - 14) < 0.8]
    assert holes, res["cylinders"]
    assert bosses, res["cylinders"]
