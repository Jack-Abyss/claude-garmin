<#
.SYNOPSIS
    End-to-end installer for the Garmin Connect MCP connector for
    Claude Desktop (Windows).

.DESCRIPTION
    Idempotent. Steps:
      1. Detect/install Python 3.12 (winget)
      2. Detect/install Claude Desktop (winget)
      3. Create the .venv virtualenv
      4. Install dependencies (mcp, garminconnect, curl_cffi, python-dotenv)
      5. Collect Garmin credentials (email, password, MFA awareness)
      6. One-time Garmin login (saves tokens in ~/.garminconnect/)
      7. Write claude_desktop_config.json to the correct path
         (Store/UWP or classic)

    Auto-detects which flavor of Claude Desktop is installed:
      - Store/UWP: %LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\
      - Classic:   %APPDATA%\Claude\

    To uninstall later: uninstall.bat (or .\uninstall.ps1)

.PARAMETER SkipPythonInstall   Do not install Python even if missing.
.PARAMETER SkipClaudeInstall   Do not install Claude Desktop even if missing.
.PARAMETER SkipGarminLogin     Skip the Garmin login (if already done).
.PARAMETER Reconfigure         Force re-collection of .env and re-login.
#>
[CmdletBinding()]
param(
    [switch]$SkipPythonInstall,
    [switch]$SkipClaudeInstall,
    [switch]$SkipGarminLogin,
    [switch]$Reconfigure
)

$ErrorActionPreference = 'Stop'
$ProjectDir = $PSScriptRoot
if (-not $ProjectDir) { $ProjectDir = (Get-Location).Path }

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "    [OK]  $msg" -ForegroundColor Green }
function Write-Info([string]$msg) { Write-Host "    [..]  $msg" -ForegroundColor Gray }
function Write-Warn2([string]$msg){ Write-Host "    [!]   $msg" -ForegroundColor Yellow }
function Write-Err2([string]$msg) { Write-Host "    [ERR] $msg" -ForegroundColor Red }

function Find-Python {
    # Look for a real Python 3.11+, excluding the Microsoft Store shim
    # under WindowsApps (it redirects to the Store on launch).
    # NOTE: -All is critical here — without it, Get-Command returns only
    # the first match on PATH, which on a typical Windows install is
    # the Store shim. We need to see *all* matches so we can pick the real one.
    foreach ($candidate in @("python.exe", "python3.exe", "py.exe")) {
        $candidates = Get-Command $candidate -All -ErrorAction SilentlyContinue |
                      Where-Object { $_.Source -notmatch "WindowsApps" }
        foreach ($cmd in $candidates) {
            try {
                $versionOutput = & $cmd.Source --version 2>&1
                if ($versionOutput -match "Python (\d+)\.(\d+)\.(\d+)") {
                    $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                    if ($major -eq 3 -and $minor -ge 11) {
                        return @{ Exe = $cmd.Source; Version = $versionOutput.ToString().Trim() }
                    }
                }
            } catch { }
        }
    }
    # Fallback: check well-known install locations directly, in case PATH
    # doesn't contain Python (e.g. session opened before winget added it).
    $wellKnown = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "C:\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($path in $wellKnown) {
        if (Test-Path $path) {
            try {
                $versionOutput = & $path --version 2>&1
                if ($versionOutput -match "Python (\d+)\.(\d+)\.(\d+)") {
                    $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                    if ($major -eq 3 -and $minor -ge 11) {
                        return @{ Exe = $path; Version = $versionOutput.ToString().Trim() }
                    }
                }
            } catch { }
        }
    }
    return $null
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Find-ClaudeConfigPath {
    # Prefer the Store/UWP path; fall back to the classic %APPDATA%\Claude.
    $storePackages = Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -ErrorAction SilentlyContinue |
                     Where-Object { $_.Name -like "Claude_*" }
    foreach ($pkg in $storePackages) {
        $cfg = Join-Path $pkg.FullName "LocalCache\Roaming\Claude\claude_desktop_config.json"
        $cfgDir = Split-Path $cfg
        # The Store path is only "ready" if Roaming\Claude exists, which means
        # the app has been launched at least once.
        if (Test-Path $cfgDir) {
            return @{ Kind = "store"; Path = $cfg; ExistingDir = $true }
        }
        return @{ Kind = "store"; Path = $cfg; ExistingDir = $false }
    }
    return @{
        Kind = "classic"
        Path = (Join-Path $env:APPDATA "Claude\claude_desktop_config.json")
        ExistingDir = (Test-Path (Join-Path $env:APPDATA "Claude"))
    }
}

