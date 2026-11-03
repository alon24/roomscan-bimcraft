# The room model: how fixes are kept

**The rule: a fix is stored as a fix, and rebuilding can never undo it.**

| Layer | Holds | Changed by | File |
|---|---|---|---|
| Evidence | Photos, LiDAR, phone positions | Nobody | `photos/`, `mesh.bin`, ... |
| Points | Clicks and their types | The user | `walk_points.json`, `walk_result.json` |
| Elements | Walls, doors, windows, open stretches | The server proposes, the user confirms | `model.json` |
| Outputs | Plan, BIMCraft, IFC, Blender | Always worked out | `layout.json`, `room.bimcraft`, ... |

## Walls
- A wall is a line, not two corners. Corners are where neighbouring walls cross, or half way between their ends if they run side by side.
- `points`: the clicks it rests on. `pinned`: moved by hand. `confirmed`: the user says it's right. `open`: a stretch with no wall. `length_m`: what the tape says.
- A wall that is neither pinned nor confirmed follows its points. Assigning a point to a wall hands it back to its points, unless it's confirmed.
- Moving a wall so it runs side by side with a neighbour at another depth adds a short wall joining them, as for a recess.
- Tape lengths: each wall may slide across its own direction, as little as it can, confirmed walls least (`elements._solve`). The lines themselves stay, so removing a length undoes it.

## Doors and windows
Live in `layout.json` under `openings`. `marked` means set or confirmed by hand. Others come from door and window clicks and follow them, and a marked one wins over what clicks make in the same place. Door and window clicks never shape walls.

An opening stands on the nearest wall running the same way (found each time, not stored). When that wall moves, turns, is split or slides for a tape length, the opening goes with it: position, `ends` and `corners` move as the wall did and it stays on the wall's centre line. If the page moves the door itself, that wins. Delete a wall and its doors stay where they were.

## Actions
`POST /api/scans/{id}/layout/action` with `{"op": ...}`. Every one answers with the whole room, and can be undone.

| op | Arguments |
|---|---|
| `confirm_wall` | `wall`, `on` |
| `set_open` | `wall`, `on` |
| `not_a_wall` | `wall` |
| `split_wall` | `wall`, `at`: world `[x, z]`, or the middle |
| `move_wall` | `wall`, `a`, `b`: world `[x, z]` |
| `assign_point` | `point`, `wall` or null |
| `set_length` | `wall`, `m` or null |
| `confirm_opening` | `index`, `on` |

- `GET .../layout/state`: the room, the points, the checks.
- `PUT .../layout`: the plan changed directly. Each stretch finds the wall it was, and that wall moves and keeps what's known about it.
- `POST .../layout/undo`: takes back the last change, also from an earlier session. Point edits are included.
- `POST .../walk/rebuild`: on request. With confirmed walls they stay and the rest follow their points. With none, the walls start over from the points. Marked doors and windows always stay.
- `edits.jsonl` holds the last 300 states for undo. `edits_history.jsonl` keeps what was done, for good. `walk_deleted.json` keeps deleted points.

A phone or an AI should use these same actions, so that an AI can only suggest and the user confirms.
