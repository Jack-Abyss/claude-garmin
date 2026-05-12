<#
.SYNOPSIS
    Uninstaller for the Garmin Connect MCP connector for Claude Desktop.

.DESCRIPTION
    Cleanly removes:
      - the 'garmin' entry in claude_desktop_config.json (both Store and classic)
      - Garmin tokens in ~/.garminconnect/
      - the .venv virtualenv
      - the .env file (credentials)

    Does NOT touch:
      - Python 3.12 (you might use it for other projects)
      - Claude Desktop
      - The project source files themselves (delete the folder manually if you want)

.PARAMETER KeepEnv      Keep the .env (useful if you plan to reinstall later).
.PARAMETER KeepTokens   Keep the Garmin tokens in ~/.garminconnect/.
.PARAMETER KeepVenv     Keep the .venv virtualenv.
.PARAMETER Force        Don't prompt for confirmation.
#>
[CmdletBinding()]
param(
    [switch]$KeepEnv,
    [switch]$KeepTokens,
    [switch]$KeepVenv,
    [switch]$Force
)

$ErrorActionPreference = 'Continue'
$ProjectDir = $PSScriptRoot
if (-not $ProjectDir) { $ProjectDir = (Get-Location).Path }

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "    [OK]  $msg" -ForegroundColor Green }
function Write-Skip([string]$msg) { Write-Host "    [--]  $msg" -ForegroundColor Gray }
function Write-Warn2([string]$msg){ Write-Host "    [!]   $msg" -ForegroundColor Yellow }
function Write-Err2([string]$msg) { Write-Host "    [ERR] $msg" -ForegroundColor Red }

function Get-AllClaudeConfigPaths {
    $paths = @()
    # Store/UWP versions (there may be more than one across reinstalls)
    $storePackages = Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -ErrorAction SilentlyContinue |
                     Where-Object { $_.Name -like "Claude_*" }
    foreach ($pkg in $storePackages) {
        $cfg = Join-Path $pkg.FullName "LocalCache\Roaming\Claude\claude_desktop_config.json"
        if (Test-Path $cfg) { $paths += @{ Kind = "store"; Path = $cfg } }
    }
    # Classic version
    $classic = Join-Path $env:APPDATA "Claude\claude_desktop_config.json"
    if (Test-Path $classic) { $paths += @{ Kind = "classic"; Path = $classic } }
    return $paths
}

