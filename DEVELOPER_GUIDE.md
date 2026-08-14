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
| `digital_human` | 数字人视频 | 图片、音频、动作提示词 | 仅开放单人模式，固定 Plus 48G 实例 |
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
5. LTX 成功后直接选择输出并下载到 `data/outputs`。数字人成功后先保存不可覆盖的源片段，
   创建一对一 `GenerationTaskEnhancement`，再上传到固定 48G 的 SeedVR2 AI App。
6. SeedVR2 远端 ID 一经保存只查询该 ID；清晰视频下载完成后才把父任务置为 `SUCCESS`。
   失败时数字人与清晰化分别保存结构化原因和尝试历史。

数字人和视频对口型共享同一个用户并发额度。槽位计入 `UPLOADING`、`SUBMITTED`、
`RUNNING`；槽位满时新任务仍留在本地 `PENDING` 队列，不向用户返回并发错误。
`PENDING` 没有本地年龄上限。远程任务的正常排队、执行和一小时运行边界由 RunningHub
负责，本站不再从提交时间执行一小时终止判断。本站只保留默认 14400 秒（4 小时）的
异常滞留看门狗：每轮先查询远程状态；仍为 `QUEUED`/`RUNNING` 且超过看门狗时调用
RunningHub 取消。取消成功后写入 `REMOTE_WATCHDOG_TIMEOUT` 终态；取消失败则保持活动
状态继续查询并重试取消，绝不释放槽位或重新提交。配置项为
`RUNNINGHUB_REMOTE_WATCHDOG_SECONDS`，旧的 `RUNNINGHUB_TASK_TIMEOUT_SECONDS` 不再读取。
数字人子任务在两阶段间持续占用同一逻辑槽位，因此并发 5 的 6 段任务会先让前 5 段各自
完成“数字人 -> SeedVR2”，释放任一槽位后再领取第 6 段。两个阶段分别使用自己的提交时间
执行看门狗。

### 3.2 批量上传和长音频自动分流

快速创建与 Excel/CSV 都先形成批次清单。素材通过暂存 ID 与清单行绑定，同一张图片
或同一段视频可以被多行复用，不依赖文件名唯一性。

- 数字人音频不超过 35 秒、LTX 音频不超过 45 秒：直接创建一个标准视频任务。
- 超过对应工作流上限：自动建立长音频项目。两者目标分段均约 30 秒；数字人硬上限
  35 秒，LTX 硬上限 45 秒。数字人的较短上限用于降低后续 SeedVR2 的超时风险。
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

数字人运行实例由管理员按用户配置为 `default`（24G）或 `plus`（48G），创建任务时写入
适配器输入快照；之后修改用户配置不会迁移已排队或已提交任务。旧网页单条、批量和长音频
入口另有默认关闭、可按次开启的 SeedVR2 开关，同样冻结到每个 `GenerationTask`。关闭时数字人
结果下载成功即结束，不建立清晰化记录。新版工作台 4A 始终开启 SeedVR2，
SeedVR2 自身仍固定 `plus`（48G）。

### 3.3 MiniMax 完整流程

“完整流程”每个清单行只提交一次完整脚本，避免分段生成造成音色、语速和情绪不一致。
MiniMax 异步结果的远程任务 ID 会持久化，Worker 重启后继续查询。若结果包含可靠的句级
时间轴，系统按真实时间范围规划分段；数字人按约 30 秒、最长 35 秒切分，LTX 仍按约
30 秒、最长 45 秒切分，后续汇入统一的视频任务和 FIFO 队列。

语音审核默认关闭。开启后完整音频进入 `AWAITING_REVIEW`，用户可试听、通过或重新
生成；重新生成会再次调用 MiniMax，并可能产生费用。

### 3.4 新版工作台声音边界

新版浏览器不直接访问数字人后端。工作台服务端使用短期账号令牌调用
`/api/workbench/voices*`、`/api/workbench/voice-creations*` 和
`/api/workbench/audio-batches*`：

- 声音列表只暴露三个产品确认且当前 MiniMax 账号实际可用的官方音色，以及该账号已
  保存的克隆/融合音色。
