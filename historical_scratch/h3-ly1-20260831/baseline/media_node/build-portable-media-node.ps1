[CmdletBinding()]
param(
    [string]$OutputRoot = "",
    [switch]$UpdateOnly,
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
        $Source, $Destination, "/E", "/R:2", "/W:1", "/NFL", "/NDL",
        "/NJH", "/NJS", "/NP", "/XF", "*.pyc", "*.pyo", "/XD",
        "__pycache__"
    )
    $arguments += $ExcludedDirectories
    & robocopy.exe @arguments | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "复制目录失败：$Source -> $Destination，robocopy 退出码：$LASTEXITCODE"
    }
}

function Get-PortableRuntimeId {
    $material = New-Object Text.StringBuilder
    [void]$material.AppendLine("runninghub-media-runtime")
    foreach ($relative in @(
        "media_node\portable-runtime-version.txt",
        "media_node\requirements.txt",
        "media_node\asr_service\requirements.txt"
    )) {
        $path = Join-Path $projectRoot $relative
        [void]$material.AppendLine($relative.Replace('\', '/'))
        [void]$material.AppendLine(
            [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
        )
    }
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($material.ToString())
        $hash = $sha256.ComputeHash($bytes)
        return ([BitConverter]::ToString($hash)).Replace("-", "").ToLower().Substring(0, 16)
    } finally {
        $sha256.Dispose()
    }
}

function Copy-PortableApplication {
    param([Parameter(Mandatory = $true)][string]$DestinationRoot)

    Copy-Tree -Source (Join-Path $projectRoot "app") `
        -Destination (Join-Path $DestinationRoot "app") `
        -ExcludedDirectories @(
            (Join-Path $projectRoot "app\static"),
            (Join-Path $projectRoot "app\templates")
        )
    New-Item -ItemType Directory `
        -Path (Join-Path $DestinationRoot "media_node") -Force | Out-Null
    foreach ($name in @(
        "__init__.py", "launcher.py", "worker.py", "requirements.txt",
        ".env.example", "apply_portable_update.py"
    )) {
        Copy-Item -LiteralPath (Join-Path $nodeRoot $name) `
            -Destination (Join-Path $DestinationRoot "media_node\$name")
    }
    Copy-Tree -Source (Join-Path $nodeRoot "asr_service") `
        -Destination (Join-Path $DestinationRoot "media_node\asr_service")
    foreach ($name in @(
        "启动媒体节点.cmd", "配置媒体节点.cmd", "更新媒体节点.cmd", "使用说明.txt"
    )) {
        Copy-Item -LiteralPath (Join-Path $portableTemplates $name) `
            -Destination $DestinationRoot
    }
}

if (-not $OutputRoot) {
    $OutputRoot = Join-Path $projectRoot "dist"
}
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

$revision = (& git -C $projectRoot rev-parse --short=8 HEAD 2>$null).Trim()
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
$packageKind = if ($UpdateOnly) { "update" } else { "full" }
$packageName = "rh-media-$packageKind-$revision"
$packageRoot = Join-Path $OutputRoot ".$packageName.stage"
$archivePath = Join-Path $OutputRoot "$packageName.zip"
if ((Test-Path -LiteralPath $packageRoot) -or (Test-Path -LiteralPath $archivePath)) {
    throw "输出已存在：$packageName。请删除旧输出或指定新的 -OutputRoot。"
}
$ffmpegCommand = $null
$ffprobeCommand = $null
$basePython = ""
if (-not $UpdateOnly) {
    if (-not (Test-Path -LiteralPath $sourcePython -PathType Leaf)) {
        throw "缺少已安装的媒体节点环境。请先运行 media_node\install-media-node.ps1。"
    }
    if (-not (Test-Path -LiteralPath $sourceSitePackages -PathType Container)) {
        throw "媒体节点 Python 依赖不完整，请重新运行安装脚本。"
    }
    Invoke-Checked -Executable $sourcePython `
        -Arguments @(
            "-s", "-c",
            "import fastapi, funasr, modelscope, mutagen, psutil, requests, torch, uvicorn"
        ) `
        -Description "验证源媒体节点依赖；请重新运行 media_node\install-media-node.ps1"

    $ffmpegCommand = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    $ffprobeCommand = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
    if ($null -eq $ffmpegCommand -or $null -eq $ffprobeCommand) {
        throw "当前开发机找不到 ffmpeg 或 ffprobe，无法生成自带 FFmpeg 的完整包。"
    }
    $basePython = (& $sourcePython -c "import sys; print(sys.base_prefix)").Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $basePython)) {
        throw "无法确定媒体节点使用的基础 Python 目录。"
    }
}
New-Item -ItemType Directory -Path $packageRoot | Out-Null

