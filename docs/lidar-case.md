# Carry case for the LiDAR target (lidar-case)

A two-part box for carrying the [LiDAR calibration print](lidar-target.md) without breaking the wedges or scratching the
markers. The **tray** holds the plate. The **lid** is the print's negative: a socket for every pad and wedge, 0.5 mm bigger
all round, so the print sits still inside. Four **snap tabs** on the lid click into notches in the tray to close it.

<img src="../prints/lidar-case/images/case_closed.jpg" width="420"> <img src="../prints/lidar-case/images/case_open.jpg" width="420">

Files (in `prints/lidar-case/`, see [prints/README.md](../prints/README.md); the 3MFs are also served at `http://<server>/prints/`):

| File | What |
|---|---|
| `3mf/lidar-case-tray.3mf` | **Print this.** The tray, white PLA (filament 1), floor down |
| `3mf/lidar-case-lid-2colour.3mf` | **And this.** The lid, already upside down for printing: white (filament 1), with the label inlaid in black (filament 2) |
| `stl/lidar-case-tray.stl`, `lidar-case-lid-white.stl`, `lidar-case-lid-black.stl` | The same parts as meshes, standing as they do in use (the lid the right way up) |
| `cad/lidar-case.step` | For CAD |
| `cad/lidar-case.solidcraft` | The SolidCraft design (every feature named) |
| `images/case_*.jpg` | Pictures: closed, open, cut in half, the snap tab close up |

## 1. Printing (Snapmaker U1)

1. **Tray**: open `lidar-case-tray.3mf` in Snapmaker Orca. 213 x 213 x 12 mm. White PLA, 0.2 mm layers, 3 walls, 15%
   infill, no supports.
2. **Lid**: open `lidar-case-lid-2colour.3mf`. It is already lying on its top (224 x 213 x 64 mm with the tabs; the U1 takes
   270 x 270). White in slot 1, black in slot 2. The label is the first two layers (0.4 mm), flush with the top, so print the
   **first layer slowly** to keep the letters clean. 0.2 mm layers, 3 walls, **10% infill** (the sockets are big solid
   blocks in the file; the infill keeps them light), no supports: every overhang is under 2.5 mm or at 45°.
3. Check the lid in the app: the Objects list shows white and black parts and the label is black on the model. The U1 settings are inside both files.
4. Each part needs its own plate: together they don't fit on the U1's bed.
5. Check that the four tabs came out straight and are not stuck to the lid wall: you should see light through the 1 mm gap behind each one.

## 2. Using it

<img src="../prints/lidar-case/images/case_latch.jpg" align="right" width="300">

- **Pack:** lay the print in the tray (any way round fits the tray). The lid fits only one way round the print: hold it so
  its label reads the same way as the text on the print, and press it straight down. Each tab rides over the tray's edge and
  **clicks** into its notch.
- **Open:** pull the thumb tab at the bottom of one tab outwards (about 1.5 mm) and lift that side. Then do the same on the
  other side.
- **Take the print out:** two finger notches in the middle of two sides of the tray let you get under the plate's edge.
- The print can move at most 0.5 mm inside the closed case, so it won't rattle much. A 1-2 mm sheet of felt or foam in the
  tray takes up the rest.

The cut-in-half picture (case shown blue so you can tell it from the print) shows every pad and wedge in its own socket:

<img src="../prints/lidar-case/images/case_section.jpg" width="640">

## 3. Geometry (for changing it)

Units mm. Same origin as the print: the plate's bottom face, so **the print's coordinates are the case's coordinates** (+Y
towards markers 40 and 41). Clearance `c = 0.5` everywhere the print meets the case.

