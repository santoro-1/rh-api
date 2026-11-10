param(
    [string]$Root = '',
    [string]$OutputPath = '',
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'

function Get-FileSnapshot {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [ordered]@{
            path = $Path
            exists = $false
        }
    }

    $item = Get-Item -LiteralPath $Path
    $hash = Get-FileHash -LiteralPath $Path -Algorithm SHA256
    return [ordered]@{
        path = $item.FullName
        exists = $true
        length = $item.Length
        last_write_time = $item.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
        sha256 = $hash.Hash
    }
}

$defaultDeploymentName = 'PV' + [char]0x90E8 + [char]0x7F72 + [char]0x5305 + '(2)'
$defaultSoftwareName = [string]([char]0x8F6F) + [char]0x4EF6
$defaultRoot = Join-Path (Join-Path ('D:\' + $defaultDeploymentName) $defaultSoftwareName) 'Public0902_2\digital-human'
if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = $defaultRoot
}

$resolvedRoot = [System.IO.Path]::GetFullPath($Root)
if (-not (Test-Path -LiteralPath $resolvedRoot -PathType Container)) {
    throw "Workbench root not found: $resolvedRoot"
}

$databasePath = Join-Path $resolvedRoot 'data\web_storage\control.db'
if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) {
    throw "Workbench database not found: $databasePath. Root must point to the active digital-human directory."
}

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $bundledPython = Join-Path $resolvedRoot 'asr_runtime\python\python.exe'
    if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
        $PythonPath = $bundledPython
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($null -ne $pythonCommand) {
            $PythonPath = $pythonCommand.Source
        }
    }
}

if ([string]::IsNullOrWhiteSpace($PythonPath) -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Python was not found. Root must point to a complete digital-human installation, not a standalone update folder.'
}

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutputPath = Join-Path $desktop "JYD-project-db-audit-$timestamp.txt"
} else {
    $OutputPath = [System.IO.Path]::GetFullPath($OutputPath)
}

$outputDirectory = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

$pythonCode = @'
import json
import sqlite3
import sys
from pathlib import Path


database_path = Path(sys.argv[1]).resolve()
database_uri = database_path.as_uri() + "?mode=ro"


def row_dicts(cursor):
    return [dict(row) for row in cursor.fetchall()]


def safe_identifier(name):
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier: {name}")
    return '"' + name + '"'


report = {
    "schema": "jyd.project-db-readonly-audit.v1",
    "database_open_mode": "read-only",
    "database_path": str(database_path),
    "sqlite_version": sqlite3.sqlite_version,
    "warnings": [],
}

