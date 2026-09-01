# `runninghub-video` 生产部署手册

设备授权的独立 A/B 测试环境与生产环境严格分离，当前实例、HTTPS 状态、隔离边界和 A/B 验收步骤
见 [`DEVICE_AUTH_STAGING.md`](./DEVICE_AUTH_STAGING.md)。该环境只启动受限 Web，不能替代本手册的
生产发布流程。

本目录记录当前生产环境的可复现配置。正式环境使用独立目录、用户、
systemd 服务、Nginx 配置和证书，不与服务器上的其他项目共用。

## 固定部署结构

```text
/opt/runninghub-video/                 项目代码、虚拟环境、SQLite、上传与输出
/var/backups/runninghub-video/         独立备份
/etc/nginx/conf.d/runninghub-video.conf
/etc/systemd/system/runninghub-video-web.service
/etc/systemd/system/runninghub-video-audio.service
/etc/systemd/system/runninghub-video-media.service
/etc/systemd/system/runninghub-video-worker.service
/etc/letsencrypt/live/video.lanyingjk01.com/
```

- Linux 用户：`rhvideo`
- Web 监听：`127.0.0.1:18083`
- 公网入口：`https://video.lanyingjk01.com`
- 对外只使用 Nginx 的 80/443，不开放内部端口
- `.env` 权限必须为 `600`
- 音频和媒体 Worker 固定使用较低 CPU、I/O 优先级及内存上限，避免影响服务器上的
  24 小时客服项目

## 长媒体处理位置

生产服务器不安装、不启动 FunASR，也不监听 18084。ASR 与 FFmpeg 长媒体处理
统一由仓库 `media_node/` 下的 Windows 固定电脑节点负责。

生产使用 pull 模式的 Windows 媒体节点：服务器提供受 Bearer Token 保护的领取、
下载、续租和回传接口；电脑只主动访问 `https://video.lanyingjk01.com`，无需在
服务器安全组或电脑路由器开放新端口。任务默认每分钟续租，租约过期后可重新领取。

## 本地发布前要求

1. 从最新 `origin/main` 创建功能分支。
2. 本地运行完整 `pytest`。
3. 本地使用管理员、普通账号 A、普通账号 B 验证权限和数据隔离。
4. 所有数据库迁移先在测试数据库执行。
5. 提交并推送确定的 Git commit 后才能发布。
6. 禁止把 `.env`、SQLite、上传、输出或日志提交到 Git。

## Windows 安全更新脚本

推荐从 Windows 本地项目目录运行
[`deploy-update.ps1`](./deploy-update.ps1)。服务器没有安装 Git，而且仓库是私有
仓库，因此脚本会在本地把已经合并到 `main` 的准确 commit 打包后，通过 SSH
上传到本项目独立的临时目录。

### 普通代码更新（推荐日常使用）

只修改 `app/` 业务代码、页面、Prompt、测试或文档时，使用
[`deploy-code-update.ps1`](./deploy-code-update.ps1)。它复用完整部署脚本的测试、版本校验、
排空、临时 SQLite 回滚点、代码回滚和健康检查，但不会复制 `uploads/outputs`，因此不会产生
数十 GB 的备份峰值：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\deploy-code-update.ps1
powershell -ExecutionPolicy Bypass -File .\deploy\deploy-code-update.ps1 -Deploy
```

代码更新模式发现 `requirements*.txt`、`pyproject.toml`、`uv.lock`、`alembic/`、`data/`、
`tools/`、`deploy/scripts/`、`deploy/systemd/`、`deploy/nginx/` 或 `.env*` 发生变化时会拒绝继续；
这类更新必须使用下方的完整部署脚本。

### 完整更新

先更新本地 `main`：

```powershell
Set-Location -LiteralPath "D:\工作内容\轻盈健\数字人\runninghub_mvp"
git switch main
git fetch origin
git pull --ff-only origin main
```

第一次先不带 `-Deploy`。这只会运行本地测试和服务器只读检查，不会修改服务器：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\deploy-update.ps1
```

