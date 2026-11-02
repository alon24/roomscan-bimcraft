# LiDAR calibration print (lidar-target v2)

A 3D-printed target that tells the phone how wrong its LiDAR is, at which distances, at which angles and on which colours,
against a print whose size is known. The camera reads six printed markers (at three heights) to know exactly where the phone is
relative to the print, so every LiDAR pixel that lands on the print has a known true depth. Thousands of those, from 11
positions, give the correction.

<img src="../prints/lidar-target/images/target_iso_v2.png" width="420"> <img src="../prints/lidar-target/images/target_top_v2.png" width="300">

Files (in `prints/lidar-target/`, see [prints/README.md](../prints/README.md); the 3MF is also served by the server at `/prints/`):

| File | What |
|---|---|
| `3mf/lidar-target-v2-4colour.3mf` | **Use this one.** One object, four parts, coloured and already on filaments 1-4: `white` 1, `black` 2, `blue` 3, `red` 4 |
| `stl/lidar-target-v2-white.stl`, `-black.stl`, `-blue.stl`, `-red.stl` | The same four parts as meshes |
| `cad/lidar-target-v2.step` | For CAD |
| `cad/lidar-target-v2.solidcraft` | The SolidCraft design |

(v1, with four markers and two colours, was never printed and is not supported.)

## 1. Printing it (Snapmaker U1, 4 colours)

1. Open **lidar-target-v2-4colour.3mf** in Snapmaker Orca. It is one object with four parts (the Objects list shows white, black,
   blue, red), each already on its filament slot, with the U1 settings inside. **Check before printing:** the model shows all four
   colours, and after Slice the Preview shows a weight for all four filaments. Put **matte white PLA** in slot 1, **matte black** in 2, **blue** in 3, **red** in 4. No blue or red? Set
   those two parts to white: only the colour test is lost.
2. **Matte matters most**: turn **ironing off** (it makes the top shiny) and do not use silk or glossy filament. Reflections throw
   both the LiDAR and the marker reading.
3. Settings: 0.4 mm nozzle, **0.2 mm layers** (every coloured part is exactly the last 2 layers of its surface), 3 walls,
   15% infill, no supports (nothing overhangs: the wedges slope outward from a vertical inner face), no brim. The U1 changes
   tools, so the extra colours cost no purge tower.
4. Size on the bed: 200 x 200 x 57 mm (the U1 takes 270 x 270 x 270). About 3-4 hours, about 230 g white, a few grams of each other colour.
5. Check the print:
   - the six black squares are crisp, with clean white cells (a blob in a cell makes that marker unreadable);
   - the text reads **THIS SIDE UP - LIDAR TARGET v2 - 200 mm** and **APP: MEASURE > CALIBRATE > WITH THE PRINT**;
   - it lies flat: put it on a table, a sheet of paper must not slide under any edge;
   - measure the **ruler** under the bottom text with calipers: from the outer edge of the first long tick to the outer edge of
     the last is **101.0 mm** (the ticks are 1 mm wide, 10 mm apart, centre to centre 100 mm). Type what the calipers read into
     the app's **Ruler reading** field: it works out the print's scale itself (PLA shrinks a few tenths of a percent).

## 2. Using it (the app does the guiding)

App: **Measure** tab > **Calibrate** > **With the print (guided)** (also in Settings > Calibrate LiDAR with the print).

1. Lay the print flat on a **hard floor** (not a rug), text the right way up, good even light (no sun patches), with room to walk
   round it. The floor is best because the far stations are 80-120 cm from the print: at arm's length standing up, where over a
   table you would need a ladder. A low table works too. 120 cm is there because rooms are measured from a metre or more, and
   LiDAR error changes with distance, so a correction fitted only up close would not hold there.
2. Point the crosshair at the middle of the print, about 40 cm away, and tap **This is the middle**. A teal ring appears on the
   print: from now on ARKit tracks it, so the app always knows your **distance** to the print and your **tilt** (0 = looking
   straight down at it).
3. The screen shows a grid of 11 stations (distance 30 / 50 / 80 / 120 cm across, tilt down / 30° / 55° down the side) and the
   live readout: distance now -> wanted, tilt now -> wanted, aim, steady. The top line says the one thing to do next
   ("Move back 20 cm", "Move out to the side and lower the phone", "Hold still..."). Any order works: whenever you are at a
   station that still needs photos and hold still, it takes 3 photos by itself (a tap of the phone each, a stronger one when the
   station is done) and ticks it off.
