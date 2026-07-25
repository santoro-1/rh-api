# RunningHub 视频生成中转站

这是一个 FastAPI + SQLite + Jinja2 的单服务器应用：浏览器仅访问本站；Web 进程创建任务；独立 Worker 使用服务端保存且加密的 RunningHub API Key 上传素材、提交工作流、轮询状态并把视频下载到服务器。当前支持数字人视频和 LTX 2.3 视频对口型两个工作流。

版本变化见 [CHANGELOG.md](CHANGELOG.md)，当前技术状态和后续交接边界见
[PROJECT_STATUS.md](PROJECT_STATUS.md)。

## 本地前置条件

- 已安装 Anaconda Python（当前项目按你的 base 环境验证）
- Python 3.11+（需求目标为 3.12，代码兼容）
- 已安装并可在 PATH 中调用 ffprobe

## 初始化

在项目根目录执行：

    conda activate base
    python -m pip install -r requirements.txt
    Copy-Item .env.example .env
    python -m alembic upgrade head
    python -m scripts.create_admin admin

首次创建管理员时会提示输入密码。RunningHub API Key、Base URL、数字人 AI App ID、LTX Workflow ID、LTX 默认实例、默认提示词和并发数均在登录后的管理员网页中配置；Key 不会回显，且只以 Fernet 加密密文保存在 SQLite 中。

.env 至少应修改：

- APP_SECRET_KEY：一段随机长字符串
- 生产环境必须设置 APP_ENCRYPTION_KEY：Fernet URL-safe Base64 key
- COOKIE_SECURE=false：仅限本地 HTTP；上线 HTTPS 后改为 true
- MAX_VIDEO_SIZE_MB：LTX 源视频上传上限，默认 500 MB

可用以下方式生成加密 Key：

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

## 启动

打开两个终端，都先执行 conda activate base：

    python -m uvicorn app.main:app --reload

    python -m app.workers.task_worker

然后访问 http://127.0.0.1:8000 。Web 进程和 Worker 必须同时运行。

创建任务时先在统一页面选择数字人或视频对口型工作流。数字人固定使用 24G 普通版；视频对口型可为当前任务选择 `default` 或 `plus`。

任务统一先保存为 `PENDING`。Worker 按用户的最大同时任务数占用运行槽位并按创建时间依次提交；例如并发数为 2、一次创建 8 个任务时，2 个运行、6 个留在本地队列，任一槽位释放后自动补充下一个。

## 日常命令

    python -m pytest -q
    python -m alembic current
    python -m scripts.cleanup_files

清理命令仅处理终态任务，默认保留上传素材 3 天、生成视频 7 天，可通过 .env 调整。

## 手动真实 RunningHub 联调

自动化测试完全 mock RunningHub，不会产生费用。仅在需要手动验证时，于当前 PowerShell 临时设置环境变量（不要写回代码或 .env.example）：

    $env:RUNNINGHUB_API_KEY = "你的新 Key"
    python runninghub_local_test.py --image .\sample.jpg --audio .\sample.mp3 --end 0:15
    Remove-Item Env:RUNNINGHUB_API_KEY

该命令会产生真实 RunningHub 消耗。网站联调则是在管理员页面把 Key 粘贴进对应用户配置后，登录该用户创建任务；Worker 会接手后续流程。

## 数据与安全

- SQLite 启用 WAL、外键与 busy timeout，适合一个 Web 进程和一个 Worker 并发访问。
- 数据文件位于 data/uploads/用户ID/任务ID/ 和 data/outputs/用户ID/任务ID/。
- 用户密码使用 PBKDF2-SHA256 哈希；RunningHub Key 使用 Fernet 对称加密。
- 任务、图片预览和视频下载均进行登录与归属校验；管理员可查看全部任务。
- 所有登录、退出、配置修改和任务创建请求均校验 CSRF Token。
- 生产环境强制要求 HTTPS Cookie、随机应用密钥、稳定的 Fernet Key 和明确的可信域名。
- 请立即在 RunningHub 后台轮换旧 Notebook 中曾使用过的 API Key。

## 数据库表

- users：网站账号、密码哈希、管理员与启用状态
- runninghub_configs：一对一的加密 API Key 与该用户的 RunningHub 配置
- workflow_configs：每个用户、每个工作流的远程 ID、实例类型、默认提示词和启用状态
- generation_tasks：本地任务 ID、远程 taskId、输入素材、时间范围、状态、错误、usage 与本地结果路径

## 生产部署

仓库已提供 Ubuntu 单服务器部署模板，包括：

- Nginx 反向代理和大文件上传配置；
- Web、Worker、自动备份和自动清理的 systemd 服务；
- 生产环境变量模板、预检、备份与恢复脚本；
- HTTPS、上线验收、更新和日志检查步骤。

完整说明见 [deploy/README.md](deploy/README.md)。正式部署时使用一个 Web 进程和
一个 Worker；公网不直接开放 8000 端口。
