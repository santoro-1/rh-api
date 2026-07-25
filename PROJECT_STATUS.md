# 项目状态与交接说明

> 这是一份持续维护的项目状态文档。每次处理新需求前，先阅读本文件；当一轮需求完成、确认或发生重要决策后，更新本文件，而不是依赖聊天记录回忆项目状态。

## 1. 项目定位

本项目是一个可本地运行、并已完成单服务器部署前准备的
**RunningHub 数字人视频生成中转站 MVP**。

- 用户通过网页上传素材、设置参数并创建本地任务。
- Web 请求不会等待 RunningHub 完成。
- 独立 Worker 负责上传素材、提交 RunningHub、轮询状态和下载结果。
- RunningHub API Key 只在服务端使用并加密保存，浏览器不会拿到密钥。
- 当前支持 `digital_human` 与 `ltx_lip_sync` 两个工作流，后端使用独立适配器扩展工作流。

项目目录：`runninghub_mvp/`

## 2. 阅读顺序与信息优先级

处理新需求时，按下列顺序了解上下文：

1. 本文件 `PROJECT_STATUS.md`：当前已完成内容、约束、风险和下一步。
2. `../api文档.md`：RunningHub 最新调用参数；如与旧文档或代码冲突，以最新版 API 文档为准并同步修改适配器与测试。
3. `../PRODUCT_REQUIREMENTS.md`：最初产品目标与 MVP 验收要求。
4. `WORKFLOW_EXTENSION.md`：新增工作流的边界和步骤。
5. 相关适配器、路由、模板及测试：以实际代码验证现状。

不要从历史聊天记录、旧 Notebook 或 README 中猜测当前节点参数；它们可能已过时。

## 3. 当前完成状态

### 已完成

- FastAPI + Jinja2 网页应用，SQLite（WAL、外键、busy timeout）与独立 Worker。
- 管理员和普通用户登录、权限隔离；管理员可创建/编辑用户及其 RunningHub 配置。
- API Key 使用 Fernet 加密保存；密码使用 PBKDF2-SHA256 哈希。
- 图片、音频上传与文件大小/类型校验；图片预览。
- 使用 `ffprobe` 读取音频时长，并在 `ffprobe` 失败时回退到 Mutagen/WAV 解析。
- 时间格式与范围校验；默认开始时间为 `0:00`，结束时间为音频完整秒数。
- 本地任务创建、持久化 FIFO 队列、按用户并发槽位调度、任务列表/详情、状态轮询、预览与下载。
- Worker 在保存 `runninghub_task_id` 后只查询而不重复提交，避免重复扣费；成功结果下载到本地数据目录。
- 数据文件和任务权限隔离，管理员可查看全部任务。
- 多工作流基础架构：通用客户端、通用任务系统、工作流适配器、用户 × 工作流配置。
- 已注册 `digital_human` 和 `ltx_lip_sync` 两个工作流。
- 统一 `/generate` 页面可在数字人视频与 LTX 2.3 对口型之间切换；当前发布的 LTX 工作流固定要求源视频、自定义音频和正向提示词三项输入。
- 管理员可以按用户启用 LTX 工作流、配置 Workflow ID、实例类型和默认提示词。
- RunningHub 客户端支持 AI App 与私有 Workflow 两种 V2 提交端点。
- 视频上传支持 MP4、MOV、WEBM，默认大小上限由 `MAX_VIDEO_SIZE_MB=500` 控制。
- 统一生成页在两个工作流表单外提供任务级实例选择；数字人固定普通版 `default`（24G），LTX 可选 `default` 或 `plus`，选择随任务保存并由 Worker 使用。
- 管理员页面不再提供重复的数字人实例选择；账户级旧字段固定保留 `default` 作为兼容值，实际新任务以创建页面选择为准。
- 用户界面将 `ltx_lip_sync` 简称为“视频对口型”，实例选项显示为“Stand 运行（24G）”与“Plus 运行（48G）”，不展示 API 文档或 Worker 实现说明。
- 所有 POST 表单和任务 API 均使用 Session 内的 CSRF Token 校验；登录成功后保留并继续使用当前 Token。
- 生产配置会强制校验随机 `APP_SECRET_KEY`、有效 Fernet Key、`COOKIE_SECURE=true` 和明确的 `ALLOWED_HOSTS`，拒绝通配可信域名。
- FastAPI 已启用 `TrustedHostMiddleware`，并提供会检查数据库连接的 `/healthz` 健康检查。
- 已添加 Ubuntu 单服务器部署包：Nginx、Web/Worker systemd 服务、自动备份、自动清理、生产环境模板、预检和显式确认的恢复脚本。
- 部署结构固定为一个 Web 进程和一个 Worker；Uvicorn 仅监听 `127.0.0.1:8000`，由 Nginx 提供公网 HTTPS 入口。