$runtimeId = Get-PortableRuntimeId
Write-Host "==> 复制媒体节点程序" -ForegroundColor Cyan
Copy-PortableApplication -DestinationRoot $packageRoot
[IO.File]::WriteAllText(
    (Join-Path $packageRoot "media_node\runtime-required.txt"),
    "$runtimeId`n",
    (New-Object Text.UTF8Encoding($false))
)

if ($UpdateOnly) {
    $manifest = [ordered]@{
        formatVersion = 1
        revision = $revision
        runtimeId = $runtimeId
        createdAt = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json
    [IO.File]::WriteAllText(
        (Join-Path $packageRoot "portable-update.json"),
        "$manifest`n",
        (New-Object Text.UTF8Encoding($false))
    )
} else {
    Write-Host "==> 复制便携 Python" -ForegroundColor Cyan
    $pythonRoot = Join-Path $packageRoot "python"
    Copy-Tree -Source $basePython -Destination $pythonRoot -ExcludedDirectories @(
        (Join-Path $basePython "Lib\site-packages"),
        (Join-Path $basePython "Scripts")
    )
    Copy-Tree -Source $sourceSitePackages `
        -Destination (Join-Path $pythonRoot "Lib\site-packages")

    if ($IncludeLocalConfig -and (Test-Path -LiteralPath (Join-Path $nodeRoot ".env"))) {
        Write-Warning "完整包将包含当前服务器令牌，请只通过可信方式传输。"
        Copy-Item -LiteralPath (Join-Path $nodeRoot ".env") `
            -Destination (Join-Path $packageRoot "media_node\.env")
        $portableEnvironment = Join-Path $packageRoot "media_node\.env"
        $environmentText = Get-Content -LiteralPath $portableEnvironment -Raw -Encoding UTF8
        $environmentText = [regex]::Replace(
            $environmentText, "(?m)^MEDIA_WORKER_ID=.*$", "MEDIA_WORKER_ID="
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

    Write-Host "==> 复制 FFmpeg" -ForegroundColor Cyan
    $ffmpegBin = Join-Path $packageRoot "ffmpeg\bin"
    New-Item -ItemType Directory -Path $ffmpegBin -Force | Out-Null
    Copy-Item -LiteralPath $ffmpegCommand.Source -Destination $ffmpegBin
    Copy-Item -LiteralPath $ffprobeCommand.Source -Destination $ffmpegBin
    [IO.File]::WriteAllText(
        (Join-Path $packageRoot "portable-runtime.txt"),
        "$runtimeId`n",
        (New-Object Text.UTF8Encoding($false))
    )

    Write-Host "==> 验证完整便携包" -ForegroundColor Cyan
    $portablePython = Join-Path $pythonRoot "python.exe"
    $oldPath = $env:PATH
    $oldPythonPath = $env:PYTHONPATH
    $oldNoUserSite = $env:PYTHONNOUSERSITE
    try {
        $env:PATH = "$ffmpegBin;$oldPath"
        $env:PYTHONPATH = $packageRoot
        $env:PYTHONNOUSERSITE = "1"
        Invoke-Checked -Executable $portablePython `
            -Arguments @(
                "-s", "-c",
                "import mutagen, requests, torch; import media_node.worker; import media_node.asr_service.app; print('portable runtime ok')"
            ) `
            -Description "验证便携 Python"
        Invoke-Checked -Executable (Join-Path $ffmpegBin "ffmpeg.exe") `
            -Arguments @("-version") `
            -Description "验证便携 FFmpeg"
    } finally {
        $env:PATH = $oldPath
        $env:PYTHONPATH = $oldPythonPath
        $env:PYTHONNOUSERSITE = $oldNoUserSite
    }
}

Write-Host "==> 创建扁平 ZIP" -ForegroundColor Cyan
$archivePython = if (Test-Path -LiteralPath $sourcePython) {
    $sourcePython
} else {
    (Get-Command python.exe -ErrorAction Stop).Source
}
Invoke-Checked -Executable $archivePython `
    -Arguments @(
        (Join-Path $nodeRoot "create_portable_zip.py"),
        "--flat", $packageRoot, $archivePath
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
Write-Host "已生成：$archivePath" -ForegroundColor Green
if ($UpdateOnly) {
    Write-Host "把更新 ZIP 放进已安装节点目录，关闭节点后双击‘更新媒体节点.cmd’。" -ForegroundColor Yellow
} elseif (-not $IncludeLocalConfig) {
    Write-Host "完整包未携带真实令牌，解压后请先双击‘配置媒体节点.cmd’。" -ForegroundColor Yellow
}
