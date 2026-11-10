param(
    [string]$Python = "D:\Myanaconda\python.exe",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8766,
    [string]$ServerUrl = "http://127.0.0.1:8001",
    [string]$AccessToken = "operator123",
    [string]$DraftRoot = "",
    [switch]$ResetData
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Test = & (Join-Path $ProjectRoot "scripts\development\initialize_test_environment.ps1") `
    -ProjectRoot $ProjectRoot -ResetData:$ResetData

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
if ($DraftRoot) {
    if (-not (Test-Path -LiteralPath $DraftRoot -PathType Container)) {
        throw "剪映草稿目录不存在: $DraftRoot"
    }
    $env:JYD_LOCAL_DRAFT_ROOT = (Resolve-Path -LiteralPath $DraftRoot).Path
} elseif (Test-Path -LiteralPath "D:\剪映草稿\JianyingPro Drafts" -PathType Container) {
    $env:JYD_LOCAL_DRAFT_ROOT = "D:\剪映草稿\JianyingPro Drafts"
} else {
    Remove-Item Env:JYD_LOCAL_DRAFT_ROOT -ErrorAction SilentlyContinue
}

$env:JYD_COLLECTOR_STATE_ROOT = $Test.CollectorState
$env:JYD_COLLECTOR_WORKSPACE_ROOT = $Test.Libraries
$env:JYD_FONT_LIBRARY_ROOT = Join-Path $Test.Libraries "font_library"
$env:JYD_RENDER_SERVER_URL = $ServerUrl
$env:JYD_ACCESS_TOKEN = $AccessToken

Write-Host "[TEST] 采集器: http://127.0.0.1:$Port"
Write-Host "[TEST] 上传目标: $ServerUrl"
Write-Host "[TEST] 数据目录: $($Test.Root)"
& $Python -u (Join-Path $ProjectRoot "apps\collector\run_local_collector.py") `
    --host $HostAddress `
    --port $Port `
    --server-url $ServerUrl `
    --access-token $AccessToken
