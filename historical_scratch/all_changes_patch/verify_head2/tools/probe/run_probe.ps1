param(
    [string]$Python = "D:\Myanaconda\python.exe",
    [Parameter(Mandatory = $true)]
    [string]$TemplateDraftDir,
    [string]$OutputRoot,
    [string]$OutputName = "",
    [string]$ReplaceFirstText = "",
    [string]$ReplaceText = "",
    [Int32]$TargetTextTrackIndex = 0,
    [Int32]$TargetTextSegmentIndex = 0,
    [Int64]$TextStartUs = -1,
    [Int64]$TextDurationUs = 0,
    [string]$ReplaceVideoPath = "",
    [string]$ReplaceVideoMaterialName = "",
    [string]$ReplaceVideoSegmentPath = "",
    [Int64]$VideoSourceStartUs = -1,
    [Int64]$VideoSourceDurationUs = 0,
    [Int64]$VideoTargetStartUs = -1,
    [Int64]$VideoTargetDurationUs = 0,
    [string]$ReplaceAudioPath = "",
    [string]$ReplaceAudioMaterialName = "",
    [string]$ReplaceAudioSegmentPath = "",
    [string]$AddAudioPath = "",
    [Int32]$TargetAudioTrackIndex = 0,
    [Int32]$TargetAudioSegmentIndex = 0,
    [Int64]$AudioSourceStartUs = -1,
    [Int64]$AudioSourceDurationUs = 0,
    [Int64]$AudioTargetStartUs = -1,
    [Int64]$AudioTargetDurationUs = 0,
    [switch]$DumpEffects,
    [switch]$DumpNestedDrafts,
    [string]$ReplaceNestedVideoSegmentPath = "",
    [Int32]$TargetNestedDraftIndex = 0,
    [Int32]$TargetNestedVideoTrackIndex = 0,
    [Int32]$TargetNestedVideoSegmentIndex = 0,
    [Int64]$NestedVideoSourceStartUs = -1,
    [Int64]$NestedVideoSourceDurationUs = 0,
    [Int64]$NestedVideoTargetStartUs = -1,
    [Int64]$NestedVideoTargetDurationUs = 0,
    [string]$ExportFirstEffectJson = "",
    [string]$EffectJsonPath = "",
    [switch]$AddEffectJsonToVideo,
    [string]$EffectSourceDraftDir = "",
    [switch]$ReplaceFirstEffectFromSource,
    [Int32]$TargetVideoTrackIndex = 0,
    [Int32]$TargetVideoSegmentIndex = 0,
    [Int64]$EffectStartUs = -1,
    [Int64]$EffectDurationUs = 0,
    [Int64]$FirstVideoTargetDurationUs = 0
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$runner = Join-Path $PSScriptRoot "run_probe.py"
$cliArgs = @(
    $runner,
    "--template-draft-dir", $TemplateDraftDir
)

if ($OutputRoot) {
    $cliArgs += @("--output-root", $OutputRoot)
}
if ($OutputName) {
    $cliArgs += @("--output-name", $OutputName)
}
if ($ReplaceFirstText) {
    $cliArgs += @("--replace-first-text", $ReplaceFirstText)
}
if ($ReplaceText) {
    $cliArgs += @("--replace-text", $ReplaceText)
}
$cliArgs += @("--target-text-track-index", "$TargetTextTrackIndex")
$cliArgs += @("--target-text-segment-index", "$TargetTextSegmentIndex")
$cliArgs += @("--text-start-us", "$TextStartUs")
$cliArgs += @("--text-duration-us", "$TextDurationUs")
if ($ReplaceVideoPath) {
    $cliArgs += @("--replace-video-path", $ReplaceVideoPath)
}
if ($ReplaceVideoMaterialName) {
    $cliArgs += @("--replace-video-material-name", $ReplaceVideoMaterialName)
}
if ($ReplaceVideoSegmentPath) {
    $cliArgs += @("--replace-video-segment-path", $ReplaceVideoSegmentPath)
}
$cliArgs += @("--video-source-start-us", "$VideoSourceStartUs")
$cliArgs += @("--video-source-duration-us", "$VideoSourceDurationUs")
$cliArgs += @("--video-target-start-us", "$VideoTargetStartUs")
$cliArgs += @("--video-target-duration-us", "$VideoTargetDurationUs")
if ($ReplaceAudioPath) {
    $cliArgs += @("--replace-audio-path", $ReplaceAudioPath)
}
if ($ReplaceAudioMaterialName) {
    $cliArgs += @("--replace-audio-material-name", $ReplaceAudioMaterialName)
}
if ($ReplaceAudioSegmentPath) {
    $cliArgs += @("--replace-audio-segment-path", $ReplaceAudioSegmentPath)
}
if ($AddAudioPath) {
    $cliArgs += @("--add-audio-path", $AddAudioPath)
}
$cliArgs += @("--target-audio-track-index", "$TargetAudioTrackIndex")
$cliArgs += @("--target-audio-segment-index", "$TargetAudioSegmentIndex")
$cliArgs += @("--audio-source-start-us", "$AudioSourceStartUs")
$cliArgs += @("--audio-source-duration-us", "$AudioSourceDurationUs")
$cliArgs += @("--audio-target-start-us", "$AudioTargetStartUs")
$cliArgs += @("--audio-target-duration-us", "$AudioTargetDurationUs")
if ($DumpEffects) {
    $cliArgs += @("--dump-effects")
}
if ($DumpNestedDrafts) {
    $cliArgs += @("--dump-nested-drafts")
}
if ($ReplaceNestedVideoSegmentPath) {
    $cliArgs += @("--replace-nested-video-segment-path", $ReplaceNestedVideoSegmentPath)
}
$cliArgs += @("--target-nested-draft-index", "$TargetNestedDraftIndex")
$cliArgs += @("--target-nested-video-track-index", "$TargetNestedVideoTrackIndex")
$cliArgs += @("--target-nested-video-segment-index", "$TargetNestedVideoSegmentIndex")
$cliArgs += @("--nested-video-source-start-us", "$NestedVideoSourceStartUs")
$cliArgs += @("--nested-video-source-duration-us", "$NestedVideoSourceDurationUs")
$cliArgs += @("--nested-video-target-start-us", "$NestedVideoTargetStartUs")
$cliArgs += @("--nested-video-target-duration-us", "$NestedVideoTargetDurationUs")
if ($ExportFirstEffectJson) {
    $cliArgs += @("--export-first-effect-json", $ExportFirstEffectJson)
}
if ($EffectJsonPath) {
    $cliArgs += @("--effect-json-path", $EffectJsonPath)
}
if ($AddEffectJsonToVideo) {
    $cliArgs += @("--add-effect-json-to-video")
}
if ($EffectSourceDraftDir) {
    $cliArgs += @("--effect-source-draft-dir", $EffectSourceDraftDir)
}
if ($ReplaceFirstEffectFromSource) {
    $cliArgs += @("--replace-first-effect-from-source")
}
$cliArgs += @("--target-video-track-index", "$TargetVideoTrackIndex")
$cliArgs += @("--target-video-segment-index", "$TargetVideoSegmentIndex")
$cliArgs += @("--effect-start-us", "$EffectStartUs")
$cliArgs += @("--effect-duration-us", "$EffectDurationUs")
if ($FirstVideoTargetDurationUs -gt 0) {
    $cliArgs += @("--first-video-target-duration-us", "$FirstVideoTargetDurationUs")
}

& $Python @cliArgs
exit $LASTEXITCODE
