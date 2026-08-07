> 最新核对时间：2026-08-04（北京时间，UTC+8）

# 项目状态与交接说明

> 本文件保留阶段快照、重要决策和历史交接，因此部分后文章节描述的是当时状态，
> 不能单独作为当前实现依据。当前架构、状态机、开发、测试和部署总说明统一见
> [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md)；具体行为以代码和 Alembic 迁移为准。

## 0. 当前进度快照

当前代码支持 **“主站持久化调度 + Windows 独立媒体节点处理长音频”**。是否已将某个
具体 commit 发布到生产，应通过安全部署脚本的只读预检和服务器部署版本现场确认，
不再在此文档固化分支名或 commit，避免状态迅速失效。

> 2026-07-31 最新决策优先于下方尚未逐段清理的历史描述：服务器实测无法在安全
> 内存上限内加载当前 Paraformer 模型，生产 ASR 环境和 systemd 单元已经移除。
> 本地现已实现 Windows pull 媒体节点，负责 ASR、切音频和切视频；服务器只提供
> 受独立 Token 保护的领取、素材下载、续租和结果回传 API。电脑不开放公网端口。
> 批量创建同时支持同一图片/视频按素材 ID 复用到多条音频或脚本，不再依赖唯一
> 文件名配对。当前数据库迁移头为 `0022_content_analysis_cache`。
> 批量上传会逐行自动分流：不超过 45 秒直接创建任务，超过 45 秒在原批次行下自动
> 拆成多个子任务。视频对口型使用 ASR 对齐原脚本，数字人按停顿切分并复用图片；
> 人工确认默认关闭。双人数字人暂不对用户开放。
> 固定电脑运行文件已经统一收口到 `media_node/`，旧的独立 ASR 启动脚本不再使用。

> 新版工作台模块 3 已接入本项目的官方/自定义音色、声音克隆/融合和 MiniMax 音频
> 批次。工作台批次强制停在 `AWAITING_REVIEW`，本模块不自动创建 RunningHub 视频；
> 自动化测试没有调用真实付费接口，也没有部署或修改云端账号数据库。

> 新版工作台模块 4A 已在本地接通：工作台显式确认后才放行既有声音任务到原有
> RunningHub 流程；单/多片段均按已确认音频时长标准化，多片段自动拼接，原始片段
> 保留。音频后、4A 前仍可替换图片。模块 4B 已由工作台复用现有本地剪映队列完成
> 单行字幕、可选 BGM 和最终成片；本项目没有新增后端或账号，变体和生产部署均未包含。
> 新旧入口现由 `generation_batches.source_channel` 明确隔离：旧网页和历史数据为
> `legacy_web`，新工作台声音批次为 `new_workbench`。旧网页列表及旧拼接算法保持原行为。

> 后续智能内容分析模块 1～8 已完成本地自动化闭环：数字人服务端提供统一工作台接口、
> 音乐/字幕分支独立校验、按脚本哈希缓存和默认并发 10；工作台已完成逐行快照、MiniMax
> `raw_cues` 语义映射、本地音乐唯一 Top1、手动覆盖和变体冻结继承。跨项目契约测试会把
> 本服务实际响应直接交给工作台消费。完整 mock 回归为本项目 `216 passed`、工作台
> `260 passed`；未调用真实豆包或部署生产。

- 生产站点使用 `video.lanyingjk01.com`，项目目录为 `/opt/runninghub-video`，Nginx
  只代理本机 `127.0.0.1:18083`。目标结构为独立 Web、语音 Worker、媒体 Worker、
  视频 Worker 四个 systemd 服务；服务器不安装或启动 FunASR。
- 当前迁移头为 `0022_content_analysis_cache`。每次发布必须先运行只读预检，以实际服务器
  版本、服务状态、迁移和健康检查为准。
- `deploy/deploy-update.ps1` 负责 commit 锁定、项目独立备份、上传、迁移和只重启
  本项目服务；不得绕开脚本直接在生产服务器编辑代码。
- `deploy/deploy-update.ps1` 默认只做本地测试和服务器只读检查；明确使用 `-Deploy`
  并输入两次 commit 确认词后，才会备份、上传和只重启本项目四个服务。脚本不会
  修改 Nginx、证书或其他项目。
- 已在本地实现数字人/视频对口型批量生成：默认网页快速入口、Excel/CSV 清单、
  逐文件暂存、严格预检、原子创建、批次进度、失败项重试和整批删除。批次历史列表
  可直接手动删除终态记录，也允许清理没有活动 Worker 的本地卡住记录。
- 批量现在同时支持“上传现成音频”和“MiniMax 根据每行脚本生成音频”。两条路径在
  音频准备完成后汇入相同的视频任务创建、FIFO 队列和 RunningHub Worker；直接上传
  音频入口是长期能力，不依赖 MiniMax。
