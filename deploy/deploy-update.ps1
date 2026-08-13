[CmdletBinding()]
param(
    [switch]$Deploy,
    [switch]$SkipTests,
    [string]$Server = "root@47.111.171.139",
    [string]$SshKey = "C:\Users\san\.ssh\jyd_auth_deploy_ed25519",
    [string]$AppDir = "/opt/runninghub-video",
    [string]$BackupDir = "/var/backups/runninghub-video",
    [string]$LinuxUser = "rhvideo",
    [string]$Domain = "video.lanyingjk01.com",
    [ValidateRange(5, 720)][int]$DrainTimeoutMinutes = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:CoreServices = @(
    "runninghub-video-web.service",
    "runninghub-video-audio.service",
    "runninghub-video-worker.service"
)
$script:MediaService = "runninghub-video-media.service"
$script:OptionalServices = @($script:MediaService)
$script:Services = @($script:CoreServices) + $script:OptionalServices
$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$script:LocalTemp = $null
$script:RemoteTemp = $null
$script:DrainToken = $null
$script:DrainEnabled = $false

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-LocalText {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Command,
        [Parameter(Mandatory = $true)][string]$Description
    )
    # Windows PowerShell 5.1 otherwise decodes UTF-8 native command output
    # with the active legacy code page. Git paths containing Chinese then
    # become mojibake and no longer match the explicit deletion allowlist.
    $previousOutputEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
    try {
        $output = & $Command 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        [Console]::OutputEncoding = $previousOutputEncoding
    }
    if ($exitCode -ne 0) {
        throw "$Description 失败：`n$($output -join [Environment]::NewLine)"
    }
    return (($output | ForEach-Object { "$_" }) -join "`n").Trim()
}

function Invoke-RemoteScript {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string]$Description
    )
    # PowerShell here-strings use CRLF on Windows. Normalize before passing the
    # script to Linux; otherwise Bash reads options such as "pipefail\r".
    $normalizedScript = $Script.Replace("`r`n", "`n").Replace("`r", "`n")
    $encoded = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes($normalizedScript)
    )
    $previousErrorAction = $ErrorActionPreference
    $previousOutputEncoding = [Console]::OutputEncoding
    $ErrorActionPreference = "Continue"
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
    try {
        $output = & ssh -n -T -i $SshKey `
            -o BatchMode=yes -o ConnectTimeout=10 `
            -o ConnectionAttempts=3 -o ServerAliveInterval=5 `
            -o ServerAliveCountMax=3 `
            $Server "printf %s $encoded | base64 -d | bash" 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
        [Console]::OutputEncoding = $previousOutputEncoding
    }
    if ($exitCode -ne 0) {
        throw "$Description 失败：`n$($output -join [Environment]::NewLine)"
    }
    return (($output | ForEach-Object { "$_" }) -join "`n").Trim()
}

function Assert-ExactScope {
    if ($AppDir -ne "/opt/runninghub-video") {
        throw "为防止误操作，本脚本只允许 AppDir=/opt/runninghub-video。"
    }
    if ($BackupDir -ne "/var/backups/runninghub-video") {
        throw "为防止误操作，本脚本只允许 BackupDir=/var/backups/runninghub-video。"
    }
    if ($LinuxUser -ne "rhvideo") {
        throw "为防止影响其他服务，本脚本只允许 LinuxUser=rhvideo。"
    }
    if ($Domain -ne "video.lanyingjk01.com") {
        throw "域名与本项目生产配置不一致，已拒绝继续。"
    }
    if (-not (Test-Path -LiteralPath $SshKey -PathType Leaf)) {
        throw "找不到 SSH 密钥：$SshKey"
    }
}

function Confirm-Exact {
    param(
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][string]$Expected
    )
    Write-Host ""
    Write-Host $Prompt -ForegroundColor Yellow
    $answer = Read-Host "请输入 $Expected 继续"
    if ($answer -cne $Expected) {
        throw "确认词不匹配，部署已取消；服务器代码未被覆盖。"
    }
}

