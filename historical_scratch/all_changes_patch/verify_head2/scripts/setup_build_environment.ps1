param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildCacheRoot = Join-Path (Split-Path -Parent $ProjectRoot) ".jyd-build-cache"
$EnvironmentRoot = Join-Path $BuildCacheRoot ".collector-build-cpython"
$EnvironmentPython = Join-Path $EnvironmentRoot "Scripts\python.exe"

function New-BuildEnvironment {
    param([switch]$Clear)

    New-Item -ItemType Directory -Path $BuildCacheRoot -Force | Out-Null
    $VenvArguments = @("-m", "venv")
    if ($Clear) {
        $VenvArguments += "--clear"
    }
    $VenvArguments += $EnvironmentRoot
    & $Python @VenvArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the build environment with: $Python"
    }
}

function Test-BuildEnvironmentPip {
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $EnvironmentPython -m pip --version 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
}

if (-not (Test-Path -LiteralPath $EnvironmentPython -PathType Leaf)) {
    New-BuildEnvironment
}

# Environments previously created by `uv venv` can contain Python but omit pip.
# Repair that cache in place first; only rebuild it if ensurepip is unavailable.
if (-not (Test-BuildEnvironmentPip)) {
    Write-Host "Build environment has no pip; repairing it with ensurepip..."
    & $EnvironmentPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not repair pip in the existing cache; rebuilding the environment."
        New-BuildEnvironment -Clear
    }
}

if (-not (Test-BuildEnvironmentPip)) {
    throw "Build environment is missing pip after repair: $EnvironmentRoot"
}

& $EnvironmentPython -m pip install -r (Join-Path $ProjectRoot "requirements-collector-build.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed with exit code $LASTEXITCODE"
}
Write-Host "Build environment ready: $EnvironmentRoot"
