Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$Args = @()
    )

    try {
        $version = & $Exe @Args -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        if ([version]$version.Trim() -ge [version]"3.10") {
            return [pscustomobject]@{ Exe = $Exe; Args = $Args; Version = $version.Trim() }
        }
    } catch {
        return $null
    }
    return $null
}

function Find-Python {
    $candidates = @(
        @{ Exe = "py"; Args = @("-3.13") },
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "py"; Args = @("-3.11") },
        @{ Exe = "py"; Args = @("-3.10") },
        @{ Exe = "python3.13"; Args = @() },
        @{ Exe = "python3.12"; Args = @() },
        @{ Exe = "python3.11"; Args = @() },
        @{ Exe = "python3.10"; Args = @() },
        @{ Exe = "python3"; Args = @() },
        @{ Exe = "python"; Args = @() }
    )

    foreach ($candidate in $candidates) {
        if (Get-Command $candidate.Exe -ErrorAction SilentlyContinue) {
            $result = Test-PythonCandidate -Exe $candidate.Exe -Args $candidate.Args
            if ($null -ne $result) {
                return $result
            }
        }
    }
    return $null
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required but was not found on PATH. Install Git for Windows and rerun this installer."
}

$Python = Find-Python
if ($null -eq $Python) {
    throw "Python 3.10+ is required but was not found on PATH."
}

Write-Host "[install] Using Python $($Python.Version)"

$VenvPython = Join-Path $Venv "Scripts\python.exe"
$VenvAgent = Join-Path $Venv "Scripts\cdx-agent.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "[install] Creating virtual environment at $Venv"
    & $Python.Exe @($Python.Args + @("-m", "venv", $Venv))
}

Write-Host "[install] Upgrading pip"
& $VenvPython -m pip install --upgrade pip

Write-Host "[install] Installing CDX-AGENT in editable mode"
& $VenvPython -m pip install -e $Root

Write-Host "[install] Verifying cdx-agent --help"
& $VenvAgent --help | Out-Null

Write-Host "[install] Success"
Write-Host "[install] To activate the environment:"
Write-Host "  $Venv\Scripts\Activate.ps1"
