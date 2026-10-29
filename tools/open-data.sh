#!/bin/sh
# Open the server's data folder (scans, app files) in the file manager. Files in it belong to root (the container wrote them):
# delete them from the web page (Storage) or with sudo.
cd "$(dirname "$0")/.." || exit 1
dir="$PWD/data/${1:-scans}"
[ -d "$dir" ] || dir="$PWD/data"
echo "$dir"
xdg-open "$dir" >/dev/null 2>&1 &
