# RunningHub 视频生成中转站

这是一个 FastAPI + SQLite + Jinja2 的单服务器应用：浏览器仅访问本站；Web 进程创建任务；独立 Worker 使用服务端保存且加密的 RunningHub API Key 上传素材、提交工作流、轮询状态并把视频下载到服务器。当前支持数字人视频和 LTX 2.3 视频对口型两个工作流。

## 文档导航

- [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)：当前架构、业务流程、本地开发、状态机、
  测试、媒体节点、生产发布和扩展方式的统一开发入口。
- [CHANGELOG.md](CHANGELOG.md)：面向版本的功能变化、兼容性说明和验证记录。
- [数字人网站与剪映工作台集成说明.md](数字人网站与剪映工作台集成说明.md)：数字人账号、任务分流、剪映工作台集成、日志规划和本地验收记录。
- [MAINTENANCE.md](MAINTENANCE.md)：模块边界、迁移红线、日志规范和修改检查清单。
- [deploy/README.md](deploy/README.md)：生产预检、发布、验收、备份和恢复。
- [media_node/README.md](media_node/README.md)：固定电脑媒体节点的安装、迁移及完整包/
  小更新包分发。
- [WORKFLOW_EXTENSION.md](WORKFLOW_EXTENSION.md)：新增 RunningHub 工作流适配器的最小步骤。
- [PROJECT_STATUS.md](PROJECT_STATUS.md)：阶段快照和历史交接；当前技术事实以开发者指南
  和代码为准。

## 本地前置条件

- 已安装 Anaconda Python（当前项目按你的 base 环境验证）
- Python 3.11+（需求目标为 3.12，代码兼容）
- 已安装 ffmpeg，并可在 PATH 中调用 `ffmpeg` 和 `ffprobe`

## 初始化

在项目根目录执行：

    conda activate base
    python -m pip install -r requirements.txt
    Copy-Item .env.example .env
    python -m alembic upgrade head
    python -m scripts.create_admin admin

首次创建管理员时会提示输入密码。RunningHub、MiniMax 与豆包 Ark 的账号连接、工作流 ID
和用户级参数在登录后的用户管理页配置；豆包内容分析的服务端统一并发上限通过
`ARK_MAX_CONCURRENCY` 配置，默认 10。API Key 与工作流访问密码不会
回显，且只以 Fernet 加密密文保存在 SQLite 中。LTX 发布设置未开启“加密访问”时，
工作流访问密码保持为空即可。语速、音量、语调等每批使用参数不放在用户
配置中，而是在批量生成页选择。

.env 至少应修改：

- APP_SECRET_KEY：一段随机长字符串
- 生产环境必须设置 APP_ENCRYPTION_KEY：Fernet URL-safe Base64 key
- COOKIE_SECURE=false：仅限本地 HTTP；上线 HTTPS 后改为 true
- MAX_VIDEO_SIZE_MB：LTX 源视频上传上限，默认 500 MB
- LONG_AUDIO_ALIGNMENT_PROVIDER：长音频默认对齐器，当前为 `funasr_http`
- ASR_BASE_URL：独立 ASR 服务地址，本地默认 `http://127.0.0.1:18084`
- MEDIA_PROCESSING_MODE：`local` 为同机处理，`remote` 为授权电脑主动领任务
- MEDIA_WORKER_TOKEN：远程模式专用随机令牌，生产环境至少 32 个字符

可用以下方式生成加密 Key：

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

## 启动

首次初始化完成后，双击项目根目录的 `启动系统.cmd` 即可。它会在后台完成迁移，
同时启动 Web、语音 Worker、媒体 Worker 和视频 Worker；如果 `.asr-runtime`
中的独立环境已经安装完成，也会启动本地 ASR 服务。随后自动打开默认批量生成页。
无需保留 PowerShell 窗口；重复双击不会重复启动同一套服务。