- 不同网站用户配置完全相同的 MiniMax API Key 时，`credential_fingerprint` 作为共享范围。
  `accounts.py` 为每个用户和 `MiniMaxConfig` 物化独立 `MiniMaxVoiceAsset`，但这些副本使用
  相同 provider `voice_id`。保存、激活和历史迁移会同步副本；删除只设置当前用户副本的
  `is_saved=false`，不能删除或隐藏其他用户副本。官方系统音色仍按用户独立同步。
- 克隆、融合、试听和保存复用 `voice_studio.py` 与语音 Worker，不建立平行任务队列。
- 音频批次使用工作台专用的无图片校验计划和既有 `create_batch`、
  `AudioGenerationTask`；只接收脚本、音色和语音参数，并强制 `reviewRequired=True`。
  生成完成后停在 `AWAITING_REVIEW`，不得自动创建视频任务。
- 工作台可下载完整 MP3 和 MiniMax 原始字幕 cues；重新生成必须再次确认费用，只重置
  指定行并保留历史 attempt。重试请求可携带 `speed`（`0.5–2.0`），路由在重置任务前
  更新 `AudioGenerationTask.speed`，确保下一版实际使用工作台当前语速，而非旧版本参数。
- 工作台专用路由只负责编排与序列化，账号、声音资产、批次和批次行都按令牌用户过滤。
- `GenerationBatch.source_channel` 是新旧入口的唯一判据。旧网页及迁移前历史数据固定为
  `legacy_web`，工作台声音批次固定为 `new_workbench`；禁止根据批次名或 `request_key`
  推断来源。旧网页 `/batches` 只列出 `legacy_web`，工作台声音批次接口只接受
  `new_workbench`；幂等键也不得跨来源复用。

### 3.5 新版工作台画面 4A 边界

- `POST /api/workbench/audio-batches/{batch_id}/items/{item_id}/composition` 在显式费用确认
  后接收工作台当时的当前图片，把图片绑定到已生成音频，再通过既有声音审核门，并复用
  原有媒体、RunningHub 视频和拼接 Worker。声音阶段不会提前上传或绑定图片。
- 工作台通过任务清单中的 `composition` 字段读取排队、数字人生成、`VIDEO_ENHANCING`、
  拼接、完成或失败状态；浏览器仍不直接访问本服务。
- 每个数字人源片段在云端保留；主分段下载返回 SeedVR2 清晰片段，独立 `/source` 地址
  返回数字人源片段。无论一个还是多个分段，都以
  已确认音频时长为目标进行视频/音频归一化；明显短于音频超过 1 秒时失败，不静默补长。
- 多分段按原顺序拼接，接缝使用 0.25 秒画面叠化。上一段先补同长度尾帧再与下一段
  开头重叠，音频仍按目标时长顺序拼接，因此基础视频总时长和字幕绝对时间轴不缩短。
  单分段仍只生成标准化基础视频。基础视频不是字幕/BGM 成片；4A 不调用剪映、不生成变体。
- 重试接口只重置失败的 RunningHub/下载任务或失败的拼接，不重做已经成功的付费子任务。
  清晰化失败只重做 SeedVR2，绝不重新执行已成功的数字人阶段。
- 迁移前已成功且尚无增强记录的数字人分段，不会后台自动计费补跑。工作台在用户明确确认
  清晰化费用后可调用 `POST /api/workbench/tasks/{item_id}/enhancement/backfill`；服务端把
  原 `result_path` 原子迁入 `GenerationTaskEnhancement.source_result_path`，保留数字人远端
  ID，只把父任务恢复为 `RUNNING` 等待 SeedVR2 48G。整行先校验后变更，重复调用不重复提交。
- 单片段标准化、逐段按音频时长补帧/裁切只适用于 `new_workbench`。`legacy_web`
  单片段继续绕过拼接，旧多片段继续使用不带目标时长的原快速拼接；不得把工作台算法
  再次扩散为全局默认。
- 音频成功到 4A 启动前，图片仍由工作台管理和替换；composition 请求携带当时的当前
  图片。启动后图片锁定，避免已提交任务被本地状态静默改写。
- 4B 字幕、BGM 和最终 `composition_video` 完全由工作台现有本地剪映队列负责。本项目
  不增加 4B 接口或队列；工作台 4B 失败重试不得重新放行本项目已成功的 RunningHub 任务。

