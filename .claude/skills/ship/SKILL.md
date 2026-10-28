---
name: ship
description: Release loop for roomscan-bimcraft. Use after any change to server/ or ios/ to test, version, commit, push, wait for the iOS build, fetch the IPA and redeploy the server. Also use when asked to "ship", "deploy", "build the app" or "get me the new IPA".
---

# Ship roomscan-bimcraft

The phone is dumb and the server is smart. Put new logic in `server/` when you can: a server change needs only step 5, no new IPA and no re-sideload.

## 1. Test
```bash
cd server && BIMCRAFT_CLI=$(realpath ../../BIMCraft/target/release/bimcraft-cli) .venv/bin/python -m pytest -q tests
```
- Python is 3.12 in `server/.venv` (made with `uv venv --python 3.12`). System Python is 3.10.
- Without `BIMCRAFT_CLI` the BIMCraft integration tests are skipped, so a passing run proves less. Set it.
- Check any new `bimcraft-cli` script syntax first: `bimcraft-cli commands` lists the parameters. Never guess names. A script stops at its first failing line and names it.

## 2. README
If the change is something a person would notice, update `README.md` first: how-to text, a Changelog line, and a fresh screenshot in `docs/img` when the web page changed (command at the bottom of the README).

## 2b. Version
Edit `VERSION` (minor bump for a feature). The build number is the git commit count, so the phone, CI and `deploy.sh` agree.

## 3. Commit and push
Small commit, message says what changed for the user. End it with the attribution trailers from the session's system reminder (they change with the model, so don't copy an old one). Push starts the iOS build in GitHub Actions (only `ios/**` and `workflow_dispatch` matter, but every push is fine).

## 4. IPA (only when `ios/` changed)
```bash
id=$(gh run list -R alon24/roomscan-bimcraft -L1 --json databaseId -q '.[0].databaseId')
until s=$(gh run view $id -R alon24/roomscan-bimcraft --json status,conclusion -q '.status+" "+.conclusion'); [[ $s == completed* ]]; do sleep 20; done; echo $s
rm -f ~/Downloads/roomscan-ipa/RoomScan.ipa
gh run download $id -R alon24/roomscan-bimcraft -n RoomScan.ipa --dir ~/Downloads/roomscan-ipa
```
Run the wait in the background. Always download after a green build, delete the old file first, and tell the user the build number. They sideload by hand with iloader "Install IPA" (SideStore/Sideloader sign-in was broken with Apple error -80046; re-check later). Free-account signing lasts 7 days.

## 5. Server
```bash
PORT=8010 ./deploy.sh      # prints "Server vX build N"
```
- Chrome caches the page: check changes at `http://localhost:8010/?v=N` (N = anything new).
- Files in `./data` are root-owned (written by the container). `rm -rf data` can fail; use `docker compose run` or sudo.
- `docker compose exec -T server python -c "import ..."` checks a new dependency exists in the image, not only in the venv.
- Read phone problems with `curl localhost:8010/api/issues` instead of asking the user to describe the screen.

## Rules learned the hard way
- Never `pkill -f` a pattern that appears in your own command line.
- `RoomCaptureViewDelegate` needs `init?(coder:)` and `encode(with:)` or CI fails.
- Object Capture on iOS only has `.preview`/`.reduced` detail.
- Mesh anchors never arrived on the user's iPhone 13 Pro; the depth point cloud is the primary path.
- `[hidden]` is overridden by `display:flex` classes: the page has `[hidden]{display:none!important}`.
- Setting `textContent` on an element wipes children you prepended earlier.
- The user can delete scans from another browser while you work. If the scan list shrinks, check `docker compose logs server | grep DELETE` before assuming a bug.
- Verify UI changes in a real browser (Claude in Chrome), click at the real positions in a screenshot, and close the tab afterwards.
- Real scans are the only test of guessed JSON key names: save the first real one into `server/tests/fixtures/`.
- The server image is also built in CI (`server-image.yml`, amd64 + arm64 on native runners, pushed to ghcr). Another computer runs `docker compose -f docker-compose.prebuilt.yml pull && up -d`; `deploy.sh` is only for building from source.
- Verify in Chrome by calling the page: set a file input with a `DataTransfer` and dispatch `change`; `read_page`/JS output that contains a query string comes back as `[BLOCKED]`, so use screenshots for that.
- A background wait on `gh run list -L1` picks whichever run is newest at that moment: after a push, `sleep 15` first or it watches the previous build.