看到“只读检查通过”后，再明确进入部署模式：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\deploy-update.ps1 -Deploy
```

脚本会在关键写操作前显示将修改的范围，并要求输入两次带 commit 的确认词。
第二次确认后会自动进入“排空模式”：登录、浏览、预览和下载继续可用；新建、修改
任务以及 Worker 领取下一条任务会暂停；已经提交或执行中的任务会继续收尾。脚本只
等待真正执行中的任务，不要求 `PENDING` 队列为空，排空后自动完成发布并恢复领取。
合并队列只把正在合并，或所有分段任务均已成功且确实可以开始合并的记录视为繁忙；
底层分段已经失败、取消或缺失的历史 `MERGE_PENDING` 标记不会永久阻塞发布，也不会
由部署脚本改写其业务状态。
默认最多等待 120 分钟，可用 `-DrainTimeoutMinutes` 在 5 到 720 分钟范围内调整。

排空标记位于 `data/runtime/deployment-drain.json`，由发布脚本以本次随机 token 管理，
只删除自己创建的标记；标记还带有过期时间，即使发布终端异常退出也不会永久锁住
操作。首次把排空功能发布到尚不支持它的旧版本时仍要求整个队列空闲一次，此后的
发布才会自动排空。

它只允许操作以下固定范围：

- `/opt/runninghub-video`
- `/var/backups/runninghub-video`
- `/var/tmp/runninghub-video-*`
- `runninghub-video-web.service`
- `runninghub-video-audio.service`
- `runninghub-video-media.service`
- `runninghub-video-worker.service`

脚本不会修改 Nginx、证书、安全组或其他项目。完整数据备份创建成功后会按精确文件名自动
轮换；默认保留最近 1 份完整数据备份和 1 份名称符合规则的部署代码回滚快照，独立数据库快照
和配置备份不在该轮换范围内。以下情况会拒绝
自动发布：工作区不干净、本地不是最新 `main`、测试失败、执行中任务在排空超时后
仍未结束、主项目
`requirements.txt` 发生变化、发布范围不匹配，或本次 Git 变更包含文件删除。

如果上传或预检阶段失败，生产服务尚未停止。代码覆盖或迁移阶段失败时，脚本会
恢复发布前代码并尝试重新启动本项目服务；数据库不会被静默覆盖，完整数据备份
会保留在 `/var/backups/runninghub-video/`，需要确认后再按“回滚与恢复”处理。

代码发布并完成数据库迁移后，再单独启用远程节点模式：

```bash
/bin/bash /opt/runninghub-video/deploy/scripts/configure-remote-media-worker.sh \
  remote --confirm
```

脚本会先备份 `.env`，生成或保留独立的 `MEDIA_WORKER_TOKEN`，设置
`MEDIA_PROCESSING_MODE=remote` 和 `LONG_AUDIO_ALIGNMENT_PROVIDER=funasr_http`，
只重启本项目 Web 与媒体 Worker，并把需要复制到固定电脑 `media_node/.env` 的令牌输出
一次。令牌不得提交到 Git、聊天记录或截图中。

需要暂时停用电脑节点、恢复服务器启发式本地处理时执行：

```bash
/bin/bash /opt/runninghub-video/deploy/scripts/configure-remote-media-worker.sh \
  local --confirm
