param(
    [string]$Python = "D:\Myanaconda\python.exe",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8001,
    [string]$DraftRoot = "",
    [ValidateSet("embedded", "agent")]
    [string]$ExecutionMode = "embedded",
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
    $env:JYD_WEB_DRAFT_ROOT = (Resolve-Path -LiteralPath $DraftRoot).Path
} elseif (Test-Path -LiteralPath "D:\剪映草稿\JianyingPro Drafts" -PathType Container) {
    $env:JYD_WEB_DRAFT_ROOT = "D:\剪映草稿\JianyingPro Drafts"
} else {
    Remove-Item Env:JYD_WEB_DRAFT_ROOT -ErrorAction SilentlyContinue
}

$env:JYD_EXECUTION_MODE = $ExecutionMode
$env:JYD_WEB_STORAGE_ROOT = $Test.WebStorage
$env:JYD_DATABASE_PATH = Join-Path $Test.WebStorage "control.db"
$env:JYD_TEMPLATE_LIBRARY_ROOT = $Test.Templates
$env:JYD_AUDIO_LIBRARY_ROOT = Join-Path $Test.Libraries "audio_library"
$env:JYD_EFFECT_LIBRARY_ROOT = Join-Path $Test.Libraries "effect_library"
$env:JYD_FONT_LIBRARY_ROOT = Join-Path $Test.Libraries "font_library"
$env:JYD_STICKER_LIBRARY_ROOT = Join-Path $Test.Libraries "sticker_library"
$env:JYD_TEXT_EFFECT_LIBRARY_ROOT = Join-Path $Test.Libraries "text_effect_library"
$env:JYD_TEXT_STYLE_LIBRARY_ROOT = Join-Path $Test.Libraries "text_style_library"
$env:JYD_TEXT_TEMPLATE_LIBRARY_ROOT = Join-Path $Test.Libraries "text_template_library"
$env:JYD_DECRYPT_WORK_ROOT = Join-Path $Test.Root "decrypted"
$env:JYD_ADMIN_COOKIE_NAME = "jyd_admin_session_test"
$env:JYD_SITE_COOKIE_NAME = "jyd_site_session_test"
$env:JYD_AUTH_SERVER_URL = "http://127.0.0.1:8000"
$env:JYD_LTX_WORKBENCH_URL = "http://127.0.0.1:8792"
$env:JYD_AUTH_AUTHORITY = "false"

Write-Host ('[TEST] Website: http://127.0.0.1:{0}/app' -f $Port)
Write-Host ('[TEST] Data root: {0}' -f $Test.Root)
Write-Host '[TEST] Production data will not be modified.'
$Launcher = Join-Path $ProjectRoot 'apps\processor\run_web_api.py'
& $Python -u $Launcher --host $HostAddress --port $Port
