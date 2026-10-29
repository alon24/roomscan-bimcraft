#!/bin/sh
# Rebuild and restart the server with the version from VERSION and the build number = git commit count.
# The iOS build in CI uses the same two numbers, so matching numbers mean matching code.
cd "$(dirname "$0")"
export APP_VERSION=$(cat VERSION)
export APP_BUILD=$(git rev-list --count HEAD)
export DATA_HOST_PATH="$PWD/data"   # shown on the web page (Storage) so you can find the scans folder on this computer
docker compose up -d --build
echo "Server v$APP_VERSION build $APP_BUILD"