剪映工作台可以直接使用本网站现有账号登录，并通过 `/api/workbench/tasks` 拉取当前账号自己的旧任务收件箱记录。批次详情中的“后处理清单”是给人查看的中文页面；对应 `/api/batches/.../postproduction` 仍保留为机器接口。旧收件箱继续按原规则提示多片段人工处理；新版 `/app/new` 的 4A 画面模块按 2026-08-04 决策自动拼接多片段并保存为基础视频，同时保留全部原始片段。上传粗剪替换入口留待模块 5。

新版工作台模块 3 还通过 `/api/workbench/voices*`、`/api/workbench/voice-creations*`
和 `/api/workbench/audio-batches*` 复用本网站的 MiniMax 官方/自定义音色、声音制作任务
和异步音频 Worker。工作台音频批次固定开启审核，生成完成后停在
`AWAITING_REVIEW`，供工作台同步 MP3 和原始时间戳；本阶段不会自动进入 RunningHub
画面生成。浏览器仍只访问工作台后端，MiniMax Key 不下发。

模块 4A 已增加工作台专用的画面启动、状态、失败阶段重试和基础视频下载接口。用户在
工作台确认费用后，既有音频审核门才放行到原有音频/视频 Worker；单片段和多片段结果
都按已确认音频时长标准化，多片段按顺序拼接，并在接缝使用 0.25 秒保时长叠化。
RunningHub 原始片段继续保留，标准化结果只标记为 `base_video`。音频完成后、4A 启动前，
工作台仍可替换图片；启动请求绑定最后一次选择的当前图片。

新旧入口通过批次字段 `source_channel` 明确隔离：历史数据及原网页创建的批次为
`legacy_web`，新工作台创建的声音批次为 `new_workbench`。原网页批次列表只显示
`legacy_web`；工作台的单片段标准化和多片段按音频时长补帧/裁切不会应用到旧网页。
旧网页单片段仍直接使用原 RunningHub 结果，多片段仍只生成原有快速顺序拼接预览。

新版工作台的 4A 音频来自 MiniMax 原始时间戳，提交 RunningHub 时只把实际小数时长向上
取整为整秒（例如 `24.4` 秒使用 `25` 秒），不再追加旧批量链路的 `0.5` 秒静音尾垫。
旧版上传音频和旧批量生成继续保留原尾垫规则。

模块 4B 位于工作台本地后端，使用其现有剪映渲染队列为 `base_video` 添加 MiniMax
时间轴单行字幕和可选 BGM，再登记 `composition_video`。本项目没有为 4B 新增后端、
账号、RunningHub 调用或变体任务；4B 失败重试不会重新调用本项目已成功的付费任务。

后续智能内容分析模块 1～8 已完成本地 mock 自动化闭环。本项目的
`POST /api/workbench/content-analysis` 每次只接收一条精确脚本，同时返回分别校验的
`music_intent` 与无时间戳 `subtitle_units`；工作台负责逐行版本快照、MiniMax `raw_cues`
映射、真实字体宽度排版和 46 首本地音乐唯一 Top1。跨项目测试会把本项目实际响应直接交给
工作台消费。内容分析失败不得触发 MiniMax、RunningHub 或剪映，也不得使已有基础视频失效。
完整 mock 回归为本项目 `216 passed`、工作台 `260 passed`；真实服务与生产发布仍需另行授权。

需要停止时双击 `停止系统.cmd`。管理员登录后可从“运行状态”页面查看四个服务的
心跳、CPU、内存、磁盘、FFmpeg、语音/媒体/视频队列数量和最近日志。日志位于
`data/logs/`，默认保留 7 天且单文件最大 10 MB，敏感凭证写入前会脱敏。

开发时仍可分别运行以下进程，以便在终端看即时输出：

    python -m scripts.serve_web
    python -m app.workers.audio_worker
    python -m app.workers.media_worker
    python -m app.workers.task_worker

修改 Worker 代码后需要重新启动对应 Worker；Web 进程不会替它重载。

## 固定电脑远程媒体节点

