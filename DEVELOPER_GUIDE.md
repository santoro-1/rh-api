# RunningHub 视频生成中转站开发者指南

> 最后核对：2026-08-03。本文件是当前架构、开发流程和发布规则的主入口；代码和
> Alembic 迁移始终是最终事实。面向使用者的变化见 `CHANGELOG.md`，生产操作细节见
> `deploy/README.md`，固定电脑节点的安装与分发见 `media_node/README.md`。

## 1. 项目定位与边界

这是一个 FastAPI、Jinja2、SQLite 构成的数字人视频生成中转站。网站保存账号配置、
素材和任务，持久化 Worker 异步调用 MiniMax 与 RunningHub；浏览器不会直接取得或
调用第三方密钥。

当前正式开放两种工作流：

| 工作流键 | 用户名称 | 主要输入 | 当前限制 |
| --- | --- | --- | --- |
| `digital_human` | 数字人视频 | 图片、音频、动作提示词 | 仅开放单人模式，普通 24G 实例 |
| `ltx_lip_sync` | 视频对口型 | 视频、音频、口播脚本 | 默认 Plus 48G，也可选 Stand 24G |

双人数字人的底层映射暂时保留，但前后端均不接受用户创建。生成结果第一版不自动
拼接；长音频会生成多个可见子任务。

系统的职责边界如下：

- Web 进程负责认证、校验、数据库事务、页面和本地任务创建，不等待第三方完成。
- 语音 Worker 负责 MiniMax 异步语音、声音制作和审核后的流转。
- 媒体 Worker 负责编排长音频分析与切割；生产远程模式下只做调度和心跳。
- Windows 媒体节点负责耗时的 FunASR、静音检测以及 FFmpeg 音视频切割。
- 视频 Worker 负责 RunningHub 上传、提交、轮询、下载、取消和自动重试。

## 2. 运行架构

### 2.1 进程与端口

| 组件 | 入口 | 默认监听或轮询 | 说明 |
| --- | --- | --- | --- |
| Web | `python -m scripts.serve_web` | 本地 `127.0.0.1:8000`；生产 `127.0.0.1:18083` | 公网只由 Nginx 提供 HTTPS |
| 语音 Worker | `python -m app.workers.audio_worker` | 数据库队列 | 单独进程，重启后续查远程任务 |
| 媒体 Worker | `python -m app.workers.media_worker` | 数据库队列 | `local` 处理或 `remote` 调度 |
| 视频 Worker | `python -m app.workers.task_worker` | RunningHub 默认每 5 秒查询 | 按用户并发和 FIFO 领取 |
| Windows 媒体节点 | `media_node/启动媒体节点.cmd` | 默认每 10 秒向主站领任务 | 只主动访问主站 HTTPS |
| 节点内 ASR | 由媒体节点启动器管理 | `127.0.0.1:18084` | 只监听本机，不对公网开放 |

`POLL_INTERVAL_SECONDS=5` 只控制 RunningHub 远程任务状态查询，不控制长音频节点。
媒体节点领取间隔由节点配置的 `MEDIA_WORKER_POLL_SECONDS` 控制；默认每 60 秒发送
心跳并续租。远程媒体任务租约默认 1800 秒，节点掉线后任务可重新回队。

### 2.2 本地与生产差异

- 本地默认 `MEDIA_PROCESSING_MODE=local`，媒体 Worker 可直接使用本机 ASR/FFmpeg。
- 生产默认 `MEDIA_PROCESSING_MODE=remote`。Ubuntu 服务器不安装 FunASR；授权的
  Windows 节点通过 Bearer Token 领取素材、续租并回传分段 ZIP。
- 节点不需要公网 IP、同一局域网、端口映射或入站防火墙规则，只要能访问主站 HTTPS。
- 多节点没有固定主从，也不按性能分配。服务器原子领取最早待处理任务，哪台空闲节点
  先领取成功就由哪台处理；每个节点内部保持单任务串行。

