# Shared helper: resolve FTP host for the TermPilot deploy + fetch-logs
# tools. Sourced by tools/deploy.sh and tools/fetch-logs.sh.
#
# Sets the FTP_HOST shell variable in the caller, in this priority:
#   1. $TERMPILOT_FTP_HOST environment variable  (highest)
#   2. ~/.config/termpilot/ftp-host file
#   3. interactive prompt; the answer is saved to the file so future
#      runs don't ask again.
#
# Exits non-zero if no host can be resolved (e.g. no env var, no saved
# file, and no TTY for the prompt).

TERMPILOT_HOST_FILE="$HOME/.config/termpilot/ftp-host"

resolve_ftp_host() {
    if [[ -n "${TERMPILOT_FTP_HOST:-}" ]]; then
        FTP_HOST="$TERMPILOT_FTP_HOST"
        return 0
    fi
    if [[ -r "$TERMPILOT_HOST_FILE" ]]; then
        FTP_HOST="$(cat "$TERMPILOT_HOST_FILE")"
        FTP_HOST="${FTP_HOST//[$'\t\r\n ']/}"
        if [[ -n "$FTP_HOST" ]]; then return 0; fi
    fi
    if [[ ! -t 0 ]]; then
        cat >&2 <<EOF
error: FTP host not configured (and no TTY for prompt).
  Set one of:
    export TERMPILOT_FTP_HOST=ftp.your.host
    echo ftp.your.host > $TERMPILOT_HOST_FILE
EOF
        return 1
    fi
    printf 'FTP host (e.g. ftp.your.host): ' >&2
    read -r FTP_HOST
    FTP_HOST="${FTP_HOST//[$'\t\r\n ']/}"
    if [[ -z "$FTP_HOST" ]]; then
        echo "error: no host entered." >&2
        return 1
    fi
    mkdir -p "$(dirname "$TERMPILOT_HOST_FILE")"
    chmod 700 "$(dirname "$TERMPILOT_HOST_FILE")" 2>/dev/null || true
    printf '%s\n' "$FTP_HOST" > "$TERMPILOT_HOST_FILE"
    chmod 600 "$TERMPILOT_HOST_FILE"
    echo "Saved $TERMPILOT_HOST_FILE; future runs won't ask." >&2
    return 0
}
