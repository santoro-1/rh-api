$ErrorActionPreference = 'Stop'

$stage = (Resolve-Path -LiteralPath 'D:\工作内容\轻盈健\数字人\.codex_tmp\semantic_clean_stage_20260819').Path
$library = (Resolve-Path -LiteralPath 'D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library').Path
$stageVideos = Join-Path $stage 'videos'
$targetVideos = Join-Path $library 'videos'
$targetCatalog = Join-Path $library 'catalog.json'
$targetQuarantine = Join-Path $library 'quarantine\platform_ui_pending.json'

if (-not $stage.StartsWith('D:\工作内容\轻盈健\数字人\.codex_tmp\semantic_clean_stage_20260819')) {
    throw "Unexpected stage path: $stage"
}
if (-not $library.StartsWith('D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library')) {
    throw "Unexpected library path: $library"
}

$directories = @(Get-ChildItem -LiteralPath $stageVideos -Directory)
if ($directories.Count -ne 45) {
    throw "Expected 45 staged directories, got $($directories.Count)"
}
foreach ($directory in $directories) {
    $destination = Join-Path $targetVideos $directory.Name
    if (Test-Path -LiteralPath $destination) {
        throw "Refusing to overwrite existing directory: $destination"
    }
}

Copy-Item -LiteralPath $targetCatalog -Destination (Join-Path $stage 'original_catalog.json')
Copy-Item -LiteralPath $targetQuarantine -Destination (Join-Path $stage 'original_platform_ui_pending.json')

foreach ($directory in $directories) {
    Copy-Item -LiteralPath $directory.FullName -Destination $targetVideos -Recurse
}

Copy-Item -LiteralPath (Join-Path $stage 'catalog.json') -Destination $targetCatalog -Force
Copy-Item -LiteralPath (Join-Path $stage 'platform_ui_pending.json') -Destination $targetQuarantine -Force

Write-Output "installed_directories=$($directories.Count)"
Write-Output "catalog=$targetCatalog"
Write-Output "quarantine=$targetQuarantine"