### 3.5.1 新版工作台管理员 RunningHub 资源池

- `/admin/runninghub-pool` 的数据库持久化开关决定新 4A 操作的执行模式；
  `RUNNINGHUB_DUAL_POOL_ENABLED` 仅在网页尚未首次保存时提供初始默认值。关闭时冻结为
  `same_account_v1`：受控测试用户仍使用现有一控多执行账号池，每个分段的数字人与 SeedVR2
  严格绑定同一执行账号；无授权普通用户才使用自己的单账号。开启后还必须同时满足用户 ID授权、`source_channel=new_workbench` 和
  `digital_human` 才冻结为 `dual_pool_v1`。普通用户、旧网页、历史 `legacy_web` 和 LTX 不进入。
- 正式授权用户仍为管理员；迁移只把当时存在的 `Cx_ceshi` 用户 ID写入受控非管理员测试授权。
  运行时不得用用户名判断权限。模式一经绑定，后续开关/授权变化不得迁移已有批次。
- `GenerationTask.user_id` 只表示业务所有者；`execution_account_id` 与逐次
  `GenerationTaskAttempt` 表示真实付费执行账号。任务、文件、结果和权限查询始终按所有者，
  RunningHub 客户端、容量、查询、取消、下载和恢复始终按尝试账号。
- 工作台清单的 `composition.execution_assignments` 按分段返回实际账号的安全摘要；一控多的
  SeedVR2 摘要复用数字人执行账号，双池读取增强阶段独立账号。响应只含内部 ID、备注名称和
  阶段状态，工作台将其持久化到逐行 `COMPOSITION_GENERATE.result` 以支持刷新和重启恢复。
- `dual_pool_v1` 使用现有数字人执行池和独立 `seedvr2_execution_accounts`。两池各按真实凭据
  指纹独立计算最多 5 个槽位，相同 Key 在旧配置、数字人池或 SeedVR2 池中不得重复形成容量。
  数字人源片段持久化后释放数字人槽位；SeedVR2 再从冻结的第二组 ID中独立原子预留。
- 数字人与 SeedVR2 各有逐次尝试账号。SeedVR2 普通失败保留原绑定和源片段重试；只有明确
  401/403、无权限、Key/App/工作流不存在等账号不可用证据时，才释放当前绑定，让下一次付费
  尝试在本次 SeedVR2 快照内换健康账号。容量已满可在提交前换空闲账号；下载失败只重下。
- 网络响应丢失、5xx 或成功响应不可解析属于 `SUBMIT_OUTCOME_UNKNOWN`。必须保留原账号和
  保守容量，禁止自动/人工换号盲目提交；管理员核对 RunningHub 前不得清除此保护。
- 管理接口可以写入/轮换 Key，但列表、工作台摘要、日志和诊断均不得返回 Key、指纹、
  Base URL 或 AI App ID。自动化测试必须 mock RunningHub/MiniMax。
- `RunningHubClient.get_account_status()` 复用付费提交前已有的 `accountStatus` 容量查询，同时以
  `Decimal` 解析 `remainCoins/remainMoney`。`runninghub_credential_balances` 按凭据指纹共享
  精确文本缓存；管理员页面可显式刷新，工作台清单只返回 RH 币、辅助钱包字段、查询时间和
  是否过期。第一版余额未知只显示提示，不阻断或迁移任务。

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

数字人父任务在源片段完成后保持 `RUNNING`，清晰化子状态独立持久化：

```text
PENDING -> UPLOADING -> SUBMITTED -> RUNNING -> SUCCESS
                                      |          `-> DOWNLOAD_FAILED
                                      `-> FAILED
任一允许取消的活动状态 ----------------> CANCELLED
```

外部接口将这一段派生为 `VIDEO_ENHANCING`。`generation_tasks.result_path` 只在清晰化成功
后指向 SeedVR2 结果；源片段位于 `GenerationTaskEnhancement.source_result_path`。

