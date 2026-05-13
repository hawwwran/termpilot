#!/usr/bin/env bash
# Re-fetch the vendored browser deps in relay/lib/vendor/.
#
# Run from a clean tree, then `git diff` to inspect the new bytes
# before committing. Checksums are pinned in relay/lib/vendor/NOTICE —
# update both this script and the NOTICE in lockstep when bumping a
# version.

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
VENDOR=relay/lib/vendor
mkdir -p "$VENDOR"

# version → (url, dest, expected sha256)
declare -a JOBS=(
  "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css|$VENDOR/xterm.min.css|64ee6c4db69b4224d3362aced0fd4cdd620e0e60b3d01566450ae2d4b9e81849"
  "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js|$VENDOR/xterm.min.js|fc1dd31b221e3e5f929486e07a80b477a8aaf9dce2b4f9c3ffe7dd25f370655d"
  "https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js|$VENDOR/jsQR.js|bc40c8a15196236b2314db0856f72ca0b49980cd5413b8c852a7349f5fee0859"
)

ok=1
for job in "${JOBS[@]}"; do
  IFS='|' read -r url dest expected <<<"$job"
  echo "→ $url"
  curl -sSfL -o "$dest" "$url"
  actual="$(sha256sum "$dest" | awk '{print $1}')"
  if [[ "$actual" == "$expected" ]]; then
    printf '  ok  %s\n' "$dest"
  else
    printf '  FAIL %s\n    expected %s\n    actual   %s\n' "$dest" "$expected" "$actual" >&2
    ok=0
  fi
done

if [[ $ok -ne 1 ]]; then
  echo
  echo "One or more checksums failed. If you intended to update a pinned" >&2
  echo "version, update both this script and relay/lib/vendor/NOTICE." >&2
  exit 1
fi

echo
echo "All vendor files match pinned checksums."
