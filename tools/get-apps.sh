#!/usr/bin/env bash
# Fetch the newest iPhone (IPA) and Android (APK) builds from GitHub Actions into data/apps/,
# so this server can hand them out at /apps.html. Needs the gh CLI, logged in.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=alon24/roomscan-bimcraft
mkdir -p data/apps

fetch() {   # workflow file, artifact name
  local id tmp
  id=$(gh run list -R "$REPO" --workflow "$1" --status success -L1 --json databaseId -q '.[0].databaseId')
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