connection = sqlite3.connect(database_uri, uri=True, timeout=10.0)
connection.row_factory = sqlite3.Row
try:
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("BEGIN")

    tables = [
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    table_set = set(tables)
    report["tables"] = tables

    report["quick_check"] = [
        str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()
    ]

    try:
        report["foreign_key_check"] = row_dicts(
            connection.execute("PRAGMA foreign_key_check")
        )[:200]
    except Exception as exc:
        report["foreign_key_check_error"] = f"{type(exc).__name__}: {exc}"

    if "project_schema_meta" in table_set:
        report["project_schema_meta"] = row_dicts(
            connection.execute(
                "SELECT key, value FROM project_schema_meta ORDER BY key"
            )
        )
    else:
        report["warnings"].append("project_schema_meta table is missing")

    count_tables = [
        "projects",
        "project_items",
        "project_assets",
        "project_input_images",
        "project_script_sources",
        "project_result_batches",
        "project_operations",
        "project_links",
        "project_user_preferences",
        "batches",
        "jobs",
        "agents",
    ]
    table_counts = {}
    for table in count_tables:
        if table in table_set:
            table_counts[table] = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {safe_identifier(table)}"
                ).fetchone()[0]
            )
    report["table_counts"] = table_counts

    if "projects" not in table_set:
        report["warnings"].append("projects table is missing")
    else:
        project_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(projects)").fetchall()
        }
        project_rows = row_dicts(
            connection.execute("SELECT rowid AS database_rowid, * FROM projects ORDER BY rowid")
        )

        safe_project_fields = [
            "database_rowid",
            "project_id",
            "project_no",
            "owner_user_id",
            "owner_username",
            "status",
            "revision",
            "created_at",
            "updated_at",
        ]
        project_summaries = []
        invalid_project_rows = []
        lookup_failures = []

        for raw_row in project_rows:
            summary = {
                key: raw_row.get(key)
                for key in safe_project_fields
                if key in raw_row
            }
            project_id = raw_row.get("project_id")
            summary["project_id_state"] = (
                "NULL"
                if project_id is None
                else "EMPTY"
                if str(project_id).strip() == ""
                else "OK"
            )

            if "project_items" in table_set and project_id is not None:
                summary["item_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM project_items WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0]
                )
            project_summaries.append(summary)

            if project_id is None or str(project_id).strip() == "":
                invalid_project_rows.append(summary)

            found = None
            if project_id is not None:
                found = connection.execute(
                    "SELECT rowid FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
            if found is None:
                lookup_failures.append(summary)

        report["projects"] = {
            "columns": sorted(project_columns),
            "summaries_without_names_or_scripts": project_summaries,
            "invalid_id_rows": invalid_project_rows,
            "list_lookup_failures": lookup_failures,
        }

        duplicate_ids = row_dicts(
            connection.execute(
                "SELECT project_id, COUNT(*) AS row_count "
                "FROM projects GROUP BY project_id HAVING COUNT(*) > 1"
            )
        )
        report["projects"]["duplicate_ids"] = duplicate_ids

        duplicate_project_numbers = row_dicts(
            connection.execute(
                "SELECT project_no, COUNT(*) AS row_count "
                "FROM projects GROUP BY project_no HAVING COUNT(*) > 1"
            )
        ) if "project_no" in project_columns else []
        report["projects"]["duplicate_project_numbers"] = duplicate_project_numbers

    child_tables = [
        "project_items",
        "project_assets",
        "project_input_images",
        "project_script_sources",
        "project_result_batches",
        "project_operations",
        "project_links",
    ]
    orphan_report = {}
    if "projects" in table_set:
        for table in child_tables:
            if table not in table_set:
                continue
            columns = {
                str(row["name"])
                for row in connection.execute(
                    f"PRAGMA table_info({safe_identifier(table)})"
                ).fetchall()
            }
            if "project_id" not in columns:
                continue
            quoted_table = safe_identifier(table)
            orphan_rows = row_dicts(
                connection.execute(
                    f"SELECT c.rowid AS child_rowid, c.project_id "
                    f"FROM {quoted_table} AS c "
                    "LEFT JOIN projects AS p ON p.project_id=c.project_id "
                    "WHERE p.rowid IS NULL ORDER BY c.rowid LIMIT 100"
                )
            )
            orphan_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {quoted_table} AS c "
                    "LEFT JOIN projects AS p ON p.project_id=c.project_id "
                    "WHERE p.rowid IS NULL"
                ).fetchone()[0]
            )
            orphan_report[table] = {
                "count": orphan_count,
                "sample_rows": orphan_rows,
            }
    report["project_orphans"] = orphan_report

    item_child_tables = ["project_assets", "project_operations", "project_links"]
    item_orphan_report = {}
    if "project_items" in table_set:
        for table in item_child_tables:
            if table not in table_set:
                continue
            columns = {
                str(row["name"])
                for row in connection.execute(
                    f"PRAGMA table_info({safe_identifier(table)})"
                ).fetchall()
            }
            if "item_id" not in columns:
                continue
            quoted_table = safe_identifier(table)
            orphan_rows = row_dicts(
                connection.execute(
                    f"SELECT c.rowid AS child_rowid, c.project_id, c.item_id "
                    f"FROM {quoted_table} AS c "
                    "LEFT JOIN project_items AS i ON i.item_id=c.item_id "
                    "WHERE c.item_id IS NOT NULL AND i.rowid IS NULL "
                    "ORDER BY c.rowid LIMIT 100"
                )
            )
            orphan_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {quoted_table} AS c "
                    "LEFT JOIN project_items AS i ON i.item_id=c.item_id "
                    "WHERE c.item_id IS NOT NULL AND i.rowid IS NULL"
                ).fetchone()[0]
            )
            item_orphan_report[table] = {
                "count": orphan_count,
                "sample_rows": orphan_rows,
            }
    report["item_orphans"] = item_orphan_report

    connection.rollback()
finally:
    connection.close()

print(json.dumps(report, ensure_ascii=False, indent=2))
'@

$header = [ordered]@{
    schema = 'jyd.project-db-audit-wrapper.v1'
    captured_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
    root = $resolvedRoot
    database = @(
        Get-FileSnapshot -Path $databasePath
        Get-FileSnapshot -Path ($databasePath + '-wal')
        Get-FileSnapshot -Path ($databasePath + '-shm')
    )
    runtime = @(
        Get-FileSnapshot -Path (Join-Path $resolvedRoot 'JianyingRenderServer.exe')
        Get-FileSnapshot -Path (Join-Path $resolvedRoot '_internal\apps\processor\frontend\new\index.html')
    )
    python = $PythonPath
    note = 'The database is opened read-only. Project names, scripts, passwords, and secrets are excluded.'
}

$previousPythonIoEncoding = $env:PYTHONIOENCODING
$previousErrorActionPreference = $ErrorActionPreference
$temporaryPythonScript = Join-Path ([System.IO.Path]::GetTempPath()) (
    'jyd-project-db-audit-' + [guid]::NewGuid().ToString('N') + '.py'
)
try {
    $env:PYTHONIOENCODING = 'utf-8'
    Set-Content -LiteralPath $temporaryPythonScript -Value $pythonCode -Encoding ASCII
    $ErrorActionPreference = 'Continue'
    $auditOutput = & $PythonPath $temporaryPythonScript $databasePath 2>&1
    $pythonExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousErrorActionPreference
    if (Test-Path -LiteralPath $temporaryPythonScript -PathType Leaf) {
        Remove-Item -LiteralPath $temporaryPythonScript -Force
    }
    if ($null -eq $previousPythonIoEncoding) {
        Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONIOENCODING = $previousPythonIoEncoding
    }
}

$reportLines = @(
    ($header | ConvertTo-Json -Depth 8)
    ''
    '===== SQLITE READ-ONLY AUDIT ====='
    ($auditOutput | ForEach-Object { [string]$_ })
)
Set-Content -LiteralPath $OutputPath -Value $reportLines -Encoding UTF8

if ($pythonExitCode -ne 0) {
    throw "Database audit failed with exit code $pythonExitCode. Details were saved to: $OutputPath"
}

Write-Host "Saved report: $OutputPath"
Write-Host 'Database was opened in read-only mode. No data was changed.'