- 批量生成内置长音频处理，并由 Windows 远程媒体节点执行。视频对口型由 Paraformer 输出字词时间戳，
  节点将 token 与完整原脚本对齐后规划约 30 秒、最长 45 秒的切点；识别文本不会
  替换原脚本。用户试听并确认后，节点再串行切割音视频、回传结果，服务器校验后
  创建标准子任务。数字人图生视频无需 ASR，只使用 FFmpeg 静音检测规划切点、切割
  音频并复用原图。两条路径都可选择人工试听确认，默认自动一条龙处理。
- 完整流程支持只含“任务编号、口播脚本”两列的 Excel/CSV；每行只调用一次 MiniMax
  异步长文本接口生成完整音频；远端任务 ID 持久化并可在 Worker 重启后续查。成功后
  下载官方句级字幕，按真实时间戳切成约 30 秒、最长 45 秒的可见子任务。LTX 同步按
  实际区间顺序切割源视频；第一版不自动拼接。
- 已兼容 MiniMax 真实异步 TAR 结果包中的 `.titles`、`time_begin/time_end` 毫秒时间戳。
  批量页支持预览全部完整文案，并提供批次级 `pronunciation_dict.tone` JSON 数组输入；
  Excel/CSV 脚本清单仍固定为两列。
- 完整流程可选“语音生成后先审核”。默认关闭并保持自动流程；开启后每行完整音频先
  暂停供试听，可单条通过、重新生成或整批通过，通过后才切分并进入 RunningHub 队列。
  重新生成会再次计费，旧版本音频保留供对比。
- 声音克隆和融合位于独立声音管理页，批量页只选择已保存音色。MiniMax 账号使用稳定
  绑定标识隔离音色，同账号轮换 API Key 不影响音色，切换官方账号才更换绑定。
- 支持从 MiniMax `get_voice` 实时校验并导入官方系统音色；官方音色标记为 `system`
  和 `ACTIVE`，没有 ¥9.9 克隆音色费，但文本合成仍计费。当前中国区默认 API 域名为
  `https://api.minimaxi.com`。
- 已新增本地一键启动/停止、后台进程守护、服务心跳、轮转脱敏日志和管理员运行状态页。
  运行状态页每 5 秒更新资源，并按文件字节游标增量追加启动、领取、第三方提交/返回、成功和异常
  事件，不整页刷新、不重置滚动位置，并隐藏成功的健康检查和轮询请求。
- 新增 `MAINTENANCE.md` 作为接手指南，说明模块归属、完整数据流、状态/费用边界、
  SQLite 迁移红线、注释规范和修改后的最低验证步骤。
- 已完成第一轮维护性拆分：批次状态、语音审核、批次生命周期和声音制作任务均有
  独立服务模块；批次路由不再计算进度或直接实现审核/删除规则，语音 Worker 不再
  实现克隆/融合/保存细节。架构回归测试会阻止这些职责重新回流。
- Windows 启动器已改用只读进程句柄判断 PID 是否存活，修复重复双击无法打开页面、
  点击停止却误报“没有运行”的问题。
- 当前新增数据库迁移头为 `0022_content_analysis_cache`，部署或本地更新代码后必须
  执行 `python -m alembic upgrade head`。
- 历史功能分支和提交边界不再作为发布依据；每次只部署安全脚本预检锁定且已推送的
  明确 commit。

## 1. 项目定位

本项目是一个可本地运行、并已完成单服务器部署前准备的
**RunningHub 数字人视频生成中转站 MVP**。

- 用户通过网页上传素材、设置参数并创建本地任务。
- Web 请求不会等待 RunningHub 完成。
- 独立语音 Worker 负责 MiniMax 音频准备，独立视频 Worker 负责上传素材、提交
  RunningHub、轮询状态和下载结果。
- RunningHub API Key 只在服务端使用并加密保存，浏览器不会拿到密钥。
- 当前支持 `digital_human` 与 `ltx_lip_sync` 两个工作流，后端使用独立适配器扩展工作流。

项目目录：`runninghub_mvp/`

## 2. 阅读顺序与信息优先级

每次新开 Codex 任务/聊天时，按下列顺序了解上下文；同一任务内不重复执行：

1. `DEVELOPER_GUIDE.md`：当前架构、流程、状态和开发发布总入口。
2. 本文件 `PROJECT_STATUS.md`：阶段决策、历史交接和仍需现场确认的状态。
3. `../api文档.md`：RunningHub 最新调用参数；如与旧文档或代码冲突，以最新版 API 文档为准并同步修改适配器与测试。
4. `../PRODUCT_REQUIREMENTS.md`：最初产品目标与 MVP 验收要求。
5. `WORKFLOW_EXTENSION.md` 和相关代码/测试：以实际实现验证现状。

