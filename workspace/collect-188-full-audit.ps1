[CmdletBinding()]
param(
    [string]$ProgramRoot = "E:\cxd\PublicVideo-x64\digital-human",
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Utf8Json {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Depth = 12
    )
    $json = $Value | ConvertTo-Json -Depth $Depth
    if ($null -eq $json) { $json = "[]" }
    [System.IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Protect-Text {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return $null }
    $text = [string]$Value
    $text = [regex]::Replace(
        $text,
        '(?i)(authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|password|secret)\s*[:=]\s*[^\s,;]+',
        '$1=<redacted>'
    )
    return $text
}

function Get-PropertyValue {
    param(
        [AllowNull()][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Copy-LogDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { return }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '\.log($|\.)|\.txt$' } |
        ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $Destination $_.Name) -Force
        }
}

$resolvedRoot = [System.IO.Path]::GetFullPath($ProgramRoot)
if (-not (Test-Path -LiteralPath $resolvedRoot -PathType Container)) {
    throw "Program root does not exist: $resolvedRoot"
}

$storageRoot = Join-Path $resolvedRoot "data\web_storage"
$databasePath = Join-Path $storageRoot "control.db"
if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) {
    throw "Database does not exist: $databasePath"
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = [Environment]::GetFolderPath("Desktop")
}
$outputRootResolved = [System.IO.Path]::GetFullPath($OutputRoot)
if (-not (Test-Path -LiteralPath $outputRootResolved -PathType Container)) {
    New-Item -ItemType Directory -Path $outputRootResolved -Force | Out-Null
}
$outputDirectory = Join-Path $outputRootResolved "188-full-audit-$stamp"
$zipPath = "$outputDirectory.zip"
New-Item -ItemType Directory -Path $outputDirectory | Out-Null

$manifest = [ordered]@{
    schema = "jyd.188-full-audit.v1"
    collected_at = (Get-Date).ToString("o")
    computer_name = $env:COMPUTERNAME
    program_root = $resolvedRoot
    read_only_scope = @(
        "SQLite project/batch/operation summaries (script text and credentials excluded)",
        "Sanitized local render job and batch status",
        "Relevant processes, listeners, disk state, executable fingerprints and logs"
    )
}
Write-Utf8Json -Value $manifest -Path (Join-Path $outputDirectory "manifest.json")

# Machine/runtime state. Process command lines are deliberately excluded because
# they can contain credentials on some installations.
$runtime = [ordered]@{ collected_at = (Get-Date).ToString("o") }
try {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    $computer = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
    $lastBootTime = if ($os.LastBootUpTime -is [datetime]) {
        ([datetime]$os.LastBootUpTime).ToString("o")
    } else {
        ([Management.ManagementDateTimeConverter]::ToDateTime([string]$os.LastBootUpTime)).ToString("o")
    }
    $runtime["os_caption"] = $os.Caption
    $runtime["os_version"] = $os.Version
    $runtime["last_boot_time"] = $lastBootTime
    $runtime["total_memory_bytes"] = [int64]$computer.TotalPhysicalMemory
    $runtime["free_memory_bytes"] = [int64]$os.FreePhysicalMemory * 1KB
} catch {
    $runtime["collection_error"] = Protect-Text $_.Exception.Message
}
Write-Utf8Json -Value $runtime -Path (Join-Path $outputDirectory "runtime.json")

$volumes = @()
try {
    $volumes = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" -ErrorAction Stop |
        Select-Object DeviceID, VolumeName, FileSystem, Size, FreeSpace
} catch {
    $volumes = @([ordered]@{ collection_error = Protect-Text $_.Exception.Message })
}
Write-Utf8Json -Value @($volumes) -Path (Join-Path $outputDirectory "volumes.json")

$allProcesses = @()
try {
    $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop)
} catch {
    $allProcesses = @()
}
$interestingProcesses = $allProcesses |
    Where-Object {
        $_.Name -match 'PublicVideo|Jianying|CapCut|ffmpeg|python|node|RunningHub'
    } |
    Select-Object Name, ProcessId, ParentProcessId, ExecutablePath, CreationDate
Write-Utf8Json -Value @($interestingProcesses) -Path (Join-Path $outputDirectory "relevant-processes.json")

