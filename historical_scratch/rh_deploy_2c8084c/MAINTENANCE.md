# 项目维护指南

这份文档写给第一次接手项目的人。它说明“代码放在哪里、哪些状态不能随便改、改完
必须验证什么”。当前业务进度和未提交范围以 `PROJECT_STATUS.md` 为准，版本变化以
`CHANGELOG.md` 为准。

## 1. 模块边界

```text
app/routes/                  HTTP、权限、CSRF、页面响应；不直接调用第三方
app/services/task_creation  单次和批量共用的视频任务校验与创建
app/services/task_management 失败重试、删除等任务生命周期操作
app/services/batch_generation.py 清单校验与批次创建
app/services/batch_status.py 批次/清单行/分段状态聚合与页面标签
app/services/batch_lifecycle.py 批次失败重试、终态删除和文件清理
app/services/audio_review.py 完整语音审核、整批通过和再次生成状态转换
app/services/speech/         MiniMax 协议、异步结果和声音制作任务
app/services/media_segmentation.py 音频/视频探测、分段计划和 FFmpeg 切割
app/services/ltx_workbench.py 独立 LTX 工作台逐行预检、幂等创建、状态与失败合同
app/services/video_merge.py 分段结果按顺序统一规格、拼接、失效和成片审核状态
app/workers/audio_worker.py  领取脚本语音队列、生成整段音频、审核后切分和视频交接
app/services/speech/voice_jobs.py 克隆/融合试听、付费保护和保存音色
app/workers/task_worker.py   按用户并发限制领取 FIFO 视频任务并调用 RunningHub
app/workflows/               RunningHub 工作流节点和输入映射
app/templates/ + app/static/ 页面结构与浏览器交互
alembic/versions/            数据库结构的唯一演进入口
scripts/local_services.py    本地一键启动、停止、子进程守护
```

新功能应放进拥有该职责的模块。路由只做输入/权限/响应编排；可复用规则放
`services`；持续轮询和外部任务恢复放 `workers`；不要在模板或 JavaScript 中复制
后端校验规则作为唯一校验。

`tests/test_maintenance_architecture.py` 固定上述高风险边界，防止后续为了省事把声音
制作、批次状态、审核和删除逻辑重新塞回 Worker 或路由。

根目录 `.editorconfig` 统一编辑器缩进、UTF-8 和换行习惯；`.gitattributes` 固定源码
使用 LF、Windows 双击脚本使用 CRLF、媒体和 Excel 作为二进制文件处理。

## 2. 核心数据流

直接上传音频：

```text
批次 API -> GenerationBatchItem -> GenerationTask(PENDING)
         -> 视频 Worker -> 数字人 RunningHub（用户任务快照的 24G/48G）-> 源片段
         -> 若任务快照开启 SeedVR2：48G RunningHub -> 清晰结果
         -> 若关闭：源片段直接成为最终结果
```

完整脚本流程：

```text
批次 API -> AudioGenerationTask(PENDING)
         -> 语音 Worker -> MiniMax 异步任务 -> 完整音频
         -> 可选 AWAITING_REVIEW
         -> 时间轴分段 -> GenerationSegment
         -> 每段 GenerationTask(PENDING) -> 数字人/SeedVR2 两阶段 FIFO Worker
```

独立上传音频 LTX 工作台：

```text
工作台 staged 视频/音频 + 表格原稿
  -> LtxPreparationJob(PENDING_ANALYSIS)
  -> Windows 媒体节点 FunASR + 原稿时间轴
  -> 单段保留原文件 / 多段同区间切音视频
  -> GenerationSegment -> LTX RunningHub Worker
  -> 原始结果保留 -> 基础视频 -> 本地剪映精确字幕交接
```

该入口的时长条件是同一行完整源视频覆盖同一行完整音频，允许 0.05 秒探测误差。不可改成
“只覆盖单个切片”，也不可错误累加项目中其他行音频。新旧 LTX 入口统一使用 30 秒单段
硬上限；正好 30 秒保留单段，超过上限进入 ASR/时间轴切分，旧单条入口则明确要求改用完整流程。

`GenerationBatch` 是用户看到的总批次；`GenerationBatchItem` 是清单中的一行；
`GenerationSegment` 是长音频切出的可见子任务；`GenerationTask` 是一个用户可见的逻辑
分段。启用放大时，数字人逻辑分段包含数字人和 SeedVR2 两笔一对一 RunningHub 调用，后者由
`GenerationTaskEnhancement` 及其 attempts 独立审计；LTX 仍只有一笔调用。批次进度必须
从现有行、分段和清晰化事实聚合。

## 3. 状态和费用边界

