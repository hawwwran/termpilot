#requires -Version 5.1
<#
Resolves the latest GitHub release of hawwwran/termpilot, downloads the
attached termpilot-windows.zip, extracts it flat into
%LOCALAPPDATA%\Programs\termpilot\, and runs the bundled install.bat.
Designed to be safe to re-run.

Usage:
  # Standalone:
  .\install-latest-version.ps1

  # Fresh-install one-liner (PowerShell):
  iwr -useb https://raw.githubusercontent.com/hawwwran/termpilot/main/windows/install-latest-version.ps1 | iex

On error the script `throw`s instead of `exit`ing, so when invoked via
`iex` the error stays visible in the user's PowerShell session
(`exit N` would tear the session down and the user would lose the
diagnostic).
#>

$ErrorActionPreference = "Stop"

$Repo  = "hawwwran/termpilot"
$Api   = "https://api.github.com/repos/$Repo/releases/latest"
$Asset = "termpilot-windows.zip"
$InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\termpilot"

function Write-Step([string]$msg) { Write-Host $msg -ForegroundColor White }
function Write-Ok([string]$msg)   { Write-Host "  $msg" -ForegroundColor Green }
function Write-Info([string]$msg) { Write-Host "  $msg" -ForegroundColor DarkGray }

Write-Host "╔══════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║   Install TermPilot for Windows (latest)     ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host "  script: $PSCommandPath" -ForegroundColor DarkGray
Write-Host ""

# --- Resolve latest tag ---------------------------------------------------
Write-Step "Resolving latest release on GitHub..."
try {
    $headers = @{ "User-Agent" = "termpilot-installer"; "Accept" = "application/vnd.github+json" }
    $rel = Invoke-RestMethod -Uri $Api -Headers $headers -TimeoutSec 30
} catch {
    throw "Could not reach the GitHub API: $($_.Exception.Message)"
}
$tag = $rel.tag_name
if (-not $tag) {
    throw "GitHub API didn't return a tag_name. Check https://github.com/$Repo/releases"
}
Write-Ok "Latest tag: $tag"

$assetUrl = $null
foreach ($a in $rel.assets) {
    if ($a.name -eq $Asset) { $assetUrl = $a.browser_download_url; break }
}
if (-not $assetUrl) {
    throw "Release $tag has no $Asset asset attached. This release may predate the Windows port. Check https://github.com/$Repo/releases"
}
Write-Info "Asset URL: $assetUrl"
Write-Host ""

# --- Download + extract (combined) ----------------------------------------
# Keep these in one try/catch so any failure dumps the full state — we've
# seen $zipPath end up empty at the extract step in the wild without an
# obvious cause upstream, and the resulting Expand-Archive / ZipFile error
# is misleading on its own.
$tmp = $null
$zipPath = $null
try {
    $tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ("termpilot-" + [guid]::NewGuid())) -Force
    $zipPath = Join-Path $tmp.FullName $Asset
    Write-Info "Staging dir: $($tmp.FullName)"
    Write-Info "Zip target:  $zipPath"

    Write-Step "Downloading $Asset..."
    Invoke-WebRequest -Uri $assetUrl -OutFile $zipPath -UseBasicParsing
    $zipSize = (Get-Item -LiteralPath $zipPath).Length
    Write-Ok "Got $zipSize bytes"
    Write-Host ""

    # The zip is flat: termpilot-win-wrap.py, install.bat, lib/, shared/,
    # … all live at the zip's root. Extract directly to the install dir,
    # wiping any prior contents at the top level first (preserves the
    # parent dir in case the user dropped data alongside it).
    Write-Step "Extracting to $InstallRoot ..."
    if (-not (Test-Path -LiteralPath $InstallRoot)) {
        New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    }
    # Best-effort wipe. SilentlyContinue tolerates locked files (rare —
    # typically only when something is still mid-tear-down); the
    # per-entry extract below overwrites whatever survives.
    Get-ChildItem -LiteralPath $InstallRoot -Force | ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }

    if ([string]::IsNullOrEmpty($zipPath) -or -not (Test-Path -LiteralPath $zipPath)) {
        throw "Zip vanished between download and extract: zipPath='$zipPath', tmp='$($tmp.FullName)'"
    }

    # Use the .NET ZipFile API directly. PS 5.1's Expand-Archive has
    # been observed to surface "Cannot validate argument on parameter
    # 'LiteralPath'. The argument is null or empty." when the
    # destination was just wiped — a misleading wrapper-level error
    # that hides the real state. Per-entry ExtractToFile gives us
    # predictable overwrite semantics on .NET Framework 4.5+ (the
    # 3-arg ExtractToDirectory overload is .NET Core only).
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        foreach ($entry in $archive.Entries) {
            $destPath = [System.IO.Path]::Combine($InstallRoot, $entry.FullName.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
            if ([string]::IsNullOrEmpty($entry.Name)) {
                # Directory entry (zip convention: trailing slash).
                if (-not (Test-Path -LiteralPath $destPath)) {
                    New-Item -ItemType Directory -Path $destPath -Force | Out-Null
                }
            } else {
                $destDir = [System.IO.Path]::GetDirectoryName($destPath)
                if ($destDir -and -not (Test-Path -LiteralPath $destDir)) {
                    New-Item -ItemType Directory -Path $destDir -Force | Out-Null
                }
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destPath, $true)
            }
        }
    } finally {
        $archive.Dispose()
    }
} catch {
    Write-Host ""
    Write-Host "[install-latest-version] Failure during download or extract." -ForegroundColor Red
    Write-Host "  message       : $($_.Exception.Message)" -ForegroundColor DarkRed
    Write-Host "  zipPath       : '$zipPath'" -ForegroundColor DarkRed
    Write-Host "  tmp.FullName  : '$(if ($tmp) { $tmp.FullName } else { '(null)' })'" -ForegroundColor DarkRed
    Write-Host "  Asset         : '$Asset'" -ForegroundColor DarkRed
    Write-Host "  InstallRoot   : '$InstallRoot'" -ForegroundColor DarkRed
    Write-Host "  env:TEMP      : '$env:TEMP'" -ForegroundColor DarkRed
    Write-Host "  env:LOCALAPPDATA: '$env:LOCALAPPDATA'" -ForegroundColor DarkRed
    Write-Host "  script        : '$PSCommandPath'" -ForegroundColor DarkRed
    throw
}

$wrapperPath = Join-Path $InstallRoot "termpilot-win-wrap.py"
if (-not (Test-Path -LiteralPath $wrapperPath)) {
    throw "termpilot-win-wrap.py not found at $wrapperPath after extraction. The zip layout may have changed; check https://github.com/$Repo/releases"
}
Write-Ok "Extracted"
Write-Host ""

# --- Run install.bat from the extracted location --------------------------
Write-Step "Running bundled install.bat..."
$installBat = Join-Path $InstallRoot "install.bat"
if (-not (Test-Path -LiteralPath $installBat)) {
    throw "install.bat missing from extracted folder."
}
$proc = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $installBat `
    -NoNewWindow -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    throw "install.bat exited with code $($proc.ExitCode). Re-run it from $InstallRoot to see the full output."
}

Write-Host ""
Write-Host "TermPilot $tag installed for Windows." -ForegroundColor Green
Write-Host "Open a new console (cmd or PowerShell), then:"
Write-Host "  termpilot --set-relay-url https://your.host/path   if not yet set"
Write-Host "  termpilot --generate-token"
Write-Host "  termpilot"