不要从历史聊天记录、旧 Notebook 或 README 中猜测当前节点参数；它们可能已过时。

## 3. 当前完成状态

### 已完成

- FastAPI + Jinja2 网页应用，SQLite（WAL、外键、busy timeout）、独立语音 Worker
  与独立视频 Worker。
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
- 已添加 Ubuntu 单服务器部署包：Nginx、Web/语音 Worker/视频 Worker systemd
  服务、自动备份、自动清理、生产环境模板、预检和显式确认的恢复脚本。
- 部署结构固定为一个 Web、一个语音 Worker 和一个视频 Worker；本机开发监听
  `127.0.0.1:8000`，当前生产环境监听 `127.0.0.1:18083`，均由 Nginx 提供公网
  HTTPS 入口。
- 视频对口型工作流支持可选 `accessPassword`：管理员输入后使用 Fernet 加密保存在工作流 `settings_json` 中，页面不回显；Worker 提交时才解密并放入请求体顶层。
- 任务详情页每 5 秒轮询活动任务；任务历史页存在活动任务时每 5 秒自动刷新，全部进入终态后停止刷新。
- RunningHub 明确返回 `FAILED` 后默认自动重试 3 次，按 60、120、240 秒退避；
  每次远程 ID 和失败详情均保留。次数耗尽后仍支持人工重试并重新开始一轮自动恢复；
  下载失败只重试下载，删除终态任务会移除记录及对应上传/结果目录。
- 任务历史页支持按北京时间起止日期筛选，所有任务时间统一按北京时间显示；支持逐项多选、全选当前筛选结果和批量删除。
- 批量删除为全有或全无：选择中只要包含活动任务，前后端都会拒绝整批操作；勾选任务期间暂停历史页自动刷新，避免选择丢失。
- 批量生成默认使用无需表格的快速入口：图片/视频和音频按页面编号顺序配对，支持
  拖动、上下移动、移除和创建前确认；页面明确提醒用户按相同顺序上传。
- Excel/CSV 保留为高级导入入口：数字人模板只有“任务编号、提示词”两列，
  视频对口型和语音生成模板只有“任务编号、口播脚本”两列；无需填写素材文件名。
- 批量素材采用逐文件暂存，不使用单个超大 multipart 请求或压缩包。
- 数字人的分辨率、视频对口型的 Stand/Plus 都是批次统一参数；双人数字人暂不开放；
  数字人直接上传音频时每行填写提示词，对口型每行填写一次口播脚本。
- 完整音频的结束时间使用向上取整到整秒，避免不足一秒的尾音被截断；实际媒体时长
  和分段计算仍保留小数精度。
- 视频对口型正向提示词只包含人物、语言和音频中的完整台词，不描述动作、镜头或画面。
- 整批在创建前做严格预检：任何一行失败都不会创建任务；通过后，每行创建独立 `PENDING` 任务并按清单顺序进入现有 FIFO 队列。
- 批次进度由子任务状态实时汇总；运行允许部分成功。批次可重试符合条件的失败项；
  全部现存任务终态，或确认没有活动视频/语音 Worker 的本地卡住批次，才可删除整批。
- 批次详情每 5 秒只请求状态 JSON 并就地更新计数和状态，不整页刷新；分段子任务默认
  折叠，脚本和提示词使用紧凑摘要，展开后再查看全文。管理员运行日志页每 5 秒增量
  追加关键事件，不会刷新整页或打断日志滚动位置。
- 登录后的默认首页为批量生成；单次生成继续保留并从导航栏切换。
- MiniMax API Key 和调用节流由管理员按用户保存；API Key 使用 Fernet 加密。声音
  资产同时绑定站内用户、MiniMax 配置和稳定的官方账号绑定标识，凭证指纹仅作审计。
- 声音克隆与融合在独立声音管理页完成，只有明确保存的最终音色才能用于完整流程；
  批量页选择一个已保存音色和统一语音参数，不在批量创建时临时克隆或融合。
- 已付费激活的 voice ID 按账号长期保留；未激活样本和已激活音色的原始样本默认在
  48 小时后清理，不删除长期 voice ID。双人数字人暂不对用户开放。
- 语音任务独立持久化并按创建顺序处理；远程克隆/合成阶段中断后不自动重复付费操作，
  而是标记失败供用户明确重试。音频生成成功后才创建原 `generation_tasks.PENDING`。
- 本地双击 `启动系统.cmd` 会先迁移，再后台启动 Web、语音 Worker 与视频 Worker；
  `停止系统.cmd` 只停止该启动器拥有的进程。管理员可在站内查看心跳、队列和近期日志。

