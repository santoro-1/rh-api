param(
    [string]$Python = "D:\Myanaconda\python.exe",
    [string]$ServerUrl = "http://127.0.0.1:8010",
    [string]$AgentId = $env:COMPUTERNAME,
    [string]$Name = $env:COMPUTERNAME,
    [string]$Token = "",
    [string]$DraftRoot = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
if ($PSBoundParameters.Count -eq 0) {
    & $Python -u (Join-Path $ProjectRoot "apps\agent\run_agent.py") --gui
    exit $LASTEXITCODE
}
if (-not $Token) {
    $Token = $env:JYD_AGENT_TOKEN
}
if (-not $Token) {
    throw "Missing agent token. Pass -Token or set JYD_AGENT_TOKEN."
}
$arguments = @(
    "-u",
    (Join-Path $ProjectRoot "apps\agent\run_agent.py"),
    "--server-url", $ServerUrl,
    "--agent-id", $AgentId,
    "--name", $Name,
    "--token", $Token
)
if ($DraftRoot) {
    $arguments += @("--draft-root", $DraftRoot)
}
& $Python @arguments