长音频的 ASR、切音频和切视频可以放在 Windows 电脑执行，服务器只负责保存
任务、素材和最终子任务。节点使用 HTTPS 主动轮询服务器，因此不需要公网 IP、
端口映射、防火墙放行或把本机 ASR 暴露到公网。

处理顺序如下：

1. 用户仍在网站上传长音频、原脚本和源视频。
2. 服务器保存项目，固定电脑节点用独立令牌领取分析任务。
3. 对口型任务调用节点内的 FunASR；数字人任务只做 FFmpeg 静音检测。
4. 开启人工确认时用户先在网站试听；默认自动进入下一步。
5. 节点再次领取切割任务，使用本机 FFmpeg 串行切音频和所需视频。
6. 节点把严格命名的分段 ZIP 回传；服务器复核段数和时长后创建 RunningHub
   子任务。

任务租约默认 30 分钟，节点每 60 秒续租。电脑断网或退出后，租约过期的任务会
自动回到队列，另一个节点可以继续领取。服务器与电脑使用同一个
`MEDIA_WORKER_TOKEN`，网站用户密码、RunningHub Key 和 MiniMax Key 都不会下发
到电脑。

节点首次配置：

```powershell
powershell -ExecutionPolicy Bypass -File .\media_node\install-media-node.ps1
notepad .\media_node\.env
```

把服务器脚本输出的 `MEDIA_WORKER_TOKEN` 填入 `media_node/.env`，确认
`MEDIA_WORKER_SERVER_URL=https://video.lanyingjk01.com`，然后双击
`media_node/启动媒体节点.cmd`。启动器会复用已经运行的兼容 ASR；若没有运行，则只在
`127.0.0.1:18084` 启动一个。关闭窗口会停止由该启动器创建的 ASR 和 Worker。
完整安装、迁移和端口冲突处理见 [`media_node/README.md`](media_node/README.md)。

节点工作目录默认是 `media_node/data`，每个任务成功或失败后都会清理。
第一版 ASR 与 FFmpeg 都限制为单任务串行，避免和电脑上已有的高精度 ASR、分镜
FFmpeg 同时抢占大量资源。

创建任务时先在统一页面选择数字人或视频对口型工作流。数字人固定使用 24G 普通版；视频对口型可为当前任务选择 `default` 或 `plus`。

任务统一先保存为 `PENDING`。Worker 按用户的最大同时任务数占用运行槽位并按创建时间依次提交；例如并发数为 2、一次创建 8 个任务时，2 个运行、6 个留在本地队列，任一槽位释放后自动补充下一个。

RunningHub 接受任务后，本站每 5 秒同步 `QUEUED`、`RUNNING`、`SUCCESS` 或 `FAILED`
状态，不再用一小时本地计时抢先判失败。为避免第三方状态永久卡死，远程任务从提交起
超过 4 小时仍未结束时进入安全看门狗：先读取一次最新状态，再调用 RunningHub 取消；
只有取消成功才释放本地槽位并标记失败，取消失败时保留原任务继续查询和重试取消。

## 批量生成

登录后的默认页面是“生成视频”。快速创建与 Excel/CSV 表格导入保持并列一级入口，
单次上传一个素材、批量上传多个素材；旧单次生成路由保留用于兼容。每次最多创建
50 条任务。“快速创建”不要求填写表格：先按任务顺序上传图片或视频，
直接上传音频模式再按相同顺序上传音频，系统按页面显示的第 1、2、3……项配对。
上传后可以拖动、上下移动或移除文件，创建前必须确认最终顺序。

- 数字人当前只开放单人模式；双人模式保留底层适配能力，暂不对用户开放。
- 视频对口型的 Stand/Plus 按批次统一设置。
- 数字人直接上传音频时每行填写提示词；视频对口型每行只填写一次口播脚本。
- 视频对口型正向提示词只写“什么人、使用什么语言、音频中的完整台词”，不需要
  动作、镜头或画面描述。
