#requires -Version 5.1
<#
Resolves the latest GitHub release of hawwwran/termpilot, downloads the
attached termpilot.zip, extracts the `windows/` subfolder to
%LOCALAPPDATA%\Programs\termpilot\windows\, and runs that copy's
install.bat. Designed to be safe to re-run.
#>

$ErrorActionPreference = "Stop"

$Repo  = "hawwwran/termpilot"
$Api   = "https://api.github.com/repos/$Repo/releases/latest"
$Asset = "termpilot.zip"
$InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\termpilot"

function Write-Step([string]$msg) { Write-Host $msg -ForegroundColor White }
function Write-Ok([string]$msg)   { Write-Host "  $msg" -ForegroundColor Green }
function Write-Info([string]$msg) { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Err2([string]$msg) { Write-Host "  $msg" -ForegroundColor Red }

Write-Host "╔══════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║   Install TermPilot for Windows (latest)     ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# --- Resolve latest tag ---------------------------------------------------
Write-Step "Resolving latest release on GitHub..."
try {
    $headers = @{ "User-Agent" = "termpilot-installer"; "Accept" = "application/vnd.github+json" }
    $rel = Invoke-RestMethod -Uri $Api -Headers $headers -TimeoutSec 30
} catch {
    Write-Err2 "Could not reach the GitHub API: $($_.Exception.Message)"
    exit 1
}
$tag = $rel.tag_name
if (-not $tag) {
    Write-Err2 "GitHub API didn't return a tag_name. Check https://github.com/$Repo/releases"
    exit 1
}
Write-Ok "Latest tag: $tag"

$assetUrl = $null
foreach ($a in $rel.assets) {
    if ($a.name -eq $Asset) { $assetUrl = $a.browser_download_url; break }
}
if (-not $assetUrl) {
    Write-Err2 "Release $tag has no $Asset asset attached."
    exit 1
}
Write-Info "Asset URL: $assetUrl"
Write-Host ""

# --- Download to temp -----------------------------------------------------
$tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ("termpilot-" + [guid]::NewGuid())) -Force
$zipPath = Join-Path $tmp.FullName $Asset
Write-Step "Downloading $Asset..."
try {
    Invoke-WebRequest -Uri $assetUrl -OutFile $zipPath -UseBasicParsing
} catch {
    Write-Err2 "Download failed: $($_.Exception.Message)"
    exit 1
}
Write-Ok "Got $((Get-Item $zipPath).Length) bytes"
Write-Host ""

# --- Extract --------------------------------------------------------------
Write-Step "Extracting to $InstallRoot ..."
$extractRoot = Join-Path $tmp.FullName "unpacked"
New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
try {
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot -Force
} catch {
    Write-Err2 "Extraction failed: $($_.Exception.Message)"
    exit 1
}

# The zip may root entries at termpilot/ or directly. Locate windows/.
$winDir = Get-ChildItem -LiteralPath $extractRoot -Recurse -Directory `
    -Filter "windows" -ErrorAction SilentlyContinue |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "termpilot-win-wrap.py") } |
    Select-Object -First 1
if (-not $winDir) {
    Write-Err2 "windows/termpilot-win-wrap.py not found inside the release zip."
    Write-Err2 "This release may not yet ship the Windows tool."
    exit 1
}

if (-not (Test-Path -LiteralPath $InstallRoot)) {
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
}
$dest = Join-Path $InstallRoot "windows"
if (Test-Path -LiteralPath $dest) {
    # Wipe the windows/ subfolder so we don't carry stale files. We keep
    # the rest of $InstallRoot intact in case users dropped data there.
    Remove-Item -LiteralPath $dest -Recurse -Force
}
Move-Item -LiteralPath $winDir.FullName -Destination $dest
Write-Ok "Extracted to $dest"
Write-Host ""

# --- Run install.bat from the extracted location --------------------------
Write-Step "Running bundled install.bat..."
$installBat = Join-Path $dest "install.bat"
if (-not (Test-Path -LiteralPath $installBat)) {
    Write-Err2 "install.bat missing from extracted folder."
    exit 1
}
$proc = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $installBat `
    -NoNewWindow -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    Write-Err2 "install.bat exited with code $($proc.ExitCode)."
    exit $proc.ExitCode
}

Write-Host ""
Write-Host "TermPilot $tag installed for Windows." -ForegroundColor Green
Write-Host "Open a new console (cmd or PowerShell), then:"
Write-Host "  termpilot --set-relay-url https://your.host/path   if not yet set"
Write-Host "  termpilot --generate-token"
Write-Host "  termpilot"
exit 0