Write-Host ""
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host "  claude-garmin - Installer for Claude Desktop"                      -ForegroundColor Cyan
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host "  Project folder: $ProjectDir"
Write-Host ""

# ----------------------------------------------------------------------------
# 1. Python
# ----------------------------------------------------------------------------
Write-Step "1/7  Check Python 3.11+"
$py = Find-Python
if (-not $py) {
    if ($SkipPythonInstall) {
        Write-Err2 "Python 3.11+ not found and SkipPythonInstall is set. Aborting."
        exit 1
    }
    Write-Warn2 "Python 3.11+ not found. Installing Python 3.12 via winget (~30s)..."
    & winget install --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Err2 "Python install failed (winget exit $LASTEXITCODE)"
        exit 1
    }
    Refresh-Path
    $py = Find-Python
    if (-not $py) {
        Write-Err2 "Python installed but not on PATH. Close and reopen PowerShell, then re-run install.bat."
        exit 1
    }
}
Write-Ok "$($py.Version) - $($py.Exe)"

# ----------------------------------------------------------------------------
# 2. Claude Desktop
# ----------------------------------------------------------------------------
Write-Step "2/7  Check Claude Desktop"
$claudeInstalled = $false
try {
    $list = & winget list --name "Claude" 2>$null
    if ($list -match "Anthropic\.Claude") { $claudeInstalled = $true }
} catch { }
if (-not $claudeInstalled) {
    if ($SkipClaudeInstall) {
        Write-Warn2 "Claude Desktop not found (skip flag set)"
    } else {
        Write-Warn2 "Claude Desktop not found. Installing via winget (~1 min)..."
        & winget install --id Anthropic.Claude --silent --accept-source-agreements --accept-package-agreements | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Claude Desktop installed"
            Write-Info "Open it once to sign in with your Anthropic account, then close it."
        } else {
            Write-Warn2 "Claude Desktop install had issues (winget exit $LASTEXITCODE). Continuing."
        }
    }
} else {
    Write-Ok "Claude Desktop already installed"
}

# ----------------------------------------------------------------------------
# 3. Virtualenv
# ----------------------------------------------------------------------------
Write-Step "3/7  Virtualenv .venv"
$venvDir    = Join-Path $ProjectDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (Test-Path $venvPython) {
    Write-Ok "venv already exists"
} else {
    & $py.Exe -m venv $venvDir
    if (-not (Test-Path $venvPython)) {
        Write-Err2 "venv creation failed"
        exit 1
    }
    Write-Ok "venv created"
}

# ----------------------------------------------------------------------------
# 4. Dependencies
# ----------------------------------------------------------------------------
Write-Step "4/7  Installing dependencies (~30-60s)"
$reqFile = Join-Path $ProjectDir "requirements.txt"
if (-not (Test-Path $reqFile)) {
    Write-Err2 "requirements.txt not found in $ProjectDir"
    exit 1
}
Write-Info "pip upgrade..."
& $venvPython -m pip install --upgrade pip --quiet 2>&1 | Out-Null
Write-Info "pip install -r requirements.txt..."
$pipOut = & $venvPython -m pip install -r $reqFile 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Err2 "pip install failed:"
    $pipOut | Select-Object -Last 15 | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
    exit 1
}
$installed = $pipOut | Select-String "Successfully installed" | Select-Object -Last 1
if ($installed) { Write-Ok ($installed.Line.Trim()) } else { Write-Ok "Dependencies already installed" }