### 当前数字人工作流（最新 API 状态）

AI App ID 默认值：`2062251097452007426`（可由管理员为用户配置）。

| 用途 | nodeId | fieldName | 当前行为 |
| --- | --- | --- | --- |
| 最长分辨率 | `503` | `value` | 用户可填正整数，默认 `1024` |
| 人物模式 | `753` | `select` | 当前固定 `1` 单人；`0` 双人暂不开放 |
| 参考图片 | `240` | `image` | 必填 |
| 总参考音频 | `339` | `audio` | 必填 |
| 截取开始/结束 | `341` | `start_time` / `end_time` | 必填 |
| 左/右人物音频 | `739` / `738` | `audio` | 当前不提交，保留供后续双人模式开发 |
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
- 任一运行任务进入成功、失败、下载失败或取消等终态后，下一轮自动补充空闲槽位。
- RunningHub 正常排队与执行不受本站一小时计时限制；提交后超过 4 小时仍无终态的任务
  才进入异常看门狗。Worker 每轮先读取最新状态，再远程取消；取消成功后以
  `REMOTE_WATCHDOG_TIMEOUT` 结束，取消失败时继续占槽并重试，避免远程失控或重复计费。
- 数字人和视频对口型共享同一用户的并发槽位。
- `FAILED` 重试前检查全部原素材仍存在，随后清除旧远程任务状态并以当前时间进入 FIFO 队尾。
- `DOWNLOAD_FAILED` 且已有远程 Task ID 时不重新生成，只恢复为运行态并重新查询、下载结果。
- 重试会清除旧错误和完成时间；原素材超过保留期后不允许重试，必须重新创建任务。
- 活动任务禁止删除；成功、失败、下载失败和取消任务可以删除，删除同时移除数据库记录、上传目录和结果目录。
- 终态任务上传素材从 `completed_at`（旧数据回退到 `updated_at`）起保留 48 小时；生成结果仍保留 7 天。

### 当前批量生成规则

- 一个批次只包含一个工作流和一种音频准备方式；每行对应一个不同的最终视频任务。
- 音频方式可选直接上传或脚本生成。直接上传时，快速入口按页面最终显示顺序把每个
  图片/视频与同序号完整音频配成一组。
- 直接上传模式逐行自动判断时长：不超过 45 秒立即创建一个任务；超过 45 秒则在
  同一个批次行下自动创建分段子任务，用户不需要预先切音频。
- 完整流程按图片/视频顺序创建父任务，每行填写独立口播脚本；整个批次只选择一个
  已在声音管理页保存的音色和统一语音参数，不在批量页上传、克隆或融合声音。
- 完整流程支持网页输入，以及只含“任务编号、口播脚本”两列的 `.xlsx` / `.csv`。
  表格不包含素材文件名；主素材严格按页面最终显示顺序与脚本行对应。
- 每行完整脚本只调用一次 MiniMax，避免同一脚本分多次合成导致音色、情绪或语速
  不一致。MiniMax 返回可靠句级时间轴时按该时间轴规划；直接上传的 LTX 长音频由
  Windows 节点调用 FunASR 获取字词时间戳、对齐原脚本并结合静音边界切分。目标约
  30 秒、硬上限 45 秒；数字人长音频只使用静音检测，不调用 ASR。
- 每个分段作为父任务下可见的 `generation_segments` 子记录，并创建一条独立
  `generation_tasks.PENDING`，继续使用现有用户并发限制、FIFO 队列和视频 Worker。
- 数字人所有分段复用同一张图片。LTX 使用每段实际音频起止时间在本地顺序切割源
  视频，源视频总时长必须不短于完整音频；源视频不足时保留整段音频，替换视频后
  从切分阶段继续，不重新调用 MiniMax。
- LTX 每段正向提示词由可修改的“人物 + 语言”前缀和该段原始脚本文字自动组成，
  不加入动作、镜头或画面描述。第一版不自动拼接分段结果。
- 当前所有用户入口只开放数字人单人模式和视频对口型。
- 页面在上传区和确认区同时提醒顺序规则，支持拖动和箭头调整，并在素材数量不一致时
  禁止创建；建议文件名带连续序号辅助人工核对。
- 数字人的分辨率、视频对口型的 Stand/Plus 在批次顶部统一设置。服务端统一参数
  覆盖清单中的旧式逐行参数，避免同批规格不一致。
- 直接上传模式的 Excel/CSV 也使用两列模板：数字人为“任务编号、提示词”，对口型
  为“任务编号、口播脚本”。完整流程同样使用“任务编号、口播脚本”。当前支持
  `.xlsx` 和 `.csv`，不支持旧式 `.xls`。
