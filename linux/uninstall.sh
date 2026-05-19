#!/usr/bin/env bash
# Removes everything Linux/macOS install.sh put on the system, plus
# runtime state. Refuses to run from inside a git checkout — copy this
# script outside the tree (e.g. /tmp) first, or invoke the deployed
# copy at ~/.local/share/termpilot/uninstall.sh.
#
# Removes:
#   - ~/.config/termpilot/                      configuration + secrets + shim
#   - ~/.cache/termpilot/                       per-cwd locks, sid spools, events.log
#   - ~/.local/share/termpilot/                 self-installed wrapper bundle
#   - ~/.config/fish/functions/termpilot.fish   fish entry point
#   - ~/.config/fish/completions/termpilot.fish fish completion
#   - The fenced source-line block from ~/.bashrc, ~/.bash_profile, ~/.zshrc
#   - OS keyring entry  service=termpilot username=default  (best-effort)
#
# Does NOT touch:
#   - Your dev git checkout
#   - Any other tool's data
#
# Usage:
#   ./uninstall.sh                   # interactive — prompts before deleting
#   ./uninstall.sh --yes        | -y # non-interactive
#   ./uninstall.sh --dry-run    | -n # show what would be removed; do nothing
#   ./uninstall.sh --help       | -h # this message

set -euo pipefail

DRY=0
YES=0
for a in "$@"; do
    case "$a" in
        --dry-run|-n) DRY=1 ;;
        --yes|-y)     YES=1 ;;
        --help|-h)
            sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//;s/^#$//'
            exit 0 ;;
        *) echo "uninstall.sh: unknown flag: $a" >&2; exit 2 ;;
    esac
done

CONFIG_DIR="$HOME/.config/termpilot"
CACHE_DIR="$HOME/.cache/termpilot"
SHARE_DIR="$HOME/.local/share/termpilot"
FISH_FN="$HOME/.config/fish/functions/termpilot.fish"
FISH_COMP="$HOME/.config/fish/completions/termpilot.fish"
TAG_OPEN="# >>> termpilot >>>"
TAG_CLOSE="# <<< termpilot <<<"

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null \
              || python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}" 2>/dev/null \
              || echo "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

# Dev-checkout guard — same rule as shared/release_channel.py: walk up
# from the script looking for .git/, up to 8 levels. The release zip is
# flat (no .git anywhere), so finding one means we're running from the
# source tree. Wiping deployed state from inside a dev checkout would
# leave the dev shim dangling (the shim points at the checkout but
# ~/.config/termpilot/ is gone), which is invariably the wrong outcome.
_detect_git_root() {
    local dir="$SCRIPT_DIR" max=8 parent
    while [[ $max -gt 0 ]]; do
        if [[ -e "$dir/.git" ]]; then
            printf '%s\n' "$dir"
            return 0
        fi
        parent="$(dirname "$dir")"
        [[ "$parent" == "$dir" ]] && break
        dir="$parent"
        max=$((max - 1))
    done
    return 1
}
if git_root="$(_detect_git_root)"; then
    echo "uninstall.sh: dev environment detected (git checkout at $git_root)." >&2
    echo "  Uninstall cancelled — running this from a dev tree would wipe the" >&2
    echo "  deployed config + cache but leave the dev shim pointing at nothing." >&2
    echo >&2
    echo "  If you really want to uninstall, either:" >&2
    echo "    - run  ~/.local/share/termpilot/uninstall.sh  (the deployed copy), or" >&2
    echo "    - copy this script outside the checkout first:" >&2
    echo "        cp '$SCRIPT_PATH' /tmp/ && /tmp/uninstall.sh \"\$@\"" >&2
    exit 4
fi

# If we're being run from inside the self-installed bundle, copy
# ourselves to a tmp file and re-exec from there so the upcoming
# `rm -rf "$SHARE_DIR"` doesn't yank the running script out from
# under bash.
if [[ "$SCRIPT_DIR" == "$SHARE_DIR" && -z "${TERMPILOT_UNINSTALL_RELOCATED:-}" ]]; then
    tmp="$(mktemp -t termpilot-uninstall.XXXXXX.sh)"
    cp -- "$SCRIPT_PATH" "$tmp"
    chmod +x "$tmp"
    export TERMPILOT_UNINSTALL_RELOCATED=1
    exec "$tmp" "$@"
fi

# Build the deletion plan.
REMOVE_DIRS=()
for d in "$CONFIG_DIR" "$CACHE_DIR" "$SHARE_DIR"; do
    [[ -e "$d" ]] && REMOVE_DIRS+=("$d")
