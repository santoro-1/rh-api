param(
    [string]$Python = "D:\Myanaconda\python.exe",
    [string]$HostAddress = "",
    [int]$Port = 8010,
    [string]$DraftRoot = "",
    [string]$DigitalHumanServerUrl = "https://video.lanyingjk01.com",
    [string]$LtxWorkbenchUrl = "http://127.0.0.1:8791",
    [ValidateSet("embedded", "agent")]
    [string]$ExecutionMode = "embedded",
    [ValidateSet("standalone", "shared")]
    [string]$ProcessingMode = "standalone"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataRoot = Join-Path $ProjectRoot "data"
$LibrariesRoot = Join-Path $DataRoot "libraries"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
$env:JYD_WEB_STORAGE_ROOT = Join-Path $DataRoot "web_storage"
$env:JYD_DATABASE_PATH = Join-Path $env:JYD_WEB_STORAGE_ROOT "control.db"
$env:JYD_TEMPLATE_LIBRARY_ROOT = Join-Path $DataRoot "template_library"
$env:JYD_AUDIO_LIBRARY_ROOT = Join-Path $LibrariesRoot "audio_library"
$env:JYD_EFFECT_LIBRARY_ROOT = Join-Path $LibrariesRoot "effect_library"
$env:JYD_FONT_LIBRARY_ROOT = Join-Path $LibrariesRoot "font_library"
$env:JYD_STICKER_LIBRARY_ROOT = Join-Path $LibrariesRoot "sticker_library"
$env:JYD_CORNER_STICKER_LIBRARY_ROOT = Join-Path $LibrariesRoot "corner_sticker_library"
$env:JYD_TEXT_EFFECT_LIBRARY_ROOT = Join-Path $LibrariesRoot "text_effect_library"
$env:JYD_TEXT_STYLE_LIBRARY_ROOT = Join-Path $LibrariesRoot "text_style_library"
$env:JYD_TEXT_TEMPLATE_LIBRARY_ROOT = Join-Path $LibrariesRoot "text_template_library"
$env:JYD_PERSONAL_LIBRARY_ROOT = Join-Path $DataRoot "personal_libraries"
$env:JYD_DECRYPT_WORK_ROOT = Join-Path $ProjectRoot "runtime\decrypted_work"
$env:JYD_ALLOW_LOCAL_FILE_ACCESS = "true"
$env:JYD_AUTH_SERVER_URL = $DigitalHumanServerUrl.TrimEnd("/")
$AuthHost = ([System.Uri]$env:JYD_AUTH_SERVER_URL).Host.ToLowerInvariant()
$CookieEnvironment = if ($AuthHost -in @("127.0.0.1", "localhost", "::1")) { "local" } else { "production" }
$env:JYD_ADMIN_COOKIE_NAME = "jyd_admin_session_$CookieEnvironment"
$env:JYD_SITE_COOKIE_NAME = "jyd_site_session_$CookieEnvironment"
$env:JYD_LTX_WORKBENCH_URL = $LtxWorkbenchUrl.TrimEnd("/")
$env:JYD_AUTH_AUTHORITY = "false"
if (-not $HostAddress) {
    $HostAddress = if ($ProcessingMode -eq "standalone") { "127.0.0.1" } else { "0.0.0.0" }
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
Write-Host ('[PRODUCTION] Website: http://127.0.0.1:{0}/app/new' -f $Port)
Write-Host ('[PRODUCTION] Digital human server: {0}' -f $env:JYD_AUTH_SERVER_URL)
Write-Host ('[PRODUCTION] Lip-sync workbench: {0}' -f $env:JYD_LTX_WORKBENCH_URL)
& $Python -u (Join-Path $ProjectRoot "apps\processor\run_web_api.py") --host $HostAddress --port $Port
