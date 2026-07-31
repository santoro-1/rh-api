[CmdletBinding()]
param(
    [string]$OutputRoot = "",
    [switch]$WithoutModels,
    [switch]$IncludeLocalConfig,
    [switch]$KeepExpanded
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$nodeRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $nodeRoot
$sourceRuntime = Join-Path $nodeRoot ".runtime\venv"
$sourcePython = Join-Path $sourceRuntime "Scripts\python.exe"
$sourceSitePackages = Join-Path $sourceRuntime "Lib\site-packages"
$portableTemplates = Join-Path $nodeRoot "portable"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description 失败，退出码：$LASTEXITCODE"
    }
}

function Copy-Tree {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string[]]$ExcludedDirectories = @()
    )
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $arguments = @(
        $Source,
        $Destination,
        "/E",
        "/R:2",
        "/W:1",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP",
        "/XF",
        "*.pyc",
        "*.pyo"
    )
    if ($ExcludedDirectories.Count -gt 0) {
        $arguments += "/XD"
        $arguments += $ExcludedDirectories
    }
    & robocopy.exe @arguments | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "复制目录失败：$Source -> $Destination，robocopy 退出码：$LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $sourcePython -PathType Leaf)) {
    throw "缺少已安装的媒体节点环境。请先运行 media_node\install-media-node.ps1。"
}
if (-not (Test-Path -LiteralPath $sourceSitePackages -PathType Container)) {
    throw "媒体节点 Python 依赖不完整，请重新运行安装脚本。"
}

$ffmpegCommand = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
$ffprobeCommand = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
if ($null -eq $ffmpegCommand -or $null -eq $ffprobeCommand) {
    throw "当前开发机找不到 ffmpeg 或 ffprobe，无法生成自带 FFmpeg 的便携包。"
}

$basePython = (& $sourcePython -c "import sys; print(sys.base_prefix)").Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $basePython)) {
    throw "无法确定媒体节点使用的基础 Python 目录。"
}

if (-not $OutputRoot) {
    $OutputRoot = Join-Path $projectRoot "dist"
}
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

$revision = (& git -C $projectRoot rev-parse --short=12 HEAD 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or -not $revision) {
    $revision = Get-Date -Format "yyyyMMddHHmmss"
} else {
    & git -C $projectRoot diff --quiet HEAD --
    $workingTreeExitCode = $LASTEXITCODE
    if ($workingTreeExitCode -eq 1) {
        $revision = "$revision-dirty"
    } elseif ($workingTreeExitCode -gt 1) {
        $revision = Get-Date -Format "yyyyMMddHHmmss"
    }
}
$packageName = "runninghub-media-node-windows-x64-$revision"
$packageRoot = Join-Path $OutputRoot $packageName
$archivePath = Join-Path $OutputRoot "$packageName.zip"
if ((Test-Path -LiteralPath $packageRoot) -or (Test-Path -LiteralPath $archivePath)) {
    throw "输出已存在：$packageName。请删除旧输出或指定新的 -OutputRoot。"
}
New-Item -ItemType Directory -Path $packageRoot | Out-Null

Write-Host "==> 复制便携 Python" -ForegroundColor Cyan
$pythonRoot = Join-Path $packageRoot "python"
Copy-Tree -Source $basePython -Destination $pythonRoot -ExcludedDirectories @(
    (Join-Path $basePython "Lib\site-packages"),
    (Join-Path $basePython "Scripts"),
    (Join-Path $basePython "__pycache__")
)
Copy-Tree -Source $sourceSitePackages `
    -Destination (Join-Path $pythonRoot "Lib\site-packages") `
    -ExcludedDirectories @((Join-Path $sourceSitePackages "__pycache__"))

Write-Host "==> 复制媒体节点程序" -ForegroundColor Cyan
Copy-Tree -Source (Join-Path $projectRoot "app") `
    -Destination (Join-Path $packageRoot "app") `
    -ExcludedDirectories @(
        (Join-Path $projectRoot "app\__pycache__"),
        (Join-Path $projectRoot "app\static"),
        (Join-Path $projectRoot "app\templates")
    )
