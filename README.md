# RunningHub 视频生成中转站

这是一个 FastAPI + SQLite + Jinja2 的单服务器应用：浏览器仅访问本站；Web 进程创建任务；独立 Worker 使用服务端保存且加密的 RunningHub API Key 上传素材、提交工作流、轮询状态并把视频下载到服务器。当前支持数字人视频和 LTX 2.3 视频对口型两个工作流。

版本变化见 [CHANGELOG.md](CHANGELOG.md)，当前技术状态和后续交接边界见
[PROJECT_STATUS.md](PROJECT_STATUS.md)。模块边界、状态约束、数据库迁移红线和
每次修改的检查清单见 [MAINTENANCE.md](MAINTENANCE.md)。

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

首次创建管理员时会提示输入密码。RunningHub 与 MiniMax 的账号连接、工作流 ID、
默认提示词和并发限制均在登录后的用户管理页配置；API Key 与工作流访问密码不会
回显，且只以 Fernet 加密密文保存在 SQLite 中。LTX 发布设置未开启“加密访问”时，
工作流访问密码保持为空即可。语速、音量、语调等每批使用参数不放在用户
配置中，而是在批量生成页选择。

.env 至少应修改：

- APP_SECRET_KEY：一段随机长字符串
- 生产环境必须设置 APP_ENCRYPTION_KEY：Fernet URL-safe Base64 key
- COOKIE_SECURE=false：仅限本地 HTTP；上线 HTTPS 后改为 true
- MAX_VIDEO_SIZE_MB：LTX 源视频上传上限，默认 500 MB

可用以下方式生成加密 Key：

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

## 启动

首次初始化完成后，双击项目根目录的 `启动系统.cmd` 即可。它会在后台完成迁移，
同时启动 Web、语音 Worker 和视频 Worker，并自动打开默认的批量生成页。无需保留
PowerShell 窗口；重复双击不会重复启动同一套服务。

需要停止时双击 `停止系统.cmd`。管理员登录后可从“运行状态”页面查看三个服务的
心跳、语音/视频队列数量和最近日志。日志位于 `data/logs/`，按天轮转并自动清理，
敏感凭证写入前会脱敏。

开发时仍可分别运行以下进程，以便在终端看即时输出：

    python -m scripts.serve_web
    python -m app.workers.audio_worker
    python -m app.workers.task_worker

修改 Worker 代码后需要重新启动对应 Worker；Web 进程不会替它重载。

创建任务时先在统一页面选择数字人或视频对口型工作流。数字人固定使用 24G 普通版；视频对口型可为当前任务选择 `default` 或 `plus`。

任务统一先保存为 `PENDING`。Worker 按用户的最大同时任务数占用运行槽位并按创建时间依次提交；例如并发数为 2、一次创建 8 个任务时，2 个运行、6 个留在本地队列，任一槽位释放后自动补充下一个。

## 批量生成

登录后的默认页面就是“批量生成”，单次生成仍可从导航栏切换。每个批次最多创建
50 条任务。默认的“快速批量生成”不要求填写表格：先按任务顺序上传图片或视频，
直接上传音频模式再按相同顺序上传音频，系统按页面显示的第 1、2、3……项配对。
上传后可以拖动、上下移动或移除文件，创建前必须确认最终顺序。

- 数字人的分辨率和单/双人模式按批次统一设置，默认单人；双人批次还要按相同顺序
  上传左、右人物音频。
- 视频对口型的 Stand/Plus 按批次统一设置。
- 每条任务只单独填写提示词；批量数字人固定使用完整音频时长。
- 视频对口型正向提示词只写“什么人、使用什么语言、音频中的完整台词”，不需要
  动作、镜头或画面描述。
- 建议文件名带 `01、02、03` 序号。系统以页面最终显示顺序为准，文件名只辅助用户核对。
- “Excel / CSV 高级导入”继续保留，适合已经整理好文件对应关系的运营表格；高级导入
  按清单中的完整文件名匹配素材。