- 素材由页面逐个上传至暂存区，并按图片、视频、总参考音频、左人物音频和右人物音频
  分组显示；每组页面序号与表格行号严格对应。旧版包含完整文件名的表格仍可导入。
- 单批默认最多 50 行，未消费的暂存素材默认合计不超过 5 GB并保留 24 小时；均可由环境变量调整。
- 预检采用全有或全无：所有行、素材、权限和参数全部通过后，数据库事务才创建批次、清单行和独立任务。
- 直接上传模式每行立即创建一条视频 `PENDING`；完整流程每行先创建一条语音
  `PENDING`，音频生成和本地切分完成后再创建多个视频子任务。批量 API 本身不直接
  调用 MiniMax 或 RunningHub，外部调用只由各自 Worker 执行。
- 清单顺序通过任务创建时间的微秒偏移保留；队列仍按用户并发数调度，数字人和视频对口型共享额度。
- 批次进度不维护第二套状态机，而是按子任务当前状态派生等待、运行、成功和失败数量。
- 创建后的执行允许部分成功；批次重试只处理仍符合素材条件的失败项，下载失败沿用“只重试下载”规则。
- 只有全部现存子任务进入终态，或批次没有活动视频/语音 Worker 时才可整批删除；
  删除批次、子任务和对应文件按全有或全无规则执行。
- 公共创建与重试逻辑分别集中在 `task_creation.py` 和 `task_management.py`；批量清单、暂存和编排按职责拆分，关键边界使用简短注释或文档字符串。
- 两种音频路径已在“音频准备完成”边界汇合；不得删除直接上传入口，也不得把通用
  视频创建和队列逻辑改成依赖 MiniMax。

### 当前 LTX 2.3 对口型工作流

私有 Workflow ID 默认值：`2080551073030434817`（可由管理员为用户配置）。

提交端点：`/openapi/v2/run/workflow/{workflowId}`。

| 用途 | nodeId | fieldName | 当前行为 |
| --- | --- | --- | --- |
| 源视频 | `237` | `video` | 必填，上传后传 RunningHub `data.fileName` |
| 自定义音频 | `246` | `audio` | 必填；上传后传 RunningHub `data.fileName` |
| 视频正向提示词 | `222` | `text` | 必填；写明人物、语言和与音频完全一致的说话内容 |
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

LTX 加密访问：

```text
RunningHub 发布设置未开启“加密访问”：不传 accessPassword
RunningHub 发布设置已开启“加密访问”：管理员必须配置工作流访问密码
密码仅以 Fernet 密文保存在 workflow_configs.settings_json
Worker 提交时在 JSON 请求体顶层传 accessPassword
密码不进入 generation_tasks.input_payload、日志或前端回显
```

### 历史验证记录

> 本节记录过去各阶段的验证证据，不代表当前工作区的最新测试数量。当前提交前应重新
> 执行 `python -m pytest -q`，并将版本级验证结果写入 `CHANGELOG.md`。

- 数据库迁移头：`0011_system_voice_categories`；已在隔离 SQLite 数据库从空库执行
  完整 `upgrade head` 并验证迁移链；官方音色分类使用原生 `ADD COLUMN`，不重建
  `minimax_voice_assets` 表。
  且模型与迁移没有待生成差异。
- 本轮针对账号删除、生产状态页和未保存试听清理的测试：`15 passed`。
- 最近一次完整 mock 测试：`99 passed`。
- 已修复原始 `0010_audio_review` 使用 SQLite 批量重建父表导致既有批次行被级联删除的
  回归。迁移现改用原生 `ADD COLUMN`，并以真实既有父子数据验证升级后记录不变。
  本地受影响的两个历史批次已恢复 3 条父任务、7 条分段和 7 条视频任务关联；修复前
  数据库快照及恢复前备份保留在 `data/`，未覆盖原始证据。
- 本地真实失败记录确认：admin 的 LTX 访问密码密文已成功保存，但当时独立 Worker 的启动时间早于修复代码写入时间，因此仍使用旧 payload；需要重启 Worker 后重新创建任务验证。
- Python 编译检查通过；当前 Windows 环境未提供 Bash，部署脚本的 `bash -n` 语法检查需在 Ubuntu 测试服务器的预检阶段补做。
- 自动化测试不得真实调用 RunningHub 或 MiniMax，避免扣费。MiniMax 实际账号、
  新音色收费提示和真实音频质量仍需由用户明确授权后手工验收。

## 4. 当前代码结构