## 3. 核心业务流程

### 3.1 直接创建视频任务

1. Web 校验用户、工作流配置、参数和素材，把任务保存为 `PENDING`。
2. 视频 Worker 按用户 `max_concurrent_tasks` 和创建时间 FIFO 领取任务。
3. Worker 上传素材，保存 RunningHub 返回的远程文件名，再提交工作流。
4. 保存 `runninghub_task_id` 后只查询该任务，不因进程重启而重复提交。
5. 成功后选择工作流输出并下载到 `data/outputs`；失败时保存结构化原因和尝试历史。

数字人和视频对口型共享同一个用户并发额度。槽位计入 `UPLOADING`、`SUBMITTED`、
`RUNNING`；槽位满时新任务仍留在本地 `PENDING` 队列，不向用户返回并发错误。

### 3.2 批量上传和长音频自动分流

快速创建与 Excel/CSV 都先形成批次清单。素材通过暂存 ID 与清单行绑定，同一张图片
或同一段视频可以被多行复用，不依赖文件名唯一性。

- 音频不超过 45 秒：直接创建一个标准视频任务。
- 音频超过 45 秒：自动建立长音频项目，目标分段约 30 秒、硬上限 45 秒。
- 开启人工确认：分析后停在试听页，可调整切点再确认。
- 未开启人工确认：分析、切割、创建子任务自动连续执行。
- 文本语音且只有一个视频子任务：后处理清单标记为自动包装，并携带 MiniMax 原始字幕
  cues；工作台可继续添加 BGM 和剪映字幕轨道。
- 上传音频或多个视频子任务：旧任务收件箱仍标记为人工处理。多段成功后按
  `segment_index` 生成快速拼接结果；新版 `/app/new` 4A 画面模块把标准化拼接结果保存为
  基础视频，同时保留全部原始片段。上传粗剪成片切换当前版本留待模块 5。

视频对口型要求原脚本。节点用 FunASR 获得字词时间戳，将识别 token 对齐到原脚本，
再结合 VAD/静音边界寻找自然停顿；识别文本只用于定位，不替换用户原脚本。每个子任务
因此能得到正确的脚本片段，并同步切出相同时间范围的音频和源视频。

数字人不需要知道台词对应关系，所以不调用 ASR。它只依据自然停顿切音频，并为所有
子任务复用原图片和动作提示词。

### 3.3 MiniMax 完整流程

“完整流程”每个清单行只提交一次完整脚本，避免分段生成造成音色、语速和情绪不一致。
MiniMax 异步结果的远程任务 ID 会持久化，Worker 重启后继续查询。若结果包含可靠的句级
时间轴，系统按真实时间范围规划分段；后续仍汇入统一的分段、视频任务和 FIFO 队列。

语音审核默认关闭。开启后完整音频进入 `AWAITING_REVIEW`，用户可试听、通过或重新
生成；重新生成会再次调用 MiniMax，并可能产生费用。

### 3.4 新版工作台声音边界

新版浏览器不直接访问数字人后端。工作台服务端使用短期账号令牌调用
`/api/workbench/voices*`、`/api/workbench/voice-creations*` 和
`/api/workbench/audio-batches*`：

- 声音列表只暴露三个产品确认且当前 MiniMax 账号实际可用的官方音色，以及该账号已
  保存的克隆/融合音色。
- 克隆、融合、试听和保存复用 `voice_studio.py` 与语音 Worker，不建立平行任务队列。
- 音频批次使用工作台专用的无图片校验计划和既有 `create_batch`、
  `AudioGenerationTask`；只接收脚本、音色和语音参数，并强制 `reviewRequired=True`。
  生成完成后停在 `AWAITING_REVIEW`，不得自动创建视频任务。
- 工作台可下载完整 MP3 和 MiniMax 原始字幕 cues；重新生成必须再次确认费用，只重置
  指定行并保留历史 attempt。
