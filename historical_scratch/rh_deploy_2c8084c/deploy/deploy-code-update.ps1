[CmdletBinding()]
param(
    [switch]$Deploy,
    [switch]$SkipTests,
    [string]$Server = "root@47.111.171.139",
    [string]$SshKey = "C:\Users\san\.ssh\jyd_auth_deploy_ed25519",
    [string]$AppDir = "/opt/runninghub-video",
    [string]$BackupDir = "/var/backups/runninghub-video",
    [string]$LinuxUser = "rhvideo",
    [string]$Domain = "video.lanyingjk01.com",
    [ValidateRange(5, 720)][int]$DrainTimeoutMinutes = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedUpdater = Join-Path $PSScriptRoot "deploy-update.ps1"
if (-not (Test-Path -LiteralPath $sharedUpdater -PathType Leaf)) {
    throw "找不到共享部署脚本：$sharedUpdater"
}

$forward = @{
    Server = $Server
    SshKey = $SshKey
    AppDir = $AppDir
    BackupDir = $BackupDir
    LinuxUser = $LinuxUser
    Domain = $Domain
    DrainTimeoutMinutes = $DrainTimeoutMinutes
    BackupMode = "Code"
}
if ($Deploy) {
    $forward["Deploy"] = $true
}
if ($SkipTests) {
    $forward["SkipTests"] = $true
}

& $sharedUpdater @forward
