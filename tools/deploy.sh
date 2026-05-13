#!/usr/bin/env bash
# Deploys relay/ to the live relay over FTPS using ~/.netrc.
# Backs up live files first into ./server-logs/backup-<ts>/.
# NEVER touches: config.php, data/, logs/.
#
# FTP host resolution (in order):
#   1. $TERMPILOT_FTP_HOST env var
#   2. ~/.config/termpilot/ftp-host file
#   3. interactive prompt (answer is saved for next time)

set -euo pipefail

# shellcheck source=lib-ftp-host.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib-ftp-host.sh"
resolve_ftp_host
resolve_ftp_credentials
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)/relay"
BACKUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)/server-logs/backup-$(date +%Y%m%dT%H%M%S)"

# TLS is required (--ssl-reqd) AND cert verification is ON by default.
# A MITM at deploy time can otherwise replace relay.php with a token-
# logging variant. Some shared hosts present a cert chain that doesn't
# match the FTP hostname (the cert is for the underlying cluster host,
# not ftp.yourdomain) — in that case set TERMPILOT_FTPS_INSECURE=1 to
# drop verification, but treat that as an opt-in to a "TLS but trust
# whoever answers" mode and only do it from networks you trust.
CURL_FLAGS=(--ssl-reqd --connect-timeout 15 --max-time 120)
if [[ "${TERMPILOT_FTPS_INSECURE:-}" == "1" ]]; then
  echo "WARNING: TERMPILOT_FTPS_INSECURE=1 — FTPS cert verification DISABLED" >&2
  echo "         A MITM on this path can swap relay.php for a token-logger." >&2
  CURL_FLAGS+=(--insecure)
fi
CURL=(curl -sS --netrc "${CURL_FLAGS[@]}")

# Portable file-size: GNU stat takes -c%s, BSD stat (macOS) takes -f%z. Skip
# both and just count bytes — works the same everywhere.
filesize() { wc -c < "$1" | tr -d ' '; }

mkdir -p "$BACKUP_DIR/lib"

echo "Backing up live files into $BACKUP_DIR"
for f in relay.php index.html .htaccess \
         manifest.webmanifest sw.js \
         icon-192.png icon-512.png icon-maskable-192.png icon-maskable-512.png; do
  if "${CURL[@]}" -o "$BACKUP_DIR/$f" "ftp://$FTP_HOST/$f" 2>/dev/null; then
    printf '  pulled %s (%s bytes)\n' "$f" "$(filesize "$BACKUP_DIR/$f" 2>/dev/null || echo 0)"
  else
    printf '  (could not pull %s — may not exist yet)\n' "$f"
  fi
done

echo
echo "Ensuring remote lib/ + lib/vendor/ exist"
"${CURL[@]}" -Q "MKD lib" "ftp://$FTP_HOST/" >/dev/null 2>&1 \
  && echo "  created lib/" \
  || echo "  lib/ already exists or MKD ignored"
"${CURL[@]}" -Q "MKD lib/vendor" "ftp://$FTP_HOST/" >/dev/null 2>&1 \
  && echo "  created lib/vendor/" \
  || echo "  lib/vendor/ already exists or MKD ignored"

echo
echo "Uploading files"
upload() {
  local local_path="$1" remote_path="$2"
  "${CURL[@]}" -T "$local_path" "ftp://$FTP_HOST/$remote_path"
  printf '  uploaded %s -> %s (%s bytes)\n' "$local_path" "$remote_path" "$(filesize "$local_path")"
}

# Stamp sw.js with a build version so the browser detects a byte-level
# change on every deploy (driving the "new version available" banner).
# We patch a temp copy and upload that — source tree stays clean.
TERMPILOT_VERSION="$(date -u +%Y%m%dT%H%M%SZ)"
# Portable mktemp: GNU's --suffix= isn't accepted by BSD/macOS mktemp.
# `mktemp -t prefix` writes to $TMPDIR with a generated suffix; works on
# both. The filename doesn't need a .js extension since curl uploads by
# path argument, not by stripping the local name.
SW_STAMPED="$(mktemp -t termpilot-sw.XXXXXX)"
trap 'rm -f "$SW_STAMPED"' EXIT
sed "s/__TERMPILOT_VERSION__/${TERMPILOT_VERSION}/g" "$SRC_DIR/sw.js" > "$SW_STAMPED"
if ! grep -q "${TERMPILOT_VERSION}" "$SW_STAMPED"; then
  echo "  ERROR: sw.js version stamp failed (placeholder not found?)" >&2
  exit 1
fi
echo "  sw.js stamped with version ${TERMPILOT_VERSION}"

upload "$SRC_DIR/relay.php"                 "relay.php"
upload "$SRC_DIR/index.html"                "index.html"
upload "$SRC_DIR/.htaccess"                 ".htaccess"
upload "$SRC_DIR/lib/crypto.js"             "lib/crypto.js"
upload "$SRC_DIR/lib/session.js"            "lib/session.js"
upload "$SRC_DIR/lib/keyboard.js"           "lib/keyboard.js"
upload "$SRC_DIR/lib/index.css"             "lib/index.css"
upload "$SRC_DIR/lib/index.js"              "lib/index.js"
# Vendored third-party assets (see relay/lib/vendor/NOTICE).
upload "$SRC_DIR/lib/vendor/xterm.min.css"  "lib/vendor/xterm.min.css"
upload "$SRC_DIR/lib/vendor/xterm.min.js"   "lib/vendor/xterm.min.js"
upload "$SRC_DIR/lib/vendor/jsQR.js"        "lib/vendor/jsQR.js"
# PWA assets
upload "$SRC_DIR/manifest.webmanifest"      "manifest.webmanifest"
upload "$SW_STAMPED"                        "sw.js"
upload "$SRC_DIR/icon-192.png"              "icon-192.png"
upload "$SRC_DIR/icon-512.png"              "icon-512.png"
upload "$SRC_DIR/icon-maskable-192.png"     "icon-maskable-192.png"
upload "$SRC_DIR/icon-maskable-512.png"     "icon-maskable-512.png"

# Trim old backup dirs — keep the 5 newest (including the one we just
# made), delete the rest. Each backup is ~700KB; left unchecked the
# server-logs/ tree grows monotonically.
BACKUP_ROOT="$(dirname "$BACKUP_DIR")"
ls -1dt "$BACKUP_ROOT"/backup-* 2>/dev/null | tail -n +6 | xargs -r rm -rf

echo
echo "Done. Backup at $BACKUP_DIR"
