# 单服务器生产部署手册

本文档用于把当前 FastAPI + SQLite + 独立 Worker 项目部署到一台 Ubuntu
24.04 LTS 服务器。部署前先在测试域名完成整套验收，再切换正式域名。

## 1. 服务器结构

```text
Internet -> Nginx :443 -> FastAPI :8000 (仅监听 127.0.0.1)
                              |
                         SQLite / data
                              |
                      RunningHub Worker
```

服务器无需 GPU。Nginx 负责公网入口和 HTTPS；systemd 分别守护 Web 与 Worker。
当前 SQLite 架构只运行一个 Web 进程和一个 Worker，不要自行增加 Uvicorn
`--workers` 或复制 Worker 服务。

## 2. 准备条件

- 一台 Ubuntu 24.04 LTS 服务器，建议从 2 核、4GB 内存、80GB SSD 起步。
- 一个已把 A/AAAA 记录解析到服务器公网 IP 的域名。
- SSH 管理权限。不要在聊天、工单或 Git 中发送密码、API Key 或 `.env`。
- 防火墙只对公网开放 SSH、80 和 443；8000 不开放。

安装系统依赖：

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip ffmpeg sqlite3 nginx certbot python3-certbot-nginx
```

## 3. 创建运行用户和目录

```bash
sudo useradd --system --create-home --home-dir /opt/runninghub --shell /usr/sbin/nologin runninghub
sudo mkdir -p /opt/runninghub /var/backups/runninghub
sudo chown -R runninghub:runninghub /opt/runninghub /var/backups/runninghub
sudo chmod 750 /opt/runninghub /var/backups/runninghub
```

把项目文件上传或检出到 `/opt/runninghub`，然后：

```bash
sudo -u runninghub python3 -m venv /opt/runninghub/.venv
sudo -u runninghub /opt/runninghub/.venv/bin/pip install --upgrade pip
sudo -u runninghub /opt/runninghub/.venv/bin/pip install -r /opt/runninghub/requirements.txt
sudo -u runninghub mkdir -p /opt/runninghub/data/uploads /opt/runninghub/data/outputs
```

## 4. 创建生产环境配置

```bash
sudo -u runninghub cp /opt/runninghub/.env.production.example /opt/runninghub/.env
sudo chmod 600 /opt/runninghub/.env
```

生成两个不同的密钥：

```bash
/opt/runninghub/.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
/opt/runninghub/.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

分别填入 `APP_SECRET_KEY` 和 `APP_ENCRYPTION_KEY`，并把
`ALLOWED_HOSTS` 改成真实域名。`APP_ENCRYPTION_KEY` 一旦丢失，数据库里已有的
RunningHub API Key 将无法解密，所以必须连同 `.env` 安全备份。

执行预检和迁移：

```bash
sudo -u runninghub /bin/bash /opt/runninghub/deploy/scripts/preflight.sh
sudo -u runninghub /opt/runninghub/.venv/bin/python -m alembic -c /opt/runninghub/alembic.ini upgrade head
sudo -u runninghub /opt/runninghub/.venv/bin/python -m scripts.create_admin admin
```

## 5. 安装 systemd 服务

```bash
sudo cp /opt/runninghub/deploy/systemd/runninghub-*.service /etc/systemd/system/
sudo cp /opt/runninghub/deploy/systemd/runninghub-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now runninghub-web runninghub-worker
sudo systemctl enable --now runninghub-backup.timer runninghub-cleanup.timer
```

检查：

```bash
sudo systemctl status runninghub-web runninghub-worker
sudo journalctl -u runninghub-web -u runninghub-worker -n 100 --no-pager
curl -H "Host: video.example.com" http://127.0.0.1:8000/healthz
```

## 6. 配置 Nginx 和 HTTPS

把模板中的 `__DOMAIN__` 替换为真实域名：

```bash
sudo sed 's/__DOMAIN__/video.example.com/g' /opt/runninghub/deploy/nginx/runninghub.conf.template \
  | sudo tee /etc/nginx/sites-available/runninghub >/dev/null
sudo ln -s /etc/nginx/sites-available/runninghub /etc/nginx/sites-enabled/runninghub
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

确认域名 HTTP 可访问后签发证书：

```bash
sudo certbot --nginx -d video.example.com --redirect
sudo certbot renew --dry-run
```

Nginx 模板已把请求体上限设为 550MB，以覆盖应用当前 500MB 的视频上限。
如果以后修改 `MAX_VIDEO_SIZE_MB`，必须同步调整 `client_max_body_size`。

## 7. 上线验收

至少完成以下验证：

1. 只能通过 HTTPS 登录，HTTP 自动跳转 HTTPS。
2. 管理员可创建、禁用用户；普通用户无法进入用户管理。
3. 数字人固定使用 Stand 24G，视频对口型可以切换 Stand/Plus。
4. 并发数设为 2 后连续提交 8 个任务：2 个进入远程处理，其余保持等待。
5. 重启 Worker 后，已有远程任务不会重复提交。
6. 上传接近上限的视频，确认不会出现 Nginx 413。
7. 手动执行一次备份，确认生成的归档不为空，并在隔离目录演练恢复。
8. 确认 `journalctl` 不输出 API Key、密码或素材内容。

## 8. 日常维护

查看日志：

```bash
sudo journalctl -u runninghub-web -f
sudo journalctl -u runninghub-worker -f
```

发布新版本：

```bash
sudo systemctl stop runninghub-worker runninghub-web
sudo -u runninghub /opt/runninghub/.venv/bin/pip install -r /opt/runninghub/requirements.txt
sudo -u runninghub /opt/runninghub/.venv/bin/python -m alembic -c /opt/runninghub/alembic.ini upgrade head
sudo systemctl start runninghub-web runninghub-worker
```

更新前先运行备份。不要删除或重建 `data/app.db` 来处理迁移问题。

手动备份：

```bash
sudo -u runninghub /bin/bash /opt/runninghub/deploy/scripts/backup.sh
```

备份归档包含数据库、上传、输出和 `.env`，因此属于敏感文件。应定期复制到
另一台机器或对象存储，并限制读取权限。

恢复会覆盖当前数据，必须先停止服务并显式确认：

```bash
sudo systemctl stop runninghub-worker runninghub-web
sudo /bin/bash /opt/runninghub/deploy/scripts/restore.sh \
  /var/backups/runninghub/runninghub-YYYYmmddTHHMMSSZ.tar.gz --confirm
sudo systemctl start runninghub-web runninghub-worker
```