- `PENDING` 只代表本地排队，不占 RunningHub 并发槽位。
- `UPLOADING`、`SUBMITTED`、`RUNNING` 占用户的视频并发槽位。
- 保存第三方任务 ID 后，不得因查询失败重新提交同一任务，否则可能重复计费。
- SeedVR2 固定 `plus`（48G）且不可由请求覆盖；数字人源片段成功后禁止因清晰化失败回退
  到数字人阶段，SeedVR2 下载失败只能复用同一远端 ID 重下。
- `GenerationTask.seedvr2_enabled` 与序列化参数是创建时冻结的费用快照。关闭时不得创建
  `GenerationTaskEnhancement`；旧网页新建任务默认关闭、可按次开启，新工作台始终开启。
- RunningHub 正常排队和执行以第三方状态为准；本地远程看门狗只处理提交后超过 4 小时
  仍无终态的异常任务。到期必须先查询、再远程取消，只有取消成功才释放槽位；取消
  失败继续轮询和重试取消，不能直接写本地失败或自动重提。
- MiniMax 异步语音的 `provider_task_id` 和原始结果包必须先持久化，再进入后续处理。
- `AWAITING_REVIEW` 表示完整音频已生成但尚未切分，不应由重启恢复逻辑标记失败。
- 重新生成语音和普通视频失败重试可能再次计费；仅视频下载失败应复用远程任务 ID。
- 独立 LTX 工作台分段失败重试必须再次确认费用并提供幂等键；`OOM_KILLED` 不自动重提。
- 用户上传的现成音频始终是独立入口，不能让视频生成强依赖 MiniMax。

状态字符串当前仍跨模型、后端聚合和页面展示使用。新增状态时至少同时检查模型枚举、
Worker 恢复、队列统计、批次聚合、重试/删除条件、页面中文映射和测试。

## 4. 数据库迁移红线

SQLite 外键已经开启级联删除。对被子表引用的父表使用 Alembic
`batch_alter_table`，可能通过“建临时表、复制、删除旧表”的过程误触发级联删除。

因此：

1. 能直接 `op.add_column` / `op.drop_column` 时，不重建父表。
2. 每个会修改既有表的迁移，都要写“先插入真实父子数据，再从上一版本升级到新版本”
   的迁移测试。
3. 升级前备份 `data/app.db`；升级后检查 `PRAGMA foreign_key_check`、关键表行数和批次
   聚合结果。
4. 不手改生产数据库结构，不删除失败迁移留下的数据，先保留快照再修复。

`tests/test_migrations.py` 是当前迁移保留父子数据的回归样例。

## 5. 注释和变量规范

注释应解释代码本身看不出的信息：

- 变量的业务含义、单位、所有者和生命周期；
- 状态为何被包含或排除；
- 幂等、计费、并发、文件保留和恢复约束；
- 为什么不能采用看似更简单的实现。

不需要给 `task_id = ...` 这类显然赋值逐行翻译。优先使用准确变量名、类型标注、
数据类字段、函数文档字符串和边界注释。注释修改后必须与行为同步，错误注释比少注释
更危险。

前端临时状态要标明它是否只是浏览器草稿；后端结构要标明用户可见行号与数据库 ID
的区别；时间变量必须注明是秒、UTC 还是北京时间展示值。

## 6. 日志和排障

管理员“运行状态”页每五秒更新资源状态并按字节游标增量读取日志，不刷新整页。
页面只显示：

- 系统和 Worker 启动、停止、异常重启；
- 任务领取、第三方提交、第三方状态变化、返回结果和下载；
- 成功、失败、人工审核等待等业务事件；
- HTTP 4xx/5xx 和未处理异常。

成功的健康检查、批次轮询和日志轮询不会出现在事件流中。完整原始日志仍保存在
`data/logs/`。日志默认保留 7 天，并在单文件达到 10 MB 时提前轮转，避免异常刷屏
占满磁盘。新增关键状态变化时使用 `log_event()`，事件码保持稳定，
详情中只放安全 ID 和状态，绝不记录 API Key、访问密码、完整用户脚本或素材内容。

当前实现适合单服务器。如果以后部署多个 Web/Worker 实例并需要长期审计，应把事件
写入专门的数据库表或集中日志系统，而不是跨服务器读取本地文件。

## 7. 每次修改的最低检查

```powershell
python -m pytest -q
python -m alembic current
python -m compileall -q app scripts
node --check app/static/operations.js
git diff --check
```

涉及页面时再手工检查桌面宽度、窄屏、长脚本、横向滚动和实时更新。涉及真实
RunningHub/MiniMax 的测试必须明确提示费用，自动化测试只使用 mock。

完成后按影响范围更新文档：用户可见变化写入 `CHANGELOG.md`；架构、数据流、状态机、
配置或发布方式变化写入 `DEVELOPER_GUIDE.md`；阶段决策和历史交接才写入
`PROJECT_STATUS.md`。服务器部署前还应完成数据库备份、恢复演练、生产环境检查和
一次独立测试环境验证。