```

服务器没有 ASR，因此 `local` 仅作为低准确度应急回退，不适合正式长音频。

## 自动备份周期

自动全量备份每 3 个日历日执行一次，仍在服务器北京时间凌晨 03:30～03:45 运行，保留最近
1 份完整数据备份。定时器每日检查，但备份服务的 `ExecCondition` 会跳过尚未到期的日期；
不会采用每月 `1/3` 日这种月底可能连续触发的表达式。

周期标记是 `/var/lib/runninghub-video-backup/last-scheduled-run`，由 systemd
`StateDirectory` 管理，服务器重启不会清空。标记在实际执行前更新，因此执行失败也不会每天
重做全量备份；排障后可以显式运行 `backup.sh`，该手动入口和部署前备份不受三天周期限制。
首次安装且无标记时会在下一个凌晨执行；修改已有周期时可以把标记初始化为当前日期，
从当天开始间隔三天。`systemctl list-timers` 显示的是下一次检查时间，不一定是实际备份日期。

2026-08-31 生产环境已将周期起点设为当天，下一次实际备份为 2026-09-03 凌晨。此操作只调整
备份服务和定时器，不发布业务代码，不重启网站或任务 Worker，不立即运行全量备份。

降低备份频率不会降低单次备份的空间峰值；脚本仍先复制上传和输出，再压缩，成功后才轮换旧包。

## 服务器发布前只读检查

```bash
systemctl is-active runninghub-video-web.service
systemctl is-active runninghub-video-audio.service
systemctl is-active runninghub-video-media.service
systemctl is-active runninghub-video-worker.service
curl -fsS -H 'Host: video.lanyingjk01.com' http://127.0.0.1:18083/healthz
```

确认任务队列没有正在执行的任务，并先运行独立备份：

```bash
sudo -u rhvideo /bin/bash /opt/runninghub-video/deploy/scripts/backup.sh
```

备份必须包含 SQLite、上传、输出和 `.env`，并保存到
`/var/backups/runninghub-video/`。`RUNNINGHUB_BACKUP_KEEP_COUNT` 同时控制完整数据备份和部署
代码回滚快照的保留数量，必须为大于 0 的整数；systemd 生产单元显式设置为 `1`。轮换只在
新归档创建并设好权限后执行，只匹配 `runninghub-video-YYYYmmddTHHMMSSZ.tar.gz` 和
`runninghub-video-code-pre-<12位commit>-YYYYmmddTHHMMSSZ.tar.gz`，不会删除独立数据库快照、
配置备份或其他用途的文件。

## 设备授权签名根（首次正式发布前）

设备授权不能复用 `APP_SECRET_KEY`，也不能在开发电脑临时生成一把密钥后当作正式根。
只有管理员明确批准初始化时，才在受控 Linux 服务器执行下面的流程。私钥目录必须位于
源码 `/opt/runninghub-video` 之外，不得进入 Git、备份给客户端的 ZIP、聊天或截图。

先准备只有服务账号可访问的父目录；`<release-id>` 使用本次批准的唯一发布编号：

```bash
sudo install -d -o rhvideo -g rhvideo -m 0700 \
  /etc/runninghub-video/device-auth
sudo -u rhvideo /opt/runninghub-video/.venv/bin/python \
  /opt/runninghub-video/deploy/device_auth_keys.py init \
  --output-directory /etc/runninghub-video/device-auth/<release-id> \
  --origin https://video.lanyingjk01.com \
  --kid <release-id> \
  --confirm CREATE-SERVER-DEVICE-SIGNING-KEY
```

工具只在 Linux、精确确认词、全新目录和 HTTPS 根域名同时满足时创建 P-256 私钥；它拒绝
把私钥写入源码目录，也不会覆盖已有目录。生成后先只读核对私钥与公钥清单是否匹配：

```bash
sudo -u rhvideo /opt/runninghub-video/.venv/bin/python \
  /opt/runninghub-video/deploy/device_auth_keys.py inspect \
  --public-document /etc/runninghub-video/device-auth/<release-id>/device-auth-public-keys.json \
  --private-key /etc/runninghub-video/device-auth/<release-id>/device-auth-signing-<release-id>.pem \
  --active-kid <release-id>
```

把生成的四个 `WORKBENCH_DEVICE_AUTH_*` 路径值写入服务器 `.env`，先保留控制模式为
`OFF`。只把 `device-auth-public-keys.json` 通过获准通道交给客户端构建电脑；绝不能传私钥或
`SERVER-DEVICE-AUTH.env`。在构建电脑编入公钥的命令为：

```powershell
Set-Location -LiteralPath 'D:\工作内容\轻盈健\数字人\runninghub_mvp'
D:\Myanaconda\python.exe .\deploy\device_auth_keys.py compile-client `
  --public-document 'D:\受控公钥\device-auth-public-keys.json' `
  --jyd-project-root 'D:\工作内容\轻盈健\公寓\jyd_plain_json_probe' `
  --confirm COMPILE-APPROVED-PUBLIC-ROOTS
```

编入后必须提交并重新构建 JYD；构建脚本会拒绝空信任根、测试根和私钥字段。密钥轮换先用
`rotate --previous-public-document ...` 产生同时包含旧、新公钥的清单，先发布双公钥客户端，
再切换服务器活动私钥。未覆盖完所有已批准电脑前不能移除旧公钥。工具不会自动部署、修改
设备数据库或启用强制模式。

