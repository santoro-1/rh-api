# `runninghub-video` 生产部署手册

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
它只允许操作以下固定范围：

- `/opt/runninghub-video`
- `/var/backups/runninghub-video`
- `/var/tmp/runninghub-video-*`
- `runninghub-video-web.service`
- `runninghub-video-audio.service`
- `runninghub-video-media.service`
- `runninghub-video-worker.service`

脚本不会修改 Nginx、证书、安全组或其他项目，不会自动删除备份。以下情况会拒绝
自动发布：工作区不干净、本地不是最新 `main`、测试失败、项目队列不为空、主项目
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
`/var/backups/runninghub-video/`。

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
