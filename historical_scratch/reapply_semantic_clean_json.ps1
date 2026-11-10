$ErrorActionPreference = 'Stop'

$stage = (Resolve-Path -LiteralPath 'D:\工作内容\轻盈健\数字人\.codex_tmp\semantic_clean_stage_20260819').Path
$library = (Resolve-Path -LiteralPath 'D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library').Path
$catalogTarget = Join-Path $library 'catalog.json'
$quarantineTarget = Join-Path $library 'quarantine\platform_ui_pending.json'

Copy-Item -LiteralPath (Join-Path $stage 'catalog.json') -Destination $catalogTarget -Force
Copy-Item -LiteralPath (Join-Path $stage 'platform_ui_pending.json') -Destination $quarantineTarget -Force

Write-Output 'reapplied catalog and quarantine JSON'