# ----------------------------------------------------------------------------
# 5. Garmin credentials (.env)
# ----------------------------------------------------------------------------
Write-Step "5/7  Garmin credentials (.env)"
$envFile = Join-Path $ProjectDir ".env"
$needCreds = (-not (Test-Path $envFile)) -or $Reconfigure
if ($needCreds) {
    Write-Host ""
    Write-Host "    Enter your Garmin Connect credentials." -ForegroundColor White
    Write-Host "    They are stored ONLY in the local .env file (gitignored)." -ForegroundColor Gray
    Write-Host "    Garmin then issues an OAuth token that is reused and refreshed" -ForegroundColor Gray
    Write-Host "    automatically; the password is only needed for the first login." -ForegroundColor Gray
    Write-Host ""

    # Validate email: non-empty and looks like an email
    while ($true) {
        $email = (Read-Host "    Garmin email").Trim()
        if ([string]::IsNullOrWhiteSpace($email)) {
            Write-Warn2 "Email cannot be empty. Try again."
            continue
        }
        if ($email -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') {
            Write-Warn2 "That doesn't look like a valid email. Try again."
            continue
        }
        break
    }

    # Validate password: non-empty, no chars that would break the .env file
    # python-dotenv handles most special chars, but newlines and unescaped
    # double quotes would corrupt the key=value structure.
    while ($true) {
        $passwordSecure = Read-Host "    Garmin password" -AsSecureString
        $password = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($passwordSecure))
        if ([string]::IsNullOrEmpty($password)) {
            Write-Warn2 "Password cannot be empty. Try again."
            continue
        }
        if ($password -match "[`r`n]") {
            Write-Warn2 "Password must not contain newlines. Try again."
            continue
        }
        if ($password -match '"') {
            Write-Warn2 "Password contains a double-quote character, which can break the .env file."
            Write-Warn2 "If you really need it, set GARMIN_PASSWORD manually in .env after install."
            $retry = Read-Host "    Try a different password? [Y/n]"
            if ($retry -notmatch '^[nN]') { continue }
        }
        break
    }

    Write-Host ""
    $mfaAnswer = Read-Host "    Is MFA (2FA) enabled on your Garmin account? [y/N]"
    $hasMfa = ($mfaAnswer -match '^[ys]')

    # IMPORTANT: write UTF-8 WITHOUT BOM. PowerShell 5.1's Set-Content -Encoding utf8
    # always adds a BOM (EF BB BF), which python-dotenv reads as part of the first
    # key name (﻿GARMIN_EMAIL), causing GARMIN_EMAIL to look unset to Python.
    $envContent = "GARMIN_EMAIL=$email`nGARMIN_PASSWORD=$password`n"
    [System.IO.File]::WriteAllText($envFile, $envContent, [System.Text.UTF8Encoding]::new($false))
    Write-Ok ".env written"
} else {
    Write-Ok ".env already exists (use -Reconfigure to recreate)"
    $hasMfa = $false  # unknown, but if tokens already exist we don't need this
}

