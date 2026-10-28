# Live guided capture (two-way, WebSocket)

Goal: while the user scans a room, frames go to the server **as they are taken**. The server works out what is still missing and sends
a small map back, so the phone can show it and say what to capture next. Same channel is used to **add to an existing scan** later.
The phone stays dumb: it captures, sends, and draws what the server says. All the thinking is on the server.

Side effects we want: no big upload at the end (the frames are already there), no 1000-file problem, a saved partial scan if the Wi-Fi drops.

## Coordinates
Everything in messages is in the phone's **ARKit world frame of this session** (metres, Y up, as in `photos.json` transforms).
For "add to a scan" the server aligns the new session to the old one and converts back, so the phone only ever sees its own frame.

## Start
`POST /api/live`  body `{ "kind": "room" | "sweep" | "object", "name": str, "project": str, "info": {...}, "resume_scan": null | scan_id }`
-> `{ "scan_id": "...", "ws": "/ws/live/<scan_id>" }`. The scan folder is created at once with `meta.status = "capturing"`.

## WebSocket `/ws/live/{scan_id}`
Phone -> server

| message | form | content |
|---|---|---|
| hello | JSON | `{type:"hello", resume_after: n, app: "v0.44.0 build 120"}` (n = last frame the server acknowledged, 0 at the start) |
| frame | **binary** | `[u32 big-endian header_len][header JSON][jpeg bytes][depth bytes]`. Header: `{type:"frame", n, jpeg_len, depth_len, photo:{...}}` where `photo` is exactly one entry of `photos.json` as the phone writes it today (file, timestamp, transform, intrinsics, width, height, exposure_duration, ..., depth_file, depth_width, depth_height). Depth = little-endian float32 metres, rows as `depth_width x depth_height` (the existing `.depth` file). |
| pose | JSON | `{type:"pose", transform:[[16 floats column-major as 4 columns of 4]], t}` about 5 per second, tiny; lets the server place "you are here" |
| finish | JSON | `{type:"finish", info:{...}}` -> server builds the model from the stored frames |
| cancel | JSON | `{type:"cancel"}` -> scan kept as a draft, not built |

Server -> phone (JSON)

| message | content |
|---|---|
| ack | `{type:"ack", n}` after the frame is safely stored (the phone may delete its copy) |
| map | every ~1.5 s, only if something changed: `{type:"map", seq, percent, summary, grid:{x0, z0, cell, w, h, data}, you:[x,y,z], needs:[...]}` |
| state | `{type:"state", status:"capturing"|"building"|"ready"|"error", scan_id, message?}` |
| aligned | patch mode: `{type:"aligned", ok, rms_mm, message}` once the new session is registered to the old scan |

`grid`: a top-down map of the room in the phone's frame. Cell size `cell` metres (0.10), origin `(x0, z0)` = world x and z of cell (0,0), row-major,
`data` = base64 of `w*h` bytes: 0 = not seen, 1 = seen but thin (few views or far), 2 = good, 3 = free floor the phone stood/looked across (optional).
Keep it under about 8 KB (cap w and h at 96).

`needs`: at most 5, most important first. Each: `{id, kind: "gap"|"thin"|"far"|"blur"|"ceiling"|"floor", text, target:[x,y,z], stand:[x,y,z]|null, look:[dx,dy,dz]|null, priority}`.
`text` is plain words ("The right wall is only seen from far away. Walk to the corner and film it for 3 seconds."). `target` is the world point to aim at, `stand` where to stand, `look` the aim direction.
The phone draws target markers in AR (like the Measure pins), an arrow when off screen, the minimap, and the top need's `text`.

## Server work per frame
Store `pNNN.jpg`, `pNNN.depth`, append to `photos.json`. Integrate depth into a coarse voxel/cell structure (5 cm), keeping per-cell hit count and which viewing sectors saw it.
Needs come from: unseen directions around the stand point (sweep), unseen floor/wall/ceiling cells next to seen ones (frontier), cells seen from far or by few views, blurry frames.
`percent` = covered share of the surfaces the server believes make up the room.

## Finish
Build `scan.ply` and the model from the stored frames with the existing pipeline (`frames` / `meshscan` / `sweep`), then `state: ready`.

## Add to an existing scan (patch)
`resume_scan = <id>`. New frames are stored in the same scan with `session: 2`. After a few frames the server registers the new session to the old
geometry (gravity is shared, so only yaw and translation are unknown; ICP on the depth points, as in `fit.py`) and sends `aligned`. Maps then show the old scan's
coverage in the new session's frame. A rebuild fuses both sessions.

## Phone resilience
Frames are written to a temp folder first and sent in order; if the socket drops the phone reconnects with `hello.resume_after` and resends. Nothing is lost if the Wi-Fi blinks.


