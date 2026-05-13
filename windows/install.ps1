#requires -Version 5.1
<#
.SYNOPSIS
  TermPilot Windows installer. Points `tp` / `termpilot` shims at the
  given source directory.

.PARAMETER Source
  The directory containing termpilot-win-wrap.py. Typically the
  directory of install.bat that called this script. Captured so the
  wrapper can mirror its event log back here for diagnosis.
#>
param(
    [Parameter(Mandatory=$true)]
    [string]$Source
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$msg) { Write-Host $msg -ForegroundColor White }
function Write-Ok([string]$msg)   { Write-Host "  $msg" -ForegroundColor Green }
function Write-Info([string]$msg) { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Warn2([string]$msg){ Write-Host "  $msg" -ForegroundColor Yellow }
function Write-Err2([string]$msg) { Write-Host "  $msg" -ForegroundColor Red }

# --- Resolve source ---------------------------------------------------------
$Source = (Resolve-Path -LiteralPath $Source).Path
$WrapperPath = Join-Path $Source "termpilot-win-wrap.py"
if (-not (Test-Path -LiteralPath $WrapperPath)) {
    Write-Err2 "termpilot-win-wrap.py not found at $WrapperPath"
    exit 1
}

Write-Step "Source: $Source"

# --- Locate Python 3.9+ -----------------------------------------------------
function Find-Python {
    foreach ($candidate in @("python", "python3", "py")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            if ($candidate -eq "py") {
                $verOut = & $candidate -3 -V 2>&1
            } else {
                $verOut = & $candidate -V 2>&1
            }
            if ($verOut -match "Python\s+(\d+)\.(\d+)") {
                $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                if ($major -ge 3 -and ($major -gt 3 -or $minor -ge 9)) {
                    if ($candidate -eq "py") {
                        return @{ Cmd = "py -3"; Version = "$major.$minor"; Path = $cmd.Path }
                    }
                    return @{ Cmd = $cmd.Path; Version = "$major.$minor"; Path = $cmd.Path }
                } else {
                    Write-Info "$($cmd.Path) is Python $major.$minor (need >= 3.9), skipping"
                }
            }
        } catch {
            Write-Info "Could not query $candidate"
        }
    }
    return $null
}

function Refresh-Path {
    # Re-read PATH from registry so a freshly-installed Python becomes
    # visible in the current process without a console restart.
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = ($machine, $user | Where-Object { $_ }) -join ";"
}

function Prompt-YesNo {
    param([string]$Question, [string]$DefaultYes = "y")
    $suffix = if ($DefaultYes -eq "y") { "[Y/n]" } else { "[y/N]" }
    while ($true) {
        Write-Host "$Question $suffix " -NoNewline -ForegroundColor Cyan
        $reply = Read-Host
        if ([string]::IsNullOrWhiteSpace($reply)) { return ($DefaultYes -eq "y") }
        $r = $reply.Trim().ToLower()
        if ($r -in @("y", "yes")) { return $true }
        if ($r -in @("n", "no"))  { return $false }
    }
}

