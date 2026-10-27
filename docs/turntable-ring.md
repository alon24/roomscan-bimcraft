# Turntable marker ring: design brief

Give this file (and the files in `docs/ring/`) to the CAD assistant. The goal is a flat printed ring that sits on a 1 kg
filament spool lying on its side. A phone looks at the ring while the spool turns. Printed square markers (ArUco, dictionary
`DICT_4X4_50`) tell the scan software, in every single frame, exactly how far the spool has turned and where its axis is.
A plain disc in the middle holds the part being scanned.

Everything below is in **millimetres**. The CAD model uses a **ring frame**: origin at the ring centre, **Z up** (toward the
camera), the white top surface of the base is **Z = 0** (see section 3), X axis through the centre of marker 0, angles counter-clockwise seen from above.

## 1. Parts (print 2 pieces, 3 colours)

| Piece | What | Size |
|---|---|---|
| **Ring** | flat annulus carrying the markers | inner Ø **128.0**, outer Ø **196.0**, base thickness **2.6** |
| **Stage** | flat disc inside the ring, the part stands on it | Ø **127.0** (0.5 mm radial clearance to the ring), thickness **2.6** |

Measure your spool first. The design assumes a standard 1 kg spool: flange outer Ø about 200 and flat. **The ring's outer Ø must be
smaller than the flange Ø by at least 2 mm** (no overhang). If your flange is smaller than 198, scale the ring using the
rules in section 6, do not just shrink it (the markers would get small).

The ring and the stage are glued or taped to the spool flange (double-sided tape). They must not slip against the spool
while it turns. Concentricity with the spool axis does **not** matter: the software measures the ring, not the spool.

If the printer bed is smaller than 200 mm, split the ring into two 180° halves with a 0.2 mm clearance dovetail, cut where
there is no marker (at angles 90° and 270°, between markers 4 and 5, and between 13 and 14).

## 2. Colours (3 bodies, one slicer filament each)

1. **White** (matte PLA): the whole base of the ring and stage, up to Z = 2.6. It is also the quiet zone around every marker,
   so it must be a diffuse matte white.
2. **Black** (matte PLA): the black cells of the 18 markers, raised **0.4** on top of the white base (Z 2.6 to 3.0).
3. **Orange** (matte PLA, any strong colour that is neither black nor white): rim ticks, the inner guide circle and the
   index triangle (section 5), also raised 0.4.

No glossy, silk or metallic filament anywhere on the top face: reflections make the markers unreadable. Do not use
transparent filament.

Export three bodies (or three STL files, same origin) named `ring_white`, `ring_black`, `ring_orange`; the slicer assigns one
filament to each. Ring and stage can share the same three bodies (the stage has no black or orange).

## 3. Heights

| Z range | Content |
|---|---|
| 0 to 2.6 | white base (ring and stage) |
| 2.6 to 3.0 | black marker cells, orange accents |

The **reference plane is the white surface, Z = 2.6 in print coordinates**; call it Z = 0 in the ring frame. The part rests on
the stage at this same height, so the part's base plane is the ring plane.

## 4. Markers

18 markers, dictionary `DICT_4X4_50`, ids **0 to 17**.

- Outer edge of the marker's black border: **22.0 x 22.0**.
- One cell = 22 / 6 = **3.6667** (each marker is 6 x 6 cells: a 1-cell black border and a 4 x 4 code).
- Marker i has its centre at radius **81.0** and angle **i x 20°** (counter-clockwise seen from above): centre =
  (81 cos(20i°), 81 sin(20i°)).
- The marker's own "up" (the top edge when you read the code upright) points **radially outward**. Its "right" points
  clockwise seen from above.
- Corner order (what the detector reports): top-left, top-right, bottom-right, bottom-left of the outer border edge.
  With up u = (cos a, sin a), right r = (sin a, -cos a), centre c, half size h = 11:
  TL = c + h(-r + u), TR = c + h(r + u), BR = c + h(r - u), BL = c + h(-r - u).
- The markers differ from each other. **Do not make one marker and pattern it.** Use `docs/ring/black_cells.json`: for
  every id it lists the 6 x 6 bit matrix (1 = black) and the black cells as polygons (4 corners each, ring coordinates,
  horizontal runs of cells already merged, 8 to 12 polygons per marker). Extrude each polygon 0.4 on top of the white base. White cells are just the
  white base showing through, they need no geometry.
- Keep the white margin around each marker (at least 1 cell, 3.7 mm, radially 6 mm). The generator already respects this.
- Minimum feature: 3.67 mm cells print cleanly with a 0.4 mm nozzle and 0.2 mm layers. Do not shrink the markers below 20.

`docs/ring/ring.json` holds the same geometry as data (centres, corner coordinates for all 18 markers). The scan software reads this
file, so **if you change any dimension, regenerate it** (section 6) and keep both files in step.

## 4b. How this was checked

`tools/make_ring.py` renders the design and runs OpenCV's ArUco detector on it. All 18 markers are found and the detected
corners match the numbers above within 0.2 mm. A preview is in `docs/ring/ring_preview.png` and `ring_preview.svg`.

## 5. Orange accents (the "notches")

These give the eye and a coarse fallback a visible angle scale. The markers do the real work.

- **Rim ticks:** every 5° (72 ticks), 1.0 wide, 3.0 long radially, from radius **94.5 to 97.5**. Every 90° (at 0°, 90°, 180°,
  270°) a long tick from **92.5 to 97.5**. Tick centre line along the radius at that angle.