## Contract rules (decided; both sides code against these)
1. **The server names the files.** It ignores `photo.file` / `photo.depth_file` from the header, numbers the frame `pNNN` continuing from the scan's highest number, and rewrites both fields in the stored `photos.json` entry. (Patch mode would otherwise collide with the old session's `p001.jpg`.)
2. **Acks are cumulative**: `ack n` = every frame up to n is on disk. A frame number the server already stored is dropped silently (idempotent resend). `hello.resume_after` is the last cumulative ack the phone saw.
3. **Patch poses**: the new session's raw transforms are kept in `sessions.json` with a per-session transform. Once `aligned`, the new frames' `transform` in `photos.json` is rewritten into the OLD scan's frame, because every consumer (`frames`, `meshscan`, `sweep`, `panorama`, `quality`) reads `transform` directly.
4. **Needs are stable**: a need keeps its `id` and position until it is satisfied or clearly outranked (hysteresis). The list is never re-ranked from scratch every update.
5. **`finish` runs in a background task**, not in the socket handler: send `state: building`, keep building if the socket drops; the phone can poll `GET /api/scans/{id}` (`meta.status`). Per-frame integration and map computation run in a worker thread; the ack is sent right after the disk write.
6. **Abandoned captures**: `meta.status = "capturing"` scans get a "Build what I have" action (`POST /api/scans/{id}/finish`) and auto-finish after 10 minutes without a socket. The list, the viewer and `/print` must not crash on that status.
7. **v1 scope: `room` and `sweep` only.** Ring and object scans keep the batch upload. Live mode also falls back to the batch upload if the socket cannot connect or the send queue falls far behind: capture is never blocked on the network.
8. **Needs v1** (keep it simple): a top-down 10 cm grid; sweep gaps by the phone dial's sectors (24 sectors x walls/ceiling/floor); wall cells seen only from more than 3 m or by fewer than 3 views; frontier = seen floor/wall cells next to unseen ones; at most 5 needs, merged spatially.
9. **Bandwidth v1**: keep the float32 depth as is (about 200 KB per frame) with the JPEG at about 2 frames per second (about 1 MB/s on Wi-Fi). The phone shows "N frames waiting to send".

## Verification on Linux
`tools/live_replay.py` streams an existing scan's frames over the WebSocket (for example the 28-frame sweep `6eb3b7f85b31`). Tests assert: acks arrive; a mid-stream disconnect + resume loses or duplicates nothing; `finish` gives the same model as the batch path for the same frames. Live maps are also pushed to the web page through `/ws/events`, so a replay can be watched in Chrome.


## Changes

### 2026-10-29 Offline first, and how the server settled the details
**Offline is the default-safe path.** The phone captures with no network exactly as before (frames on the phone, the batch `POST /api/scans` later). Live is an optional extra: capture never waits for it.

- A live scan that loses the connection **continues on the phone**. Frames the server has not acknowledged are sent after a reconnect (`hello.resume_after`).
- If the phone stops while offline, the missing frames complete the live scan with **`POST /api/scans/{id}/frames`** (multipart: `photos` = the jpg and `.depth` files, `photos_meta` = the photos.json manifest). The server matches by `timestamp`, skips frames it already stored, names the new ones itself, does **not** start a build, and answers `{added, skipped, total}`. Only for a scan whose status is `capturing` (else 409). Then finish with `POST /api/scans/{id}/finish` (answers `{status:"building"}`).
- **Patching offline: `POST /api/scans/{id}/add`** (multipart, the same fields as the batch upload: `photos`, `photos_meta`, optional `info`). The frames are stored as session N, registered to the old scan with the same code the live patch mode uses, rewritten into the old frame, and the scan is rebuilt. Answer: `{aligned, rms_mm, message, added, skipped, status?}`. If the frames cannot be matched (`aligned:false`) nothing in `photos.json` changes and the old model stays; the frames are kept in `sessions.json`.

**What the implementation does where the doc was open**
- `POST /api/live` is shared with the old RoomPlan push (a partial CapturedRoom, never stored). The server tells them apart by the body: `kind` = `"room"` or `"sweep"` starts a capture, anything else is the old behaviour.
- `hello` is answered with `ack` (the cumulative number the server holds, 0 at the start), `state` (`capturing` or `building`) and, once there is something to show, a `map`. So after `hello` read the ack before counting acks for frames.
- Extra server -> phone message: `{type:"error", message}` for a malformed frame or an unknown message; the socket stays open. A frame after `finish` is refused with an `error`.
- `aligned` is sent each time a registration attempt finishes (every 6 new frames until it succeeds). `ok:false` messages carry the advice in plain words (film an area you already scanned; or, when the room looks the same from two directions, film something distinctive).
- Before `aligned`, a patch session's frames are held in `sessions.json` only; `photos.json` never contains a transform in the wrong frame. Maps before alignment cover the new session alone and carry a first need `{id:"align", kind:"gap", priority:3}`.
- Need ids look like `gap-3`. A need keeps its id and place (target, stand, look) until it is satisfied: gone from the candidates for 3 updates in a row.
- `cancel` keeps the scan as a draft: `meta.status` stays `capturing` (with `live.json.cancelled`), so the list shows it and "Build what I have" works.
- `meta.status` values for a live scan: `capturing` -> `building` -> `ok` | `error`. A patch session sets the old scan to `capturing` and puts the old status back if nothing new could be added.
- Browser viewers get `{type:"live_map", id, name, map}` on `/ws/events` (and the latest one when a page opens), then `{type:"live_end_map", id}`.
- Auto-finish: a capture with no socket and no news for 10 minutes (`LIVE_IDLE_S` seconds) is built by the server.