- 工作台专用路由只负责编排与序列化，账号、声音资产、批次和批次行都按令牌用户过滤。
- `GenerationBatch.source_channel` 是新旧入口的唯一判据。旧网页及迁移前历史数据固定为
  `legacy_web`，工作台声音批次固定为 `new_workbench`；禁止根据批次名或 `request_key`
  推断来源。旧网页 `/batches` 只列出 `legacy_web`，工作台声音批次接口只接受
  `new_workbench`；幂等键也不得跨来源复用。

### 3.5 新版工作台画面 4A 边界

- `POST /api/workbench/audio-batches/{batch_id}/items/{item_id}/composition` 在显式费用确认
  后接收工作台当时的当前图片，把图片绑定到已生成音频，再通过既有声音审核门，并复用
  原有媒体、RunningHub 视频和拼接 Worker。声音阶段不会提前上传或绑定图片。
- 工作台通过任务清单中的 `composition` 字段读取排队、数字人生成、拼接、完成或失败
  状态；浏览器仍不直接访问本服务。
- 每个 RunningHub 成功结果均可作为不可变原始分段下载。无论一个还是多个分段，都以
  已确认音频时长为目标进行视频/音频归一化；明显短于音频超过 1 秒时失败，不静默补长。
- 多分段按原顺序拼接，单分段也生成标准化基础视频。基础视频不是字幕/BGM 成片；4A
  不调用剪映、不生成变体。
- 重试接口只重置失败的 RunningHub/下载任务或失败的拼接，不重做已经成功的付费子任务。
- 单片段标准化、逐段按音频时长补帧/裁切只适用于 `new_workbench`。`legacy_web`
  单片段继续绕过拼接，旧多片段继续使用不带目标时长的原快速拼接；不得把工作台算法
  再次扩散为全局默认。
- 音频成功到 4A 启动前，图片仍由工作台管理和替换；composition 请求携带当时的当前
  图片。启动后图片锁定，避免已提交任务被本地状态静默改写。
- 4B 字幕、BGM 和最终 `composition_video` 完全由工作台现有本地剪映队列负责。本项目
  不增加 4B 接口或队列；工作台 4B 失败重试不得重新放行本项目已成功的 RunningHub 任务。

### 3.6 取消与删除

- 尚未提交 RunningHub 的任务可以在本地直接取消。
- 已提交任务会先调用 RunningHub 官方取消接口，远端确认后才写入本地取消状态。
- 活动任务禁止删除；终态任务可删除并清理关联素材和结果，删除不可恢复。
- 批次只有全部现存任务终态，或确认属于没有活动 Worker 的本地卡住记录时才可删除。

## 4. 状态机与重试语义

数据库枚举定义在 `app/models.py`，业务代码不得另造含义相同的字符串状态。

### 4.1 视频任务

```text
PENDING -> UPLOADING -> SUBMITTED -> RUNNING -> SUCCESS
                   \-> FAILED
                                      \-> DOWNLOAD_FAILED
任一允许取消的活动状态 ----------------> CANCELLED
```

RunningHub 明确返回 `FAILED` 时，默认最多自动重试 3 次，基础等待 60 秒，实际等待为
60、120、240 秒；分别由 `RUNNINGHUB_AUTO_RETRY_LIMIT` 和
`RUNNINGHUB_AUTO_RETRY_BASE_DELAY_SECONDS` 调整。每次尝试保留远程任务 ID、错误码
和 `failedReason`。人工重试会开启新一轮自动恢复，可能再次产生费用。

上传阶段且确认没有创建远程任务的网络失败，可以安全退避重试。提交请求返回不明确时
禁止盲目重提，因为远端可能已经创建并计费。`DOWNLOAD_FAILED` 且已有远程任务 ID 时
只恢复查询和下载，不重新生成。

### 4.2 语音任务

主要状态为：

```text
PENDING -> CLONING/SYNTHESIZING -> REMOTE_PENDING
        -> AWAITING_REVIEW -> ALIGNING -> SEGMENTING -> HANDOFF -> SUCCESS
任一失败分支 -----------------------------------------------------> FAILED
```

