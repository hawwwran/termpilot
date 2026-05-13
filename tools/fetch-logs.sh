#!/usr/bin/env bash
# Fetches the deployed relay's logs via FTP into ./server-logs/.
# Logs are not exposed over HTTP (deny rule in .htaccess), so FTP is the
# intended way to pull them.
#
# Usage:
#   ./tools/fetch-logs.sh           # download relay.log + relay.log.* into server-logs/
#   ./tools/fetch-logs.sh --tail    # print combined tail to stdout
#   ./tools/fetch-logs.sh --clean   # remove all server-side logs (use sparingly)

set -euo pipefail

# shellcheck source=lib-ftp-host.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib-ftp-host.sh"
resolve_ftp_host
resolve_ftp_credentials
REMOTE_DIR="${TERMPILOT_FTP_LOG_DIR:-/logs}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)/server-logs"

# TLS is required (--ssl-reqd) AND cert verification is ON by default.
# Set TERMPILOT_FTPS_INSECURE=1 to opt into "TLS but trust whoever
# answers" mode if your shared host serves a mismatched cert chain.
# See tools/deploy.sh for the rationale.
CURL_FLAGS=(--ssl-reqd --connect-timeout 15 --max-time 60)
if [[ "${TERMPILOT_FTPS_INSECURE:-}" == "1" ]]; then
  echo "WARNING: TERMPILOT_FTPS_INSECURE=1 — FTPS cert verification DISABLED" >&2
  CURL_FLAGS+=(--insecure)
fi
CURL=(curl -sS --netrc "${CURL_FLAGS[@]}")

mkdir -p "$LOCAL_DIR"

case "${1:-}" in
  --clean)
    echo "Removing remote logs..."
    # List, then DELE each file we recognise (relay.log, .1, .2, ...).
    listing=$("${CURL[@]}" "ftp://$FTP_HOST$REMOTE_DIR/" || true)
    while read -r line; do
      name=$(awk '{print $NF}' <<< "$line")
      case "$name" in
        relay.log|relay.log.[0-9]|.rotlock)
          "${CURL[@]}" -Q "DELE $REMOTE_DIR/$name" "ftp://$FTP_HOST$REMOTE_DIR/" >/dev/null 2>&1 \
            && echo "  deleted $name" || echo "  (couldn't delete $name)"
          ;;
      esac
    done <<< "$listing"
    ;;

  --tail)
    # Combine all archives oldest-first, then active log; pipe last lines.
    files=$("${CURL[@]}" "ftp://$FTP_HOST$REMOTE_DIR/" \
      | awk '{print $NF}' | grep -E '^relay\.log(\.[0-9]+)?$' || true)
    archives=$(printf '%s\n' "$files" | grep -E '^relay\.log\.[0-9]+$' \
      | sort -t. -k3 -nr || true)
    ordered=("$archives" relay.log)
    # Stream-concatenate via curl, no local files.
    for f in $archives relay.log; do
      "${CURL[@]}" "ftp://$FTP_HOST$REMOTE_DIR/$f" 2>/dev/null || true
    done | tail -n "${2:-200}"
    ;;

  *)
    echo "Fetching logs from ftp://$FTP_HOST$REMOTE_DIR/  ->  $LOCAL_DIR/"
    listing=$("${CURL[@]}" "ftp://$FTP_HOST$REMOTE_DIR/" 2>&1 || true)
    if [[ -z "$listing" ]]; then
      echo "  (logs/ is empty or not yet created on the server)"; exit 0
    fi
    files=$(awk '{print $NF}' <<< "$listing" | grep -E '^relay\.log(\.[0-9]+)?$' || true)
    if [[ -z "$files" ]]; then
      echo "  (no log files found yet)"; exit 0
    fi
    while read -r f; do
      [[ -z "$f" ]] && continue
      "${CURL[@]}" -o "$LOCAL_DIR/$f" "ftp://$FTP_HOST$REMOTE_DIR/$f"
      printf '  %s  (%s bytes)\n' "$f" "$(wc -c < "$LOCAL_DIR/$f" | tr -d ' ')"
    done <<< "$files"
    echo "Done. View with:  less $LOCAL_DIR/relay.log"
    ;;
esac
