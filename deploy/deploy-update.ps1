[CmdletBinding()]
param(
    [switch]$Deploy,
    [switch]$SkipTests,
    [string]$Server = "root@47.111.171.139",
    [string]$SshKey = "C:\Users\san\.ssh\jyd_auth_deploy_ed25519",
    [string]$AppDir = "/opt/runninghub-video",
    [string]$BackupDir = "/var/backups/runninghub-video",
    [string]$LinuxUser = "rhvideo",
    [string]$Domain = "video.lanyingjk01.com"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:Services = @(
    "runninghub-video-web.service",
    "runninghub-video-audio.service",
    "runninghub-video-worker.service"
)
$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$script:LocalTemp = $null
$script:RemoteTemp = $null

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
    $output = & $Command 2>&1
    if ($LASTEXITCODE -ne 0) {
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
    $ErrorActionPreference = "Continue"
    try {
        $output = & ssh -i $SshKey -o BatchMode=yes -o ConnectTimeout=10 `
            -o ConnectionAttempts=1 -o ServerAliveInterval=5 `
            -o ServerAliveCountMax=3 `
            $Server "printf %s $encoded | base64 -d | bash" 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
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
    return @"
set -euo pipefail
db='$AppDir/data/app.db'
if [ ! -f "`$db" ]; then
  echo '找不到生产数据库' >&2
  exit 1
fi
sqlite3 -readonly "`$db" "
SELECT 'video', count(*) FROM generation_tasks
 WHERE status IN ('PENDING','UPLOADING','SUBMITTED','RUNNING')
UNION ALL
SELECT 'audio', count(*) FROM audio_generation_tasks
 WHERE status IN ('PENDING','PROCESSING','REMOTE_PENDING','SAVING')
UNION ALL
SELECT 'voice', count(*) FROM voice_creation_tasks
 WHERE status IN ('PENDING','PROCESSING','SAVE_PENDING','SAVING');
"
"@
}

function Assert-QueuesIdle {
    $queueOutput = Invoke-RemoteScript -Script (Get-QueueCheckScript) `
        -Description "读取本项目任务队列"
    Write-Host $queueOutput
    $busy = $false
    foreach ($line in ($queueOutput -split "`n")) {
        if ($line -match "^[^|]+\|([1-9][0-9]*)$") {
            $busy = $true
        }
    }
    if ($busy) {
        throw "本项目仍有待处理或运行中的任务，已拒绝部署。请等待任务结束后重试。"
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
        git status --porcelain
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
  systemctl --no-pager is-active $($script:Services -join " ")
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
    Assert-QueuesIdle

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

    Invoke-LocalText -Description "确认本地包含服务器旧版本" -Command {
        git cat-file -e "$deployedRevision`^{commit}"
    } | Out-Null
    $deletedFiles = Invoke-LocalText -Description "检查删除文件" -Command {
        git diff --name-only --diff-filter=D $deployedRevision $commit
    }
    if ($deletedFiles) {
        Write-Host $deletedFiles
        throw "本次包含删除文件。为避免脚本误删服务器内容，请先人工审查这些路径；本脚本不会自动删除。"
    }
    $addedFilesText = Invoke-LocalText -Description "检查新增文件" -Command {
        git diff --name-only --diff-filter=A $deployedRevision $commit
    }
    $addedFiles = @()
    if ($addedFilesText) {
        $addedFiles = @($addedFilesText -split "`n")
        foreach ($relativePath in $addedFiles) {
            $normalizedPath = $relativePath.Replace("\", "/")
            if (
                [IO.Path]::IsPathRooted($relativePath) -or
                $normalizedPath -match "(^|/)\.\.(/|$)" -or
                $normalizedPath -match "^(\.env|\.venv|data|tools)(/|$)"
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
        -Prompt "下一步会创建本项目的数据备份和代码备份，并上传 commit $shortCommit；不会停止服务。" `
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
sudo -u '$LinuxUser' /bin/bash '$AppDir/deploy/scripts/backup.sh'
code_backup='$BackupDir/runninghub-video-code-pre-$shortCommit-$timestamp.tar.gz'
tar -C '$AppDir' \
  --exclude='./.env' \
  --exclude='./.venv' \
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
        -o ConnectionAttempts=1 -o ServerAliveInterval=5 `
        -o ServerAliveCountMax=3 $archive `
        "${Server}:$script:RemoteTemp/release.tar"
    if ($LASTEXITCODE -ne 0) {
        throw "上传发布包失败。"
    }
    & scp -i $SshKey -o BatchMode=yes -o ConnectTimeout=10 `
        -o ConnectionAttempts=1 -o ServerAliveInterval=5 `
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
PATH='$AppDir/tools/ffmpeg/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin' \
  RUNNINGHUB_APP_DIR='$script:RemoteTemp/stage' \
  sudo -u '$LinuxUser' /bin/bash '$script:RemoteTemp/stage/deploy/scripts/preflight.sh'
echo '发布包校验和预检通过'
"@
    Write-Host (Invoke-RemoteScript -Script $stageScript -Description "校验发布包")

    Assert-QueuesIdle
    Confirm-Exact `
        -Prompt "即将只停止三个 runninghub-video 服务，覆盖项目代码，执行数据库迁移，再启动这三个服务。其他服务、Nginx、证书均不修改。" `
        -Expected "DEPLOY $shortCommit"

    Write-Step "更新本项目代码（服务器写操作 3）"
    $mutateScript = @"
set -euo pipefail
services='$($script:Services -join " ")'
old_revision='$deployedRevision'
rollback_code='$BackupDir/runninghub-video-code-pre-$shortCommit-$timestamp.tar.gz'
rollback() {
  echo '发布失败，正在恢复发布前代码；不会自动覆盖数据库。' >&2
  if [ -f '$script:RemoteTemp/added-files.txt' ]; then
    while IFS= read -r relative_path; do
      [ -n "`$relative_path" ] || continue
      case "`$relative_path" in
        /*|../*|*/../*|.env|.env/*|.venv|.venv/*|data|data/*|tools|tools/*)
          echo "回滚清单包含不安全路径：`$relative_path" >&2
          continue
          ;;
        *) rm -f -- '$AppDir/'"`$relative_path" ;;
      esac
    done < '$script:RemoteTemp/added-files.txt'
  fi
  tar -C '$AppDir' -xzf "`$rollback_code"
  printf '%s\n' "`$old_revision" > '$AppDir/.deployed-revision'
  chown '${LinuxUser}:${LinuxUser}' '$AppDir/.deployed-revision'
  systemctl start `$services || true
}
trap rollback ERR

systemctl stop `$services
sudo -u '$LinuxUser' tar -C '$AppDir' -xf '$script:RemoteTemp/release.tar'
printf '%s\n' '$commit' > '$AppDir/.deployed-revision'
chown '${LinuxUser}:${LinuxUser}' '$AppDir/.deployed-revision'
chmod 600 '$AppDir/.env'
PATH='$AppDir/tools/ffmpeg/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin' \
  sudo -u '$LinuxUser' /bin/bash '$AppDir/deploy/scripts/preflight.sh'
PATH='$AppDir/tools/ffmpeg/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin' \
  sudo -u '$LinuxUser' '$AppDir/.venv/bin/python' \
  -m alembic -c '$AppDir/alembic.ini' upgrade head
systemctl start `$services
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS -H 'Host: $Domain' http://127.0.0.1:18083/healthz >/dev/null; then
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
sudo -u '$LinuxUser' '$AppDir/.venv/bin/python' \
  -m alembic -c '$AppDir/alembic.ini' current
sqlite3 -readonly '$AppDir/data/app.db' 'PRAGMA quick_check;'
curl -fsS -H 'Host: $Domain' http://127.0.0.1:18083/healthz
echo
curl -fsS 'https://$Domain/healthz'
echo
nginx -t
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
    Assert-QueuesIdle

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
    if ($script:LocalTemp -and (Test-Path -LiteralPath $script:LocalTemp)) {
        Remove-Item -LiteralPath $script:LocalTemp -Recurse -Force
    }
    if ($script:RemoteTemp) {
        Write-Warning "服务器临时目录仍保留（便于排查或继续）：$script:RemoteTemp"
    }
    Set-Location -LiteralPath $script:RepoRoot
}