并非每条路径都会经过全部状态。例如直接上传音频不会调用 MiniMax，数字人长音频也
不会经过 ASR 对齐。

### 4.3 长音频项目

```text
PENDING_ANALYSIS -> ANALYZING -> REVIEW -> PENDING_CUT -> CUTTING -> COMPLETED
                                \-----------------------> CANCELLED
任一处理阶段 --------------------------------------------> FAILED
```

未开启人工确认时可跳过用户可见的 `REVIEW` 停留，但仍使用同一数据结构和切割校验。

## 5. 代码结构与模块职责

```text
app/
  main.py                 FastAPI 装配、中间件、路由和健康检查
  config.py               环境变量读取与生产安全校验
  models.py               SQLAlchemy 模型与状态枚举
  routes/                 HTTP、权限、表单和响应组装
  services/               可复用业务规则、第三方客户端、存储与状态服务
    alignment/            ASR/脚本时间轴对齐接口及实现
    speech/               MiniMax、声音资产和异步结果处理
  workers/                语音、媒体、视频三个持久化 Worker
  workflows/              RunningHub 工作流适配器与注册表
  templates/ + static/    Jinja2 页面、样式和前端交互
alembic/versions/         只增不改的数据库迁移链
deploy/                   Nginx、systemd、安全发布、备份和恢复
media_node/               Windows 节点、ASR、安装与便携包构建
scripts/                  本地启动、管理员创建、清理和诊断脚本
tests/                    全 mock 自动化测试
data/                     数据库、上传、输出、日志和运行时数据，不入库
```

模块边界的细化规则见 `MAINTENANCE.md`。核心原则是：路由不实现 Worker 状态机，
适配器不直接读表单，Worker 不绕过服务层重复实现校验，前端显示状态不能成为业务
事实来源。

## 6. 本地开发

### 6.1 前置条件与初始化

- Windows + PowerShell；Python 3.11+，目标版本为 3.12。
- FFmpeg 和 ffprobe 可从 `PATH` 调用。
- 项目当前使用 SQLite，不需要独立数据库服务。

```powershell
conda activate base
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m alembic upgrade head
python -m scripts.create_admin admin
```

本地 `.env` 至少换掉 `APP_SECRET_KEY`。生产必须显式配置稳定的 Fernet
`APP_ENCRYPTION_KEY`，不可依赖开发环境派生值。

### 6.2 启动和停止

日常本地使用可以双击 `启动系统.cmd`，它会迁移数据库并启动 Web、语音、媒体和视频
Worker；双击 `停止系统.cmd` 统一停止。开发调试时建议分终端运行：

```powershell
python -m scripts.serve_web
python -m app.workers.audio_worker
python -m app.workers.media_worker
python -m app.workers.task_worker
```

修改某个 Worker 后必须重启对应进程。Web 不会替独立 Worker 热重载代码。

### 6.3 配置分类

完整字段、默认值和注释放在 `.env.example` 与 `.env.production.example`，不要在文档中
复制第二套可能漂移的配置表。开发时重点关注：

- 应用与安全：`APP_ENV`、`APP_SECRET_KEY`、`APP_ENCRYPTION_KEY`、
  `COOKIE_SECURE`、`ALLOWED_HOSTS`。
- 存储与限制：`DATABASE_URL`、`DATA_DIR`、各素材大小和保留时间。
- RunningHub：基础地址、轮询、超时以及自动重试次数与等待。
- 长音频：`LONG_AUDIO_ALIGNMENT_PROVIDER`、`ASR_BASE_URL`、ASR 超时。
- 媒体节点：`MEDIA_PROCESSING_MODE`、`MEDIA_WORKER_TOKEN`、租约和归档上限。
- 豆包内容分析：`ARK_MAX_CONCURRENCY` 默认 10、`ARK_QUEUE_WAIT_TIMEOUT_SECONDS`
  默认 300 秒、`CONTENT_ANALYSIS_MAX_SCRIPT_CHARS` 默认 50000。