# ----------------------------------------------------------------------------
# 6. Garmin login
# ----------------------------------------------------------------------------
Write-Step "6/7  One-time Garmin login"
$tokenDir = Join-Path $HOME ".garminconnect"
if ($SkipGarminLogin) {
    Write-Warn2 "Login skipped (SkipGarminLogin flag)"
} elseif ((Test-Path $tokenDir) -and -not $Reconfigure) {
    Write-Ok "Tokens already present in $tokenDir (use -Reconfigure to re-login)"
} else {
    if ($hasMfa) {
        Write-Host ""
        Write-Warn2 "MFA enabled: open your authenticator app (or SMS app) and have"
        Write-Warn2 "the code ready. You'll be prompted below for 'MFA code:'"
        Write-Host ""
    }
    Push-Location $ProjectDir
    try {
        & $venvPython "garmin_mcp.py" login
        $loginExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ((Test-Path $tokenDir) -and $loginExit -eq 0) {
        Write-Ok "Login OK, tokens saved in $tokenDir"
    } else {
        Write-Err2 "Login failed. Check credentials in .env and re-run: install.bat -Reconfigure"
        exit 1
    }
}

# ----------------------------------------------------------------------------
# 7. Claude Desktop config
# ----------------------------------------------------------------------------
Write-Step "7/7  Claude Desktop configuration"
$cc = Find-ClaudeConfigPath
$claudeCfg = $cc.Path
$claudeDir = Split-Path $claudeCfg

Write-Info "Detected version: $($cc.Kind)"
Write-Info "Config path:      $claudeCfg"

if (-not (Test-Path $claudeDir)) {
    if ($cc.Kind -eq "store") {
        Write-Warn2 "The Store Claude config folder doesn't exist yet."
        Write-Warn2 "Open Claude Desktop once (sign in), close it, then re-run install.bat."
        exit 1
    }
    New-Item -ItemType Directory -Force -Path $claudeDir | Out-Null
}

$projForward = ($ProjectDir -replace '\\', '/').TrimEnd('/')
$venvPyForward = "$projForward/.venv/Scripts/python.exe"
$scriptForward = "$projForward/garmin_mcp.py"

# Smart merge: preserve preferences and any other MCP servers already configured.
if (Test-Path $claudeCfg) {
    try {
        $cfg = Get-Content $claudeCfg -Raw -Encoding utf8 | ConvertFrom-Json
    } catch {
        $backup = "$claudeCfg.bak"
        Write-Warn2 "Existing config is not valid JSON; backing it up to $backup"
        Move-Item $claudeCfg $backup -Force
        $cfg = [PSCustomObject]@{}
    }
} else {
    $cfg = [PSCustomObject]@{}
}

if (-not ($cfg.PSObject.Properties.Name -contains 'mcpServers')) {
    $cfg | Add-Member -MemberType NoteProperty -Name mcpServers -Value ([PSCustomObject]@{})
}
$garminEntry = [PSCustomObject]@{
    command = $venvPyForward
    args    = @($scriptForward)
}
if ($cfg.mcpServers.PSObject.Properties.Name -contains 'garmin') {
    $cfg.mcpServers.garmin = $garminEntry
} else {
    $cfg.mcpServers | Add-Member -MemberType NoteProperty -Name garmin -Value $garminEntry
}

$json = $cfg | ConvertTo-Json -Depth 10
# UTF-8 without BOM — Claude Desktop is fine either way but consistency matters,
# and downstream tools that read JSON sometimes choke on a leading BOM.
[System.IO.File]::WriteAllText($claudeCfg, $json, [System.Text.UTF8Encoding]::new($false))
Write-Ok "Config written"

# ----------------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------------
Write-Host ""
Write-Host "===================================================================" -ForegroundColor Green
Write-Host "  Installation complete!"                                            -ForegroundColor Green
Write-Host "===================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Fully close Claude Desktop (also from the tray icon) if it was open"
Write-Host "  2. Open it from the Start menu"
Write-Host "  3. Look for the 'garmin' connector under Connectors / Tools"
Write-Host "  4. Try: 'How's my recovery today?'"
Write-Host ""
Write-Host "Test the tools directly (without Claude Desktop):"
Write-Host "  .\.venv\Scripts\python.exe test_tools.py"
Write-Host ""
Write-Host "To re-configure credentials / re-login to Garmin:"
Write-Host "  .\install.bat -Reconfigure"
Write-Host ""
Write-Host "To uninstall the connector (leaves Python and Claude Desktop):"
Write-Host "  .\uninstall.bat"
Write-Host ""
