#!/usr/bin/env bash
# Mirror windows/ → the share folder accessible from the test PC.
# Idempotent. The PC mounts /home/mhavranek/Public/VirtualsData/ as a
# share; running this after each change keeps the PC-visible copy in
# sync without juggling git.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_WIN="$REPO_ROOT/windows"
SRC_SHARED="$REPO_ROOT/shared"
DST="/home/mhavranek/Public/VirtualsData/termpilot/windows"

for d in "$SRC_WIN" "$SRC_SHARED"; do
    if [[ ! -d "$d" ]]; then
        echo "error: source dir not found: $d" >&2
        exit 1
    fi
done
mkdir -p "$DST"

# Mirror windows/ flat into the share root. The share is laid out the
# same way as the release zip — wrapper, install scripts, lib/, shared/,
# VERSION.json all side-by-side — so the wrapper finds `shared/` next
# to itself at runtime without dev-tree path probing.
# --delete keeps the destination strict, except we preserve logs/ (where
# the wrapper mirrors its events.log back to me).
rsync -av --delete \
    --exclude 'logs/' \
    --exclude 'shared/' \
    --exclude '__pycache__/' \
    "$SRC_WIN/" "$DST/"

# Copy shared/ into the share root so it sits beside the wrapper.
rsync -av --delete --exclude '__pycache__/' \
    "$SRC_SHARED" "$DST/"

echo
echo "Synced windows/ + shared/ → $DST"