```text
app/
  routes/                 # 登录、管理员、任务页面和本地 API
  services/
    batch_assets.py        # 批量素材暂存、额度和所有权
    batch_generation.py    # 整批预检、映射及原子创建
    batch_manifests.py     # Excel/CSV 字段解析与模板定义
    media_segmentation.py  # 音频分段规划与音频/视频本地切割
    runninghub.py          # 通用 RunningHub HTTP 客户端
    audio.py               # 音频时长与时间校验
    storage.py             # 本地文件保存与安全路径处理
    task_creation.py       # 单次与批量共用的权限、校验和任务构造
    task_management.py     # 单次与批量共用的重试规则
    workflow_configs.py    # 用户 × 工作流配置解析
    logging_config.py      # 脱敏、按天和大小双重轮转日志
    speech/
      accounts.py          # MiniMax 加密凭证与稳定官方账号绑定
      minimax.py           # MiniMax HTTP 客户端及统一校验
      voice_studio.py      # 声音克隆、融合与保存规则
  workflows/
    base.py                # 工作流协议、素材和输出对象
    registry.py            # 工作流注册表
    digital_human.py       # 数字人专属节点、校验、输出选择
    ltx_lip_sync.py        # LTX 2.3 对口型节点、素材模式、请求和输出选择
  workers/
    audio_worker.py        # MiniMax 克隆、合成及视频任务交接
    task_worker.py         # 通用视频任务领取、上传、提交、轮询、下载
  templates/               # Jinja2 页面
  models.py                # SQLAlchemy 模型
alembic/versions/          # 数据库迁移
scripts/                   # 一键总控、Web 入口、模板生成、创建管理员和文件清理
tests/                     # pytest；RunningHub 均为 mock
data/                      # 本地数据库、上传素材、生成结果（不提交）
deploy/
  nginx/                   # Nginx 反向代理与上传限制模板
  systemd/                 # Web、语音/视频 Worker、备份、清理服务和定时器
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
0004_batch_generation
0005_minimax_audio_pipeline
0006_voice_studio
0007_full_script_pipeline
0008_minimax_async_timestamps
0009_pronunciation_dict
0010_audio_review
0011_system_voice_categories
```

LTX 工作流继续沿用 `workflow_configs` 和通用 `input_payload`。批量功能持久化批次、
清单行和暂存素材；完整流程另外持久化可见分段和每段独立生成任务。MiniMax 完整流程
包含稳定官方账号绑定、长期声音资产和独立语音任务。

主要表：

- `users`：站内账号、管理员标记、启用状态。
- `runninghub_configs`：每个用户的加密 API Key、Base URL、并发上限等账户级配置。
- `workflow_configs`：每个用户、每个工作流的 AI App ID、实例类型、默认提示词、启用状态和扩展设置。
- `generation_tasks`：本地任务、远程 `runninghub_task_id`、状态、通用 `workflow_type`、`input_payload`、本地结果路径。
- `generation_batches`：批次名称、工作流、入口来源、音频方式、幂等请求键和总行数。
- `generation_batch_items`：原始清单行、行号、行标识及其父级语音任务。
- `generation_segments`：完整流程的分段脚本、时间区间、本地音频/视频和子任务关系。
- `staged_assets`：批量创建前已校验的暂存素材、归属、类型、大小和过期时间。
- `minimax_configs`：每个用户的加密 API Key、Base URL、稳定账号绑定、凭证指纹与请求频率限制。
- `minimax_voice_assets`：临时/已激活音色、远程 voice ID、短期原始样本和账号归属。
- `audio_generation_tasks`：每行完整脚本、已保存音色、语音参数、对齐方式、状态、当前生成版本和整段音频。
- `audio_generation_attempts`：每次完整语音生成的远程编号、音频、字幕、版本和审核结果。

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

日常使用优先双击：

```powershell
启动系统.cmd
```

它会先运行迁移，再在后台启动 Web、语音 Worker 和视频 Worker，自动打开
`http://127.0.0.1:8000/generate/batch`。停止时双击：

```powershell
停止系统.cmd
```

开发调试可分别运行 `python -m scripts.serve_web`、
`python -m app.workers.audio_worker` 和 `python -m app.workers.task_worker`。
管理员登录后从“运行状态”页面查看服务心跳与 `data/logs/` 中的近期脱敏日志。

## 7. 操作与安全约束

- 不在代码、日志、测试、`.env.example`、Notebook 或回复中写入真实 API Key。
- 真实 RunningHub 联调会产生费用；除非用户明确要求，不启动 Worker 来处理真实待提交任务，也不做真实 API 调用。
- MiniMax 新音色首次正式使用和文本合成也会产生费用；除非用户明确要求，不启动
  语音 Worker 处理真实任务，也不上传真实声音样本。