### 当前数字人工作流（最新 API 状态）

AI App ID 默认值：`2062251097452007426`（可由管理员为用户配置）。

| 用途 | nodeId | fieldName | 当前行为 |
| --- | --- | --- | --- |
| 最长分辨率 | `503` | `value` | 用户可填正整数，默认 `1024` |
| 单/双人模式 | `753` | `select` | `1` 单人（默认）；`0` 双人 |
| 参考图片 | `240` | `image` | 必填 |
| 总参考音频 | `339` | `audio` | 必填 |
| 截取开始/结束 | `341` | `start_time` / `end_time` | 必填 |
| 左/右人物音频 | `739` / `738` | `audio` | 双人模式必填；单人传 `"None"` |
| 提示词 | `422` | `text` | 必填，可修改默认值 |

重要：新版 API 已取消旧的 `752`「总体模式选择」节点。稳定模式 v2 由 RunningHub 新版工作流默认使用，前端不再显示该选择，后端也不得提交 `752`。

数字人实例类型：

```text
固定 instanceType = default（24G）
usePersonalQueue = false
不传 retainSeconds
```

### 当前本地队列

- Web 创建任务时不再因运行槽位已满而返回 429；任务始终先保存为 `PENDING`。
- Worker 以每个用户的 `max_concurrent_tasks` 为槽位上限，计入 `UPLOADING`、`SUBMITTED`、`RUNNING`。
- Worker 按任务创建时间 FIFO 领取；某用户槽位已满时，其余任务继续保持 `PENDING`，不会阻塞其他用户的可运行任务。
- 任一运行任务进入成功、失败、下载失败、取消或超时等终态后，下一轮自动补充空闲槽位。
- 数字人和视频对口型共享同一用户的并发槽位。

### 当前 LTX 2.3 对口型工作流

私有 Workflow ID 默认值：`2080551073030434817`（可由管理员为用户配置）。

提交端点：`/openapi/v2/run/workflow/{workflowId}`。

| 用途 | nodeId | fieldName | 当前行为 |
| --- | --- | --- | --- |
| 源视频 | `237` | `video` | 必填，上传后传 RunningHub `data.fileName` |
| 自定义音频 | `246` | `audio` | 必填；上传后传 RunningHub `data.fileName` |
| 画面及对白提示词 | `222` | `text` | 必填；作为 `PainterLTX2Vomni` 的 positive 条件，建议与音频台词一致 |
| 最终视频输出 | `260` | - | 查询结果时优先选择该节点的视频 |

工作流内部由音频控制视频长度：`246` 的音频进入 `AudioToFPS(265)`，计算结果同时连接 `237.frame_load_cap` 和 `245.end_frame`；网页/API 不另外传视频时长。

LTX 对口型实例类型：

```text
用户可选 default / plus
页面初始值沿用该用户的 LTX 工作流配置
usePersonalQueue = false
addMetadata = true
不传 retainSeconds
```

### 最近一次验证记录

- 数据库迁移：`0003_workflow_adapters (head)`。
- 本轮未修改数据库模型，不需要新增迁移。
- 测试数量：39 项。
- 最近一次完整 mock 测试：`39 passed in 2.71s`。
- Python 编译检查通过；当前 Windows 环境未提供 Bash，部署脚本的 `bash -n` 语法检查需在 Ubuntu 测试服务器的预检阶段补做。
- 测试不得真实调用 RunningHub，避免扣费。

## 4. 当前代码结构

```text
app/
  routes/                 # 登录、管理员、任务页面和本地 API
  services/
    runninghub.py          # 通用 RunningHub HTTP 客户端
    audio.py               # 音频时长与时间校验
    storage.py             # 本地文件保存与安全路径处理
    workflow_configs.py    # 用户 × 工作流配置解析
  workflows/
    base.py                # 工作流协议、素材和输出对象
    registry.py            # 工作流注册表
    digital_human.py       # 数字人专属节点、校验、输出选择
    ltx_lip_sync.py        # LTX 2.3 对口型节点、素材模式、请求和输出选择
  workers/task_worker.py   # 通用任务领取、上传、提交、轮询、下载
  templates/               # Jinja2 页面
  models.py                # SQLAlchemy 模型
alembic/versions/          # 数据库迁移
scripts/                   # 创建管理员、清理文件等脚本
tests/                     # pytest；RunningHub 均为 mock
data/                      # 本地数据库、上传素材、生成结果（不提交）
deploy/
  nginx/                   # Nginx 反向代理与上传限制模板
  systemd/                 # Web、Worker、备份、清理服务和定时器
  scripts/                 # 生产预检、SQLite 在线备份和恢复
```