- 建议文件名带 `01、02、03` 序号。系统以页面最终显示顺序为准，文件名只辅助用户核对。
- 同一张图片或同一段视频可以对应多条不同音频/脚本。素材只需上传一次，点击列表中
  的“复用”可增加一行引用；其他分组已有多条素材时可点击“补齐”，高级导入可点击
  “填满”。后台按素材 ID 而不是文件名绑定，因此同名文件不会混淆，也不会为复用的
  大视频重复占用暂存空间。
- “Excel / CSV 表格导入”直接显示。数字人模板只有“任务编号、提示词”两列，
  视频对口型和语音生成模板只有“任务编号、口播脚本”两列。
- 表格不填写素材文件名。图片、视频、总参考音频、左人物音频和右人物音频在网页
  分组上传，各组第 1、2、3……项与表格第 1、2、3……行对应。
- 任一组素材数量与表格行数不一致，或任一行参数无效时，整批只返回校验错误，
  不创建任何任务。
- 校验通过后，每行创建一条独立 `PENDING` 任务，并按清单顺序进入原有 FIFO 队列。
- 生成页顶部提供常显的“任务名称”。上传音频时默认取首个音频文件名（去掉扩展名），
  文案生成时默认取首个任务编号或画面文件名；多条任务附带数量，用户可在提交前修改。
  任务记录页优先显示该名称，批次 UUID 继续保留用于精确排障。
- 批次详情和历史列表提供汇总进度、失败项重试和手动删除。终态批次以及没有活动
  视频/语音 Worker 的本地卡住批次可以删除；仍在排队或远程运行的任务禁止删除。
- 只有“输入文案生成语音，并且实际语音不超过 45 秒”会在单个视频成功后进入自动
  BGM/字幕分支。MiniMax 返回的原始字幕时间戳会保留在后处理清单中。
- 上传音频或语音超过 45 秒产生多个视频片段时，生成结果进入人工处理。视频 Worker
  生成的拼接文件只作为片段顺序检查预览，不作为正式成片；原始分段始终全部保留。

页面提供两种长期并存的音频入口：

- “上传现成音频”：按页面顺序与图片或视频直接配对，不依赖任何语音服务。
- “完整流程”：网页可直接填写，也可导入只含“任务编号、口播脚本”两列的
  Excel/CSV。每行脚本只提交一次 MiniMax 异步长文本语音任务；完成后按 MiniMax
  返回的官方句级时间戳切成约 30 秒、最长 45 秒的可见子任务，不再根据文字长度
  猜测切点。两种入口在“音频准备完成”后使用相同的本地视频队列。

脚本生成语音当前只支持数字人单人模式和视频对口型，不为双人数字人自动生成两路
音频。声音克隆和声音融合位于独立“声音管理”页面；只有试听后明确保存的音色和已
导入的官方系统音色才会出现在批量生成页。完整流程每批选择一个可用音色，并在页面
选择语速、音量、语调等参数。官方系统音色不收取 ¥9.9 克隆音色费，但文本合成仍按
MiniMax 账单计费；克隆、融合或设计音色首次正式使用可能另收音色费。长期音色同时
绑定站内用户和稳定的 MiniMax 官方
账号标识；同一官方账号轮换 API Key 不影响音色，管理员明确切换官方账号后旧账号
音色会被隔离。原始声音样本默认只保留 48 小时。

完整流程的数字人分段复用同一张图片。视频对口型会按音频分段的实际时间区间顺序
切割源视频，因此源视频时长必须不短于整段生成音频；每段正向提示词由可修改的
“人物 + 语言”前缀和该段原始脚本文字组成，不加入动作、镜头或画面描述。第一版
保留全部分段视频，不自动拼接；源视频不足时会保留整段音频，替换视频后可继续切分，
无需重新调用 MiniMax。

完整流程的“脚本语音设置”支持可选的批次级读音标注，输入 MiniMax 官方
`pronunciation_dict.tone` JSON 数组，例如：

```json
["燕少飞/(yan4)(shao3)(fei1)", "omg/oh my god"]
```

规则应用于本批次全部脚本；Excel/CSV 脚本清单仍只有“任务编号、口播脚本”两列。
批量页可通过“预览全部文案”集中查看完整脚本和最终视频提示词。