done
REMOVE_FILES=()
for f in "$FISH_FN" "$FISH_COMP"; do
    [[ -e "$f" ]] && REMOVE_FILES+=("$f")
done

# Defence in depth: if any deletion target contains this script's dir,
# refuse rather than rm -rf our own running file.
for path in "${REMOVE_DIRS[@]:-}" "${REMOVE_FILES[@]:-}"; do
    [[ -z "$path" ]] && continue
    case "$SCRIPT_DIR/" in
        "$path"/*)
            echo "uninstall.sh: cowardly refusing — '$path' contains this script ($SCRIPT_DIR)." >&2
            echo "  Move uninstall.sh somewhere else (e.g. /tmp) and re-run from there." >&2
            exit 3 ;;
    esac
done

PATCH_RC=()
for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc"; do
    [[ -f "$rc" ]] && grep -qF "$TAG_OPEN" "$rc" && PATCH_RC+=("$rc")
done

KEYRING_AVAIL=0
if command -v python3 >/dev/null 2>&1 && python3 -c 'import keyring' >/dev/null 2>&1; then
    KEYRING_AVAIL=1
fi

# --- Report ---------------------------------------------------------------
echo "Would remove:"
[[ ${#REMOVE_DIRS[@]} -gt 0 ]]  && for d in "${REMOVE_DIRS[@]}";  do echo "  dir:   $d"; done
[[ ${#REMOVE_FILES[@]} -gt 0 ]] && for f in "${REMOVE_FILES[@]}"; do echo "  file:  $f"; done
[[ ${#PATCH_RC[@]} -gt 0 ]]     && for rc in "${PATCH_RC[@]}";    do echo "  patch: $rc (drop block $TAG_OPEN..$TAG_CLOSE)"; done
[[ $KEYRING_AVAIL -eq 1 ]] && echo "  keyring: service=termpilot username=default (if present)"

if [[ ${#REMOVE_DIRS[@]} -eq 0 && ${#REMOVE_FILES[@]} -eq 0 && ${#PATCH_RC[@]} -eq 0 ]]; then
    echo "  (nothing to do — termpilot doesn't appear to be installed)"
    exit 0
fi

if [[ $DRY -eq 1 ]]; then
    echo "(dry run — nothing actually removed)"
    exit 0
fi

if [[ $YES -eq 0 ]]; then
    read -r -p "Proceed? [y/N] " reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *) echo "Aborted."; exit 0 ;;
    esac
fi

# --- Execute --------------------------------------------------------------

# Keyring: best-effort, never fatal — missing entries are normal.
if [[ $KEYRING_AVAIL -eq 1 ]]; then
    python3 - <<'PY' || true
import sys
try:
    import keyring
    try:
        keyring.delete_password("termpilot", "default")
        print("  keyring: removed termpilot/default")
    except Exception as e:
        # PasswordDeleteError, NoKeyringError, etc — none are fatal.
        name = type(e).__name__
        if name == "PasswordDeleteError":
            print("  keyring: no termpilot/default entry to remove")
        else:
            print(f"  keyring: skipped ({name}: {e})")
except Exception as e:
    print(f"  keyring: skipped ({type(e).__name__}: {e})", file=sys.stderr)
PY
fi

# rc files: strip fenced block.
# Note: awk has built-in `open`/`close` functions — name our vars
# tag_open/tag_close to avoid the "cannot command line assign" error.
for rc in "${PATCH_RC[@]:-}"; do
    if awk -v tag_open="$TAG_OPEN" -v tag_close="$TAG_CLOSE" '
        BEGIN { in_block = 0 }
        $0 == tag_open  { in_block = 1; next }
        $0 == tag_close { in_block = 0; next }
        !in_block       { print }
    ' "$rc" > "$rc.tmp" && mv "$rc.tmp" "$rc"; then
        echo "  unpatched: $rc"
    else
        rm -f -- "$rc.tmp"
        echo "  unpatch FAILED for: $rc (left untouched)" >&2
    fi
done

# Files first, then dirs (cheap recoverability if a file delete fails).
for f in "${REMOVE_FILES[@]:-}"; do rm -f -- "$f"; echo "  removed file: $f"; done
for d in "${REMOVE_DIRS[@]:-}";  do rm -rf -- "$d"; echo "  removed dir:  $d"; done

echo
echo "Done. To finish: open a new terminal, or in this one run"
echo "  unalias tp 2>/dev/null; unset -f termpilot 2>/dev/null"
echo
echo "If you also want to remove the running daemon's spool while a"
echo "session was active, that data was inside ~/.cache/termpilot/ and"
echo "is gone now — close any live wrappers (Ctrl-D)."