## 设备授权模式切换与 A/B 双机验收

设备授权业务代码和迁移部署完成后，模式仍由数据库单例控制，不能手工执行 SQL，也不在网页
后台提供全局强制按钮。服务器专用工具 `deploy/device_auth_control.py` 默认只读；实际切换必须
提供当前修订号、操作人、原因和工具输出的精确确认词。只允许
`OFF → OBSERVE → ENFORCE → OBSERVE → OFF`，禁止 `OFF → ENFORCE` 和
`ENFORCE → OFF` 跳级。进入 `ENFORCE` 前还会核对正式签名私钥/公钥匹配、至少一个有效设备
授权以及全局影响确认。成功切换与审计记录在同一个数据库事务中。

先只读检查：

```bash
cd /opt/runninghub-video
sudo -u rhvideo /opt/runninghub-video/.venv/bin/python \
  /opt/runninghub-video/deploy/device_auth_control.py
```

输出的 `allowed_transitions` 会给出当前修订对应的确认词。例如当前为 `OFF`、修订为 `1` 时，
先进入观察模式：

```bash
sudo -u rhvideo /opt/runninghub-video/.venv/bin/python \
  /opt/runninghub-video/deploy/device_auth_control.py \
  --set-mode OBSERVE \
  --expected-revision 1 \
  --operator san \
  --reason 'A local and B 250 acceptance preparation' \
  --confirm 'CHANGE-WORKBENCH-DEVICE-MODE:OFF->OBSERVE:REVISION-1'
```

真正验证未授权 B 机被拦截时，重新只读检查并使用当次返回的修订号和确认词。开启强制还必须
显式确认全局影响：

```bash
sudo -u rhvideo /opt/runninghub-video/.venv/bin/python \
  /opt/runninghub-video/deploy/device_auth_control.py \
  --set-mode ENFORCE \
  --expected-revision 2 \
  --operator san \
  --reason 'short A local and B 250 copy rejection window' \
  --confirm 'CHANGE-WORKBENCH-DEVICE-MODE:OBSERVE->ENFORCE:REVISION-2' \
  --acknowledge-global-impact
```

示例修订号只能用于说明，不能照抄生产当前值。验收后先按工具最新输出把 `ENFORCE` 切回
`OBSERVE`；确认观察记录无异常后，若需要再单独从 `OBSERVE` 切回 `OFF`。

本轮固定角色为：当前开发电脑 `A-local`，250 处理机 `B-250`。两台电脑使用同一网站测试账号，
账号设备额度固定为 `1`；申请授权时把设备备注分别填写成上述两个值。A 获批，B 保持
`PENDING`、`REJECTED` 或 `REVOKED`，不能批准。完整复制测试要把 A 解压后的程序目录复制到 B；
还可以额外复制 `%ProgramData%\PublicVideoWorkbench\Licensing` 的非权威缓存，但不得导出、复制
或删除 Windows CNG/TPM 私钥。

服务器证据工具 `deploy/device_auth_acceptance.py` 的 `capture` 只读数据库，并只保存公开设备
摘要、授权编号、状态、版本、模式和有限审计动作；不包含密码、令牌、私钥、RunningHub Key、
本地路径或硬件序列号。先准备独立证据目录：

```bash
sudo install -d -o rhvideo -g rhvideo -m 0700 \
  /var/lib/runninghub-video-device-acceptance
```

A 获批后记录基线；`<账号>`、`<包SHA256>` 和 `<构建名>` 使用实际值：

```bash
sudo -u rhvideo /opt/runninghub-video/.venv/bin/python \
  /opt/runninghub-video/deploy/device_auth_acceptance.py capture \
  --username '<账号>' --role A --grant-label 'A-local' \
  --machine-label '当前开发电脑' --package-sha256 '<包SHA256>' \
  --build-label '<构建名>' \
  --output /var/lib/runninghub-video-device-acceptance/A-v1.json
```

B 从复制目录启动、申请出自己的新设备并保持未批准后，在短时 `ENFORCE` 窗口记录 B：