$processNameById = @{}
foreach ($process in $allProcesses) {
    $processNameById[[string]$process.ProcessId] = $process.Name
}

$listeners = @()
try {
    $listeners = Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Sort-Object LocalPort, LocalAddress |
        ForEach-Object {
            [ordered]@{
                local_address = $_.LocalAddress
                local_port = $_.LocalPort
                owning_process_id = $_.OwningProcess
                process_name = $processNameById[[string]$_.OwningProcess]
            }
        }
} catch {
    $listeners = @([ordered]@{ collection_error = (Protect-Text $_.Exception.Message) })
}
Write-Utf8Json -Value @($listeners) -Path (Join-Path $outputDirectory "listening-ports.json")

# Fingerprint the deployed executables and public launcher documentation. This is
# enough to compare deployments without collecting program binaries.
$fingerprintCandidates = @(
    (Join-Path (Split-Path $resolvedRoot -Parent) "PublicVideoWorkbenchLauncher.exe"),
    (Join-Path $resolvedRoot "JianyingRenderServer.exe"),
    (Join-Path $resolvedRoot "README-PROCESSOR.md"),
    (Join-Path $resolvedRoot "START-HERE.txt")
)
$fingerprints = foreach ($candidate in $fingerprintCandidates) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $file = Get-Item -LiteralPath $candidate
        $hash = Get-FileHash -LiteralPath $candidate -Algorithm SHA256
        [ordered]@{
            name = $file.Name
            full_path = $file.FullName
            length = $file.Length
            last_write_time = $file.LastWriteTime.ToString("o")
            file_version = $file.VersionInfo.FileVersion
            sha256 = $hash.Hash
        }
    }
}
Write-Utf8Json -Value @($fingerprints) -Path (Join-Path $outputDirectory "program-fingerprints.json")

# All local render-job status files, sanitized to exclude scripts, templates,
# media paths and variant styling payloads.
$jobSummaries = @()
$jobsRoot = Join-Path $storageRoot "jobs"
if (Test-Path -LiteralPath $jobsRoot -PathType Container) {
    foreach ($statusFile in Get-ChildItem -LiteralPath $jobsRoot -Recurse -Filter "status.json" -File -ErrorAction SilentlyContinue) {
        try {
            $record = Get-Content -LiteralPath $statusFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            $variant = Get-PropertyValue $record "variant"
            $result = Get-PropertyValue $record "result"
            $outputMp4 = Get-PropertyValue $result "output_mp4"
            $jobSummaries += [ordered]@{
                job_id = Get-PropertyValue $record "job_id"
                batch_id = Get-PropertyValue $record "batch_id"
                batch_index = Get-PropertyValue $record "batch_index"
                batch_total = Get-PropertyValue $record "batch_total"
                project_id = Get-PropertyValue $variant "project_id"
                item_id = Get-PropertyValue $variant "item_id"
                row_key = Get-PropertyValue $variant "row_key"
                kind = Get-PropertyValue $variant "kind"
                status = Get-PropertyValue $record "status"
                created_at = Get-PropertyValue $record "created_at"
                queued_at = Get-PropertyValue $record "queued_at"
                started_at = Get-PropertyValue $record "started_at"
                heartbeat_at = Get-PropertyValue $record "heartbeat_at"
                finished_at = Get-PropertyValue $record "finished_at"
                retry_count = Get-PropertyValue $record "retry_count"
                assigned_agent_id = Get-PropertyValue $record "assigned_agent_id"
                cancel_requested = Get-PropertyValue $record "cancel_requested"
                error = Protect-Text (Get-PropertyValue $record "error")
                exported = Get-PropertyValue $result "exported"
                output_mp4_recorded = -not [string]::IsNullOrWhiteSpace([string]$outputMp4)
                output_mp4_exists = if ([string]::IsNullOrWhiteSpace([string]$outputMp4)) { $false } else { Test-Path -LiteralPath $outputMp4 -PathType Leaf }
                output_deleted = Get-PropertyValue $record "output_deleted"
                output_delete_reason = Get-PropertyValue $record "output_delete_reason"
                draft_deleted = Get-PropertyValue $record "draft_deleted"
                draft_delete_reason = Get-PropertyValue $record "draft_delete_reason"
                status_file_last_write_time = $statusFile.LastWriteTime.ToString("o")
            }
        } catch {
            $jobSummaries += [ordered]@{
                status_file = $statusFile.FullName
                parse_error = Protect-Text $_.Exception.Message
            }
        }
    }
}
Write-Utf8Json -Value @($jobSummaries) -Path (Join-Path $outputDirectory "local-render-jobs.json") -Depth 8