function Get-QueueCheckScript {
    param([switch]$InFlightOnly)
    if ($InFlightOnly) {
        $videoStatuses = "'UPLOADING','SUBMITTED','RUNNING'"
        $audioStatuses = "'CLONING','SYNTHESIZING','REMOTE_PENDING','ALIGNING','SEGMENTING','HANDOFF'"
        $voiceStatuses = "'CLONING','SYNTHESIZING','SAVING'"
        $mediaStatuses = "'ANALYZING','CUTTING'"
        $mergePredicate = "bi.merged_video_status = 'MERGING'"
    } else {
        $videoStatuses = "'PENDING','UPLOADING','SUBMITTED','RUNNING'"
        $audioStatuses = "'PENDING','CLONING','SYNTHESIZING','REMOTE_PENDING','ALIGNING','SEGMENTING','HANDOFF'"
        $voiceStatuses = "'PENDING','CLONING','SYNTHESIZING','SAVE_PENDING','SAVING'"
        $mediaStatuses = "'PENDING_ANALYSIS','ANALYZING','PENDING_CUT','CUTTING'"
        # MERGE_PENDING may remain on a historical row whose child tasks have
        # already failed or been cancelled.  Such a row cannot be claimed by
        # the merge worker and must not permanently block a bootstrap deploy.
        $mergePredicate = @"
bi.merged_video_status = 'MERGING'
 OR (
   bi.merged_video_status = 'MERGE_PENDING'
   AND EXISTS (
     SELECT 1 FROM generation_segments AS gs
      WHERE gs.batch_item_id = bi.id
   )
   AND NOT EXISTS (
     SELECT 1
       FROM generation_segments AS gs
       LEFT JOIN generation_tasks AS gt ON gt.segment_id = gs.id
      WHERE gs.batch_item_id = bi.id
        AND (gt.id IS NULL OR gt.status <> 'SUCCESS')
   )
 )
"@
    }
    return @"
set -euo pipefail
db='$AppDir/data/app.db'
if [ ! -f "`$db" ]; then
  echo '找不到生产数据库' >&2
  exit 1
fi
sqlite3 -readonly "`$db" "
SELECT 'video', count(*) FROM generation_tasks
 WHERE status IN ($videoStatuses)
UNION ALL
SELECT 'audio', count(*) FROM audio_generation_tasks
 WHERE status IN ($audioStatuses)
UNION ALL
SELECT 'voice', count(*) FROM voice_creation_tasks
 WHERE status IN ($voiceStatuses)
UNION ALL
SELECT 'media', count(*) FROM long_audio_projects
 WHERE status IN ($mediaStatuses)
UNION ALL
SELECT 'merge', count(*) FROM generation_batch_items AS bi
 WHERE $mergePredicate;
"
"@
}

function Get-QueueStatus {
    param([switch]$InFlightOnly)
    return Invoke-RemoteScript -Script (Get-QueueCheckScript -InFlightOnly:$InFlightOnly) `
        -Description "读取本项目任务队列"
}

function Test-QueueBusy {
    param([Parameter(Mandatory = $true)][string]$QueueOutput)
    foreach ($line in ($QueueOutput -split "`n")) {
        if ($line -match "^[^|]+\|([1-9][0-9]*)$") {
            return $true
        }
    }
    return $false
}

function Show-QueueStatus {
    $queueOutput = Get-QueueStatus
    Write-Host $queueOutput
}

function Assert-QueuesIdle {
    $queueOutput = Get-QueueStatus
    Write-Host $queueOutput
    if (Test-QueueBusy -QueueOutput $queueOutput) {
        throw "本项目仍有待处理或运行中的任务，已拒绝部署。请等待任务结束后重试。"
    }
}

