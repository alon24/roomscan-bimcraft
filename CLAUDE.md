# roomscan-bimcraft

Scan a room with iPhone LiDAR (Apple RoomPlan), send the scan to a self-hosted web app, convert it into a BIMCraft model (walls, doors, windows, rooms, floor), and download `.bimcraft` / `.ifc`.

Developed entirely on **Linux**. There is no Mac: the iOS app is built in GitHub Actions and sideloaded.

## Architecture

```
iPhone (native, minimal)            Server (Docker, self-hosted)                 BIMCraft
┌──────────────────────┐  HTTP POST ┌────────────────────────────────┐         ┌──────────────┐
│ RoomCaptureView scan │ ─────────► │ FastAPI: store scan            │         │ desktop app  │
│ CapturedRoom → JSON  │  JSON+USDZ │ converter: JSON → script.txt   │ ──────► │ opens        │
│ upload to server     │            │ bimcraft-cli run → .bimcraft,  │ download│ .bimcraft    │
└──────────────────────┘            │   .ifc, plan.png               │         └──────────────┘
                                    │ web UI: list, 3D preview, dl   │
                                    └────────────────────────────────┘
```

**Principle:** the iOS app stays dumb (capture and send). All logic that changes often (geometry mapping, cleanup, UI) lives on the server, so it can be iterated on Linux without rebuilding or re-sideloading the app.

## Repo layout

```
roomscan-bimcraft/
├── CLAUDE.md
├── ios/
│   ├── project.yml            # XcodeGen spec (never hand-write .xcodeproj on Linux)
│   └── RoomScan/
│       ├── RoomScanApp.swift
│       ├── ScanView.swift     # UIViewRepresentable around RoomCaptureView
│       ├── Uploader.swift     # multipart POST to server
│       ├── SettingsView.swift # server URL (stored in UserDefaults)
│       └── Info.plist
├── server/
│   ├── app/
│   │   ├── main.py            # FastAPI routes + static UI
│   │   ├── convert.py         # RoomPlan JSON → BIMCraft script
│   │   ├── geometry.py        # transforms, wall endpoints, projections
│   │   └── static/            # index.html + three.js viewer
│   ├── tests/
│   │   ├── fixtures/          # sample CapturedRoom JSON files
│   │   └── test_convert.py
│   ├── requirements.txt
│   └── Dockerfile             # multi-stage: build bimcraft-cli from source, then python
├── docker-compose.yml
└── .github/workflows/ios.yml  # macOS runner → unsigned .ipa artifact
```

## iOS app

- Swift + SwiftUI, **iOS 17+** (for `CapturedRoom.floors` and `parentIdentifier` on doors/windows). Requires a LiDAR iPhone (Pro models).
- `RoomCaptureView` + `RoomCaptureViewDelegate`:
  - `captureView(shouldPresent:error:)` → return true
  - `captureView(didPresent: CapturedRoom, error:)` → final result
- `CapturedRoom` is `Codable`: encode with `JSONEncoder` and send as `scan.json`.
- Also export `capturedRoom.export(to: url, exportOptions: .parametric)` as `scan.usdz` (optional preview/archive).
- Upload: multipart `POST {serverURL}/api/scans` with fields `json`, `usdz`, `name`.
- Fallback: share sheet (`ShareLink`) for the JSON file if the server is unreachable.
- Info.plist:
  - `NSCameraUsageDescription`
  - `NSLocalNetworkUsageDescription`
  - `NSAppTransportSecurity` → `NSAllowsLocalNetworking = YES` (plain HTTP to LAN server)
- Keep the app small. No conversion logic in Swift.

## Building the IPA (no Mac)

GitHub Actions, `runs-on: macos-latest` (free on a public repo):

```bash
brew install xcodegen
cd ios && xcodegen generate
xcodebuild -project RoomScan.xcodeproj -scheme RoomScan -configuration Release \
  -sdk iphoneos -derivedDataPath build \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" build
mkdir -p Payload && cp -r build/Build/Products/Release-iphoneos/RoomScan.app Payload/
zip -r RoomScan.ipa Payload
```

Upload `RoomScan.ipa` with `actions/upload-artifact`. Trigger on push to `ios/**` and on `workflow_dispatch`.

## Installing on the iPhone (free Apple ID, sideload)

One-time, from Linux:
1. `sudo apt install usbmuxd`
2. Download the iloader AppImage from https://github.com/nab138/iloader/releases
3. Set an iPhone passcode, plug in by USB, run iloader, sign in with your Apple ID, install **SideStore** (it places the pairing file).
4. iPhone: Settings → Privacy & Security → **Developer Mode** on. Install **LocalDevVPN** from the App Store.

Each build:
5. Download `RoomScan.ipa` to the phone (from the Actions artifact, or serve it from the server at `/ipa`).
6. SideStore → My Apps → **+** → pick the IPA.

Every 7 days: refresh in SideStore (on-device). Free Apple ID limit: ~3 active sideloaded apps including SideStore.

## RoomPlan data model (what the converter reads)

All units **metres**, world space is right-handed, **Y up**.