# All local render batches and their project/item/job identity mapping.
$batchSummaries = @()
$batchesRoot = Join-Path $storageRoot "batches"
if (Test-Path -LiteralPath $batchesRoot -PathType Container) {
    foreach ($batchFile in Get-ChildItem -LiteralPath $batchesRoot -Recurse -Filter "batch.json" -File -ErrorAction SilentlyContinue) {
        try {
            $record = Get-Content -LiteralPath $batchFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            $jobs = @()
            foreach ($job in @(Get-PropertyValue $record "jobs")) {
                if ($null -eq $job) { continue }
                $variant = Get-PropertyValue $job "variant"
                $jobs += [ordered]@{
                    job_id = Get-PropertyValue $job "job_id"
                    index = Get-PropertyValue $job "index"
                    project_id = Get-PropertyValue $variant "project_id"
                    item_id = Get-PropertyValue $variant "item_id"
                    row_key = Get-PropertyValue $variant "row_key"
                    kind = Get-PropertyValue $variant "kind"
                }
            }
            $batchSummaries += [ordered]@{
                batch_id = Get-PropertyValue $record "batch_id"
                status = Get-PropertyValue $record "status"
                created_at = Get-PropertyValue $record "created_at"
                total = Get-PropertyValue $record "total"
                metadata_expires_at = Get-PropertyValue $record "metadata_expires_at"
                jobs = $jobs
                batch_file_last_write_time = $batchFile.LastWriteTime.ToString("o")
            }
        } catch {
            $batchSummaries += [ordered]@{
                batch_file = $batchFile.FullName
                parse_error = Protect-Text $_.Exception.Message
            }
        }
    }
}
Write-Utf8Json -Value @($batchSummaries) -Path (Join-Path $outputDirectory "local-render-batches.json") -Depth 10

# Read the project database using the bundled Python in strict read-only mode.
# Only diagnostic fields are emitted. Script text, media paths, JSON payloads,
# account tables and configuration/credential files are deliberately excluded.
$pythonCandidates = @(
    (Join-Path $resolvedRoot "asr_runtime\python\python.exe"),
    (Join-Path $resolvedRoot "python\python.exe")
)
$pythonExecutable = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $pythonExecutable) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) { $pythonExecutable = $pythonCommand.Source }
}
if (-not $pythonExecutable) {
    throw "No Python runtime was found for the read-only SQLite summary."
}

$pythonScriptPath = Join-Path $outputDirectory "summarize_database.py"
$databaseSummaryPath = Join-Path $outputDirectory "database-summary.json"
$pythonCode = @'
import json
import os
import re
import sqlite3
import sys
from pathlib import Path


def parse_json(value, default):
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        parsed = json.loads(value)
        return parsed
    except Exception:
        return default