function Enable-DeploymentDrain {
    $script:DrainToken = [Guid]::NewGuid().ToString("N")
    $expiresAtEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + `
        (($DrainTimeoutMinutes + 30) * 60)
    $drainScript = @"
set -euo pipefail
runtime='$AppDir/data/runtime'
marker="`$runtime/deployment-drain.json"
partial="`$marker.$script:DrainToken.partial"
install -d -m 700 -o '$LinuxUser' -g '$LinuxUser' "`$runtime"
printf '%s\n' '{"token":"$script:DrainToken","commit":"$commit","expiresAtEpoch":$expiresAtEpoch}' > "`$partial"
chown '${LinuxUser}:${LinuxUser}' "`$partial"
chmod 600 "`$partial"
mv -f -- "`$partial" "`$marker"
echo "已进入排空模式：`$marker"
"@
    Write-Host (Invoke-RemoteScript -Script $drainScript -Description "进入排空模式")
    $script:DrainEnabled = $true
}

function Disable-DeploymentDrain {
    if (-not $script:DrainEnabled -or -not $script:DrainToken) {
        return
    }
    $disableScript = @"
set -euo pipefail
marker='$AppDir/data/runtime/deployment-drain.json'
if [ -f "`$marker" ] && grep -Fq '"token":"$script:DrainToken"' "`$marker"; then
  rm -f -- "`$marker"
  echo '已退出排空模式'
else
  echo '排空标记已不存在或不属于本次发布，未修改'
fi
"@
    try {
        Write-Host (Invoke-RemoteScript -Script $disableScript -Description "退出排空模式")
        $script:DrainEnabled = $false
    } catch {
        Write-Warning "自动退出排空模式失败：$($_.Exception.Message)"
    }
}