New-Item -ItemType Directory -Path (Join-Path $packageRoot "media_node") -Force | Out-Null
foreach ($name in @(
    "__init__.py",
    "launcher.py",
    "worker.py",
    "requirements.txt",
    ".env.example"
)) {
    Copy-Item -LiteralPath (Join-Path $nodeRoot $name) `
        -Destination (Join-Path $packageRoot "media_node\$name")
}
Copy-Tree -Source (Join-Path $nodeRoot "asr_service") `
    -Destination (Join-Path $packageRoot "media_node\asr_service") `
    -ExcludedDirectories @((Join-Path $nodeRoot "asr_service\__pycache__"))

if ($IncludeLocalConfig -and (Test-Path -LiteralPath (Join-Path $nodeRoot ".env"))) {
    Write-Warning "便携包将包含当前服务器令牌，请只通过可信方式传输。"
    Copy-Item -LiteralPath (Join-Path $nodeRoot ".env") `
        -Destination (Join-Path $packageRoot "media_node\.env")
    $portableEnvironment = Join-Path $packageRoot "media_node\.env"
    $environmentText = Get-Content -LiteralPath $portableEnvironment `
        -Raw -Encoding UTF8
    $environmentText = [regex]::Replace(
        $environmentText,
        "(?m)^MEDIA_WORKER_ID=.*$",
        "MEDIA_WORKER_ID="
    )
    [IO.File]::WriteAllText(
        $portableEnvironment,
        $environmentText,
        (New-Object Text.UTF8Encoding($false))
    )
} else {
    Copy-Item -LiteralPath (Join-Path $nodeRoot ".env.example") `
        -Destination (Join-Path $packageRoot "media_node\.env")
}

if (-not $WithoutModels) {
    $models = Join-Path $nodeRoot ".runtime\models"
    if (Test-Path -LiteralPath $models -PathType Container) {
        Write-Host "==> 复制 ASR 模型缓存" -ForegroundColor Cyan
        Copy-Tree -Source $models `
            -Destination (Join-Path $packageRoot "media_node\.runtime\models")
    } else {
        Write-Warning "本机没有 ASR 模型缓存，新电脑首次处理时会自动下载。"
    }
}

Write-Host "==> 复制 FFmpeg 和一键启动文件" -ForegroundColor Cyan
$ffmpegBin = Join-Path $packageRoot "ffmpeg\bin"
New-Item -ItemType Directory -Path $ffmpegBin -Force | Out-Null
Copy-Item -LiteralPath $ffmpegCommand.Source -Destination $ffmpegBin
Copy-Item -LiteralPath $ffprobeCommand.Source -Destination $ffmpegBin
Copy-Item -LiteralPath (Join-Path $portableTemplates "启动媒体节点.cmd") -Destination $packageRoot
Copy-Item -LiteralPath (Join-Path $portableTemplates "配置媒体节点.cmd") -Destination $packageRoot
Copy-Item -LiteralPath (Join-Path $portableTemplates "使用说明.txt") -Destination $packageRoot

Write-Host "==> 验证便携包" -ForegroundColor Cyan
$portablePython = Join-Path $pythonRoot "python.exe"
$oldPath = $env:PATH
$oldPythonPath = $env:PYTHONPATH
try {
    $env:PATH = "$ffmpegBin;$oldPath"
    $env:PYTHONPATH = $packageRoot
    Invoke-Checked -Executable $portablePython `
        -Arguments @(
            "-c",
            "import requests, torch; import media_node.worker; import media_node.asr_service.app; print('portable runtime ok')"
        ) `
        -Description "验证便携 Python"
    Invoke-Checked -Executable (Join-Path $ffmpegBin "ffmpeg.exe") `
        -Arguments @("-version") `
        -Description "验证便携 FFmpeg"
} finally {
    $env:PATH = $oldPath
    $env:PYTHONPATH = $oldPythonPath
}

Write-Host "==> 创建 ZIP（依赖和模型较大，这一步可能需要数分钟）" -ForegroundColor Cyan
Invoke-Checked -Executable $sourcePython `
    -Arguments @(
        (Join-Path $nodeRoot "create_portable_zip.py"),
        $packageRoot,
        $archivePath
    ) `
    -Description "创建便携包 ZIP"

if (-not $KeepExpanded) {
    $resolvedOutput = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\')
    $resolvedPackage = [IO.Path]::GetFullPath($packageRoot)
    if (-not $resolvedPackage.StartsWith($resolvedOutput + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理不在输出目录内的临时打包目录：$resolvedPackage"
    }
    Remove-Item -LiteralPath $resolvedPackage -Recurse -Force
}

Write-Host ""
Write-Host "便携包已生成：$archivePath" -ForegroundColor Green
if (-not $IncludeLocalConfig) {
    Write-Host "该包未携带真实令牌，解压后请先双击‘配置媒体节点.cmd’。" -ForegroundColor Yellow
}