用户级 RunningHub、MiniMax、豆包 Ark 和工作流 ID 通过管理员页面维护，不写入仓库环境模板。

## 7. 数据库与迁移

当前迁移头为 `0022_content_analysis_cache`。SQLite 启用 WAL、外键和 busy timeout，设计目标
是一个 Web 加三个本地 Worker 的单服务器部署，不是多主集群。

修改模型时：

1. 新增下一个 Alembic 迁移，不修改已经发布的旧迁移。
2. 同时验证空库升级和带既有父子数据的升级。
3. 避免 SQLite 批量重建父表触发意外级联删除；能用原生 `ADD COLUMN` 时优先使用。
4. 在本地运行 `python -m alembic upgrade head` 和 `python -m alembic current`。
5. 生产发布前备份数据库，发布脚本执行迁移后再重启本项目服务。

主要领域表在根 `README.md` 的“数据库表”章节有简表，字段和关联以 `app/models.py`
为准。

## 8. 自动化测试与质量门槛

```powershell
python -m pytest -q
python -m alembic current
python -m compileall -q app scripts media_node
node --check app/static/operations.js
git diff --check
```

页面改动还要手工检查桌面、窄屏、长文本、表格横向滚动和局部状态刷新。媒体节点改动
应同时检查源码安装和便携包入口；发布包构建器会禁用用户级 Python 包，防止本机全局
依赖掩盖缺包。

自动化测试必须 mock RunningHub 和 MiniMax，不能产生真实费用。真实联调需要用户明确
知晓计费，并使用临时环境变量或管理员页面配置，绝不把凭证写入测试或提交记录。

## 9. Windows 媒体节点开发与分发

源码安装、端口冲突和迁移步骤见 `media_node/README.md`。面向固定电脑优先分发独立包：

```powershell
# 首次安装或运行环境不兼容时生成完整包
powershell -ExecutionPolicy Bypass -File .\media_node\build-portable-media-node.ps1

# 只有代码变化且运行环境兼容时生成小更新包
powershell -ExecutionPolicy Bypass -File .\media_node\build-portable-media-node.ps1 -UpdateOnly
```

完整包 `dist/rh-media-full-*.zip` 包含 Python、依赖、FFmpeg、程序和默认模型缓存，通常
约 1.3 GB。ZIP 内为扁平结构，建议解压到 `F:\RHMedia` 之类的短路径。目标电脑先运行
“配置媒体节点.cmd”，再运行“启动媒体节点.cmd”。

小更新包 `dist/rh-media-update-*.zip` 通常只有几 MB。关闭节点后，把 ZIP 放到已解压
节点根目录，双击“更新媒体节点.cmd”。更新器会检查运行环境版本、备份旧代码，并保留
`.env`、Token、模型、Python、FFmpeg、日志和工作数据。依赖或模型兼容版本变化时必须
重新分发完整包。

默认禁止把真实 `.env` 和 Token 放进包。只有通过可信渠道交付给明确电脑时才使用
`-IncludeLocalConfig`；构建器仍会清空 Worker ID，避免新电脑与旧节点重名。

## 10. 日志、监控与排障

运行状态页展示服务心跳、CPU、内存、磁盘、FFmpeg 数量、队列数量和事件日志。日志写入
`data/logs/`，默认保留 7 天、单文件 10 MB 后轮转，并可由管理员下载最近日志包。

关键事件使用稳定的 `log_event()` 事件码。第三方网络异常应记录脱敏后的异常类型、
底层原因、目标域名、耗时和 HTTP 状态；上传异常还应记录素材槽位和大小，但不得记录
API Key、访问密码、完整脚本或用户敏感内容。

常见排查顺序：