function Wait-QueuesDrained {
    $deadline = [DateTimeOffset]::UtcNow.AddMinutes($DrainTimeoutMinutes)
    $idleChecks = 0
    while ($true) {
        $queueOutput = Get-QueueStatus -InFlightOnly
        Write-Host "执行中任务：$($queueOutput.Replace("`n", ", "))"
        if (-not (Test-QueueBusy -QueueOutput $queueOutput)) {
            $idleChecks += 1
            if ($idleChecks -ge 2) {
                Write-Host "执行中的任务已稳定排空；排队任务将在发布完成后继续。" -ForegroundColor Green
                return
            }
            Write-Host "首次检测为空，等待 10 秒复核，避免领取任务的临界竞争。"
        } else {
            $idleChecks = 0
        }
        if ([DateTimeOffset]::UtcNow -ge $deadline) {
            throw "等待执行中任务排空超过 $DrainTimeoutMinutes 分钟，已取消发布。"
        }
        Start-Sleep -Seconds 10
    }
}

try {
    Set-Location -LiteralPath $script:RepoRoot
    Assert-ExactScope

    Write-Step "本地发布资格检查（不会修改服务器）"
    $branch = Invoke-LocalText -Description "读取当前分支" -Command {
        git branch --show-current
    }
    if ($branch -ne "main") {
        throw "当前分支是 $branch。请先切换并更新 main：git switch main；git pull --ff-only origin main"
    }
    $dirty = Invoke-LocalText -Description "检查工作区" -Command {
        # The release is created with git archive, so only tracked staged or
        # unstaged changes can alter its contents. Do not enumerate stale
        # untracked pytest directories that may belong to another Windows
        # security context and emit access warnings in Windows PowerShell 5.1.
        git status --porcelain --untracked-files=no 2>$null
    }
    if ($dirty) {
        throw "本地工作区存在未提交改动，已拒绝发布。"
    }
    $commit = Invoke-LocalText -Description "读取发布 commit" -Command {
        git rev-parse HEAD
    }
    $originMain = Invoke-LocalText -Description "读取 origin/main" -Command {
        git rev-parse origin/main
    }
    if ($commit -ne $originMain) {
        throw "本地 main 与 origin/main 不一致。请先执行 git fetch origin 和 git pull --ff-only origin main。"
    }
    $shortCommit = $commit.Substring(0, 12)
    Write-Host "准备检查 commit：$commit"

    if (-not $SkipTests) {
        Write-Step "运行本地完整测试"
        & python -m pytest -q
        if ($LASTEXITCODE -ne 0) {
            throw "本地测试失败，已拒绝发布。"
        }
    } else {
        Write-Warning "已使用 -SkipTests；只有在本次 commit 已经完整测试过时才应这样做。"
    }

    Write-Step "服务器只读检查"
    $readonlyScript = @"
set -euo pipefail
echo '部署版本：'
if [ -f '$AppDir/.deployed-revision' ]; then
  cat '$AppDir/.deployed-revision'
else
  echo 'MISSING'
fi
echo '本项目服务：'
  systemctl --no-pager is-active $($script:CoreServices -join " ")
  for optional_service in $($script:OptionalServices -join " "); do
    if systemctl cat "`$optional_service" >/dev/null 2>&1; then
      systemctl --no-pager is-active "`$optional_service"
    else
      echo "`$optional_service PENDING_INSTALL"
    fi
  done
  echo '本项目健康检查：'
  curl --connect-timeout 3 --max-time 8 -fsS \
    -H 'Host: $Domain' http://127.0.0.1:18083/healthz
echo
echo '.env 权限：'
stat -c '%a %U:%G %n' '$AppDir/.env'
echo '磁盘：'
df -h '$AppDir' | tail -n 1
echo 'Nginx 语法：'
  timeout 15 nginx -t
echo '现有关键服务监听端口：'
for port in 18080 18081 18082; do
  ss -ltn | awk -v expected=":`$port" '
    `$1 == "LISTEN" && substr(`$4, length(`$4) - length(expected) + 1) == expected {
      found=1
    }
    END { exit(found ? 0 : 1) }
  '
  echo "port `$port LISTEN"
done
"@
    $serverStatus = Invoke-RemoteScript -Script $readonlyScript `
        -Description "服务器只读检查"
    Write-Host $serverStatus
    Write-Host "本项目当前队列（只读；排队任务不会阻止支持排空模式的发布）："
    Show-QueueStatus

    $deployedRevision = Invoke-RemoteScript -Description "读取当前部署版本" -Script @"
set -euo pipefail
test -f '$AppDir/.deployed-revision'
tr -d '\r\n' < '$AppDir/.deployed-revision'
"@
    if ($deployedRevision -notmatch "^[0-9a-f]{40}$") {
        throw "服务器部署版本标记无效，拒绝自动覆盖。"
    }
    if ($deployedRevision -eq $commit) {
        Write-Host ""
        Write-Host "服务器已经是该 commit，无需重复部署。" -ForegroundColor Green
        return
    }
    $drainSupport = Invoke-RemoteScript -Description "检查线上排空能力" -Script @"
set -euo pipefail
if [ -f '$AppDir/app/services/deployment_drain.py' ] && \
   grep -Fq 'DRAIN_MARKER_NAME' '$AppDir/app/services/deployment_drain.py'; then
  echo 'SUPPORTED'
else
  echo 'BOOTSTRAP'
fi
"@
    if ($drainSupport -eq "BOOTSTRAP") {
        Write-Warning "当前线上版本尚不识别排空标记；首次发布本功能时仍需队列完全空闲。此后更新会自动排空。"
    }

    Invoke-LocalText -Description "确认本地包含服务器旧版本" -Command {
        git cat-file -e "$deployedRevision`^{commit}"
    } | Out-Null
    $deletedFiles = Invoke-LocalText -Description "检查删除文件" -Command {
        git -c core.quotePath=false diff --name-only --diff-filter=D `
            $deployedRevision $commit
    }
    $allowedDeletedFiles = @(
        "asr_service/README.md",
        "asr_service/app.py",
        "asr_service/requirements.txt",
        "deploy/scripts/install-asr.sh",
        "deploy/systemd/runninghub-video-asr.service",
        "scripts/remote_media_node.py",
        "scripts/remote_media_worker.py",
        "启动ASR服务.cmd",
	"WORKBENCH_INTEGRATION_20260803.md",
        "启动远程媒体节点.cmd"
    )
    if ($deletedFiles) {
        Write-Host $deletedFiles
        foreach ($relativePath in ($deletedFiles -split "`n")) {
            if ($relativePath -notin $allowedDeletedFiles) {
                throw "本次包含未获批准的删除文件：$relativePath"
            }
        }
        Write-Host "删除项仅包含已迁移到 media_node/ 的旧媒体节点文件。"
    }
    $addedFilesText = Invoke-LocalText -Description "检查新增文件" -Command {
        git -c core.quotePath=false diff --name-only --diff-filter=A `
            $deployedRevision $commit
    }
    $addedFiles = @()
    if ($addedFilesText) {
        $addedFiles = @($addedFilesText -split "`n")
        foreach ($relativePath in $addedFiles) {
            $normalizedPath = $relativePath.Replace("\", "/")
            if (
                [IO.Path]::IsPathRooted($relativePath) -or
                $normalizedPath -match "(^|/)\.\.(/|$)" -or
                $normalizedPath -match "^(\.env|\.venv|\.asr-runtime|data|tools)(/|$)"
            ) {
                throw "新增文件路径不安全，拒绝自动发布：$relativePath"
            }
        }
    }

    if (-not $Deploy) {
        Write-Host ""
        Write-Host "只读检查通过；没有修改服务器。" -ForegroundColor Green
        Write-Host "确认要部署时重新运行："
        Write-Host "  powershell -ExecutionPolicy Bypass -File .\deploy\deploy-update.ps1 -Deploy"
        return
    }

    Confirm-Exact `
        -Prompt "下一步会创建本项目备份并上传 commit $shortCommit；不会安装服务器 ASR，也不会停止服务。" `
        -Expected "BACKUP $shortCommit"

    Write-Step "打包当前 Git commit（不包含 .env、数据库、上传、输出和日志）"
    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $script:LocalTemp = Join-Path ([IO.Path]::GetTempPath()) `
        "runninghub-video-$shortCommit-$timestamp"
    New-Item -ItemType Directory -Path $script:LocalTemp | Out-Null
    $archive = Join-Path $script:LocalTemp "release-$shortCommit.tar"
    $addedFilesManifest = Join-Path $script:LocalTemp "added-files.txt"
    & git archive --format=tar --output=$archive $commit
    if ($LASTEXITCODE -ne 0) {
        throw "创建 Git 发布包失败。"
    }
    [IO.File]::WriteAllLines(
        $addedFilesManifest,
        $addedFiles,
        (New-Object Text.UTF8Encoding($false))
    )
    $archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $script:RemoteTemp = "/var/tmp/runninghub-video-$shortCommit-$timestamp"

    Write-Step "创建本项目独立备份（服务器写操作 1）"
    $backupScript = @"
set -euo pipefail
umask 077
install -d -m 700 -o '$LinuxUser' -g '$LinuxUser' '$script:RemoteTemp'
install -d -m 700 -o '$LinuxUser' -g '$LinuxUser' '$BackupDir'
sudo -u '$LinuxUser' /bin/bash '$AppDir/deploy/scripts/backup.sh'
code_backup='$BackupDir/runninghub-video-code-pre-$shortCommit-$timestamp.tar.gz'
tar -C '$AppDir' \
  --exclude='./.env' \
  --exclude='./.venv' \
  --exclude='./.asr-runtime' \
  --exclude='./data' \
  --exclude='./tools' \
  -czf "`$code_backup" .
chmod 600 "`$code_backup"
echo "代码备份：`$code_backup"
"@
    Write-Host "将写入：$BackupDir 和临时目录 $script:RemoteTemp"
    Write-Host (Invoke-RemoteScript -Script $backupScript -Description "创建生产备份")

    Write-Step "上传并校验发布包（服务器写操作 2，仅写临时目录）"
    & scp -i $SshKey -o BatchMode=yes -o ConnectTimeout=10 `
        -o ConnectionAttempts=3 -o ServerAliveInterval=5 `
        -o ServerAliveCountMax=3 $archive `
        "${Server}:$script:RemoteTemp/release.tar"
    if ($LASTEXITCODE -ne 0) {
        throw "上传发布包失败。"
    }
    & scp -i $SshKey -o BatchMode=yes -o ConnectTimeout=10 `
        -o ConnectionAttempts=3 -o ServerAliveInterval=5 `
        -o ServerAliveCountMax=3 $addedFilesManifest `
        "${Server}:$script:RemoteTemp/added-files.txt"
    if ($LASTEXITCODE -ne 0) {
        throw "上传回滚清单失败。"
    }
    $stageScript = @"
set -euo pipefail
actual=`$(sha256sum '$script:RemoteTemp/release.tar' | awk '{print `$1}')
test "`$actual" = '$archiveHash'
chown '${LinuxUser}:$LinuxUser' \
  '$script:RemoteTemp/release.tar' \
  '$script:RemoteTemp/added-files.txt'
chmod 600 \
  '$script:RemoteTemp/release.tar' \
  '$script:RemoteTemp/added-files.txt'
install -d -m 700 -o '$LinuxUser' -g '$LinuxUser' '$script:RemoteTemp/stage'
sudo -u '$LinuxUser' tar -C '$script:RemoteTemp/stage' -xf '$script:RemoteTemp/release.tar'
ln -s '$AppDir/.env' '$script:RemoteTemp/stage/.env'
ln -s '$AppDir/.venv' '$script:RemoteTemp/stage/.venv'
ln -s '$AppDir/data' '$script:RemoteTemp/stage/data'
if [ -d '$AppDir/tools' ]; then
  ln -s '$AppDir/tools' '$script:RemoteTemp/stage/tools'
fi
cmp -s '$AppDir/requirements.txt' '$script:RemoteTemp/stage/requirements.txt' || {
  echo 'requirements.txt 有变化，必须先单独审查依赖更新。' >&2
  exit 1
}
sudo -u '$LinuxUser' env \
  PATH='$AppDir/tools/ffmpeg/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin' \
  RUNNINGHUB_APP_DIR='$script:RemoteTemp/stage' \
  /bin/bash '$script:RemoteTemp/stage/deploy/scripts/preflight.sh'
migration_check='$script:RemoteTemp/migration-check.db'
sqlite3 -readonly '$AppDir/data/app.db' \
  '.timeout 30000' ".backup '`$migration_check'"
chown '${LinuxUser}:${LinuxUser}' "`$migration_check"
chmod 600 "`$migration_check"
(
  cd '$script:RemoteTemp/stage'
  sudo -u '$LinuxUser' env \
    PATH='$AppDir/tools/ffmpeg/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin' \
    DATABASE_URL="sqlite:///`$migration_check" \
    '$AppDir/.venv/bin/python' -m alembic -c alembic.ini upgrade head
)
sqlite3 -readonly "`$migration_check" 'PRAGMA quick_check;' | grep -qx 'ok'
echo '数据库迁移已在临时副本验证通过'
echo '发布包校验和预检通过'
"@
    Write-Host (Invoke-RemoteScript -Script $stageScript -Description "校验发布包")

    Confirm-Exact `
        -Prompt "即将进入排空模式：暂停本项目新建/修改和领取新任务，等待执行中任务完成后，只停止本项目 runninghub-video 服务并更新。浏览、预览和下载保持可用；不会安装或启动服务器 ASR，也不会修改其他项目、Nginx 或证书。" `
        -Expected "DEPLOY $shortCommit"

    Write-Step "进入排空模式并等待执行中的任务结束"
    if ($drainSupport -eq "BOOTSTRAP") {
        Assert-QueuesIdle
        Enable-DeploymentDrain
        Assert-QueuesIdle
    } else {
        Enable-DeploymentDrain
        Wait-QueuesDrained
    }

    Write-Step "更新本项目代码（服务器写操作 3）"
    $mutateScript = @"
set -euo pipefail
core_services='$($script:CoreServices -join " ")'
media_service='$script:MediaService'
services="`$core_services"
media_unit='/etc/systemd/system/runninghub-video-media.service'
media_unit_backup='$script:RemoteTemp/runninghub-video-media.service.before'
media_unit_existed=0
if systemctl cat "`$media_service" >/dev/null 2>&1; then
  media_unit_existed=1
  services="`$services `$media_service"
  cp -a -- "`$media_unit" "`$media_unit_backup"
fi
old_revision='$deployedRevision'
rollback_code='$BackupDir/runninghub-video-code-pre-$shortCommit-$timestamp.tar.gz'
pre_deploy_db='$script:RemoteTemp/pre-deploy-app.db'
pre_deploy_db_partial='$script:RemoteTemp/pre-deploy-app.db.partial'
rollback() {
  exit_code="`$1"
  trap - ERR
  set +e
  echo '发布失败，正在恢复发布前代码和数据库。' >&2
  systemctl stop `$core_services "`$media_service" >/dev/null 2>&1 || true
  if [ -f "`$pre_deploy_db" ]; then
    if [ -f '$AppDir/data/app.db' ]; then
      sqlite3 -readonly '$AppDir/data/app.db' \
        ".backup '$script:RemoteTemp/failed-app.db'" >/dev/null 2>&1 || true
    fi
    rm -f -- \
      '$AppDir/data/app.db' \
      '$AppDir/data/app.db-wal' \
      '$AppDir/data/app.db-shm'
    install -m 600 -o '$LinuxUser' -g '$LinuxUser' \
      "`$pre_deploy_db" '$AppDir/data/app.db'
  fi
  if [ -f '$script:RemoteTemp/added-files.txt' ]; then
    while IFS= read -r relative_path; do
      [ -n "`$relative_path" ] || continue
      case "`$relative_path" in
        /*|../*|*/../*|.env|.env/*|.venv|.venv/*|.asr-runtime|.asr-runtime/*|data|data/*|tools|tools/*)
          echo "回滚清单包含不安全路径：`$relative_path" >&2
          continue
          ;;
        *) rm -f -- '$AppDir/'"`$relative_path" ;;
      esac
    done < '$script:RemoteTemp/added-files.txt'
  fi
  tar -C '$AppDir' -xzf "`$rollback_code"
  if [ "`$media_unit_existed" -eq 1 ]; then
    cp -a -- "`$media_unit_backup" "`$media_unit"
  else
    systemctl disable "`$media_service" >/dev/null 2>&1 || true
    rm -f -- "`$media_unit"
  fi
  printf '%s\n' "`$old_revision" > '$AppDir/.deployed-revision'
  chown '${LinuxUser}:${LinuxUser}' '$AppDir/.deployed-revision'
  systemctl daemon-reload
  rollback_services="`$core_services"
  if [ "`$media_unit_existed" -eq 1 ]; then
    rollback_services="`$rollback_services `$media_service"
  fi
  systemctl start `$rollback_services
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    curl --connect-timeout 2 --max-time 5 -fsS \
      -H 'Host: $Domain' http://127.0.0.1:18083/healthz >/dev/null && break
    sleep 1
  done
  exit "`$exit_code"
}
trap 'rollback `$?' ERR

systemctl stop `$services
sqlite3 -readonly '$AppDir/data/app.db' \
  '.timeout 30000' ".backup '`$pre_deploy_db_partial'"
sqlite3 -readonly "`$pre_deploy_db_partial" 'PRAGMA quick_check;' | grep -qx 'ok'
mv -f -- "`$pre_deploy_db_partial" "`$pre_deploy_db"
chmod 600 "`$pre_deploy_db"
sudo -u '$LinuxUser' tar -C '$AppDir' -xf '$script:RemoteTemp/release.tar'
install -m 644 -o root -g root \
  '$AppDir/deploy/systemd/runninghub-video-media.service' "`$media_unit"
printf '%s\n' '$commit' > '$AppDir/.deployed-revision'
chown '${LinuxUser}:${LinuxUser}' '$AppDir/.deployed-revision'
chmod 600 '$AppDir/.env'
sudo -u '$LinuxUser' env \
  PATH='$AppDir/tools/ffmpeg/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin' \
  /bin/bash '$AppDir/deploy/scripts/preflight.sh'
(
  cd '$AppDir'
  sudo -u '$LinuxUser' env \
    PATH='$AppDir/tools/ffmpeg/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin' \
    '$AppDir/.venv/bin/python' -m alembic -c alembic.ini upgrade head
)
systemctl daemon-reload
services="`$core_services `$media_service"
systemctl enable "`$media_service" >/dev/null
systemctl start `$services
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl --connect-timeout 2 --max-time 5 -fsS \
    -H 'Host: $Domain' http://127.0.0.1:18083/healthz >/dev/null; then
    break
  fi
  if [ "`$attempt" -eq 10 ]; then
    echo '新版本健康检查失败' >&2
    false
  fi
  sleep 1
done
systemctl is-active `$services >/dev/null
sqlite3 -readonly '$AppDir/data/app.db' 'PRAGMA quick_check;' | grep -qx 'ok'
trap - ERR
"@
    Invoke-RemoteScript -Script $mutateScript -Description "更新生产代码" | Out-Null

    Write-Step "发布后验收（只读）"
    $verifyScript = @"
set -euo pipefail
systemctl is-active $($script:Services -join " ")
test "`$(tr -d '\r\n' < '$AppDir/.deployed-revision')" = '$commit'
(
  cd '$AppDir'
  sudo -u '$LinuxUser' '$AppDir/.venv/bin/python' \
    -m alembic -c alembic.ini current
)
sqlite3 -readonly '$AppDir/data/app.db' 'PRAGMA quick_check;'
curl --connect-timeout 3 --max-time 8 -fsS \
  -H 'Host: $Domain' http://127.0.0.1:18083/healthz
echo
curl --connect-timeout 3 --max-time 10 -fsS 'https://$Domain/healthz'
echo
timeout 15 nginx -t
for port in 18080 18081 18082; do
  ss -ltn | awk -v expected=":`$port" '
    `$1 == "LISTEN" && substr(`$4, length(`$4) - length(expected) + 1) == expected {
      found=1
    }
    END { exit(found ? 0 : 1) }
  '
  echo "existing critical port `$port LISTEN"
done
"@
    Write-Host (Invoke-RemoteScript -Script $verifyScript -Description "发布后验收")
    Write-Host "发布后的队列（排队任务将在退出排空后继续）："
    Show-QueueStatus
    Disable-DeploymentDrain

    Write-Step "清理本项目发布临时目录（服务器写操作 4）"
    $cleanupScript = @"
set -euo pipefail
case '$script:RemoteTemp' in
  /var/tmp/runninghub-video-*) rm -rf -- '$script:RemoteTemp' ;;
  *) echo '临时目录不在允许范围，拒绝清理' >&2; exit 1 ;;
esac
"@
    Invoke-RemoteScript -Script $cleanupScript -Description "清理发布临时目录" | Out-Null
    $script:RemoteTemp = $null

    Write-Host ""
    Write-Host "部署完成：$commit" -ForegroundColor Green
    Write-Host "数据备份和代码备份保留在：$BackupDir"
    Write-Host "Nginx 配置、证书和其他项目均未修改。"
}
finally {
    Disable-DeploymentDrain
    if ($script:LocalTemp -and (Test-Path -LiteralPath $script:LocalTemp)) {
        Remove-Item -LiteralPath $script:LocalTemp -Recurse -Force
    }
    if ($script:RemoteTemp) {
        Write-Warning "服务器临时目录仍保留（便于排查或继续）：$script:RemoteTemp"
    }
    Set-Location -LiteralPath $script:RepoRoot
}
