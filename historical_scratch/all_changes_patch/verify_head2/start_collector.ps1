param(
    [string]$Python = "D:\Myanaconda\python.exe",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765,
    [string]$ServerUrl = "http://127.0.0.1:8010",
    [string]$AccessToken = "operator123",
    [string]$DraftRoot = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:JYD_PERSONAL_LIBRARY_ROOT = Join-Path $ProjectRoot "data\personal_libraries"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
$env:JYD_COLLECTOR_STATE_ROOT = Join-Path $ProjectRoot "runtime\collector_state"
$env:JYD_COLLECTOR_WORKSPACE_ROOT = Join-Path $ProjectRoot "data\libraries"
$env:JYD_FONT_LIBRARY_ROOT = Join-Path $ProjectRoot "data\libraries\font_library"
$env:JYD_RENDER_SERVER_URL = $ServerUrl
$env:JYD_ACCESS_TOKEN = $AccessToken
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
$arguments = @(
    "-u",
    (Join-Path $ProjectRoot "apps\collector\run_local_collector.py"),
    "--host", $HostAddress,
    "--port", $Port
)
if ($env:JYD_LOCAL_DRAFT_ROOT) {
    $arguments += @("--draft-root", $env:JYD_LOCAL_DRAFT_ROOT)
}
if ($ServerUrl) {
    $arguments += @("--server-url", $ServerUrl)
}
if ($AccessToken) {
    $arguments += @("--access-token", $AccessToken)
}
& $Python @arguments