1. 确认 Web、语音、媒体、视频 Worker 是否都在运行且代码版本一致。
2. 通过任务 ID 查事件链：创建、领取、上传、提交、远程状态、下载或重试。
3. 区分本地队列等待、媒体节点等待、RunningHub 排队和远程执行失败。
4. 远程媒体任务检查节点心跳、Worker ID、租约、Token 和回传归档校验。
5. 第三方失败优先查看保存的 HTTP 信息与 `failedReason`，不要只看页面短错误。
6. 数据问题先备份 SQLite，再做只读查询；不要直接手改状态掩盖原因。

## 11. 生产发布与回滚

生产目录为 `/opt/runninghub-video`，Nginx 只代理本机 `127.0.0.1:18083`。systemd 管理
Web、语音 Worker、媒体 Worker、视频 Worker 以及备份/清理定时器。服务器不运行 ASR。

Windows 开发机的标准安全发布流程：

```powershell
git switch main
git fetch origin
git pull --ff-only origin main
git status
git log -1 --oneline

# 只做本地测试和服务器只读预检
powershell -ExecutionPolicy Bypass -File .\deploy\deploy-update.ps1

# 预检通过后，显式部署并按提示确认 commit
powershell -ExecutionPolicy Bypass -File .\deploy\deploy-update.ps1 -Deploy
```

脚本创建项目独立备份、上传指定 commit、执行迁移并只重启本项目服务；不会安装服务器
ASR，也不应修改其他项目端口、Nginx 或证书。完整预检、验收与恢复命令只在
`deploy/README.md` 维护，发布前必须按该文档执行。

## 12. 扩展工作流或服务商

新增 RunningHub 工作流时：

1. 在 `app/workflows/` 新建适配器，实现参数校验、持久化输入、上传槽位、提交 payload
   和输出选择。
2. 在 `app/workflows/registry.py` 注册稳定工作流键。
3. 增加用户级工作流配置、创建页面选项和批量规则；不要在视频 Worker 写工作流分支。
4. 为节点映射、参数校验、输出选择、重试和权限补测试。
5. 如模型或数据库结构变化，新增迁移并更新环境模板与发布说明。

更换 MiniMax、ASR 或远程媒体实现时优先扩展既有接口：语音能力位于
`app/services/speech/`，对齐能力位于 `app/services/alignment/`，不要把具体服务商
协议泄漏到批量路由和页面。更详细的工作流适配器清单见 `WORKFLOW_EXTENSION.md`。

## 13. 安全与维护规则

- 密码使用 PBKDF2-SHA256；第三方密钥和工作流访问密码使用 Fernet 加密。
- 生产环境必须使用稳定随机的应用密钥、HTTPS Cookie 和明确的可信域名。
- 媒体节点只获得专用 Bearer Token，不获得网站密码、RunningHub Key 或 MiniMax Key。
- 文件读取必须通过安全相对路径解析，所有下载、预览和任务 API 都做用户归属校验。
- 不提交 `.env`、SQLite、用户素材、生成结果、日志、模型缓存或便携发行包。
- 不改写已发布迁移，不在生产服务器直接编辑代码，不用聊天记录代替仓库文档。

文档维护约定：架构、流程、配置或发布方式变化时更新本指南；用户可见变化更新
`CHANGELOG.md`；专项操作同步更新 `deploy/README.md` 或 `media_node/README.md`；
`PROJECT_STATUS.md` 只保留阶段快照和历史交接，不再承担完整开发文档职责。

## 14. 新版工作台精确时间轴边界（2026-08-05）

- `/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition` 在批准 4A 时给内部
  视频参数写入 `timing_mode=exact_timestamps`。
- 数字人工作流仍使用整秒时间码，因此小数音频时长使用 `ceil`，例如 `24.4 -> 25`；该
  模式下 `generation_tail_padding_seconds()` 返回 `0`，上传文件和 RunningHub `end_time`
  都不追加静音尾垫。
- 没有这个内部标记的旧批次、上传音频和历史入口仍执行原有、最多 `0.5` 秒且不跨越
  `45` 秒的安全尾垫。不要把本规则改成全局删除尾垫。

