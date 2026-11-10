param(
    [switch]$Clean,
    [ValidateSet("Fastest", "Optimal", "NoCompression")]
    [string]$CompressionLevel = "Fastest"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $ProjectRoot "scripts\build\build_processor.ps1") `
    -DeploymentMode shared `
    -CompressionLevel $CompressionLevel `
    -Clean:$Clean

Write-Host "Ready: $ProjectRoot\release\JianyingRenderServer-shared-windows-x64.zip"