4. With 3 or more stations done (all 11 is best, about 3 minutes), tap **Send and calculate**. The server answers in a few
   seconds with the error before and after, the left-over error by distance, by angle and by colour, and how ARKit's own scale
   compares with the print. **Use this correction for measuring** makes the Measure tab use it (tape checks, if any, still win).

Why the phone alone is enough: ARKit tracks the print at 60 fps on the phone, no network needed while you move. The precise part
(reading the markers, fitting) runs on the server afterwards from the photos; the photos and the result are kept in
`data/lidarcal/<time>/` so a better analysis can be re-run on them later.

## 3. The geometry (exact, for rebuilding or changing it)

Units mm. Origin = centre of the plate's **bottom** face, **Z up**, **+Y towards markers 40 and 41** (the top of the text).
`server/app/lidartarget.py` holds the same numbers; change both together and bump `VERSION`.

| Part | Colour | Where | Size |
|---|---|---|---|
| Plate | white | x, y in [-100, 100], z in [0, 5] | 200 x 200 x 5 |
| Pad 10 | white | centre (-23, +23) | 40 x 40, top at z = 15 |
| Pad 20 | white | centre (+23, +23) | 40 x 40, top at z = 25 |
| Pad 30 | white | centre (+23, -23) | 40 x 40, top at z = 35 |
| Pad 40 | white | centre (-23, -23) | 40 x 40, top at z = 45 |
| Wedge 15° | white | +Y edge: y 61..91, x -25..25 | high side (8.04 mm) at y = 61, slope down to 0 at y = 91 |
| Wedge 30° | white | -Y edge: y -61..-91, x -25..25 | 17.32 mm high at y = -61 |
| Wedge 45° | white | +X edge: x 61..91, y -25..25 | 30.00 mm high at x = 61 |
| Wedge 60° | white | -X edge: x -61..-91, y -25..25 | 51.96 mm high at x = -61 |
| Markers 40-43 | black | centres (-76, +76) 40, (+76, +76) 41, (+76, -76) 42, (-76, -76) 43 | 36 x 36 (cells of 6 mm), z 5 to 5.4 |
| Marker 44 | black | on pad 20, centre (+23, +23) | 30 x 30 (cells of 5 mm), z 25 to 25.4 |
| Marker 45 | black | on pad 40, centre (-23, -23) | 30 x 30 (cells of 5 mm), z 45 to 45.4 |
| Patch | blue | on pad 10, centre (-23, +23) | 36 x 36, z 15 to 15.4 |
| Patch | red | on pad 30, centre (+23, -23) | 36 x 36, z 35 to 35.4 |
| Text | black | baselines y = +49 and y = -54, centred on x = 0 | Ubuntu Bold, 4.5 mm caps, z 5 to 5.4 |
| Ruler | black | baseline y -56.5..-55.5, x -50.5..50.5; ticks every 10 mm (x = -50..50), 1 mm wide, down to y -58.5 (long ones at 0, 50, 100 mm: -60) | z 5 to 5.4 |

Wedge heights are 30 mm x tan(angle). Slopes face outward, so from straight above the beam meets them at exactly 15/30/45/60°.

Markers: ArUco **DICT_4X4_50**, ids 40-45 (the turntable ring uses 0-17, so the two never get mixed up). Each is a black square
with its white cells cut out (the cutters 0.1 mm larger than a cell, so white cells that touch only at a corner join cleanly);
rows top (+Y) to bottom, columns left (-X) to right, 1 = black:

```
id 40    id 41    id 42    id 43    id 44    id 45
111111   111111   111111   111111   111111   111111
111101   111011   111001   111001   111011   111011
110001   101011   111011   101111   110111   100011
111101   111011   101111   101001   100011   100011
101111   101111   100111   111011   101111   101001
111111   111111   111111   111111   111111   111111
```

OpenCV's corner order for each marker (top-left, top-right, bottom-right, bottom-left of the marker image) is, in plate
coordinates, (cx-h, cy+h), (cx+h, cy+h), (cx+h, cy-h), (cx-h, cy-h) with h = half its side, at the top of the marker (its surface
+ 0.4). Corners at z = 5.4, 25.4 and 45.4: three heights, which pins the camera's tilt and distance apart far better than one plane.

Rebuilding it in SolidCraft: the design has 18 timeline items in 2 folders and 13 parameters (`plate`, `plate_t`, `inlay`, `pad`, `pad_at`, `pad1`-`pad4`, `patch`, `wedge_in`, `wedge_run`, `wedge_w`). The marker cells are fixed numbers on purpose: they are the spec the server reads.

**White shape** (body `white`)