## 15. 新旧批次隔离边界（2026-08-05）

- 迁移 `0020_batch_source_channel` 为历史行补入 `legacy_web`；所有新工作台声音批次必须
  在校验计划阶段显式写入 `new_workbench`。
- 原网页历史列表只查询 `legacy_web`，工作台任务收件箱仍可按账号读取兼容的历史数字人
  任务；这两个“可见性”用途不同，不要合并查询条件。
- 音频交接后，`legacy_web` 单片段写入 `NOT_APPLICABLE`，旧多片段写入
  `MERGE_PENDING` 并以 `target_durations=None` 进入原快速拼接。`new_workbench` 无论单段
  或多段均进入按确认音频时长标准化的基础视频分支。
- 修改来源、交接或拼接代码时，必须同时回归：历史迁移默认值、旧网页列表隔离、旧单段
  不拼接、旧多段不传目标时长、新工作台单段仍标准化。

## 16. 内容分析契约 v1（2026-08-06）

模块 0～8 之后新增的音乐与字幕语义优化使用独立、版本化契约。智能内容分析模块 1～8 已
完成服务端契约、Ark 配置、业务接口、缓存、工作台消费、时间轴映射和本地音乐 Top1：

- 权威代码位于 `app/services/content_analysis/`，契约版本为
  `jyd.content-analysis.v1`。
- 一次未来的模型响应只包含 `music_intent` 和 `subtitle_units`；前景图片关键词延期到
  后续契约版本，v1 禁止 `visual_cues`。
- `subtitle_units` 使用 Python Unicode code point 的左闭右开字符位置，必须首尾相接、
  完整覆盖原始脚本，且每段满足 `original_script[start:end] == text`。
- 模型不得返回字幕时间戳或本地音乐文件身份。MiniMax 时间轴映射、真实字体测宽和本地
  Top1 音乐选择仍由工作台负责。
- `music-matcher.v1` 的评分维度和硬过滤条件已经在 `taxonomy.py` 冻结，后续 Excel
  运行时清单和匹配器必须复用，不得另建第二套权重。
- 契约测试位于 `tests/test_content_analysis_contract.py`，第三方接入前必须继续覆盖漏字、
  改字、重复、乱序、越界、绑定冲突和额外字段。

跨项目实施状态、每个模块的实际结果和下一步入口记录在工作区
`智能内容分析开发文档.md`。当前未调用付费服务，未部署生产环境，也未修改云端账号数据库。

## 17. 豆包 Ark 配置与客户端（2026-08-06）

- 迁移 `0021_ark_configs` 新增用户一对一豆包配置；API Key 使用现有 Fernet 机制加密，
  管理员页面只写入或轮换，不回显明文，浏览器和工作台均不获得 Key。
- 每个用户独立保存启用状态、Base URL、模型、1～120 秒超时和 0～5 次额外重试。
  启用时必须已有可解密 Key 且模型非空；停用时允许先保存不完整配置。
- 独立客户端位于 `app/services/content_analysis/ark.py`，只封装 Ark OpenAI 兼容的
  `/chat/completions` HTTP 传输，对超时、429 和明确 5xx 做有限重试，并支持注入 mock
  session 与 sleep。模块 3 不发起内容分析业务调用。
- `ArkAPIError` 只暴露稳定错误码、HTTP 状态、重试属性、请求 ID 和尝试次数，不把请求
  消息、完整响应或凭证写入异常文本和普通日志。
- 模块 4 已增加 Prompt、结构化响应的音乐/字幕分支独立校验、缓存和数字人服务端工作台
  接口；工作台本地项目消费、语义字幕和音乐 Top1 已在模块 5～7 完成。自动化测试不得调用
  真实豆包服务。

## 18. 统一内容分析接口与缓存（2026-08-06）

- `POST /api/workbench/content-analysis` 使用现有工作台短期令牌，只接收精确原始脚本和
  可选 `force_refresh`；浏览器和工作台不会获得 Ark Key。
