param(
    [string]$Root = '',
    [string]$PythonPath = '',
    [string]$BackupParent = ''
)

$ErrorActionPreference = 'Stop'

function Get-OptionalFileHash {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return 'MISSING'
    }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

function Assert-WorkbenchStopped {
    $serverProcesses = @(Get-Process -Name 'JianyingRenderServer' -ErrorAction SilentlyContinue)
    if ($serverProcesses.Count -gt 0) {
        throw 'JianyingRenderServer is still running. Close the workbench and try again.'
    }

    $listeners = @()
    try {
        $listeners = @(Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue)
    } catch {
        $listeners = @()
    }
    if ($listeners.Count -gt 0) {
        throw 'Port 8010 is still listening. Close the workbench and try again.'
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

$webStoragePath = Join-Path $resolvedRoot 'data\web_storage'
$databasePath = Join-Path $webStoragePath 'control.db'
$walPath = $databasePath + '-wal'
$shmPath = $databasePath + '-shm'
if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) {
    throw "Workbench database not found: $databasePath"
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
    throw 'Python was not found. Root must point to the complete digital-human installation.'
}

if ([string]::IsNullOrWhiteSpace($BackupParent)) {
    $BackupParent = [Environment]::GetFolderPath('Desktop')
}
$BackupParent = [System.IO.Path]::GetFullPath($BackupParent)
if (-not (Test-Path -LiteralPath $BackupParent -PathType Container)) {
    New-Item -ItemType Directory -Path $BackupParent -Force | Out-Null
}

Assert-WorkbenchStopped

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupDirectory = Join-Path $BackupParent "JYD-project-db-backup-$timestamp"
if (Test-Path -LiteralPath $backupDirectory) {
    throw "Backup directory already exists: $backupDirectory"
}
New-Item -ItemType Directory -Path $backupDirectory | Out-Null

$originalFingerprints = [ordered]@{
    database = Get-OptionalFileHash -Path $databasePath
    wal = Get-OptionalFileHash -Path $walPath
    shm = Get-OptionalFileHash -Path $shmPath
}

Copy-Item -LiteralPath $databasePath -Destination (Join-Path $backupDirectory 'control.db')
if (Test-Path -LiteralPath $walPath -PathType Leaf) {
    Copy-Item -LiteralPath $walPath -Destination (Join-Path $backupDirectory 'control.db-wal')
}
if (Test-Path -LiteralPath $shmPath -PathType Leaf) {
    Copy-Item -LiteralPath $shmPath -Destination (Join-Path $backupDirectory 'control.db-shm')
}

$workDirectory = Join-Path $backupDirectory 'repair-work'
New-Item -ItemType Directory -Path $workDirectory | Out-Null
$workingDatabasePath = Join-Path $workDirectory 'control.db'
$repairedDatabasePath = Join-Path $workDirectory 'control.repaired.db'
Copy-Item -LiteralPath (Join-Path $backupDirectory 'control.db') -Destination $workingDatabasePath
if (Test-Path -LiteralPath (Join-Path $backupDirectory 'control.db-wal') -PathType Leaf) {
    Copy-Item -LiteralPath (Join-Path $backupDirectory 'control.db-wal') -Destination ($workingDatabasePath + '-wal')
}

$manifest = [ordered]@{
    schema = 'jyd.project-db-backup.v1'
    captured_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
    source_root = $resolvedRoot
    source_database = $databasePath
    fingerprints = $originalFingerprints
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $backupDirectory 'backup-manifest.json') -Encoding UTF8

$pythonCode = @'
import json
import sqlite3
import sys
from pathlib import Path


BAD_PROJECTS = {
    1: {
        "project_id": "46873ee1b8ae4663bc47fbe6f3af3c1e",
        "project_no": "DH-20260902-0001",
    },
    2: {
        "project_id": "6ba52452e01f4d3ab382eceeaa8962ac",
        "project_no": "DH-20260902-0002",
    },
}

GOOD_PROJECTS = {
    3: {
        "project_id": "cc28884f60d84f40b6f8db4bd95fb2eb",
        "project_no": "DH-20260902-0001",
    },
    4: {
        "project_id": "6c8ce44af4364780a308bf6cbbfd696d",
        "project_no": "DH-20260902-0002",
    },
}


def fail(message):
    raise RuntimeError(message)


def table_names(connection):
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]