RunningHub 明确返回 `FAILED` 时，默认最多自动重试 3 次，基础等待 60 秒，实际等待为
60、120、240 秒；分别由 `RUNNINGHUB_AUTO_RETRY_LIMIT` 和
`RUNNINGHUB_AUTO_RETRY_BASE_DELAY_SECONDS` 调整。每次尝试保留远程任务 ID、错误码
和 `failedReason`。人工重试会开启新一轮自动恢复，可能再次产生费用。

上传阶段且确认没有创建远程任务的网络失败，可以安全退避重试。提交请求返回不明确时
禁止盲目重提，因为远端可能已经创建并计费。`DOWNLOAD_FAILED` 且已有远程任务 ID 时
只恢复查询和下载，不重新生成。

新版工作台在 RunningHub 手动取消数字人阶段后，若用户又更换当前图片或分辨率，下一次
4A 启动必须清除该行旧视频阶段、保留已审核 MiniMax 音频，并按本次图片 SHA-256 与分辨率
创建全新的数字人命令。只有存在数字人取消任务且没有 SeedVR2 enhancement 时允许解除原
分辨率锁；SeedVR2 阶段取消仍复用已保存数字人源片段，只创建新的清晰化命令。

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
- 存储与限制：`DATABASE_URL`、`DATA_DIR`、各素材大小和保留时间；
  `MAX_IMAGE_SIZE_MB` 的默认值为 200，适用于旧单条页、旧批量页和工作台 4A
  暂存的数字人 JPG/PNG/WEBP 图片。
- RunningHub：基础地址、轮询、超时以及自动重试次数与等待。
- 长音频：`LONG_AUDIO_ALIGNMENT_PROVIDER`、`ASR_BASE_URL`、ASR 超时。
- 媒体节点：`MEDIA_PROCESSING_MODE`、`MEDIA_WORKER_TOKEN`、租约和归档上限。
- 豆包内容分析：`ARK_MAX_CONCURRENCY` 默认 10、`ARK_QUEUE_WAIT_TIMEOUT_SECONDS`
  默认 300 秒、`CONTENT_ANALYSIS_MAX_SCRIPT_CHARS` 默认 50000。

用户级 RunningHub、MiniMax、豆包 Ark 和工作流 ID 通过管理员页面维护，不写入仓库环境模板。

## 7. 数据库与迁移

当前迁移头为 `0034_runninghub_credential_balance`。`0031` 新增双池基础实体、执行模式与
阶段账号快照，`0032` 只新增网页运行模式单例控制表，`0033` 直接增加旧网页任务级 SeedVR2
开关字段，`0034` 新增按不可逆凭据指纹共享的 RunningHub RH 币安全缓存表；均不重建既有父表。统一内容分析缓存从
`0022_content_analysis_cache` 开始，`0028` 扩展统一视觉计划字段和缓存键，`0029` 新增
数字人清晰化与尝试表；`0030` 只在内容分析缓存增加独立标题状态、结果和错误字段。
历史已完成数字人任务仍不自动补跑 SeedVR2。
SQLite 启用 WAL、外键和 busy timeout，设计目标
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

脚本创建项目独立备份、上传指定 commit、执行迁移并只重启本项目服务；完整数据备份成功后
按 `RUNNINGHUB_BACKUP_KEEP_COUNT` 轮换，生产 systemd 默认保留最近 2 份。它不会安装服务器
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
- 数字人工作流仍使用整秒时间码，因此小数音频时长先使用 `ceil`，例如 `24.4 -> 25`。
  `generation_tail_padding_seconds()` 仍返回 `0`，不修改或补静音到 MiniMax 音频；但
  `new_workbench` 整个任务的最后一个视频分段冻结
  `workbench_final_segment_tail_seconds=1`，adapter 只把该段 RunningHub `end_time` 再加
  1 秒。中间分段不加，单分段任务视为最后分段。
- 合并标准化目标和工作台后处理清单必须同时保留这个冻结的 1 秒画面，不能因权威音频较短
  将其裁掉；独立 MiniMax 语音仍按正文音频时长使用，不需要制造新的静音文件。
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
- 对工作台公开的响应包含 `music_intent`、`subtitle_units`、`visual_plan` 和唯一两行 `title`；
  内部 Ark 响应使用紧凑的 `jyd.content-analysis.provider.v4`，一次返回音乐意图、字幕断点编号、
  选中的 `anchor_id`/`concept_id`/`priority` 与标题。服务端按原脚本本地切片，并校验视觉项只能
  引用请求中提供的候选。模型不返回视觉时间、素材身份或呈现参数。
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