- 用户希望手动控制 Web/Worker 端口和进程。除非明确要求，不要擅自启动、停止或结束其进程。
- 运行测试前，确认测试数据库和 `tests/.runtime` 与实际 `data/app.db` 隔离。
- 保留用户已有的未提交改动；先查看 `git status`，只修改当前需求涉及的文件。
- 新版 API 变更时，必须同时更新：工作流适配器、任务创建路由、前端表单、相关测试和本文件。

## 8. 当前范围外 / 后续可做

生产环境已经完成域名、HTTPS、Nginx、systemd、备份和资源限制配置。后续更新必须
先在本地分支完成修改与测试，再合并到 `main`，最后按安全更新流程从 Git 拉取；
禁止直接覆盖生产数据目录、现有 Nginx 配置或其他项目服务。

可继续扩展或验收：

- 图生视频、文生视频等更多适配器及专属页面。
- 通用的管理员“按工作流配置”界面（当前管理员表单主要配置数字人工作流）。
- 任务统计、对象存储、Webhook 通知、多 Worker。
- 使用真实 MiniMax 测试账号手工验收上传、克隆、融合、计费提示和生成音质；自动化
  测试只使用 mock，本地实现完成不代表第三方线上接口与账单已验收。
- 在真实 Windows 桌面上双击验收一键启动/停止和浏览器自动打开；自动测试不启动
  用户真实服务或处理现有队列。
- 异机备份落地；当前备份仍保存在服务器独立备份目录。
- 用户自助注册、找回密码、邮件验证、套餐或支付系统；当前账号仍由管理员创建。

## 9. 本次仓库更新范围

2026-07-25 已将以下三部分作为同一次版本更新提交并推送到现有仓库：

1. 数字人取消旧总体模式并固定 Stand 24G。
2. 新增视频对口型工作流和按用户并发调度的持久化 FIFO 队列。
3. 新增生产安全加固与 Ubuntu 单服务器部署前准备。

远端分支为 `origin/main`，提交为 `3b96633`。

本轮发布首先包含 2026-07-27 完成的 LTX 加密访问修复和任务历史增强：

```text
app/routes/admin.py
app/routes/tasks.py
app/config.py
app/static/app.css
app/services/security.py
app/templates/admin_user_form.html
app/templates/task_detail.html
app/templates/tasks.html
app/web.py
app/workflows/ltx_lip_sync.py
app/workers/task_worker.py
scripts/cleanup_files.py
tests/test_auth_and_permissions.py
tests/test_task_management.py
tests/test_worker.py
tests/test_workflow_adapters.py
.env.example
.env.production.example
README.md
CHANGELOG.md
PROJECT_STATUS.md
../ltx2.3对口型api文档.md（仓库外的本地 API 说明）
```

上述“任务历史管理”子阶段未修改数据库模型。失败重试和删除操作都由用户在任务
历史页主动触发，不自动重提；后续批量、语音、声音管理和完整流程阶段另有
`0004`、`0005`、`0006`、`0007`、`0008`、`0009`、`0010` 七个迁移。

2026-07-27 在上述修复之上继续完成批量生成基础能力，包括：

```text
alembic/versions/0004_batch_generation.py
app/models.py
app/main.py
app/routes/batches.py
app/routes/tasks.py
app/services/batch_assets.py
app/services/batch_generation.py
app/services/batch_status.py
app/services/batch_lifecycle.py
app/services/audio_review.py
app/services/batch_manifests.py
app/services/storage.py
app/services/task_creation.py
app/services/task_management.py
app/templates/batch_generate.html
app/templates/batches.html
app/templates/batch_detail.html
app/templates/base.html
app/static/templates/*.xlsx
app/static/app.css
scripts/cleanup_files.py
tests/test_batch_generation.py
tests/test_task_management.py
.env.example
.env.production.example
requirements.txt
README.md
CHANGELOG.md
PROJECT_STATUS.md
```

批量代码按清单解析、暂存、编排、共用任务创建和共用任务管理拆分，较长的快速配对
交互独立放在 `app/static/batch_generate.js`，没有改写既有 RunningHub FIFO 调度语义。

2026-07-27 在批量基础上继续完成完整语音流程与本地运维能力，主要新增或修改：

