import numpy as np
import trimesh

from app import fit


def _part():
    box = trimesh.creation.box(extents=(40, 25, 15))
    peg = trimesh.creation.cylinder(radius=5, height=30)
    peg.apply_translation([12, 4, 8])
    return trimesh.boolean.union([box, peg], engine="manifold")


def test_a_moved_and_turned_copy_lines_up_with_almost_no_difference():
    ref = _part()
    scan = ref.copy()
    T = trimesh.transformations.rotation_matrix(np.radians(37), [0, 0, 1])
    T = T @ trimesh.transformations.rotation_matrix(np.radians(90), [1, 0, 0])      # lying on another side
    T[:3, 3] = [31, -12, 7]
    scan.apply_transform(T)
    res = fit.compare(scan, ref)
    assert res["rms_mm"] < 0.4 and res["max_mm"] < 1.5, res["rms_mm"]
    assert abs(res["volume_ratio"] - 1) < 0.01


def test_a_scan_that_is_too_fat_reads_positive():
    ref = _part()
    scan = ref.copy()
    scan.apply_scale(1.05)
    res = fit.compare(scan, ref)
    assert res["signed_mean_mm"] > 0.3 and res["volume_ratio"] > 1.1