完整流程默认在语音生成完成后自动切分并进入视频队列。需要人工把关音色或读音时，
可在创建批次前勾选“语音生成后先审核”：系统会先暂停在完整音频，支持在线试听、
单条通过、重新生成和整批通过。重新生成会再次产生 MiniMax 文本合成费用，旧版本
不会被覆盖，可在批次详情中展开试听；只有通过的版本才会继续切分并创建 RunningHub
视频任务。

直接上传音频时，用户仍按一行一组上传完整素材。系统逐行检查音频：不超过 45 秒
直接创建一个视频任务，超过 45 秒则在同一个批次行下自动拆成多个子任务：

- 视频对口型上传源视频、长音频和完整原脚本。默认 `funasr_http` provider 由独立
  Paraformer 服务返回字词时间戳，主站与原脚本对齐，在自然停顿切音频和源视频，
  并把每段原脚本写入对应子任务提示词。ASR 文本不会替换原脚本；运行实例默认
  Plus 48G。
- 数字人图生视频上传源图片和长音频，不调用 ASR，只用静音检测寻找自然停顿；
  系统切分音频后为所有子任务复用同一张图片和动作提示词。

两条路径都生成约 30 秒、最长 45 秒的分段。“长音频拆分后先试听确认”默认关闭并
自动继续创建 `generation_segments` 子任务；开启时才停在试听页调整边界。独立环境
安装与迁移方法见 `media_node/README.md`。

批量暂存素材默认最多合计 5 GB、保留 24 小时，可通过
`MAX_BATCH_ITEMS`、`MAX_BATCH_TOTAL_UPLOAD_MB` 和
`STAGED_ASSET_RETENTION_HOURS` 调整。定时清理命令会同时清理过期暂存素材。

任务详情页会轮询单个运行中任务；任务历史页在存在活动任务时每 5 秒自动刷新。
批次详情只在后台更新状态，不会整页刷新或重置滚动位置。管理员运行页面每 5 秒
更新资源和增量追加启动、领取、第三方提交/返回、成功与异常事件，不会整页刷新或打断当前
滚动位置；成功的批次状态轮询、日志轮询和健康检查会被隐藏，服务器上的原始日志
文件仍完整保留。

RunningHub 已明确返回 `FAILED` 的视频任务默认自动重试 3 次，分别等待 60、120、
240 秒后复用原素材重新排队；每次远程任务 ID 和失败详情都会保留。三次耗尽后，
仍可在任务历史页点击“失败重试”，人工重试会重新开始一轮最多 3 次的自动恢复，
并可能再次产生 RunningHub 费用。网络提交结果不明确时不会自动重提。仅下载失败会
保留远程 Task ID，只重试本地下载。次数与基础等待可通过
`RUNNINGHUB_AUTO_RETRY_LIMIT`、`RUNNINGHUB_AUTO_RETRY_BASE_DELAY_SECONDS`
调整。远程异常滞留看门狗默认 4 小时，可通过
`RUNNINGHUB_REMOTE_WATCHDOG_SECONDS` 调整；旧的
`RUNNINGHUB_TASK_TIMEOUT_SECONDS` 已停止使用。所有终态任务都可以删除，删除会同时清除任务记录、上传素材和本地结果，
且无法恢复；活动任务不能删除。

任务历史支持按北京时间的开始日期和结束日期检索。每条任务都可以勾选，也可以
全选当前筛选结果并批量删除；批量删除只接受全部处于终态的任务，如果选择中包含
排队中或运行中的任务，本次操作不会删除任何记录。开始勾选后，列表自动刷新会暂缓，
避免清空当前选择。

## 日常命令

    python -m pytest -q
    python -m alembic current
    python -m scripts.cleanup_files

清理命令仅处理终态任务：上传素材从任务结束时间起保留 48 小时，生成视频保留
7 天，可通过 `.env` 中的 `UPLOAD_RETENTION_DAYS` 和 `OUTPUT_RETENTION_DAYS`
调整。失败重试依赖原上传素材；超过保留期被清理后，需要重新创建任务并上传素材。

