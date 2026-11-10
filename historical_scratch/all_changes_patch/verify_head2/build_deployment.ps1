param(
    [switch]$Clean,
    [switch]$UpdateOnly,
    [switch]$WithoutLibraries,
    [string]$DigitalHumanServerUrl = "",
    [ValidateSet("Fastest", "Optimal", "NoCompression")]
    [string]$CompressionLevel = "Fastest"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Arguments = @{ CompressionLevel = $CompressionLevel }
if ($Clean) {
    $Arguments.Clean = $true
}

Write-Host "Building the two packages used by the single processing-computer deployment..."
& (Join-Path $ProjectRoot "scripts\build\build_collector.ps1") @Arguments
$ProcessorArguments = $Arguments.Clone()
if ($UpdateOnly) {
    $ProcessorArguments.UpdateOnly = $true
}
if ($WithoutLibraries) {
    $ProcessorArguments.WithoutLibraries = $true
}
if ($DigitalHumanServerUrl) {
    $ProcessorArguments.DigitalHumanServerUrl = $DigitalHumanServerUrl
}
& (Join-Path $ProjectRoot "scripts\build\build_processor.ps1") @ProcessorArguments
Write-Host "Ready: $ProjectRoot\release\JianyingDraftCollector-windows-x64.zip"
if ($UpdateOnly) {
    Write-Host "Ready: $ProjectRoot\release\JianyingRenderServer-update-windows-x64.zip"
} elseif ($WithoutLibraries) {
    Write-Host "Ready: $ProjectRoot\release\JianyingRenderServer-no-libraries-windows-x64.zip"
} else {
    Write-Host "Ready: $ProjectRoot\release\JianyingRenderServer-windows-x64.zip"
}
