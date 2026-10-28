#!/usr/bin/env bash
# Fetch the newest iPhone (IPA) and Android (APK) builds from GitHub Actions into data/apps/,
# so this server can hand them out at /apps.html. Needs the gh CLI, logged in.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=alon24/roomscan-bimcraft
if ! mkdir -p data/apps 2>/dev/null; then
  # data/ is owned by root when the container wrote it: make the folder from inside the container, open to the current user
  docker compose run --rm --no-deps --entrypoint sh server -c 'mkdir -p /app/data/apps && chmod 777 /app/data/apps'
fi

fetch() {   # workflow file, artifact name
  local id tmp
  id=$(gh run list -R "$REPO" --workflow "$1" -L 20 --json databaseId,conclusion -q '[.[] | select(.conclusion == "success")][0].databaseId // empty')
  [ -n "$id" ] || { echo "no successful $1 run yet"; return 0; }
  tmp=$(mktemp -d)
  gh run download "$id" -R "$REPO" -n "$2" --dir "$tmp"
  mv -f "$tmp/$2" "data/apps/$2"
  rmdir "$tmp"
  echo "got $2 (run $id)"
}

fetch ios.yml RoomScan.ipa
fetch android.yml RoomScan.apk
ls -l data/apps
