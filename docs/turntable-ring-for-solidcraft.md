# Turntable marker ring: build brief for SolidCraft (self-contained)

Build a flat 3-colour ring of printed square markers plus a disc. Units: **millimetres**. Z is up. The white top surface of the
base is the reference plane. Everything you need is in this file; no other file is required.

## What to model (3 bodies)

1. **ring_white**: an annulus, outer diameter **196**, inner diameter **128**, extruded **2.6** (Z 0 to 2.6).
   A separate disc, diameter **127**, extruded **2.6** (Z 0 to 2.6), centred at the origin.
2. **ring_black**: the black cells of 18 markers (below), each cell polygon extruded **0.4** on top of the white (Z 2.6 to 3.0).
3. **ring_orange**: orange accents (below), extruded **0.4** (Z 2.6 to 3.0).

Origin = ring centre. X axis points to the centre of marker 0. Angles are counter-clockwise seen from above (from +Z).
Call `list_commands` first and use the real command names. Sketch on the XY plane (offset to Z = 2.6 for the raised layers),
close each polygon, extrude. Use `batch` for the many small polygons.

## Markers (black cells)

18 markers, i = 0..17. Marker i: angle a = 20 x i degrees, centre c = (81 cos a, 81 sin a).
Marker size **22 x 22**, so one cell is **s = 22 / 6 = 3.666667**. Each marker is a 6 x 6 grid of cells; `#` = black (build it), `.` = white (build nothing).

Define for marker i: up u = (cos a, sin a) (radially outward), right r = (sin a, -cos a).
Grid cell at row R (0 = top row, as written below) and column C (0 = left column) occupies, in marker-local coordinates
(x along r, y along u, origin at the marker centre):

x from -11 + C*s to -11 + (C+1)*s,  y from 11 - (R+1)*s to 11 - R*s.

World point of a local corner (x, y) = c + x*r + y*u. So each black cell is a rotated square with side s; build its 4 corners
this way. To save work, merge horizontally adjacent black cells in the same row into one rectangle (x from the first cell's left to the last cell's right).
The markers all differ: do not copy one marker around the ring.

Read each grid exactly as printed (top row first, left to right):

**Marker 0** (angle 0°, centre (81.000, 0.000))
```
######
#.#..#
##.#.#
###..#
###.##
######
```
**Marker 1** (angle 20°, centre (76.115, 27.704))
```
######
######
#....#
#.##.#
#.#.##
######
```
**Marker 2** (angle 40°, centre (62.050, 52.066))
```
######
###..#
###..#
###.##
#..#.#
######
```
**Marker 3** (angle 60°, centre (40.500, 70.148))
```
######
#.##.#
#.##.#
##.###
##..##
######
```
**Marker 4** (angle 80°, centre (14.066, 79.769))
```
######
##.#.#
##.###
#.##.#
#...##
######
```
**Marker 5** (angle 100°, centre (-14.066, 79.769))
```
######
##...#
#.##.#
#..###
#..#.#
######
```
**Marker 6** (angle 120°, centre (-40.500, 70.148))
```
######
#.##.#
#...##
###.##
#...##
######
```
**Marker 7** (angle 140°, centre (-62.050, 52.066))
```
######
#..###
##.###
#....#
###.##
######
```
**Marker 8** (angle 160°, centre (-76.115, 27.704))
```
######
#....#
#...##
#..#.#
#.#.##
######
```
**Marker 9** (angle 180°, centre (-81.000, 0.000))
```
######
#..###
#....#
##.#.#
##..##
######
```
**Marker 10** (angle 200°, centre (-76.115, -27.704))
```
######
#....#
#.##.#
#.##.#
####.#
######
```
**Marker 11** (angle 220°, centre (-62.050, -52.066))
```
######
####.#
####.#
#.#.##
##...#
######
```
**Marker 12** (angle 240°, centre (-40.500, -70.148))
```
######
######
#...##
#.#..#
##...#
######
```
**Marker 13** (angle 260°, centre (-14.066, -79.769))
```
######
###.##
#.#.##
######
#....#
######
```
**Marker 14** (angle 280°, centre (14.066, -79.769))
```
######
###.##
##.###
#.#..#
####.#
######
```
**Marker 15** (angle 300°, centre (40.500, -70.148))
```
######
###.##
##..##
###..#
#...##
######
```
**Marker 16** (angle 320°, centre (62.050, -52.066))
```
######
##.###
##..##
##..##
##.#.#
######
```
**Marker 17** (angle 340°, centre (76.115, -27.704))
```
######
##..##
##..##
######
######
######
```

Check: marker 0's top row is all black (that is the black border), and so is its left and right column and bottom row.
After building, look from above (+Z): each marker must read upright with its top edge pointing away from the centre.

## Orange accents (raised 0.4)

- **Rim ticks:** 72 ticks, one every 5 degrees (k x 5 degrees, k = 0..71). Each is a rectangle 1.0 wide (tangential) and
  runs radially from radius 94.5 to 97.5. When k is a multiple of 18 (0, 90, 180, 270 degrees) it runs from 92.5 to 97.5 instead.
- **Inner guide circle:** a ring band from radius 64.5 to 65.5.
- **Index triangle:** points (66.0, 0), (69.5, 2.5), (69.5, -2.5).

## Checks before export

- White bounding box 196 x 196 x 2.6. Black and orange tops at Z = 3.0.
- Marker 0 centred at (81, 0), marker 5 at angle 100 degrees, marker 9 at 180 degrees (centre (-81, 0)).
- No black or orange within 6 mm of the ring's outer edge (r > 92) except the orange ticks, and none on the disc.
- Export `ring_white`, `ring_black`, `ring_orange` as separate STL files with the same origin (or one 3MF with three objects).

## Printing (for the human)

Matte PLA only, no gloss or silk. Layer 0.2 mm; white up to layer 13, black and orange on layers 14 and 15. The top must stay flat
within 0.3 mm. Marker size after printing must be 22.0 +/- 0.2 mm.
