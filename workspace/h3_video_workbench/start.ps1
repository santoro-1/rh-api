$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = Join-Path $projectRoot "src"
$port = if ($env:H3_WORKBENCH_PORT) { $env:H3_WORKBENCH_PORT } else { "8792" }
Set-Location $projectRoot
Start-Process "http://127.0.0.1:$port"
python -m uvicorn h3_workbench.api:app --host 127.0.0.1 --port $port
