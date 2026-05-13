#!/usr/bin/env bash
# Stage a flat per-platform release tree and zip it.
#
# Usage:
#   tools/build-release.sh linux     # → dist/termpilot-linux-macos.zip
#   tools/build-release.sh windows   # → dist/termpilot-windows.zip
#
# The output zip is *flat*: extracting it gives the wrapper, install
# scripts, lib/, shared/, VERSION.json, README.md side-by-side at the
# install root. No `linux/` or `windows/` subdir leaks into the zip.

set -euo pipefail

PLAT="${1:-}"
if [[ "$PLAT" != "linux" && "$PLAT" != "windows" ]]; then
    echo "usage: $0 linux|windows" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
DIST_DIR="$REPO_ROOT/dist"
STAGE_DIR="$DIST_DIR/staging-$PLAT"

case "$PLAT" in
    linux)   ZIP_NAME="termpilot-linux-macos.zip" ;;
    windows) ZIP_NAME="termpilot-windows.zip"     ;;
esac

echo "==> Staging $PLAT release under $STAGE_DIR"
mkdir -p "$DIST_DIR"
rm -rf "$STAGE_DIR" "$DIST_DIR/$ZIP_NAME"
mkdir -p "$STAGE_DIR"

# Copy the platform tree (flat into staging root). We exclude tests/
# from the zip — they aren't useful to end users and would inflate it.
# rsync with --exclude is the cleanest way to express this.
rsync -a \
    --exclude='tests/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$REPO_ROOT/$PLAT/" "$STAGE_DIR/"

# Copy shared/ verbatim (the wrappers find it next to themselves at
# runtime via the same probe used in dev).
rsync -a \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$REPO_ROOT/shared" "$STAGE_DIR/"

# VERSION.json at repo root is canonical (CI rewrites it pre-build).
cp "$REPO_ROOT/VERSION.json" "$STAGE_DIR/"

# Platform-specific README. The linux/ and windows/ subtrees both have
# their own README.md; that's what ships. (The repo-root README is an
# umbrella overview, not an end-user doc.)
if [[ -f "$REPO_ROOT/$PLAT/README.md" ]]; then
    cp "$REPO_ROOT/$PLAT/README.md" "$STAGE_DIR/README.md"
fi

# Pack from inside the staging dir so paths in the zip are top-level.
(
    cd "$STAGE_DIR"
    # -X strips extended attributes; -r recurses; -q quiet.
    zip -rqX "$DIST_DIR/$ZIP_NAME" .
)

echo "==> Built $DIST_DIR/$ZIP_NAME"
ls -l "$DIST_DIR/$ZIP_NAME"
echo "==> Contents (top level):"
(cd "$STAGE_DIR" && find . -maxdepth 2 -mindepth 1 | sort | sed 's|^\./|  |')