Write-Host ""
Write-Host "===================================================================" -ForegroundColor Yellow
Write-Host "  claude-garmin - Uninstaller"                                       -ForegroundColor Yellow
Write-Host "===================================================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "Will remove:"
Write-Host "  - the 'garmin' entry from Claude Desktop config(s)"
if (-not $KeepTokens) { Write-Host "  - Garmin tokens at $HOME\.garminconnect\" } else { Write-Host "  - (tokens kept)" -ForegroundColor Gray }
if (-not $KeepEnv)    { Write-Host "  - .env (credentials) in $ProjectDir" }     else { Write-Host "  - (.env kept)" -ForegroundColor Gray }
if (-not $KeepVenv)   { Write-Host "  - virtualenv .venv in $ProjectDir" }       else { Write-Host "  - (.venv kept)" -ForegroundColor Gray }
Write-Host ""
Write-Host "Will NOT remove: Python, Claude Desktop, or the project source files." -ForegroundColor Gray
Write-Host ""

if (-not $Force) {
    $confirm = Read-Host "Continue? [y/N]"
    if ($confirm -notmatch '^[ySs]') {
        Write-Host "Cancelled." -ForegroundColor Yellow
        exit 0
    }
}

# ----------------------------------------------------------------------------
# Pre-step: kill any python.exe instances running from THIS .venv
# (Claude Desktop spawns the MCP server as a child python.exe; if it's still
# running it locks .pyd/.dll files and Remove-Item on .venv fails partway.)
# ----------------------------------------------------------------------------
$projectVenv = Join-Path $ProjectDir ".venv"
$lockingProcs = Get-Process python -ErrorAction SilentlyContinue |
                Where-Object { $_.Path -and $_.Path.StartsWith($projectVenv, [StringComparison]::OrdinalIgnoreCase) }
if ($lockingProcs) {
    Write-Step "Stopping python.exe processes holding the .venv"
    foreach ($p in $lockingProcs) {
        Write-Info "Killing PID $($p.Id) ($($p.Path))"
        try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch { Write-Warn2 "couldn't kill PID $($p.Id): $_" }
    }
    Start-Sleep -Seconds 1
}

# ----------------------------------------------------------------------------
# 1. Remove 'garmin' from Claude Desktop config(s)
# ----------------------------------------------------------------------------
Write-Step "Remove 'garmin' from Claude Desktop config(s)"
$configs = Get-AllClaudeConfigPaths
if ($configs.Count -eq 0) {
    Write-Skip "No claude_desktop_config.json found"
} else {
    foreach ($c in $configs) {
        try {
            $cfg = Get-Content $c.Path -Raw -Encoding utf8 | ConvertFrom-Json
            if ($cfg.PSObject.Properties.Name -contains 'mcpServers' -and
                $cfg.mcpServers.PSObject.Properties.Name -contains 'garmin') {
                $cfg.mcpServers.PSObject.Properties.Remove('garmin')
                # Write UTF-8 without BOM (see install.ps1 for the same fix).
                $json = $cfg | ConvertTo-Json -Depth 10
                [System.IO.File]::WriteAllText($c.Path, $json, [System.Text.UTF8Encoding]::new($false))
                Write-Ok "Removed from $($c.Kind): $($c.Path)"
            } else {
                Write-Skip "$($c.Kind) has no 'garmin' entry"
            }
        } catch {
            Write-Err2 "Error reading $($c.Path): $_"
        }
    }
}

# ----------------------------------------------------------------------------
# 2. Garmin tokens
# ----------------------------------------------------------------------------
Write-Step "Garmin tokens"
$tokenDir = Join-Path $HOME ".garminconnect"
if ($KeepTokens) {
    Write-Skip "skipped (-KeepTokens)"
} elseif (Test-Path $tokenDir) {
    Remove-Item -Recurse -Force $tokenDir
    Write-Ok "Deleted $tokenDir"
} else {
    Write-Skip "Not present"
}

# ----------------------------------------------------------------------------
# 3. .env
# ----------------------------------------------------------------------------
Write-Step ".env file"
$envFile = Join-Path $ProjectDir ".env"
if ($KeepEnv) {
    Write-Skip "skipped (-KeepEnv)"
} elseif (Test-Path $envFile) {
    Remove-Item -Force $envFile
    Write-Ok "Deleted $envFile"
} else {
    Write-Skip "Not present"
}

# ----------------------------------------------------------------------------
# 4. Virtualenv
# ----------------------------------------------------------------------------
Write-Step ".venv virtualenv"
$venvDir = Join-Path $ProjectDir ".venv"
if ($KeepVenv) {
    Write-Skip "skipped (-KeepVenv)"
} elseif (Test-Path $venvDir) {
    # Use -ErrorAction Stop so partial failures hit the catch block, and
    # verify with Test-Path afterwards (Remove-Item may report success per-file
    # while leaving folders behind if a child file was locked).
    try {
        Remove-Item -Recurse -Force $venvDir -ErrorAction Stop
    } catch {
        # First attempt failed — wait and retry once (handles transient locks)
        Start-Sleep -Seconds 2
        try { Remove-Item -Recurse -Force $venvDir -ErrorAction Stop } catch { }
    }
    if (Test-Path $venvDir) {
        Write-Warn2 "Couldn't fully delete .venv (some files were locked)."
        Write-Warn2 "Close Claude Desktop completely (incl. tray icon), then delete this folder manually:"
        Write-Warn2 "  $venvDir"
    } else {
        Write-Ok "Deleted $venvDir"
    }
} else {
    Write-Skip "Not present"
}

# ----------------------------------------------------------------------------
# 5. __pycache__ (silent cleanup, no message)
# ----------------------------------------------------------------------------
$pycache = Join-Path $ProjectDir "__pycache__"
if (Test-Path $pycache) {
    Remove-Item -Recurse -Force $pycache -ErrorAction SilentlyContinue
}

# ----------------------------------------------------------------------------
Write-Host ""
Write-Host "===================================================================" -ForegroundColor Green
Write-Host "  Uninstall complete"                                                -ForegroundColor Green
Write-Host "===================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next (optional):"
Write-Host "  - Restart Claude Desktop: the 'garmin' connector won't appear anymore"
Write-Host "  - Delete the project folder if you don't need it: $ProjectDir"
Write-Host "  - To also remove Python / Claude Desktop, use winget:"
Write-Host "      winget uninstall Python.Python.3.12"
Write-Host "      winget uninstall Anthropic.Claude"
Write-Host ""
