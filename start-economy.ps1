#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Start the kryten-economy service.

.DESCRIPTION
    Locates Python (virtual env, conda, or system), resolves the config file,
    and launches kryten-economy.  Passes any extra arguments straight through
    to the service (e.g. --log-level DEBUG).

.PARAMETER Config
    Path to config.yaml.  Defaults to .\config.yaml in the script directory.

.PARAMETER LogLevel
    Logging verbosity: DEBUG | INFO | WARNING | ERROR.  Default: INFO.

.PARAMETER ValidateConfig
    Parse and validate config.yaml then exit without starting the service.

.PARAMETER NoBanner
    Suppress the startup banner.

.EXAMPLE
    .\start-economy.ps1

.EXAMPLE
    .\start-economy.ps1 -LogLevel DEBUG

.EXAMPLE
    .\start-economy.ps1 -Config C:\kryten\config.yaml -LogLevel DEBUG

.EXAMPLE
    .\start-economy.ps1 -ValidateConfig
#>

[CmdletBinding()]
param(
    [string] $Config    = "",
    [ValidateSet("DEBUG","INFO","WARNING","ERROR")]
    [string] $LogLevel  = "INFO",
    [switch] $ValidateConfig,
    [switch] $NoBanner
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Resolve script root ───────────────────────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-VenvValid {
    param([string]$Path)

    if (-not (Test-Path $Path)) { return $false }

    $pythonExe = Join-Path $Path "Scripts\python.exe"
    $pipExe = Join-Path $Path "Scripts\pip.exe"
    $pyvenvCfg = Join-Path $Path "pyvenv.cfg"

    if (-not (Test-Path $pyvenvCfg)) { return $false }
    if (-not (Test-Path $pythonExe) -or -not (Test-Path $pipExe)) { return $false }

    try {
        & $pythonExe -c "import sys" *> $null
        return $true
    } catch {
        return $false
    }
}

function New-VirtualEnvironment {
    param([string]$Path)

    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCmd) {
        & uv venv $Path
    } else {
        & python -m venv $Path
    }
}

function Ensure-PreferredVenv {
    $venvPath = Join-Path $ScriptDir ".venv"

    if (Test-VenvValid $venvPath) {
        return
    }

    if (Test-Path $venvPath) {
        try {
            Remove-Item -Recurse -Force $venvPath -ErrorAction Stop
        } catch {
            throw "Could not remove corrupted .venv at $venvPath. Close terminals/processes using it and retry."
        }
    }

    New-VirtualEnvironment $venvPath

    if (-not (Test-VenvValid $venvPath)) {
        throw "Failed to create a valid .venv at $venvPath"
    }
}

# ── Banner ────────────────────────────────────────────────────────────
if (-not $NoBanner) {
    Write-Host ""
    Write-Host "  kryten-economy" -ForegroundColor Cyan -NoNewline
    Write-Host "  channel currency microservice" -ForegroundColor DarkGray
    Write-Host ""
}

# ── Locate Python ─────────────────────────────────────────────────────
function Find-Python {
    # 1. Preferred local virtual env (.venv)
    $preferred = Join-Path $ScriptDir ".venv"
    if (Test-VenvValid $preferred) {
        return (Join-Path $preferred "Scripts\python.exe")
    }

    # 2. Other local virtual env names
    foreach ($venvName in @(".venv", "venv", "env")) {
        $venvPath = Join-Path $ScriptDir $venvName
        if (Test-VenvValid $venvPath) {
            return (Join-Path $venvPath "Scripts\python.exe")
        }
    }

    # 3. Conda env (CONDA_PREFIX set by 'conda activate')
    if ($env:CONDA_PREFIX) {
        $candidate = Join-Path $env:CONDA_PREFIX "python.exe"
        if (Test-Path $candidate) { return $candidate }
    }

    # 4. python / python3 on PATH
    foreach ($name in @("python", "python3")) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }

    return $null
}

Ensure-PreferredVenv

$Python = Find-Python
if (-not $Python) {
    Write-Error "Could not find a Python interpreter.  Activate a virtual environment or install Python."
    exit 1
}

Write-Host "  Python   : $Python" -ForegroundColor DarkGray

# ── Verify kryten_economy is importable ──────────────────────────────
$check = & $Python -c "import kryten_economy" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "kryten_economy is not installed in this Python environment.`nRun: pip install -e ."
    exit 1
}

# ── Resolve config path ───────────────────────────────────────────────
if ($Config -eq "") {
    $Config = Join-Path $ScriptDir "config.yaml"
}
$Config = Resolve-Path $Config -ErrorAction SilentlyContinue
if (-not $Config) {
    Write-Error "Config file not found.  Use -Config <path> or place config.yaml next to this script."
    exit 1
}

Write-Host "  Config   : $Config" -ForegroundColor DarkGray
Write-Host "  LogLevel : $LogLevel" -ForegroundColor DarkGray
Write-Host ""

# ── Build argument list ───────────────────────────────────────────────
$ServiceArgs = @(
    "-m", "kryten_economy",
    "--config", "$Config",
    "--log-level", $LogLevel
)
if ($ValidateConfig) {
    $ServiceArgs += "--validate-config"
}

# ── Run ───────────────────────────────────────────────────────────────
if ($ValidateConfig) {
    Write-Host "  Validating config..." -ForegroundColor Yellow
} else {
    Write-Host "  Starting service  (Ctrl+C to stop)" -ForegroundColor Green
    Write-Host ""
}

try {
    & $Python @ServiceArgs
    $exitCode = $LASTEXITCODE
} catch {
    Write-Host ""
    Write-Host "  Service interrupted." -ForegroundColor Yellow
    $exitCode = 130
}

Write-Host ""
if ($exitCode -eq 0 -or $exitCode -eq 130) {
    Write-Host "  kryten-economy stopped." -ForegroundColor DarkGray
} else {
    Write-Host "  kryten-economy exited with code $exitCode." -ForegroundColor Red
}
Write-Host ""
exit $exitCode
