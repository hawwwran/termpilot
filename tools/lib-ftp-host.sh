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

# Ensure ~/.netrc has a `machine $FTP_HOST` entry. If not, prompt for
# username + password and append. Call AFTER resolve_ftp_host so
# FTP_HOST is set.
#
# Password reading is done via `read -s` from /dev/tty so it never
# crosses redirected stdout (e.g. into a logfile). The .netrc file is
# chmod 600 — curl --netrc refuses looser permissions.
resolve_ftp_credentials() {
    if [[ -z "${FTP_HOST:-}" ]]; then
        echo "error: resolve_ftp_credentials called before resolve_ftp_host." >&2
        return 1
    fi
    local NETRC="$HOME/.netrc"
    if [[ -f "$NETRC" ]] && awk -v host="$FTP_HOST" '
            /^[[:space:]]*machine[[:space:]]+/ {
                if ($2 == host) { found=1; exit }
            }
            END { exit !found }
        ' "$NETRC"; then
        return 0
    fi
    if [[ ! -r /dev/tty ]]; then
        cat >&2 <<EOF
error: no ~/.netrc entry for $FTP_HOST (and no TTY for prompt).
  Add one yourself:
    cat >> ~/.netrc <<NETRC
    machine $FTP_HOST
        login YOUR_FTP_USERNAME
        password YOUR_FTP_PASSWORD
    NETRC
    chmod 600 ~/.netrc
EOF
        return 1
    fi
    echo "" >&2
    echo "FTP credentials for $FTP_HOST are not in ~/.netrc." >&2
    echo "Enter them now (password will not echo)." >&2
    local user pass
    printf 'FTP username: ' > /dev/tty
    read -r user < /dev/tty
    user="${user//[$'\t\r\n ']/}"
    if [[ -z "$user" ]]; then
        echo "error: no username entered." >&2
        return 1
    fi
    printf 'FTP password: ' > /dev/tty
    read -rs pass < /dev/tty
    printf '\n' > /dev/tty
    if [[ -z "$pass" ]]; then
        echo "error: no password entered." >&2
        return 1
    fi
    # Append a fresh block. We deliberately don't strip existing blocks
    # here — if the user has a partial / malformed entry, awk's "found"
    # check above would have returned 0 already and we'd never get here.
    {
        printf 'machine %s\n'    "$FTP_HOST"
        printf '    login %s\n'  "$user"
        printf '    password %s\n' "$pass"
    } >> "$NETRC"
    chmod 600 "$NETRC"
    echo "Saved ~/.netrc entry for $FTP_HOST (chmod 600)." >&2
    return 0
}
