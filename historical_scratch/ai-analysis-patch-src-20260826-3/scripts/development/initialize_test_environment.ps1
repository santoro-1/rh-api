param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [switch]$ResetData
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$RuntimeRoot = Join-Path $ProjectRoot "runtime"
$TestRoot = Join-Path $RuntimeRoot "test_environment"

if ($ResetData -and (Test-Path -LiteralPath $TestRoot -PathType Container)) {
    $ResolvedTestRoot = (Resolve-Path -LiteralPath $TestRoot).Path
    $ExpectedPrefix = $RuntimeRoot.TrimEnd('\') + '\'
    if (-not $ResolvedTestRoot.StartsWith($ExpectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理项目 runtime 目录以外的路径: $ResolvedTestRoot"
    }
    Remove-Item -LiteralPath $ResolvedTestRoot -Recurse -Force
}

New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null
$TestLibraries = Join-Path $TestRoot "libraries"
$TestTemplates = Join-Path $TestRoot "template_library"

function Initialize-TestCopy {
    param(
        [string]$Source,
        [string]$Destination
    )
    if (Test-Path -LiteralPath $Destination -PathType Container) {
        return
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    if (Test-Path -LiteralPath $Source -PathType Container) {
        Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $Destination -Recurse -Force
    }
}

Initialize-TestCopy -Source (Join-Path $ProjectRoot "data\libraries") -Destination $TestLibraries
Initialize-TestCopy -Source (Join-Path $ProjectRoot "data\template_library") -Destination $TestTemplates
New-Item -ItemType Directory -Path (Join-Path $TestRoot "web_storage") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $TestRoot "collector_state") -Force | Out-Null

[pscustomobject]@{
    Root = $TestRoot
    Libraries = $TestLibraries
    Templates = $TestTemplates
    WebStorage = (Join-Path $TestRoot "web_storage")
    CollectorState = (Join-Path $TestRoot "collector_state")
}