## 手动真实 RunningHub 联调

自动化测试完全 mock RunningHub，不会产生费用。仅在需要手动验证时，于当前 PowerShell 临时设置环境变量（不要写回代码或 .env.example）：

    $env:RUNNINGHUB_API_KEY = "你的新 Key"
    python runninghub_local_test.py --image .\sample.jpg --audio .\sample.mp3 --end 0:15
    Remove-Item Env:RUNNINGHUB_API_KEY

该命令会产生真实 RunningHub 消耗。网站联调则是在管理员页面把 Key 粘贴进对应用户配置后，登录该用户创建任务；Worker 会接手后续流程。

## 数据与安全

- SQLite 启用 WAL、外键与 busy timeout，适合一个 Web、一个语音 Worker、一个
  媒体调度 Worker 和一个视频 Worker 并发访问。
- 数据文件位于 data/uploads/用户ID/任务ID/ 和 data/outputs/用户ID/任务ID/。
- 用户密码使用 PBKDF2-SHA256 哈希；RunningHub Key 使用 Fernet 对称加密。
- 任务、图片预览和视频下载均进行登录与归属校验；管理员可查看全部任务。
- 所有登录、退出、配置修改和任务创建请求均校验 CSRF Token。
- 生产环境强制要求 HTTPS Cookie、随机应用密钥、稳定的 Fernet Key 和明确的可信域名。
- 请立即在 RunningHub 后台轮换旧 Notebook 中曾使用过的 API Key。

## 数据库表

- users：网站账号、密码哈希、管理员与启用状态
- runninghub_configs：一对一的加密 API Key 与该用户的 RunningHub 配置
- minimax_configs：一对一的 MiniMax 加密 API Key、稳定官方账号绑定、凭证指纹与调用节流配置
- ark_configs：一对一的豆包 Ark 加密 API Key、模型、启用状态、超时与有限重试配置
- content_analysis_caches：按用户、脚本哈希、模型、契约和 Prompt 版本隔离的音乐/字幕分析缓存
- minimax_voice_assets：该账号的临时声音样本和已激活长期 voice ID
- audio_generation_tasks：完整脚本、已保存音色、异步 MiniMax 任务编号、句级时间轴、
  语音参数、对齐方式、审核状态和当前生成版本
- audio_generation_attempts：每次 MiniMax 生成的完整音频、字幕、远程编号和审核结果
- workflow_configs：每个用户、每个工作流的远程 ID、实例类型、默认提示词和启用状态
- generation_tasks：本地任务 ID、远程 taskId、输入素材、时间范围、状态、错误、usage 与本地结果路径
- generation_batches / generation_batch_items：带 `source_channel`、`correlation_id` 的批次元数据、脚本
  父任务和原始清单行
- generation_segments：完整流程的分段脚本、时间区间、分段素材和 RunningHub 子任务关系
- staged_assets：批量创建前已经校验、等待清单引用的暂存素材
- long_audio_projects：长音频原始素材、脚本、可编辑分段草稿、远程节点租约、
  资源指标、处理状态和目标批次

## 生产部署

仓库已提供 Ubuntu 单服务器部署模板，包括：

- Nginx 反向代理和大文件上传配置；
- Web、语音 Worker、媒体 Worker、视频 Worker、自动备份和自动清理的 systemd 服务；
- 生产环境变量模板、预检、备份与恢复脚本；
- HTTPS、上线验收、更新和日志检查步骤。

完整说明见 [deploy/README.md](deploy/README.md)。正式部署时使用一个 Web 进程、
一个语音 Worker、一个媒体 Worker 和一个视频 Worker；公网不直接开放 8000 端口。
服务器默认不再安装或启动 FunASR；长媒体使用 Windows 节点时，服务器媒体 Worker
只负责保留队列心跳，实际 ASR 与 FFmpeg 由授权节点执行。
