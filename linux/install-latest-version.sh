#!/usr/bin/env bash
# TermPilot end-user installer.
#
# Resolves the latest GitHub release of hawwwran/termpilot, downloads
# the attached zip, extracts it to ~/.local/share/termpilot/, runs the
# bundled install.sh, then offers to mint a device token and deploy
# the relay to your server.
#
# Designed to be run two ways:
#   1) From inside an already-extracted zip:
#        ./install-latest-version.sh
#   2) Piped straight from a fresh checkout via curl:
#        curl -fsSL https://raw.githubusercontent.com/hawwwran/termpilot/main/linux/install-latest-version.sh | bash
#
# All interactive prompts read from /dev/tty, so the curl-piped form
# still works.

set -u

REPO="hawwwran/termpilot"
INSTALL_ROOT="$HOME/.local/share/termpilot"
ASSET="termpilot-linux-macos.zip"
ZIP_URL_LATEST="https://github.com/${REPO}/releases/latest/download/${ASSET}"
API_LATEST="https://api.github.com/repos/${REPO}/releases/latest"

RED='\033[0;31m'
GREEN='\033[0;32m'
WHITE='\033[1;37m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
DIM='\033[2m'
NC='\033[0m'

step()  { echo -e "${WHITE}$*${NC}"; }
ok()    { echo -e "  ${GREEN}✓${NC} $*"; }
fail()  { echo -e "  ${RED}✗${NC} $*" >&2; }
info()  { echo -e "  ${DIM}$*${NC}"; }
hr()    { echo -e "${CYAN}────────────────────────────────────${NC}"; }

prompt() {
    # $1 = prompt text, $2 = default ('y' or 'n'). Reads /dev/tty so the
    # script works when piped from curl. Returns 0 for yes, 1 for no.
    local q="$1" def="$2" reply prompt_str
    if [[ "$def" == "y" ]]; then prompt_str="$q [Y/n] "
    else                          prompt_str="$q [y/N] "; fi
    # `-r /dev/tty` returns true even when the process has no
    # controlling terminal (the device file exists), so the real check
    # is whether a write actually succeeds. Probe in a subshell.
    if ! ( : > /dev/tty ) 2>/dev/null; then
        info "(non-interactive: defaulting to $def)"
        [[ "$def" == "y" ]]
        return
    fi
    printf '%s' "$prompt_str" > /dev/tty
    read -r reply < /dev/tty || reply=""
    reply="${reply:-$def}"
    [[ "${reply,,}" == "y" || "${reply,,}" == "yes" ]]
}

echo -e "${CYAN}╔════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║       Install TermPilot (latest)       ║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════╝${NC}"
echo ""

# ---- Tool checks ----------------------------------------------------------
step "Checking required tools..."
missing=()
for cmd in curl unzip python3; do
    if ! command -v "$cmd" >/dev/null 2>&1; then missing+=("$cmd"); fi
