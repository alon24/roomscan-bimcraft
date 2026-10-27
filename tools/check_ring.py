#!/usr/bin/env python3
"""Does a photo of the printed ring work? Usage: server/.venv/bin/python tools/check_ring.py photo.jpg
Prints which marker ids were found and the apparent size of each in pixels (aim for 60 px or more per marker edge)."""
import sys

import cv2
import numpy as np

img = cv2.imread(sys.argv[1], cv2.IMREAD_GRAYSCALE)
det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), cv2.aruco.DetectorParameters())
corners, ids, _ = det.detectMarkers(img)
if ids is None:
    sys.exit("no markers found: check focus, light (no glare) and distance")
for c, i in sorted(zip(corners, ids.ravel()), key=lambda x: x[1]):
    c = c.reshape(4, 2)
    edge = np.mean([np.linalg.norm(c[k] - c[(k + 1) % 4]) for k in range(4)])
    print(f"id {i:2d}  edge {edge:5.1f} px")
print(f"{len(ids)} of 18 markers found")
