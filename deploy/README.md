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
/etc/systemd/system/runninghub-video-asr.service
/etc/systemd/system/runninghub-video-worker.service
/etc/letsencrypt/live/video.lanyingjk01.com/
```

- Linux 用户：`rhvideo`
- Web 监听：`127.0.0.1:18083`
- ASR 仅监听服务器环回地址：`127.0.0.1:18084`
- 公网入口：`https://video.lanyingjk01.com`
- 对外只使用 Nginx 的 80/443，不开放内部端口
- `.env` 权限必须为 `600`
- 音频和媒体 Worker 固定使用较低 CPU、I/O 优先级及内存上限，避免影响服务器上的
  24 小时客服项目

## ASR 隔离环境

ASR 使用 `/opt/runninghub-video/.asr-runtime/venv`，不向主项目 `.venv`
安装 PyTorch 或 FunASR。模型下载到 `/opt/runninghub-video/data/asr-models`。
`deploy/scripts/install-asr.sh` 会计算依赖指纹：

- 首次部署时创建隔离环境并安装 CPU 版依赖。
- 普通代码更新且依赖未变化时只验证环境，随后跳过安装。
- ASR 依赖或指定的 PyTorch 版本变化时才重建隔离环境。
- 首次缺少 `ASR_SHARED_TOKEN` 时生成独立随机密钥并写入权限为 `600` 的 `.env`。
- 安装使用项目内持久 pip 缓存、长读取超时和重试；生产服务器的通用 Python
  依赖默认从阿里云镜像下载，CPU 版 PyTorch 仍从官方 wheel 源获取。

ASR systemd 单元限制为单进程、`CPUQuota=150%`、`MemoryMax=2560M`，并只监听
`127.0.0.1:18084`。不得开放安全组、防火墙或 Nginx 公网代理到该端口。

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
- `runninghub-video-asr.service`
- `runninghub-video-worker.service`

脚本不会修改 Nginx、证书、安全组或其他项目，不会自动删除备份。以下情况会拒绝
自动发布：工作区不干净、本地不是最新 `main`、测试失败、项目队列不为空、主项目
`requirements.txt` 发生变化、发布范围不匹配，或本次 Git 变更包含文件删除。
ASR 独立依赖由安装脚本通过指纹单独管理。

如果上传或预检阶段失败，生产服务尚未停止。代码覆盖或迁移阶段失败时，脚本会
恢复发布前代码并尝试重新启动本项目服务；数据库不会被静默覆盖，完整数据备份
会保留在 `/var/backups/runninghub-video/`，需要确认后再按“回滚与恢复”处理。

## 服务器发布前只读检查

```bash
systemctl is-active runninghub-video-web.service
systemctl is-active runninghub-video-audio.service
systemctl is-active runninghub-video-media.service
systemctl is-active runninghub-video-asr.service
systemctl is-active runninghub-video-worker.service
curl -fsS -H 'Host: video.lanyingjk01.com' http://127.0.0.1:18083/healthz
curl -fsS http://127.0.0.1:18084/healthz
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
RUNNINGHUB_APP_DIR=/opt/runninghub-video \
  RUNNINGHUB_RELEASE_DIR=/opt/runninghub-video \
  RUNNINGHUB_LINUX_USER=rhvideo \
  /bin/bash /opt/runninghub-video/deploy/scripts/install-asr.sh
```

只重启本项目五个服务：

```bash
systemctl restart runninghub-video-web.service
systemctl restart runninghub-video-audio.service
systemctl restart runninghub-video-media.service
systemctl restart runninghub-video-asr.service
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

1. 五个 `runninghub-video` 服务均为 active。
2. `/healthz` 返回 `{"status":"ok"}`。
3. HTTPS 登录、管理员页面和普通用户页面正常。
4. 用户 A、B 的音色、任务、上传和输出互相不可见。
5. 使用官方音色完成一条最小真实任务。
6. 管理员资源面板可看到 CPU、内存、磁盘、FFmpeg 和队列状态。
7. Web、语音和视频日志均写入 `data/logs/`，且敏感信息已脱敏。
8. 同时验证服务器上的关键客服服务仍然正常。

## 回滚与恢复

代码异常优先回滚到发布前确认的 Git commit。涉及数据恢复时，必须先停止
本项目五个服务，再显式执行：

```bash
/bin/bash /opt/runninghub-video/deploy/scripts/restore.sh \
  /var/backups/runninghub-video/runninghub-video-YYYYmmddTHHMMSSZ.tar.gz \
  --confirm
```

恢复操作会替换当前数据，只能在确认备份路径和影响范围后执行。
