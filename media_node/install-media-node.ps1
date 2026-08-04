[CmdletBinding()]
param(
    [string]$PythonCommand = "python",
    [string]$PypiIndexUrl = "https://pypi.org/simple",
    [string]$TorchIndexUrl = "https://download.pytorch.org/whl/cpu"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$nodeRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $nodeRoot
$runtimeRoot = Join-Path $nodeRoot ".runtime"
$venvPython = Join-Path $runtimeRoot "venv\Scripts\python.exe"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host "Creating the isolated media-node Python environment..." -ForegroundColor Cyan
    Invoke-Checked -Executable $PythonCommand `
        -Arguments @("-m", "venv", (Join-Path $runtimeRoot "venv")) `
        -Description "Create Python environment"
}

Invoke-Checked -Executable $venvPython `
    -Arguments @("-m", "pip", "install", "--upgrade", "pip") `
    -Description "Upgrade pip"
Invoke-Checked -Executable $venvPython `
    -Arguments @(
        "-m", "pip", "install", "torch", "torchaudio",
        "--index-url", $TorchIndexUrl
    ) `
    -Description "Install PyTorch"
Invoke-Checked -Executable $venvPython `
    -Arguments @(
        "-m", "pip", "install",
        "-r", (Join-Path $nodeRoot "requirements.txt"),
        "-i", $PypiIndexUrl
    ) `
    -Description "Install media-node dependencies"
Invoke-Checked -Executable $venvPython `
    -Arguments @(
        "-s",
        "-c",
        "import fastapi, funasr, modelscope, mutagen, psutil, requests, torch, uvicorn"
    ) `
    -Description "Verify isolated media-node dependencies"

foreach ($command in @("ffmpeg", "ffprobe")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command was not found. Install FFmpeg and add it to PATH."
    }
}

$environmentFile = Join-Path $nodeRoot ".env"
if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $nodeRoot ".env.example") `
        -Destination $environmentFile
    Write-Host "Created media_node\.env. Add the server token before starting." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Media node installation completed." -ForegroundColor Green
Write-Host "Next: edit media_node\.env, then double-click the start command in media_node."
