#!/usr/bin/env bash
# Mirror windows/ → the share folder accessible from the test PC.
# Idempotent. The PC mounts /home/mhavranek/Public/VirtualsData/ as a
# share; running this after each change keeps the PC-visible copy in
# sync without juggling git.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/windows"
DST="/home/mhavranek/Public/VirtualsData/termpilot/windows"

if [[ ! -d "$SRC" ]]; then
    echo "error: source dir not found: $SRC" >&2
    exit 1
fi
mkdir -p "$DST"

# --delete keeps the destination strictly in sync. We exclude the logs/
# subfolder so wrapper-written logs on the test PC don't get clobbered.
rsync -av --delete --exclude 'logs/' --exclude '__pycache__/' \
    "$SRC/" "$DST/"

echo
echo "Synced $SRC → $DST"