- `POST /api/workbench/content-analysis` 使用现有工作台短期令牌，接收精确原始脚本、可选
  `force_refresh` 和可选 `visual_context`；后者只含 catalog 版本、概念说明、原文字符锚点、
  短语上下文及 `explicit/enrichment/seam_broll` 用途，禁止素材路径、时间戳及剪映轨道信息。
  浏览器和工作台不会获得 Ark Key。Prompt v13 要求普通空镜和连接处空镜都按当前/下一段语境
  判断：直接强相关返回 priority 2，自然且不误导的宽相关可返回 priority 1，唯一可选、勉强相关
  或无关候选都必须跳过，不能为了凑频率强行填充；同一次调用增加 `title` 第四字段，第一行最多 5 字、
  第二行最多 5 字，禁止空白、重复、空洞标题党和脚本外事实。Prompt v16 进一步要求标题独立
  满足法律、网络生态、隐私、未成年人、低俗、伪科学、医疗科普和私域引流约束；低风险体重
  管理词使用自然中性表达，硬风险改用“生活提醒/理性看待”，禁止用近形字或符号绕过审核。
- `app/services/content_analysis/analysis.py` 负责固定 Prompt、Ark JSON Schema、响应提取、
  音乐、字幕、视觉与标题分支独立校验、确定性字符索引修复、成功分支保护和脱敏状态日志。
- 迁移 `0028_unified_content_visual_plan` 在原缓存上增加视觉状态与结果，并把 catalog 版本和
  visual context SHA-256 纳入缓存键。缓存不保存豆包原始完整响应；完整失败不形成粘性缓存，部分成功和完整
  成功可复用，强制刷新失败不得覆盖此前合法分支。
- 迁移 `0030_content_analysis_title` 增加标题分支缓存。标题只生成一份，工作台把
  `line_1/line_2` 映射到封面，不增加第二次 Ark 调用；正文视频顶部由工作台固定为单行
  “世界冠军带你自律”（字号 19），不消费模型标题。
- 迁移 `0023_batch_correlation_id` 为批次增加独立日志关联号；历史批次用批次 ID 回填，
  新工作台传入的关联号会被语音、媒体和视频 Worker 持续继承。不得用 `request_key`
  替代 `correlation_id`。
- 迁移 `0024_shared_minimax_voices` 不改变表结构或唯一约束，只按相同
  `credential_fingerprint` 为既有用户补齐自定义音色副本并同步激活状态。迁移不得通过
  重建 `minimax_configs` 取消绑定唯一约束，否则 SQLite 外键级联可能删除已有音色。
- 单 Web 进程使用统一有界信号量，默认最多同时发出 10 个 Ark 请求；其余请求等待，默认
  最长 300 秒。当前生产结构只有一个 Web 进程；若将来增加 Web 进程或多台主机，必须先
  把进程内信号量替换为数据库或 Redis 共享租约，不能简单把进程数相乘。
- 429、超时、连接错误和明确 5xx 继续使用模块 3 的有限退避。分析失败不调用 MiniMax、
  RunningHub 或剪映，不改变基础视频状态。