- `app/services/content_analysis/analysis.py` 负责固定 Prompt、Ark JSON Schema、响应提取、
  音乐与字幕分支独立校验、确定性字符索引修复、成功分支保护和脱敏状态日志。
- 迁移 `0022_content_analysis_cache` 按用户、脚本 SHA-256、模型、契约版本和 Prompt
  版本缓存结果。缓存不保存豆包原始完整响应；完整失败不形成粘性缓存，部分成功和完整
  成功可复用，强制刷新失败不得覆盖此前合法分支。
- 单 Web 进程使用统一有界信号量，默认最多同时发出 10 个 Ark 请求；其余请求等待，默认
  最长 300 秒。当前生产结构只有一个 Web 进程；若将来增加 Web 进程或多台主机，必须先
  把进程内信号量替换为数据库或 Redis 共享租约，不能简单把进程数相乘。
- 429、超时、连接错误和明确 5xx 继续使用模块 3 的有限退避。分析失败不调用 MiniMax、
  RunningHub 或剪映，不改变基础视频状态。

## 19. 工作台内容分析消费边界（2026-08-06）

- 工作台智能内容分析模块 5 已按 `ProjectItem` 消费 `/api/workbench/content-analysis`；一次
  HTTP/Ark 请求仍只包含一条精确脚本，不支持把多行拼成一个模型输入。
- 工作台单批最多并发 10 行，并保存逐行、逐分支快照。服务端缓存仍按用户和脚本哈希隔离，
  不新增工作台项目 ID 维度；同账号相同脚本可以共享服务端缓存，但工作台状态各行独立。
- Excel/CSV 导入和脚本编辑不自动分析；点击“生成声音预览”时才分析本批声音目标中首次
  导入或文本变化的行。工作台脚本变化只使该行声音和分析快照失效，其余行不会重新请求。
  文本未变而重新生成声音不会重做分析；显式单行分析重试才传 `force_refresh=true`。
- 模块 5 不映射 MiniMax 时间轴、不执行本地音乐 Top1，也不改变数字人后端现有任务状态。

## 20. 工作台语义字幕映射边界（2026-08-06）

- 工作台智能内容分析模块 6 已消费本服务返回的 `subtitle_units`，但映射完全发生在工作台
  本地 4B 阶段；数字人服务端和大模型仍不得返回字幕时间戳。
- 工作台使用 MiniMax `raw_cues` 的真实时间范围作为唯一锚点，并核对当前脚本、分析脚本
  摘要、当前音频脚本摘要及 raw cues 音频绑定。任一不一致就回退原有排版。
- 空格和换行可不出现在 MiniMax cue 文本中，其他字符必须精确一致；`~` 不是通配符。
- 模块 6 不改变本项目缓存、Ark/MiniMax 独立失败重试、RunningHub 状态或 BGM 选择。

## 21. 智能内容分析跨项目验收（2026-08-06）

- 模块 8 新增 `tests/test_content_analysis_workbench_integration.py`，把本项目
  `analyze_content` 的真实序列化结果直接传入工作台的响应复核、语义字幕映射和本地音乐
  匹配器，防止两边独立 mock 都通过但实际响应结构不兼容。
- 跨项目测试覆盖双分支成功、音乐成功/字幕失败、字幕成功/音乐失败、安全字符索引重算，
  以及空格、换行和 `~` 的精确保留；同时确认 Top1 响应不包含候选列表或 Top3。
- 模块 8 新增验收 `3 passed`；内容分析定向回归 `48 passed`；本项目完整 mock 回归
  `216 passed`，工作台完整 mock 回归 `260 passed`，0 failure、0 error。
- 验收未发现运行时代码缺陷。测试没有发出真实豆包、MiniMax、RunningHub 或剪映请求，
  没有部署生产环境，也没有修改云端账号数据库。真实服务质量与生产发布仍需独立授权。