- **Inner guide circle:** a circular band at radius **65.0**, 1.0 wide (64.5 to 65.5). Seen from the camera it shows
  the ring plane as an ellipse, which helps the software find the plate quickly.
- **Index triangle:** points: (66.0, 0), (69.5, 2.5), (69.5, -2.5). It sits just inside marker 0 and points toward the
  centre. It tells a person (and a fallback detector) where angle 0 is.

All orange features are raised 0.4 like the black cells. Keep orange off the markers' quiet zones.

## 6. Parameters (to rescale or regenerate)

| Parameter | Value | Rule |
|---|---|---|
| ring outer Ø | 196 | flange Ø minus at least 2 |
| ring inner Ø | 128 | outer minus 2 x 34 |
| marker count | 18 | pitch 20°; neighbours stay at least 6 mm apart |
| marker size | 22 | at least 20; the radial band is size + 12 wide |
| marker centre radius | 81 | (inner Ø / 2) + 6 + size / 2 |
| stage Ø | 127 | ring inner Ø minus 1 |

To change them, edit the constants at the top of `tools/make_ring.py` and run
`server/.venv/bin/python tools/make_ring.py`; it rewrites `ring.json`, `black_cells.json` and the previews and re-runs the
detection check.

## 7. Print settings

- Matte PLA (or PETG-matte), 0.4 mm nozzle, layer height 0.2, first layer 0.2.
- Ring and stage face down with the markers up. 3 or more perimeters, 100% top layers on the markers. Use ironing on the
  top face if available for a very flat surface.
- Colour change at layer 14 (Z = 2.6) if you use a single-extruder filament swap: white for layers 1 to 13, then
  black. For multi-material printers, assign the three bodies. (With one extruder you can only do two colours this way:
  skip orange or paint it.)
- Flatness: the top surface must stay within 0.3 mm over the whole ring. Use a textured or smooth plate with good adhesion, no
  warping, and let the ring cool before removing it. A warped ring makes the pose less accurate.
- Check marker size with calipers after printing: it must be **22.0 +/- 0.2**. If it is off, edit `marker_size_mm` in
  `ring.json` to the measured value; the software uses that file.

## 8. Using it

1. Tape the ring and stage on the spool flange, concentric by eye. Put the part in the middle of the stage.
2. Phone on a stand 25 to 40 cm from the part, looking down at the ring at about 30 to 45 degrees. At least 4 markers
   must be visible at all times; a tall part hides the far ones, which is fine.
3. Even light, no glare on the ring. Plain background if possible.
4. Turn the spool slowly (about one turn in 30 to 60 s), by hand or with a motor. The phone records every frame (photo +
   LiDAR depth).
5. Check a photo: `server/.venv/bin/python tools/check_ring.py photo.jpg` prints which markers were found and how many
   pixels wide each is. Aim for at least 60 px per marker edge and at least 4 markers found.

## 9. What the software does with it (for context)

For every frame the server detects the markers, solves the ring's position relative to the camera from all visible marker
corners (using `corners_mm` in `ring.json` and the phone's lens data), and then expresses the frame's LiDAR points in the
**ring frame**. All frames land in the same frame, so they simply add up to the whole part. This does not need a steady turning speed
or a fixed phone. It also gives true scale from the marker size.

Flipping the part to scan its underside: the ring still gives a stable frame, but the part moved relative to it. The two scans
are lined up where they overlap (starting guess: 180 degrees about a horizontal axis). A second set of markers fixed to a jig that holds
the part removes this step. That is a later improvement.

## 10. Files

- `docs/ring/ring.json`: all dimensions and the corner coordinates of the 18 markers (the software reads this).
- `docs/ring/black_cells.json`: black polygons per marker and the 6 x 6 bit matrices (the CAD model uses this).
- `docs/ring/ring_preview.svg` / `.png`: picture of the finished ring (orange shown in the SVG only).
- `tools/make_ring.py`: regenerates the data and verifies it with OpenCV.
- `tools/check_ring.py`: checks a photo of the printed ring.

## 11. Instructions for the CAD assistant (SolidCraft)

1. Call `list_commands` first and use the real command names; those below are the intent.
2. Create three bodies. For each, sketch on the XY plane (origin at the ring centre) and extrude upward with Z as in section 3.
3. **White:** sketch the outer circle Ø196 and the inner circle Ø128, extrude 2.6 (ring). Then a Ø127 circle, extrude 2.6 (stage).
4. **Black:** on the plane at Z = 2.6, for each of the 18 markers, sketch every polygon in `black_cells.json` (batch the calls) and extrude 0.4 (join into the black body). Total about 200 polygons.
5. **Orange:** on the plane at Z = 2.6, sketch the 72 ticks, the guide circle band (inner R 64.5, outer R 65.5) and the index triangle, extrude 0.4.
6. Verify: bounding box of white is 196 x 196 x 2.6; black and orange extend to Z = 3.0; marker 0 is on the +X axis, marker 5 at 100 degrees.
7. Inspect marker 0 and marker 17 with a top view and compare the pattern with `ring_preview.png`. Mirror images are wrong: seen from above, markers read upright with their top edge pointing away from the centre.
8. Export `ring_white`, `ring_black`, `ring_orange` as separate STL (or one 3MF with three objects).