function Install-Python-Winget {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }
    Write-Step "Installing Python via winget (user scope, no admin needed)..."
    $args = @(
        "install", "--id", "Python.Python.3.12", "--exact",
        "--source", "winget", "--scope", "user",
        "--accept-source-agreements", "--accept-package-agreements",
        "--silent"
    )
    $proc = Start-Process -FilePath "winget" -ArgumentList $args `
        -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -eq 0) {
        Write-Ok "winget reported success."
        return $true
    }
    Write-Warn2 "winget exit $($proc.ExitCode). Will try direct download."
    return $false
}

function Install-Python-Direct {
    # Per-user install of CPython from python.org. Adds itself to PATH
    # (PrependPath=1) and skips the launcher to avoid admin prompts.
    $ver = "3.12.7"
    $url = "https://www.python.org/ftp/python/$ver/python-$ver-amd64.exe"
    $tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ("python-dl-" + [guid]::NewGuid())) -Force
    $exe = Join-Path $tmp.FullName "python-$ver-amd64.exe"
    Write-Step "Downloading Python $ver installer from python.org..."
    try {
        Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    } catch {
        Write-Err2 "Download failed: $($_.Exception.Message)"
        return $false
    }
    Write-Ok "Downloaded $((Get-Item $exe).Length) bytes"
    Write-Step "Running silent per-user install (no admin needed)..."
    $args = @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1",
        "Include_pip=1", "Include_launcher=0",
        "Include_test=0", "Include_doc=0", "SimpleInstall=1"
    )
    $proc = Start-Process -FilePath $exe -ArgumentList $args -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        Write-Err2 "Python installer exit $($proc.ExitCode)."
        return $false
    }
    Write-Ok "Python installed."
    return $true
}

Write-Step "Locating Python..."
$pyInfo = Find-Python
if (-not $pyInfo) {
    Write-Warn2 "No Python 3.9+ found on PATH."
    Write-Host ""
    Write-Host "TermPilot needs Python 3.9 or newer to run the wrapper."
    Write-Host "Install options:"
    Write-Host "  - winget install Python.Python.3.12  (user scope, no admin)"
    Write-Host "  - python.org installer downloaded silently to your user profile"
    Write-Host ""
    if (Prompt-YesNo "Install Python now?" "y") {
        $ok = Install-Python-Winget
        if (-not $ok) { $ok = Install-Python-Direct }
        if ($ok) {
            Refresh-Path
            $pyInfo = Find-Python
        }
        if (-not $pyInfo) {
            Write-Err2 "Python install finished but `python` still isn't on PATH."
            Write-Err2 "Open a NEW console window and re-run install.bat."
            exit 1
        }
        Write-Ok "Python now on PATH."
    } else {
        Write-Err2 "Skipped. Install Python from https://www.python.org/downloads/windows/"
        Write-Err2 "and re-run install.bat. (Tick 'Add python.exe to PATH'.)"
        exit 1
    }
}
$python = $pyInfo.Cmd
Write-Ok "Using Python $($pyInfo.Version) at $($pyInfo.Path)"

# --- Install pip dependencies ----------------------------------------------
Write-Step "Installing Python dependencies (pywinpty, keyring, cryptography)..."
$reqFile = Join-Path $Source "requirements.txt"
if (-not (Test-Path -LiteralPath $reqFile)) {
    Write-Err2 "requirements.txt missing next to install.ps1"
    exit 1
}
$pipArgs = @("-m", "pip", "install", "--user", "--upgrade", "-r", $reqFile)
$pyParts = $python -split " "
$pyExe = $pyParts[0]
$pyExtra = @()
if ($pyParts.Length -gt 1) { $pyExtra = $pyParts[1..($pyParts.Length-1)] }
$proc = Start-Process -FilePath $pyExe -ArgumentList ($pyExtra + $pipArgs) `
    -NoNewWindow -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    Write-Err2 "pip install failed (exit $($proc.ExitCode))."
    Write-Err2 "Run manually:  $python -m pip install --user -r `"$reqFile`""
    exit $proc.ExitCode
}
Write-Ok "Dependencies installed."

# --- Choose shim directory --------------------------------------------------
# Always %LOCALAPPDATA%\Programs\termpilot\bin so PATH-editing converges
# regardless of where the source tree lives (share, downloaded zip, etc).
$BinDir = Join-Path $env:LOCALAPPDATA "Programs\termpilot\bin"
if (-not (Test-Path -LiteralPath $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}
Write-Step "Writing shims to $BinDir"

# termpilot.cmd + tp.cmd dispatch to python on the wrapper. We pin the
# python path captured above so a later python install on PATH can't
# silently redirect.
$pythonForShim = $python
$shimBody = @"
@echo off
rem Auto-generated by termpilot install.ps1. Re-run install.bat to repoint.
$pythonForShim "$WrapperPath" %*
"@
Set-Content -LiteralPath (Join-Path $BinDir "termpilot.cmd") -Value $shimBody -Encoding ASCII
Set-Content -LiteralPath (Join-Path $BinDir "tp.cmd")        -Value $shimBody -Encoding ASCII
Write-Ok "termpilot.cmd and tp.cmd written"

# --- PATH ------------------------------------------------------------------
Write-Step "Ensuring $BinDir is on USER PATH..."
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
$parts = $userPath -split ";" | Where-Object { $_ -ne "" }
$already = $false
foreach ($p in $parts) {
    if ((Resolve-Path -LiteralPath $p -ErrorAction SilentlyContinue) -and
        ((Resolve-Path -LiteralPath $p).Path -ieq (Resolve-Path -LiteralPath $BinDir).Path)) {
        $already = $true
        break
    }
}
if ($already) {
    Write-Ok "Already on USER PATH."
} else {
    $newPath = if ($userPath) { "$userPath;$BinDir" } else { "$BinDir" }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Ok "Added to USER PATH. Open a new console for it to take effect."
}

# Add to the current PowerShell session immediately so the user can
# poke at `termpilot --version` without re-launching.
if (-not (($env:Path -split ";") -contains $BinDir)) {
    $env:Path = "$env:Path;$BinDir"
    Write-Info "Also added to this session's PATH."
}

# --- Config dir + install-source record ------------------------------------
$ConfigDir = Join-Path $env:APPDATA "termpilot"
if (-not (Test-Path -LiteralPath $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
}
Set-Content -LiteralPath (Join-Path $ConfigDir "install_source.txt") `
    -Value $Source -Encoding UTF8 -NoNewline
Write-Ok "Recorded install source at $ConfigDir\install_source.txt"
Write-Info "Wrapper events.log will be mirrored to $Source\logs\"

# Make sure the logs/ dir exists so the wrapper can write straight in.
$LogsDir = Join-Path $Source "logs"
try {
    if (-not (Test-Path -LiteralPath $LogsDir)) {
        New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
    }
    Write-Ok "Logs folder ready: $LogsDir"
} catch {
    Write-Warn2 "Could not create $LogsDir ($($_.Exception.Message))."
    Write-Warn2 "The wrapper will write logs to %LOCALAPPDATA%\termpilot\events.log only."
}

# --- Report ----------------------------------------------------------------
Write-Host ""
Write-Host "Installed." -ForegroundColor Green
Write-Host "  shim dir:      $BinDir"
Write-Host "  wrapper:       $WrapperPath"
Write-Host "  config dir:    $ConfigDir"
Write-Host "  install source: $Source"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
$relayUrlFile = Join-Path $ConfigDir "relay-url"
if (-not (Test-Path -LiteralPath $relayUrlFile)) {
    Write-Host "  1. Tell termpilot where your relay lives:"
    Write-Host "       termpilot --set-relay-url https://your.host/path"
}
Write-Host "  2. Generate your encryption token:"
Write-Host "       termpilot --generate-token"
Write-Host "  3. Paste the hex into the browser's manage-tokens dialog."
Write-Host "  4. cd into a project and run:  termpilot"
exit 0