def redact(value):
    if value is None:
        return None
    text = str(value)
    return re.sub(
        r"(?i)(authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )


def table_exists(connection, table):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def select_rows(connection, table, columns, order_by=""):
    if not table_exists(connection, table):
        return []
    existing = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    selected = [column for column in columns if column in existing]
    if not selected:
        return []
    sql = f"SELECT {', '.join(selected)} FROM {table}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    return [dict(row) for row in connection.execute(sql)]


def h3_batch_summary(batch):
    if not isinstance(batch, dict):
        return None
    return {
        "batch_id": batch.get("batch_id"),
        "status": batch.get("status"),
        "row_ids": batch.get("row_ids") if isinstance(batch.get("row_ids"), list) else [],
        "last_synced_at": batch.get("last_synced_at"),
    }


def h3_segment_summary(segment):
    if not isinstance(segment, dict):
        return None
    normalized_video = any(
        bool(segment.get(key))
        for key in (
            "normalized_video_download",
            "normalized_video_url",
            "video_download_url",
            "video_url",
            "output_url",
        )
    )
    return {
        "segment_id": segment.get("segment_id"),
        "index": segment.get("index"),
        "status": segment.get("status"),
        "can_retry": segment.get("can_retry"),
        "retry_count": segment.get("retry_count"),
        "error_code": segment.get("error_code"),
        "error_message": redact(segment.get("error_message")),
        "has_normalized_video_download": normalized_video,
    }


database_path = Path(sys.argv[1]).resolve()
output_path = Path(sys.argv[2]).resolve()
uri = "file:" + database_path.as_posix() + "?mode=ro"
connection = sqlite3.connect(uri, uri=True, timeout=10)
connection.row_factory = sqlite3.Row
connection.execute("PRAGMA query_only=ON")

summary = {
    "schema": "jyd.188-database-summary.v1",
    "database_file": str(database_path),
    "database_size_bytes": database_path.stat().st_size,
    "projects": [],
    "items": [],
    "assets": [],
    "operations": [],
    "result_batches": [],
    "links": [],
}

project_rows = select_rows(
    connection,
    "projects",
    [
        "project_id", "project_no", "name", "status", "revision",
        "settings_json", "created_at", "updated_at",
    ],
    "updated_at DESC",
)
for row in project_rows:
    settings = parse_json(row.pop("settings_json", "{}"), {})
    h3 = settings.get("h3") if isinstance(settings.get("h3"), dict) else {}
    batches = []
    for batch in h3.get("batches", []):
        record = h3_batch_summary(batch)
        if record:
            batches.append(record)
    row["generation_mode"] = settings.get("generation_mode")
    row["h3"] = {
        "remote_batch_id": h3.get("remote_batch_id"),
        "remote_status": h3.get("remote_status"),
        "last_synced_at": h3.get("last_synced_at"),
        "batches": batches,
    }
    summary["projects"].append(row)

item_rows = select_rows(
    connection,
    "project_items",
    [
        "item_id", "project_id", "row_key", "position", "status",
        "current_image_asset_id", "current_audio_asset_id",
        "current_base_video_asset_id", "current_video_asset_id",
        "settings_json", "created_at", "updated_at",
    ],
    "project_id, position",
)
for row in item_rows:
    settings = parse_json(row.pop("settings_json", "{}"), {})
    h3 = settings.get("h3") if isinstance(settings.get("h3"), dict) else {}
    segments = []
    for segment in h3.get("segments", []):
        record = h3_segment_summary(segment)
        if record:
            segments.append(record)
    for asset_column in (
        "current_image_asset_id",
        "current_audio_asset_id",
        "current_base_video_asset_id",
        "current_video_asset_id",
    ):
        asset_id = row.pop(asset_column, None)
        row[asset_column.replace("_asset_id", "_asset_recorded")] = bool(asset_id)
    row["h3"] = {
        "remote_batch_id": h3.get("remote_batch_id"),
        "remote_item_id": h3.get("remote_item_id"),
        "remote_status": h3.get("remote_status"),
        "last_synced_at": h3.get("last_synced_at"),
        "invalidated_reason": h3.get("invalidated_reason"),
        "segments": segments,
    }
    summary["items"].append(row)

asset_rows = select_rows(
    connection,
    "project_assets",
    [
        "asset_id", "project_id", "item_id", "asset_type", "version",
        "status", "source_type", "managed_path", "created_at", "updated_at",
    ],
    "project_id, item_id, asset_type, version",
)
for row in asset_rows:
    managed_path = row.pop("managed_path", None)
    row["managed_path_recorded"] = bool(managed_path)
    row["managed_file_exists"] = bool(managed_path and os.path.isfile(managed_path))
    summary["assets"].append(row)

operation_rows = select_rows(
    connection,
    "project_operations",
    [
        "operation_id", "correlation_id", "project_id", "item_id",
        "operation_type", "status", "attempt_count", "error_code",
        "error_message", "created_at", "updated_at", "started_at", "finished_at",
    ],
    "created_at DESC",
)
for row in operation_rows:
    row["error_message"] = redact(row.get("error_message"))
    summary["operations"].append(row)

result_rows = select_rows(
    connection,
    "project_result_batches",
    [
        "result_batch_id", "project_id", "date_key", "date_label", "batch_no",
        "export_path", "operation_type", "status", "jianying_batch_id",
        "error_message", "created_at", "updated_at",
    ],
    "created_at DESC",
)
for row in result_rows:
    export_path = row.pop("export_path", None)
    row["export_path_recorded"] = bool(export_path)
    row["export_path_exists"] = bool(export_path and os.path.exists(export_path))
    row["error_message"] = redact(row.get("error_message"))
    summary["result_batches"].append(row)

link_rows = select_rows(
    connection,
    "project_links",
    [
        "link_id", "project_id", "item_id", "system", "relation",
        "external_id", "created_at",
    ],
    "created_at DESC",
)
summary["links"] = link_rows

summary["counts"] = {
    key: len(summary[key])
    for key in ("projects", "items", "assets", "operations", "result_batches", "links")
}

connection.close()
output_path.write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
)
'@
[System.IO.File]::WriteAllText($pythonScriptPath, $pythonCode, (New-Object System.Text.UTF8Encoding($false)))
& $pythonExecutable $pythonScriptPath $databasePath $databaseSummaryPath
if ($LASTEXITCODE -ne 0) {
    throw "SQLite summary failed with exit code $LASTEXITCODE"
}