- 文件名重复、缺少素材或任一行参数无效时，整批只返回校验错误，不创建任何任务。
- 校验通过后，每行创建一条独立 `PENDING` 任务，并按清单顺序进入原有 FIFO 队列。
- 批次详情提供汇总进度、失败项重试和整批删除；只有全部子任务终态时才能删除整批。

页面提供两种长期并存的音频入口：

- “上传现成音频”：按页面顺序与图片或视频直接配对，不依赖任何语音服务。
- “完整流程”：网页可直接填写，也可导入只含“脚本编号、脚本内容”两列的
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

规则应用于本批次全部脚本；Excel/CSV 脚本清单仍只有“脚本编号、脚本内容”两列。
批量页可通过“预览全部文案”集中查看完整脚本和最终视频提示词。

完整流程默认在语音生成完成后自动切分并进入视频队列。需要人工把关音色或读音时，
可在创建批次前勾选“语音生成后先审核”：系统会先暂停在完整音频，支持在线试听、
单条通过、重新生成和整批通过。重新生成会再次产生 MiniMax 文本合成费用，旧版本
不会被覆盖，可在批次详情中展开试听；只有通过的版本才会继续切分并创建 RunningHub
视频任务。

直接上传音频模式不自动切分，单条音频最长 45 秒；超过时请先自行拆成多行，或改用
完整流程自动切分。

批量暂存素材默认最多合计 5 GB、保留 24 小时，可通过
`MAX_BATCH_ITEMS`、`MAX_BATCH_TOTAL_UPLOAD_MB` 和
`STAGED_ASSET_RETENTION_HOURS` 调整。定时清理命令会同时清理过期暂存素材。

任务详情页会轮询单个运行中任务；任务历史页在存在活动任务时每 5 秒自动刷新。
批次详情只在后台更新状态，不会整页刷新或重置滚动位置。管理员运行日志每 2 秒
增量追加启动、领取、第三方提交/返回、成功和异常事件，不会整页刷新或打断当前
滚动位置；成功的批次状态轮询、日志轮询和健康检查会被隐藏，服务器上的原始日志
文件仍完整保留。

生成失败和下载失败任务可以在任务历史页点击“失败重试”。普通生成失败会复用
保留的上传素材重新进入队列，可能再次产生 RunningHub 费用；仅下载失败会保留
远程 Task ID，只重试本地下载。所有终态任务都可以删除，删除会同时清除任务记录、
上传素材和本地结果，且无法恢复；活动任务不能删除。

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

- SQLite 启用 WAL、外键与 busy timeout，适合一个 Web、一个语音 Worker 和一个
  视频 Worker 并发访问。
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
- minimax_voice_assets：该账号的临时声音样本和已激活长期 voice ID
- audio_generation_tasks：完整脚本、已保存音色、异步 MiniMax 任务编号、句级时间轴、
  语音参数、对齐方式、审核状态和当前生成版本
- audio_generation_attempts：每次 MiniMax 生成的完整音频、字幕、远程编号和审核结果
- workflow_configs：每个用户、每个工作流的远程 ID、实例类型、默认提示词和启用状态
- generation_tasks：本地任务 ID、远程 taskId、输入素材、时间范围、状态、错误、usage 与本地结果路径
- generation_batches / generation_batch_items：批次元数据、脚本父任务和原始清单行
- generation_segments：完整流程的分段脚本、时间区间、分段素材和 RunningHub 子任务关系
- staged_assets：批量创建前已经校验、等待清单引用的暂存素材

## 生产部署

仓库已提供 Ubuntu 单服务器部署模板，包括：

- Nginx 反向代理和大文件上传配置；
- Web、语音 Worker、视频 Worker、自动备份和自动清理的 systemd 服务；
- 生产环境变量模板、预检、备份与恢复脚本；
- HTTPS、上线验收、更新和日志检查步骤。

完整说明见 [deploy/README.md](deploy/README.md)。正式部署时使用一个 Web 进程、
一个语音 Worker 和一个视频 Worker；公网不直接开放 8000 端口。