def table_columns(connection, table):
    if not table.replace("_", "").isalnum():
        fail(f"Unsafe table name: {table}")
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def raw_project_rows(connection):
    rows = connection.execute(
        "SELECT rowid, project_id, project_no, owner_user_id, status, "
        "created_at, updated_at FROM projects NOT INDEXED ORDER BY rowid"
    ).fetchall()
    return [
        {
            "rowid": int(row[0]),
            "project_id": row[1],
            "project_no": row[2],
            "owner_user_id": row[3],
            "status": row[4],
            "created_at": row[5],
            "updated_at": row[6],
        }
        for row in rows
    ]


def assert_expected_rows(connection):
    rows = raw_project_rows(connection)
    by_rowid = {int(row["rowid"]): row for row in rows}
    expected_rowids = set(BAD_PROJECTS) | set(GOOD_PROJECTS)
    if set(by_rowid) != expected_rowids:
        fail(
            "Database state changed. Expected project rowids "
            f"{sorted(expected_rowids)}, found {sorted(by_rowid)}. No repair was applied."
        )

    for rowid, expected in {**BAD_PROJECTS, **GOOD_PROJECTS}.items():
        actual = by_rowid[rowid]
        for key in ("project_id", "project_no"):
            if actual.get(key) != expected[key]:
                fail(
                    f"Database state changed at rowid {rowid}: "
                    f"expected {key}={expected[key]!r}, found {actual.get(key)!r}. "
                    "No repair was applied."
                )
    return rows


def child_reference_counts(connection, project_id):
    counts = {}
    for table in table_names(connection):
        if not table.startswith("project_"):
            continue
        if "project_id" not in table_columns(connection, table):
            continue
        count = int(
            connection.execute(
                f'SELECT COUNT(*) FROM "{table}" NOT INDEXED WHERE project_id=?',
                (project_id,),
            ).fetchone()[0]
        )
        counts[table] = count
    return counts


def quote_identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


