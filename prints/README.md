# Prints: everything for each printed part

One folder per part, the same four folders in each. This is the only copy: `~/designs/lidar-target` and
`~/designs/lidar-case` are links to these folders, so SolidCraft opens and saves the same files.

```
prints/
├── lidar-target/          the LiDAR calibration print, v2 (docs/lidar-target.md)
│   ├── 3mf/               print this: lidar-target-v2-4colour.3mf
│   ├── cad/               lidar-target-v2.solidcraft (the design), lidar-target-v2.step
│   ├── stl/               one mesh per colour
│   └── images/            renders
└── lidar-case/            the carry case for it (docs/lidar-case.md)
    ├── 3mf/               print these: lidar-case-tray.3mf, lidar-case-lid-2colour.3mf
    ├── cad/               lidar-case.solidcraft (the design), lidar-case.step
    ├── stl/               tray, lid white, lid black
    └── images/            closed, open, cut in half, snap tab
```

The server's download copies of the 3MFs are in `server/app/static/prints/`: copy a new 3MF there too when one changes.