```bash
sudo -u rhvideo /opt/runninghub-video/.venv/bin/python \
  /opt/runninghub-video/deploy/device_auth_acceptance.py capture \
  --username '<账号>' --role B --grant-label 'B-250' \
  --machine-label '250处理机' --package-sha256 '<同一个包SHA256>' \
  --build-label '<同一个构建名>' \
  --output /var/lib/runninghub-video-device-acceptance/B-copy.json

sudo -u rhvideo /opt/runninghub-video/.venv/bin/python \
  /opt/runninghub-video/deploy/device_auth_acceptance.py verify-copy \
  --a /var/lib/runninghub-video-device-acceptance/A-v1.json \
  --b /var/lib/runninghub-video-device-acceptance/B-copy.json \
  --output /var/lib/runninghub-video-device-acceptance/copy-report.json
```

报告只有在 A/B 属于同一账号和同一包、A 仍有效、B 未获批准、两台设备的公钥摘要、设备编号和
授权编号均不同，并且 B 的快照取自 `ENFORCE` 时才通过。每个输出文件都使用独占创建，工具拒绝
覆盖旧证据。

连续更新免激活需要在 A 机按 V1、V2、V3、V4 至少记录四份不同构建、不同包摘要的快照，随后：

```bash
sudo -u rhvideo /opt/runninghub-video/.venv/bin/python \
  /opt/runninghub-video/deploy/device_auth_acceptance.py verify-upgrade \
  --capture /var/lib/runninghub-video-device-acceptance/A-v1.json \
  --capture /var/lib/runninghub-video-device-acceptance/A-v2.json \
  --capture /var/lib/runninghub-video-device-acceptance/A-v3.json \
  --capture /var/lib/runninghub-video-device-acceptance/A-v4.json \
  --output /var/lib/runninghub-video-device-acceptance/upgrade-report.json
```

四次快照的账号、公钥摘要、`device_id` 和 `grant_id` 必须完全一致且始终有效；构建名和包
SHA-256 必须各不相同。更新时只覆盖程序文件，不能删除机器级 CNG 身份、注册表定位记录、
`%ProgramData%` 授权缓存或 Agent 回执。

## 更新代码

只发布已确认的 Git commit，不在服务器上直接编辑应用代码。

```bash
cd /opt/runninghub-video
sudo -u rhvideo /opt/runninghub-video/.venv/bin/pip install \
  -r /opt/runninghub-video/requirements.txt
sudo -u rhvideo /bin/bash /opt/runninghub-video/deploy/scripts/preflight.sh
sudo -u rhvideo /opt/runninghub-video/.venv/bin/python \
  -m alembic -c /opt/runninghub-video/alembic.ini upgrade head
```

只重启本项目四个服务：

```bash
systemctl restart runninghub-video-web.service
systemctl restart runninghub-video-audio.service
systemctl restart runninghub-video-media.service
systemctl restart runninghub-video-worker.service
```

不得停止、重启或修改服务器上的其他项目。

## Nginx

只有反向代理配置确实变化时才更新
`/etc/nginx/conf.d/runninghub-video.conf`。更新后必须先验证：

```bash
nginx -t
```

验证通过后只能 reload：

```bash
systemctl reload nginx
```

不得 restart Nginx，也不得覆盖其他项目的 Nginx 配置或证书。

## 发布后验收

1. 四个 `runninghub-video` 服务均为 active。
2. `/healthz` 返回 `{"status":"ok"}`。
3. HTTPS 登录、管理员页面和普通用户页面正常。
4. 用户 A、B 的音色、任务、上传和输出互相不可见。
5. 使用官方音色完成一条最小真实任务。
6. 管理员资源面板可看到 CPU、内存、磁盘、FFmpeg 和队列状态。
7. Web、语音和视频日志均写入 `data/logs/`，且敏感信息已脱敏。
8. 同时验证服务器上的关键客服服务仍然正常。

## 回滚与恢复

代码异常优先回滚到发布前确认的 Git commit。涉及数据恢复时，必须先停止
本项目四个服务，再显式执行：

```bash
/bin/bash /opt/runninghub-video/deploy/scripts/restore.sh \
  /var/backups/runninghub-video/runninghub-video-YYYYmmddTHHMMSSZ.tar.gz \
  --confirm
```

恢复操作会替换当前数据，只能在确认备份路径和影响范围后执行。