def verify_repaired(connection):
    rows = raw_project_rows(connection)
    expected_by_id = {
        row["project_id"]: row["project_no"] for row in GOOD_PROJECTS.values()
    }
    actual_by_id = {row["project_id"]: row["project_no"] for row in rows}
    if actual_by_id != expected_by_id:
        fail(f"Post-repair project rows are unexpected: {actual_by_id!r}")

    normal_count = int(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
    raw_count = int(
        connection.execute("SELECT COUNT(*) FROM projects NOT INDEXED").fetchone()[0]
    )
    if normal_count != 2 or raw_count != 2:
        fail(
            f"Post-repair project counts disagree: indexed={normal_count}, raw={raw_count}"
        )

    for expected_row in GOOD_PROJECTS.values():
        found = connection.execute(
            "SELECT project_no FROM projects WHERE project_id=?",
            (expected_row["project_id"],),
        ).fetchone()
        if found is None or str(found[0]) != expected_row["project_no"]:
            fail(f"Valid project lookup failed: {expected_row['project_id']}")

    for expected_row in BAD_PROJECTS.values():
        found = connection.execute(
            "SELECT rowid FROM projects WHERE project_id=?",
            (expected_row["project_id"],),
        ).fetchone()
        if found is not None:
            fail(f"Removed project is still indexed: {expected_row['project_id']}")

    foreign_key_rows = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    if foreign_key_rows:
        fail(f"Foreign key check failed: {foreign_key_rows!r}")
    if integrity_rows != ["ok"]:
        fail(f"Integrity check failed: {integrity_rows!r}")

    return {
        "indexed_project_count": normal_count,
        "raw_project_count": raw_count,
        "projects": rows,
        "foreign_key_check": foreign_key_rows,
        "integrity_check": integrity_rows,
        "indexes": [
            {
                "sequence": int(row[0]),
                "name": str(row[1]),
                "unique": bool(row[2]),
                "origin": str(row[3]),
            }
            for row in connection.execute("PRAGMA index_list(projects)").fetchall()
        ],
    }


def rebuild_database(source, destination_path):
    if destination_path.exists():
        fail(f"Rebuild destination already exists: {destination_path}")

    schema_rows = source.execute(
        "SELECT rowid, type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
    ).fetchall()
    table_schema_rows = [row for row in schema_rows if str(row[1]) == "table"]
    index_schema_rows = [row for row in schema_rows if str(row[1]) == "index"]
    trigger_schema_rows = [row for row in schema_rows if str(row[1]) == "trigger"]
    view_schema_rows = [row for row in schema_rows if str(row[1]) == "view"]

    source_user_version = int(source.execute("PRAGMA user_version").fetchone()[0])
    source_application_id = int(source.execute("PRAGMA application_id").fetchone()[0])
    source_counts = {}
    destination_counts = {}
    skipped_projects = []

    destination = sqlite3.connect(str(destination_path), timeout=30.0)
    try:
        destination.execute("PRAGMA foreign_keys = OFF")
        destination.execute("BEGIN IMMEDIATE")
        try:
            for row in table_schema_rows:
                destination.execute(str(row[4]))

            for row in table_schema_rows:
                table = str(row[2])
                columns = [
                    str(column[1])
                    for column in source.execute(
                        f"PRAGMA table_info({quote_identifier(table)})"
                    ).fetchall()
                ]
                if not columns:
                    fail(f"No columns found for table: {table}")

                selected_rows = source.execute(
                    f"SELECT * FROM {quote_identifier(table)} NOT INDEXED"
                ).fetchall()
                source_counts[table] = len(selected_rows)

                if table == "projects":
                    project_id_index = columns.index("project_id")
                    filtered_rows = []
                    bad_ids = {
                        expected["project_id"] for expected in BAD_PROJECTS.values()
                    }
                    for selected_row in selected_rows:
                        values = tuple(selected_row)
                        if values[project_id_index] in bad_ids:
                            skipped_projects.append(values[project_id_index])
                            continue
                        filtered_rows.append(values)
                    rows_to_insert = filtered_rows
                else:
                    rows_to_insert = [tuple(selected_row) for selected_row in selected_rows]

                if rows_to_insert:
                    column_sql = ", ".join(quote_identifier(name) for name in columns)
                    placeholders = ", ".join("?" for _ in columns)
                    destination.executemany(
                        f"INSERT INTO {quote_identifier(table)} ({column_sql}) "
                        f"VALUES ({placeholders})",
                        rows_to_insert,
                    )
                destination_counts[table] = len(rows_to_insert)

            expected_skipped = sorted(
                expected["project_id"] for expected in BAD_PROJECTS.values()
            )
            if sorted(skipped_projects) != expected_skipped:
                fail(
                    f"Expected to skip {expected_skipped!r}, "
                    f"actually skipped {sorted(skipped_projects)!r}"
                )

            source_has_sequence = source.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='sqlite_sequence'"
            ).fetchone()
            destination_has_sequence = destination.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='sqlite_sequence'"
            ).fetchone()
            if source_has_sequence and destination_has_sequence:
                sequence_rows = source.execute(
                    "SELECT name, seq FROM sqlite_sequence NOT INDEXED"
                ).fetchall()
                destination.execute("DELETE FROM sqlite_sequence")
                if sequence_rows:
                    destination.executemany(
                        "INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)",
                        [tuple(row) for row in sequence_rows],
                    )

            for row in index_schema_rows:
                destination.execute(str(row[4]))
            for row in trigger_schema_rows:
                destination.execute(str(row[4]))
            for row in view_schema_rows:
                destination.execute(str(row[4]))

            destination.execute(f"PRAGMA user_version = {source_user_version}")
            destination.execute(f"PRAGMA application_id = {source_application_id}")
            destination.commit()
        except Exception:
            destination.rollback()
            raise

        for table, source_count in source_counts.items():
            expected_count = source_count - 2 if table == "projects" else source_count
            actual_count = int(
                destination.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table)}"
                ).fetchone()[0]
            )
            if actual_count != expected_count or destination_counts[table] != expected_count:
                fail(
                    f"Row count mismatch for {table}: source={source_count}, "
                    f"copied={destination_counts[table]}, destination={actual_count}, "
                    f"expected={expected_count}"
                )

        after = verify_repaired(destination)
        return {
            "source_table_counts": source_counts,
            "destination_table_counts": destination_counts,
            "skipped_project_ids": sorted(skipped_projects),
            "source_user_version": source_user_version,
            "source_application_id": source_application_id,
            "schema_object_counts": {
                "tables": len(table_schema_rows),
                "indexes": len(index_schema_rows),
                "triggers": len(trigger_schema_rows),
                "views": len(view_schema_rows),
            },
            "after": after,
        }
    finally:
        destination.close()