| Part | Where | Size |
|---|---|---|
| Tray block | x, y in [-106.5, 106.5], z in [-3, 6] | 213 x 213, floor 3 mm |
| Plate pocket | x, y in [-100.5, 100.5], z from 0 | 201 x 201 (plate 200 + 2c) |
| Lip (aligns the lid) | ring between 201 and 205 square, z 6 to 9 | 2 mm wide, 3 mm high |
| Finger notches | x in [-15, 15], \|y\| 100.5 to 104.5, z 1 to 9.5 | through the lip, 2 mm wall left outside |
| Latch notches (tray) | \|x\| 105 to 107, y = ±60 ± 9, z -3 to 0.8 | open at the bottom, 0.8 high ledge for the hook |
| Lid shell | x, y in [-106.5, 106.5], z 6 to 61.4 | walls 3.7 (inside 205.6, 0.3 over the lip), top 2.4 (inside ceiling at z = 59) |
| Centre socket | x, y in [-46, 46], z 7 to 59 | four pad pockets 41 x 41, each to its pad top + marker/patch 0.4 + c (15.9, 25.9, 35.9, 45.9) |
| Wedge sockets | 36 x 56 blocks over each wedge (centre ±76), z 7 to 59 | pocket = the wedge grown by c: inner face at 60.5, slope moved out by c / cos(angle) |
| Snap tab | \|x\| 107.5 to 109.5 (1 mm off the wall), y = ±60 ± 8, z -2.5 to 31 | 2 mm thick, 16 wide, flexes over 33 mm; joined to the wall by a 45° gusset at z 31 to 34 |
| Hook | inward from the tab's foot to \|x\| = 105.3, flat top at z = 0.5, 45° lead-in below | catches 1.2 mm under the tray's ledge, 0.3 mm play |
| Thumb tab | \|x\| 109.5 to 112, z -2.5 to 1 | to pull the tab out |
| Label | inlaid in the lid top, z 61.0 to 61.4 | "LIDAR TARGET v2" (Ubuntu Bold, 14 mm caps) and "PULL A TAB OUT TO OPEN" (7 mm) |

Why the tabs are like that: the hook has to move 1.2 mm to pass the tray's edge. A 2 mm thick PLA strip 33 mm long bends that
far at about 0.3% strain (PLA is fine to about 2% once and about 1% over and over), so it clicks thousands of times without
cracking. A short tab cut out of the 3.7 mm wall would be at about 3% and snap.

Nothing on the lid comes lower than z = 7, so the sockets never touch the corner markers, the text or the ruler on the plate
(their tops are at z = 5.4).

## 4. Rebuilding it in SolidCraft

31 timeline items in 5 folders, 24 parameters. The print's sizes (`plate`, `pad`, `wedge_in`...) and the clearance `c` are parameters, so the sockets follow them. Tab profiles, block sizes and notch x positions are fixed numbers.

- **Tray:** `Sk_TrayOutline` + `TrayBlock`. `Sk_TrayRings` (205 and 201 squares) gives both `Lip` (the ring, starting at `tray_top`) and `Pocket` (the inner square, cut). `Sk_TrayNotches` (all 6 rectangles) gives `FingerNotches` and `LatchNotches`, each with its own `start_offset`.
- **Lid:** `Sk_LidOutline` at the ceiling (213 and 205.6 squares) gives `LidWalls` (the ring, downward) and `LidTop` (both regions, upward).
- **Negative of the print:** `Sk_Sockets` + `Sockets` (5 blocks, downward). `Sk_PadPockets` + `PadPocket1`-`4`, one cut per pad height. `Sk_WedgePockets_15_30` and `_45_60` + a cut each: triangles grown by `c`.
- **Snap tabs:** `Sk_SnapTabs_North` and `_South`: one closed profile per tab (hook, foot, thumb tab and gusset together), 2 per sketch, extruded `tab_w` symmetric. Use a sketch per side, not Mirror: in SolidCraft 0.29 mirroring a symmetric extrude from an offset plane left the tabs 6 mm wide instead of 16.
- **Label:** `Sk_Label`, `LabelCut`, `LabelInlay`, `LabelOneBody` (Combine into one `label` body; about 3 s).

Result: 3 bodies (`tray`, `lid`, `label`). The lid must be 1 shell. 3MF: same format as the target's (see `docs/lidar-target.md`), lid rotated 180° about X.

## 5. What was checked

- Interference with the real print (Blender, exact boolean): 0 mm³ between any part of the print and the tray or lid.
  Lifted by 0.4 mm still 0; by 0.6 mm it touches. So it sits with at most 0.5 mm play.
- The lid is a single closed solid; volumes as modelled: tray 169 cm³, lid 900 cm³ solid (the slicer's infill makes it far
  lighter), label 0.5 cm³.
- Slicer: Snapmaker Orca loads the tray as 1 part and the lid as 2, inside the bed. The final slice has to be checked in the app (see 1.): its command line crashes on any Orca-format project, its own exports included.
- Not yet printed. The snap tabs are the part to watch: if they are too stiff, sand the hook's lead-in; if they are loose,
  the hook depth (1.2 mm, `HookXX` sketches) can go up to 1.5.