- 真实 Ark 验收发现 Lite 模型可能把合法 `music_intent` 字段直接提升到响应顶层并遗漏字幕。
  当前仍只发出一次组合请求，Prompt v15 不要求模型复述整段字幕 JSON：用户消息同时
  提供未修改原文和带 `B` 编号的候选边界，模型只返回 `prefer_after`/`allow_after` 编号。
  Prompt 按任务执行顺序分为角色、目标、输入、输出、业务规则和完整 few-shot；根字段、值域
  与额外字段限制由 strict JSON Schema 负责，避免在 system/user/Schema 三处重复堆叠。
  候选边界先经过 `jieba==0.42.1` 确定性词典分词（`HMM=False`）和词内安全过滤，不再把
  完整中文词内部位置交给模型。结构助词之前仍禁止切分；结构助词之后若已形成完整修饰语则
  可以成为候选，但“类的/中的/里的/内的/上的/下的”等黏连后缀仍整体保护。标点和空白断点
  由服务端本地补入。相邻断点间的可显示正文以 13 个全角中文字等效宽度为硬上限而非凑满
  目标；Prompt v15 明确允许“类别/问题/评价 → 答案”在整行尚未超宽时形成强语义节拍。
  工作台仍把普通模型断点视作软偏好，并使用真实字体和本地语法约束作最终排版。
  Provider 偶尔回传未提供的词内、数字内或越界位置时，服务端会丢弃这些无效软偏好，
  保留其余合法位置并继续加入本地标点/空白断点；单个坏位置不得再使整条字幕分支失败。
  长脚本输出预算按 `max(4096, 字符数 × 12)` 计算且上限为 8192。若仍收到精确的裸音乐
  对象，音乐分支保留成功，字幕分支使用 `SUBTITLE_MISSING` 安全降级，不自动补发第二次
  付费请求。

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

## 22. 独立视觉语境分析（2026-08-07）

- `app/services/visual_analysis/` 定义严格的请求/响应模型、Ark JSON Schema、Prompt 和结果
  复核。候选字符切片必须逐字匹配脚本，每个候选必须且只能返回一次，概念必须来自该候选
  的允许集合，任何额外字段（包括时间和本地路径）都会被拒绝。
- `POST /api/workbench/visual-analysis` 继续使用工作台短期令牌和用户级 Ark 配置。浏览器不
  直连云端，Ark Key 不下发到工作台。
- `visual_analysis_caches` 使用迁移 `0026_visual_analysis_cache`。缓存键包含用户、脚本摘要、
  素材目录版本、候选集合摘要、响应契约、Prompt 和模型；强制刷新更新同一精确缓存项。
- 云端只输出 `SHOW/REVIEW/SKIP`、概念、用法、重要度、置信度和原因码，不选择具体图片、
  不计算时间。无效响应、Ark 错误或排队超时返回独立失败状态且不写缓存。

该接口现为迁移期兼容入口。工作台项目主流程已改为通过 content-analysis provider v3 在
同一次 Ark 请求中取得 selected-only `visual_plan`；旧视觉缓存不与新统一缓存互相命中。
具体素材、raw cues 时间和剪映写入仍全部由工作台本地处理。

## 23. 语音标点停顿配方（方案已确认，尚未实施）

- 跨项目权威方案位于工作区 `语音标点停顿配方开发文档.md`。它不属于内容分析或视觉分析
  契约，也不增加 Ark 请求。
- 工作台保留原始脚本并只提交版本化停顿配方；本服务在付费调用前严格复核规则、脚本摘要
  和人工覆盖，再编译 MiniMax 专用 `<#x#>` 标记。服务商标记不得写回字幕、字数、日志或
  视觉分析文本。
- 幂等摘要必须覆盖原文、音色、语音参数、规则版本、人工覆盖和有效文本摘要；相同成功或
  运行中请求不得重复提交 MiniMax。
- 当前代码仍按原始 `speech_script` 直接调用 MiniMax。本节只是冻结下一阶段边界，不能在
  未实现契约、迁移和跨项目测试前宣称功能已经生效。

## 24. 新工作台图片版本绑定（2026-08-08）

- 迁移 `0027_audio_primary_sha256` 给 `AudioGenerationTask` 增加 `primary_sha256`。4A 客户端
  上传当前图片时同时提交内容摘要；服务端校验上传字节并把摘要返回到任务清单。
- 摘要相同视为 HTTP 幂等重试，不重复创建 RunningHub 任务。摘要变化时仅在旧画面子任务
  已结束后清除旧分段任务和合并结果，把已批准音频任务重新置为 `PENDING` 以重做分段交接；
  `output_path`、`subtitle_path`、当前音频 attempt 和审核记录保持不变，不重新调用 MiniMax。
- 工作台下载 `base_video` 前必须核对云端 `composition.image_sha256` 与本地操作快照；缺失或
  不一致以 `REMOTE_IMAGE_VERSION_MISMATCH` 失败，禁止把旧图片视频登记为当前版本。