database_path = Path(sys.argv[1]).resolve()
mode = str(sys.argv[2])

if mode == "rebuild":
    if len(sys.argv) < 4:
        fail("Rebuild destination path is required")
    destination_path = Path(sys.argv[3]).resolve()
    uri = database_path.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        before_rows = assert_expected_rows(connection)
        bad_reference_counts = {}
        for rowid, expected in BAD_PROJECTS.items():
            counts = child_reference_counts(connection, expected["project_id"])
            bad_reference_counts[str(rowid)] = counts
            nonzero = {name: count for name, count in counts.items() if count != 0}
            if nonzero:
                fail(
                    f"Old project rowid {rowid} still has child records: {nonzero!r}. "
                    "No repair was applied."
                )

        try:
            before_integrity = [
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            ]
        except Exception as exc:
            before_integrity = [f"{type(exc).__name__}: {exc}"]

        rebuild = rebuild_database(connection, destination_path)
        result = {
            "schema": "jyd.project-db-logical-rebuild.v2",
            "mode": "rebuild-working-copy",
            "database": str(database_path),
            "destination": str(destination_path),
            "before_projects": before_rows,
            "before_integrity_check": before_integrity,
            "bad_project_child_reference_counts": bad_reference_counts,
            "rebuild": rebuild,
        }
    finally:
        connection.close()
elif mode == "verify":
    uri = database_path.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        result = {
            "schema": "jyd.project-db-logical-rebuild.v2",
            "mode": "verify-live-database-read-only",
            "database": str(database_path),
            "after": verify_repaired(connection),
        }
    finally:
        connection.close()
else:
    fail(f"Unknown mode: {mode}")

print(json.dumps(result, ensure_ascii=True, indent=2))
'@

$temporaryPythonScript = Join-Path ([System.IO.Path]::GetTempPath()) (
    'jyd-project-db-repair-' + [guid]::NewGuid().ToString('N') + '.py'
)
$repairOutput = @()
$verifyOutput = @()
$replacementCompleted = $false
$liveMutationStarted = $false
$stagedDatabasePath = ''
$preReplaceDatabasePath = ''
$previousPythonIoEncoding = $env:PYTHONIOENCODING
$previousErrorActionPreference = $ErrorActionPreference