- `walls`, `doors`, `windows`, `openings`, `objects`, `floors` — arrays of surfaces/objects
- Each surface has:
  - `identifier` (UUID)
  - `dimensions` [x, y, z]: x = width (length along the wall), y = height, z ≈ 0
  - `transform` 4×4 column-major: column 0 = local X axis (along the wall), column 3 = centre position
  - `category`, `confidence`, `completedEdges`
  - doors/windows/openings: `parentIdentifier` = host wall id (iOS 17+)
  - `curve` for curved walls (iOS 17+); treat as straight in v1
- **Verify the actual JSON key names against a real scan** before relying on them. Save the first real scan into `server/tests/fixtures/`.

## Coordinate mapping RoomPlan → BIMCraft

BIMCraft plan coordinates are millimetres, X east, Y north, Z up.

```
plan_x_mm =  world_x * 1000
plan_y_mm = -world_z * 1000
height_mm =  world_y * 1000   (relative to floor level)
```

Wall endpoints:
```
c   = transform column 3 (x, y, z)
ax  = transform column 0 (x, y, z), normalised
p1  = c - ax * width/2
p2  = c + ax * width/2   → map both to plan
```

Door/window `at` point: project the opening centre onto its host wall's centre line (in plan), so it sits exactly on the wall.

Floor level: min Y of floor surfaces, else min of (wall centre y − height/2). Subtract it from every height.

Cleanup passes (geometry.py):
- snap wall endpoints that are within ~100 mm of each other into one shared node
- optionally square near-90° corners (configurable tolerance, default off)
- drop walls shorter than ~150 mm
- if the snapped walls form a closed loop, emit one `wall` command with `"closed": true`

## BIMCraft

Repo: https://github.com/IPedrax/BIMCraft (Rust, MIT/Apache-2.0). Engine/CLI need Rust 1.90+, desktop needs 1.95.

Local clone is at `../BIMCraft`:
```bash
cd ../BIMCraft && cargo build --release && cd -
export BIMCRAFT_CLI=$(realpath ../BIMCraft/target/release/bimcraft-cli)
```

Script format: one command per line, `id {json}`, lengths in **mm**:
```
wall {"points": [[0,0],[4200,0],[4200,3600],[0,3600]], "closed": true, "type": "Exterior - 300mm"}
door {"at": [1800, 0]}
window {"at": [4200, 1500]}
room {"point": [2100, 1800], "name": "Bedroom"}
floor {}
```

Run:
```bash
$BIMCRAFT_CLI run --script room.txt --save room.bimcraft --save room.ifc --save plan.png
$BIMCRAFT_CLI info room.bimcraft
$BIMCRAFT_CLI commands          # full command + parameter reference
```

**Before adding parameters** (wall height, wall type/thickness, door/window width/height/sill, level height), check `bimcraft-cli commands` output; don't guess parameter names. A script stops at the first failing line and names it.

Notes:
- A wall's exterior is to the **left** of start→end; draw clockwise for outward-facing walls.
- BIMCraft also has an MCP server; possible later integration.

## Server

- Python 3.12, FastAPI, uvicorn. Storage: `data/scans/<id>/` with `scan.json`, `scan.usdz`, `script.txt`, `room.bimcraft`, `room.ifc`, `plan.png`, `meta.json`.
- Routes:
  - `POST /api/scans` — upload (multipart), convert immediately, return id + status
  - `GET  /api/scans` — list
  - `GET  /api/scans/{id}` — meta + conversion log/errors
  - `POST /api/scans/{id}/convert` — re-run conversion (after converter changes)
  - `GET  /api/scans/{id}/{file}` — download artifacts
  - `GET  /` — web UI
- Web UI: scan list, plan.png, three.js 3D preview built from the JSON (boxes for walls/doors/windows), download buttons, re-convert button.
- Run `bimcraft-cli` via `subprocess` with a timeout; capture stderr into the scan's log.
- Dockerfile: stage 1 `rust:1` builds `bimcraft-cli` from the BIMCraft repo (pin a commit); stage 2 `python:3.12-slim` copies the binary.
- `docker-compose.yml`: one service, port 8000, volume `./data:/app/data`.
- No auth in v1 (LAN only). Leave a hook for a token header later.

## Testing

- `pytest server/tests` — converter unit tests on fixture JSONs: endpoint math, snapping, loop closing, door projection.
- Integration test: fixture → script → `bimcraft-cli run` must succeed (skip if `BIMCRAFT_CLI` is unset).
- Write a synthetic fixture (simple 4×3.6 m room, one door, one window) first so work can start before a real scan exists.

## Task order

1. Server skeleton + converter with a **synthetic fixture**; produce a valid `.bimcraft` via `bimcraft-cli`.
2. Dockerfile + compose; verify the conversion runs in the container.
3. iOS app: XcodeGen project, scan view, JSON export, share-sheet fallback.
4. GitHub Actions workflow → unsigned IPA artifact.
5. Sideload, do a real scan, add it as a fixture, fix key names and mapping.
6. Upload from the app to the server; web UI list + downloads.
7. Three.js preview; cleanup passes (snap, square corners).
8. Later: multi-room scans (`CapturedStructure`, iOS 17), wall/door sizes from scan dimensions, MCP integration.

## Conventions

- Keep the iOS code minimal; push logic to the server.
- Commit fixtures (real scans are small JSON).
- Exact commands in docs; the user prefers concise, step-by-step instructions.