### 关键边界

1. `RunningHubClient` 只负责：上传文件、按 AI App/Workflow 端点提交任务、查询任务、下载结果。
2. `task_worker.py` 不应包含任何工作流节点 ID、素材字段假设或输出解析细节。
3. 每个工作流在自己的适配器中负责：输入字段、默认值、节点映射、参数校验、输出选择。
4. 新增工作流时，增加适配器并在 `app/workflows/registry.py` 注册；不要把新节点散落到通用路由或 Worker。

## 5. 数据库与迁移

当前迁移链：

```text
0001_initial
0002_runninghub_submitted_at
0003_workflow_adapters
```

本轮 LTX 工作流沿用 `workflow_configs` 和通用 `input_payload`，未修改模型或迁移链。

主要表：

- `users`：站内账号、管理员标记、启用状态。
- `runninghub_configs`：每个用户的加密 API Key、Base URL、并发上限等账户级配置。
- `workflow_configs`：每个用户、每个工作流的 AI App ID、实例类型、默认提示词、启用状态和扩展设置。
- `generation_tasks`：本地任务、远程 `runninghub_task_id`、状态、通用 `workflow_type`、`input_payload`、本地结果路径。

修改模型后必须新增 Alembic 迁移，并运行：

```powershell
python -m alembic upgrade head
```

不要删除或重建 `data/app.db` 来“解决”迁移问题；其中可能有真实任务和用户配置。

## 6. 本地运行

环境：用户使用 Anaconda `base` 环境；本机已用 `D:\Myanaconda\python.exe` 验证。

```powershell
conda activate base
cd "D:\工作内容\轻盈健\数字人\runninghub_mvp"
python -m pip install -r requirements.txt
python -m alembic upgrade head
python -m scripts.create_admin admin
```

Web：

```powershell
python -m uvicorn app.main:app --reload
```

Worker（另开一个终端）：

```powershell
python -m app.workers.task_worker
```

访问地址：`http://127.0.0.1:8000`

## 7. 操作与安全约束

- 不在代码、日志、测试、`.env.example`、Notebook 或回复中写入真实 API Key。
- 真实 RunningHub 联调会产生费用；除非用户明确要求，不启动 Worker 来处理真实待提交任务，也不做真实 API 调用。
- 用户希望手动控制 Web/Worker 端口和进程。除非明确要求，不要擅自启动、停止或结束其进程。
- 运行测试前，确认测试数据库和 `tests/.runtime` 与实际 `data/app.db` 隔离。
- 保留用户已有的未提交改动；先查看 `git status`，只修改当前需求涉及的文件。
- 新版 API 变更时，必须同时更新：工作流适配器、任务创建路由、前端表单、相关测试和本文件。

## 8. 当前范围外 / 后续可做

部署前的仓库准备已经完成；当前仍未实际购买或连接服务器，也未配置真实域名、
DNS、Nginx 或 HTTPS 证书。实际部署必须先使用测试域名按 `deploy/README.md`
完成验收，不能把部署模板存在视为已经上线。

可继续扩展：

- 图生视频、文生视频、双人数字人等更多适配器及专属页面。
- 通用的管理员“按工作流配置”界面（当前管理员表单主要配置数字人工作流）。
- 批量任务、任务统计、对象存储、Webhook 通知、多 Worker。
- 实际服务器部署、域名解析、HTTPS 签发和异机备份落地。
- 用户自助注册、找回密码、邮件验证、套餐或支付系统；当前账号仍由管理员创建。

## 9. 本次仓库更新范围

2026-07-25 将以下三部分作为同一次版本更新提交到现有仓库：

1. 数字人取消旧总体模式并固定 Stand 24G。
2. 新增视频对口型工作流和按用户并发调度的持久化 FIFO 队列。
3. 新增生产安全加固与 Ubuntu 单服务器部署前准备。

面向使用者的变化摘要统一维护在 `CHANGELOG.md`。工作区后续如出现未提交改动，
应在本节追加用途和文件范围，避免与已提交版本混淆。

## 10. 每次完成需求后的更新模板

每轮需求完成后，请更新本文件中的以下内容：

1. `当前完成状态`：新增或改变了什么行为。
2. `当前数字人工作流`：如 API 节点、默认参数或素材要求变化。
3. `数据库与迁移`：新增迁移编号和是否已经执行。
4. `最近一次验证记录`：测试数量、完整测试结果、必要的手工验证。
5. `当前范围外 / 后续可做`：新增明确待办、已完成项或不再适用项。
6. `工作区提示`：未提交改动的用途；提交后可清空此节。

更新时只记录可公开的技术状态，绝不记录真实 API Key、密码、真实用户素材路径或敏感任务内容。