```text
alembic/versions/0005_minimax_audio_pipeline.py
app/models.py
app/config.py
app/main.py
app/routes/admin.py
app/routes/batches.py
app/routes/operations.py
app/services/batch_generation.py
app/services/batch_manifests.py
app/services/logging_config.py
app/services/speech/
app/services/speech/voice_jobs.py
app/services/storage.py
app/workers/audio_worker.py
app/workers/task_worker.py
app/templates/admin_user_form.html
app/templates/batch_generate.html
app/templates/batch_detail.html
app/templates/batches.html
app/templates/base.html
app/templates/login.html
app/templates/operations.html
app/static/batch_generate.js
app/static/app.css
app/static/templates/*.xlsx
scripts/cleanup_files.py
scripts/generate_batch_templates.mjs
scripts/local_services.py
scripts/serve_web.py
启动系统.cmd
停止系统.cmd
deploy/systemd/runninghub-video-audio.service
deploy/README.md
tests/test_audio_worker.py
tests/test_voice_jobs.py
tests/test_maintenance_architecture.py
tests/test_auth_and_permissions.py
tests/test_batch_generation.py
tests/test_deployment_assets.py
.env.example
.env.production.example
README.md
CHANGELOG.md
PROJECT_STATUS.md
```

MiniMax HTTP 逻辑以 `D:\工作内容\轻盈健\音频合成` 中已经本地验证的 Python 实现为
依据重构；当前中国区默认 Base URL 为 `https://api.minimaxi.com`，管理员仍可按
账号修改。没有
把语速、语调、融合声音 A/B 固化在用户配置中。语音外部调用由独立持久化 Worker
处理；批量 API 仍只创建本地记录，不直接绕过队列调用第三方。

2026-07-28 在上述功能之上完成声音管理语义修正和完整脚本流程，主要新增：

```text
alembic/versions/0006_voice_studio.py
alembic/versions/0007_full_script_pipeline.py
alembic/versions/0008_minimax_async_timestamps.py
alembic/versions/0009_pronunciation_dict.py
alembic/versions/0010_audio_review.py
alembic/versions/0011_system_voice_categories.py
app/routes/voices.py
app/services/media_segmentation.py
app/services/speech/async_outputs.py
app/services/speech/voice_studio.py
app/templates/voices.html
app/static/voices.js
app/static/templates/script-batch-template.xlsx
tests/test_full_script_pipeline.py
tests/test_minimax_async.py
```

声音克隆与融合只在声音管理页执行，批量页仅消费已保存音色。完整流程的 Excel/CSV
只含“任务编号、口播脚本”，每行一次生成整段音频并创建可见分段子任务；LTX 本地
顺序切割源视频。此段是 2026-07-28 的历史状态；当前直接上传的 LTX 长音频已经接入
FunASR 字词时间戳、原脚本对齐和静音边界，详见 `DEVELOPER_GUIDE.md`。

2026-07-28 继续修复批量页面实际使用问题：移除面向开发过程的完整流程提示，增大
脚本和提示词输入框；分段音频下载补齐安全路径解析；FFmpeg/FFprobe 子进程在 Windows
隐藏窗口；批次详情使用状态 JSON 就地更新并压缩分段内容。运行日志改为事件实时流，
关键队列和数据结构补充维护注释，并新增 `MAINTENANCE.md` 接手指南。

面向使用者的变化摘要统一维护在 `CHANGELOG.md`。上述发布分支是历史记录，不是当前
发布目标；当前工作区和生产版本必须分别用 Git 与安全部署预检现场确认。

## 10. 每次完成需求后的更新模板

每轮需求完成后，按影响范围更新文档：

1. `DEVELOPER_GUIDE.md`：架构、流程、状态机、开发或发布方式的当前事实。
2. `CHANGELOG.md`：用户可见变化、兼容性和该版本验证结果。
3. 本文件 `当前完成状态`：只记录重要阶段决策、风险或交接快照。
4. `当前数字人工作流`：如 API 节点、默认参数或素材要求变化。
5. `数据库与迁移`：新增迁移编号和升级要求。
6. `当前范围外 / 后续可做`：新增明确待办、已完成项或不再适用项。

更新时只记录可公开的技术状态，绝不记录真实 API Key、密码、真实用户素材路径或敏感任务内容。

## 2026-08-05 新版工作台时长边界

- 新版工作台时间戳任务使用内部 `exact_timestamps` 标记，RunningHub 结束时间仅向上取整
  到整秒，不追加旧批量链路的 `0.5` 秒静音尾垫。
- 旧版上传音频、旧批量任务和无标记历史任务继续保持原算法。

## 2026-08-07 语义前景图片云端边界

- 已实现独立 `jyd.visual-analysis.v1` 响应契约、工作台 API、Ark 严格结构化输出和用户级
  精确缓存；数据库迁移头随当前未提交的 RunningHub 资源池迁移之后为
  `0026_visual_analysis_cache`。
- 云端只做语境消歧，不保存或生成本地素材路径和时间戳。失败响应不缓存，不影响任何既有
  音视频分支。
- 本轮只完成代码、mock 测试和文档同步；未部署生产、未修改生产账号数据库、未进行真实
  付费调用。
- 当前工作区完整 mock 回归 `272 passed`。