done
if (( ${#missing[@]} > 0 )); then
    fail "Missing required tools: ${missing[*]}"
    fail "Install them and re-run. On Debian/Ubuntu: sudo apt install ${missing[*]}"
    exit 1
fi
ok "curl, unzip, python3 present"
echo ""

# ---- Resolve latest tag ---------------------------------------------------
step "Resolving latest release..."
TAG="$(curl -fsSL "$API_LATEST" 2>/dev/null \
       | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tag_name",""))')"
if [[ -z "$TAG" ]]; then
    fail "Could not fetch latest release from GitHub API."
    fail "Check connectivity, or visit https://github.com/${REPO}/releases"
    exit 1
fi
ok "Latest tag: ${GREEN}${TAG}${NC}"
echo ""

# ---- Download zip ---------------------------------------------------------
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
ZIP_PATH="$TMPDIR/$ASSET"

step "Downloading ${ASSET}..."
if ! curl -fSL --progress-bar "$ZIP_URL_LATEST" -o "$ZIP_PATH"; then
    fail "Download failed."
    exit 1
fi
SIZE="$(wc -c < "$ZIP_PATH")"
ok "Got $(printf '%s' "$SIZE" | python3 -c 'n=int(input()); print(f"{n/1024:.0f} KB")')"
echo ""

# ---- Extract --------------------------------------------------------------
step "Extracting to ${INSTALL_ROOT}..."
mkdir -p "$INSTALL_ROOT"
# Clean prior contents but keep the dir itself (preserves any data the
# user might have placed inside, plus surrounding state like a worktree
# parent we don't own). Top-level entries only — fast and safe.
# Spare mcp-venv/: it's the pip-installed `mcp` package, not part of the
# release zip. install.sh keys reactivation off its presence, so wiping
# it here silently breaks the MCP for every upgrade.
find "$INSTALL_ROOT" -mindepth 1 -maxdepth 1 ! -name mcp-venv -exec rm -rf {} +
if ! unzip -q "$ZIP_PATH" -d "$INSTALL_ROOT"; then
    fail "unzip failed."
    exit 1
fi
ok "Extracted"
echo ""

# Sanity-check the layout
if [[ ! -x "$INSTALL_ROOT/install.sh" ]]; then
    # install.sh might not be executable post-extract; fix and continue.
    chmod +x "$INSTALL_ROOT/install.sh" 2>/dev/null
fi
if [[ ! -f "$INSTALL_ROOT/install.sh" ]]; then
    fail "install.sh missing from the extracted zip. Aborting."
    exit 1
fi
chmod +x "$INSTALL_ROOT/termpilot-wrap" 2>/dev/null || true

# ---- Run install.sh -------------------------------------------------------
step "Running bundled install.sh..."
hr
( cd "$INSTALL_ROOT" && ./install.sh ) || {
    fail "install.sh exited non-zero. Aborting."
    exit 1
}
hr
echo ""

# ---- Token mint (optional) ------------------------------------------------
step "Checking for an existing device token..."
WRAP="$INSTALL_ROOT/termpilot-wrap"
# `has-token` isn't a subcommand; we instead check via `--show-token` exit
# behaviour. Simpler: probe the storage paths directly so we don't trigger
# the sudo gate just to see whether a token exists.
HAS_TOKEN=0
if [[ -f "$HOME/.config/termpilot/token" ]]; then HAS_TOKEN=1; fi
if python3 -c '
import sys
try:
    import keyring
    if keyring.get_password("termpilot", "default"): sys.exit(0)
except Exception: pass
sys.exit(1)
' 2>/dev/null; then HAS_TOKEN=1; fi

if (( HAS_TOKEN )); then
    ok "Token already present — leaving it alone."
else
    info "No device token found yet."
    if prompt "Generate a fresh 32-byte token now?" y; then
        echo ""
        # --generate-token is sudo-gated by the wrapper; the user will be
        # prompted for their sudo password.
        "$WRAP" --generate-token || fail "Token generation failed."
    else
        info "Skipping. Run 'termpilot --generate-token' later when you're ready."
    fi
fi
echo ""

# ---- Optional deploy ------------------------------------------------------
# The deploy helper only ships in dev trees (not the end-user zip), so
# the prompt only appears when tools/deploy.sh is reachable from the
# install root.
if [[ -x "$INSTALL_ROOT/tools/deploy.sh" ]] \
   && prompt "Deploy the relay server (uploads relay/ over FTPS)?" n; then
    echo ""
    step "Running tools/deploy.sh..."
    hr
    ( cd "$INSTALL_ROOT" && ./tools/deploy.sh ) || {
        fail "Deploy failed. You can re-run later with:"
        info "  cd $INSTALL_ROOT && ./tools/deploy.sh"
    }
    hr
fi

echo ""
echo -e "${GREEN}Installation complete.${NC}"
echo -e "  Version:   ${GREEN}${TAG}${NC}"
echo -e "  Installed: ${CYAN}${INSTALL_ROOT}${NC}"
echo -e "  Activate:  ${YELLOW}open a new terminal${NC} (or source ~/.bashrc)"
echo ""
echo -e "${WHITE}Then in any project dir:${NC}"
echo -e "  ${CYAN}termpilot --set-relay-url https://your.host/path${NC}   if not yet set"
echo -e "  ${CYAN}termpilot${NC}                                          launch \$SHELL in a session"
echo -e "  ${CYAN}termpilot bash${NC}                                     or any program — no -- needed"
echo -e "  ${CYAN}termpilot --version${NC}                                check the installed version"
echo -e "  ${CYAN}termpilot --update${NC}                                 install the next release"
