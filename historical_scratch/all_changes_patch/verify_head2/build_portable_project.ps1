param(
    [switch]$BuildReleases,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$arguments = @{}
if ($BuildReleases) {
    $arguments.BuildReleases = $true
}
if ($Python) {
    $arguments.Python = $Python
}
& (Join-Path $ProjectRoot "scripts\build\build_portable_project.ps1") @arguments