try {
    Set-Content -LiteralPath $temporaryPythonScript -Value $pythonCode -Encoding ASCII
    $env:PYTHONIOENCODING = 'utf-8'
    $ErrorActionPreference = 'Continue'

    $repairOutput = & $PythonPath $temporaryPythonScript $workingDatabasePath 'rebuild' $repairedDatabasePath 2>&1
    $repairExitCode = $LASTEXITCODE
    if ($repairExitCode -ne 0) {
        throw "Working-copy logical rebuild failed with exit code $repairExitCode. The live database was not changed."
    }

    Assert-WorkbenchStopped
    $currentFingerprints = [ordered]@{
        database = Get-OptionalFileHash -Path $databasePath
        wal = Get-OptionalFileHash -Path $walPath
        shm = Get-OptionalFileHash -Path $shmPath
    }
    foreach ($name in @('database', 'wal', 'shm')) {
        if ($currentFingerprints[$name] -ne $originalFingerprints[$name]) {
            throw "Live database files changed after backup ($name). The live database was not changed by this script."
        }
    }

    $replacementId = [guid]::NewGuid().ToString('N')
    $stagedDatabasePath = $databasePath + '.repairing-' + $replacementId + '.tmp'
    $preReplaceDatabasePath = $databasePath + '.pre-repair-' + $replacementId + '.bak'
    Copy-Item -LiteralPath $repairedDatabasePath -Destination $stagedDatabasePath
    $workingHash = Get-OptionalFileHash -Path $repairedDatabasePath
    $stagedHash = Get-OptionalFileHash -Path $stagedDatabasePath
    if ($workingHash -ne $stagedHash) {
        Remove-Item -LiteralPath $stagedDatabasePath -Force -ErrorAction SilentlyContinue
        throw 'Staged repaired database hash mismatch. The live database was not changed.'
    }

    $liveMutationStarted = $true
    if (Test-Path -LiteralPath $walPath -PathType Leaf) {
        Remove-Item -LiteralPath $walPath -Force
    }
    if (Test-Path -LiteralPath $shmPath -PathType Leaf) {
        Remove-Item -LiteralPath $shmPath -Force
    }
    Move-Item -LiteralPath $databasePath -Destination $preReplaceDatabasePath
    Move-Item -LiteralPath $stagedDatabasePath -Destination $databasePath
    $replacementCompleted = $true

    $verifyOutput = & $PythonPath $temporaryPythonScript $databasePath 'verify' 2>&1
    $verifyExitCode = $LASTEXITCODE
    if ($verifyExitCode -ne 0) {
        throw "Live database verification failed with exit code $verifyExitCode."
    }
    if (Test-Path -LiteralPath $preReplaceDatabasePath -PathType Leaf) {
        Remove-Item -LiteralPath $preReplaceDatabasePath -Force
    }
} catch {
    $failure = $_
    if ($liveMutationStarted) {
        Copy-Item -LiteralPath (Join-Path $backupDirectory 'control.db') -Destination $databasePath -Force
        if (Test-Path -LiteralPath (Join-Path $backupDirectory 'control.db-wal') -PathType Leaf) {
            Copy-Item -LiteralPath (Join-Path $backupDirectory 'control.db-wal') -Destination $walPath -Force
        } elseif (Test-Path -LiteralPath $walPath -PathType Leaf) {
            Remove-Item -LiteralPath $walPath -Force
        }
        if (Test-Path -LiteralPath (Join-Path $backupDirectory 'control.db-shm') -PathType Leaf) {
            Copy-Item -LiteralPath (Join-Path $backupDirectory 'control.db-shm') -Destination $shmPath -Force
        } elseif (Test-Path -LiteralPath $shmPath -PathType Leaf) {
            Remove-Item -LiteralPath $shmPath -Force
        }
    }
    $failureReport = @(
        'RESULT=FAILED'
        ('ERROR=' + [string]$failure.Exception.Message)
        ('BACKUP=' + $backupDirectory)
        'LIVE_DATABASE_RESTORED=' + [string]$liveMutationStarted
        ''
        '===== WORKING COPY LOGICAL REBUILD OUTPUT ====='
        ($repairOutput | ForEach-Object { [string]$_ })
        ''
        '===== LIVE VERIFY OUTPUT ====='
        ($verifyOutput | ForEach-Object { [string]$_ })
    )
    $failureReport | Set-Content -LiteralPath (Join-Path $backupDirectory 'repair-result.txt') -Encoding UTF8
    throw $failure
} finally {
    $ErrorActionPreference = $previousErrorActionPreference
    if ($null -eq $previousPythonIoEncoding) {
        Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONIOENCODING = $previousPythonIoEncoding
    }
    if (Test-Path -LiteralPath $temporaryPythonScript -PathType Leaf) {
        Remove-Item -LiteralPath $temporaryPythonScript -Force
    }
    if (-not [string]::IsNullOrWhiteSpace($stagedDatabasePath) -and (Test-Path -LiteralPath $stagedDatabasePath -PathType Leaf)) {
        Remove-Item -LiteralPath $stagedDatabasePath -Force
    }
}

$successReport = @(
    'RESULT=SUCCESS'
    ('COMPLETED_AT=' + (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz'))
    ('BACKUP=' + $backupDirectory)
    ('LIVE_DATABASE=' + $databasePath)
    ('LIVE_DATABASE_SHA256=' + (Get-OptionalFileHash -Path $databasePath))
    ''
    '===== WORKING COPY LOGICAL REBUILD ====='
    ($repairOutput | ForEach-Object { [string]$_ })
    ''
    '===== LIVE DATABASE VERIFY ====='
    ($verifyOutput | ForEach-Object { [string]$_ })
)
$resultPath = Join-Path $backupDirectory 'repair-result.txt'
$successReport | Set-Content -LiteralPath $resultPath -Encoding UTF8

Write-Host 'Repair completed successfully.'
Write-Host "Backup: $backupDirectory"
Write-Host "Report: $resultPath"
