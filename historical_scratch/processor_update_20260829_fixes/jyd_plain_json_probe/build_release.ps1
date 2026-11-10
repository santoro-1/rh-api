param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$arguments = @{}
if ($Python) {
    $arguments.Python = $Python
}
& (Join-Path $ProjectRoot "scripts\build\build_all_releases.ps1") @arguments