1. `Sk_Plate`, `Plate`: the outline, extruded `plate_t`.
2. `Sk_Pads` (master sketch on top of the plate, 4 squares), `Pad1`-`Pad4`: one extrude per height, each picking its square.
3. `Sk_Wedges_15_30`, `Wedges_15_30` and `Sk_Wedges_45_60`, `Wedges_45_60`: two triangles per sketch, on planes given as `{origin, x_dir, y_dir: [0,0,1]}` so the sketch's y is the world Z, extruded `wedge_w` symmetric, heights `wedge_run * tan(angle)`.

**Colour layers** (bodies `black`, `blue`, `red`, all `inlay` thick)

4. `Sk_PadTops` (master sketch at z = 0: markers 44 and 45, both patches), `BluePatch`, `RedPatch`, `Marker44`, `Marker45`: each picks its profile and starts at its pad's top with `start_offset`.
5. `Sk_PlateTop` (markers 40-43, ruler, both text lines), `BlackOnPlate`: one extrude joined into `black`, so all the black is one body without a slow Combine.

Each marker is one rectangle plus closed outlines of its white cells, every cell 0.1 mm larger so cells touching at a corner merge. Check: render the top view and run OpenCV's ArUco detector: 40 top-left, 41 top-right, 42 bottom-right, 43 bottom-left, 44 right of centre at the top, 45 left of centre at the bottom. A mirrored marker does not decode.

**3MF:** export one STL per colour and write Orca's own project format (`3D/Objects` with one object per part, `Metadata/model_settings.config` with each part's `extruder`, `Metadata/project_settings.config` with the U1 settings and filament colours). The PrusaSlicer-style config does **not** work: Orca merges the parts into one and prints everything on filament 1. Place the object at the bed centre (135.5, 136), not at (0, 0).

Rules if you change it: keep a white margin of at least one cell round every marker; keep tall parts away from the markers so
they are not hidden at 55° tilt; keep everything matte.

## 4. How the numbers are worked out (server)

`POST /api/lidar-cal` (photos + depth + `photos.json` + optional `print_scale`), `GET /api/lidar-target` (stations, tolerances),
`GET /api/lidar-cal/latest`. For each photo:

1. **Camera pose from the markers.** Detect DICT_4X4_50 markers 40-45 (sub-pixel corners), solve the pose from all their
   corners with SQPnP (not IPPE: IPPE assumes the points lie at z = 0) and refine it (Levenberg-Marquardt). Needs 2 markers.
   ARKit's intrinsics are used with the principal point moved by half a pixel (OpenCV puts pixel centres at whole numbers).
2. **True depth per LiDAR pixel.** Each depth pixel's ray (through its centre, with the intrinsics scaled to 256 x 192) is cast
   against the target, built from convex pieces (plate, pads, wedges, markers and patches as half-spaces). That gives the depth it
   should read (z along the optical axis, the same as LiDAR reports), the face it hits, its colour and the angle of incidence.
   Pixels within 2 of a face boundary are left out (LiDAR blurs edges), and so are pixels off the print.
3. **Station.** The camera's distance to the plate's centre and its tilt from straight down put the photo on a station
   (within 15% and 7°).

Then all frames together (at most 4000 pixels per frame, so a close-up does not outweigh the rest):

- `true = a * measured + b`, fitted on the **white** surfaces (walls are mostly light), least squares, refitted without points
  more than 3 robust sigmas (and over 5 mm) off. The same form as the app's tape calibration, so it slots into the Measure tab.
- The error left after the correction by true distance (0-0.4, 0.4-0.65, 0.65-1, 1-1.5 m), by angle of incidence (15° bins) and
  **by colour** (white, black, blue, red): dark or coloured surfaces can read long or short in the LiDAR's infrared, and steep
  surfaces too, which one scale and offset cannot fix.
- **Camera against the print**: the camera positions from the markers (metric, from the printed size) against ARKit's camera
  positions for the same photos, as the median ratio of the distances between pairs: 1.000 means ARKit's metres are the print's.
- `good` when the error after is under 15 mm, the scale within 10% of 1 and the offset under 5 cm.

Tested on synthetic frames (photos with each marker drawn on its own surface by homography, depth cast from the model with a
known error, black surfaces reading 4 mm long): a LiDAR that reads true = 1.02 x measured - 8 mm comes back as 1.02 / -8 mm
within 0.4% / 2 mm, black is reported 4 mm long and the other colours true, every frame lands on its station, ARKit-vs-print 1.00
(`server/tests/test_lidartarget.py`). Not yet tried with a real print and phone.