# Copy only diagnostic logs. Configuration files, SQLite files, media, scripts and
# raw job payloads are never copied.
$logsDestination = Join-Path $outputDirectory "logs"
Copy-LogDirectory -Source (Join-Path (Split-Path $resolvedRoot -Parent) "logs") -Destination (Join-Path $logsDestination "launcher")
Copy-LogDirectory -Source (Join-Path $resolvedRoot "logs") -Destination (Join-Path $logsDestination "processor")
Copy-LogDirectory -Source (Join-Path $resolvedRoot "data\logs") -Destination (Join-Path $logsDestination "data")

# Relevant Windows Application errors from the last seven days.
$eventSummaries = @()
try {
    $events = Get-WinEvent -FilterHashtable @{
        LogName = "Application"
        StartTime = (Get-Date).AddDays(-7)
    } -ErrorAction Stop | Where-Object {
        $_.ProviderName -match 'Jianying|CapCut|Application Error|Windows Error Reporting|Python' -or
        $_.Message -match 'Jianying|PublicVideo|RenderServer'
    } | Select-Object -First 500
    foreach ($event in $events) {
        $eventSummaries += [ordered]@{
            time_created = if ($event.TimeCreated) { $event.TimeCreated.ToString("o") } else { $null }
            provider_name = $event.ProviderName
            event_id = $event.Id
            level = $event.LevelDisplayName
            message = Protect-Text $event.Message
        }
    }
} catch {
    $eventSummaries = @([ordered]@{ collection_error = Protect-Text $_.Exception.Message })
}
Write-Utf8Json -Value @($eventSummaries) -Path (Join-Path $outputDirectory "application-events.json") -Depth 6

# High-level counts make it easy to verify that all locally retained batches were
# included before sending the archive.
$collectorSummary = [ordered]@{
    collected_at = (Get-Date).ToString("o")
    project_database = $databasePath
    local_render_job_status_files = $jobSummaries.Count
    local_render_batch_files = $batchSummaries.Count
    relevant_processes = @($interestingProcesses).Count
    listening_ports = @($listeners).Count
    log_files = if (Test-Path -LiteralPath $logsDestination) { @(Get-ChildItem -LiteralPath $logsDestination -Recurse -File).Count } else { 0 }
    database_summary_created = (Test-Path -LiteralPath $databaseSummaryPath -PathType Leaf)
}
Write-Utf8Json -Value $collectorSummary -Path (Join-Path $outputDirectory "collector-summary.json")

Compress-Archive -Path (Join-Path $outputDirectory "*") -DestinationPath $zipPath -CompressionLevel Optimal

Write-Host ""
Write-Host "Full 188 audit collection completed." -ForegroundColor Green
Write-Host "Folder: $outputDirectory"
Write-Host "ZIP:    $zipPath"
Write-Host "Please send the ZIP file for machine-wide analysis."
