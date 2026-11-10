# 开发者指南

本文面向需要继续开发、调试和发布“影变批剪工作台”的维护者。用户安装和日常操作请阅读根目录 `START_HERE.md`；具体素材格式、接口字段和部署方式请按本文末尾的专题文档索引继续阅读。

> 2026-08-10：数字人云端的每个分段现已在数字人成功后进入固定 48G 的 SeedVR2。
> 本地工作台不调用放大流，只把 `VIDEO_ENHANCING` 视为活动状态，并在就绪后下载
> `quality_variant=seedvr2_upscaled` 的清晰片段。数字人源片段仍保存在云端。

> 2026-08-12：新版工作台新增逐任务站姿 / 坐姿画面规范及勾选批量设置。
> 两套字幕、人名板、固定文字和语义图片参数见 `LAYOUT_PROFILES.md`。

## 1. 项目定位与边界

本项目通过读写剪映草稿 JSON 和 Windows UI 自动化完成批量视频生产，主要能力包括：

- 读取本机剪映草稿，必要时通过 `jy-draftc` 解密高版本草稿。
- 从 MP4 创建基础草稿，或从已导入的剪辑母版生成副本。
- 替换或新增 BGM、视频特效、字幕字体、贴纸、花字和文字模板。
- 应用镜像、裁剪、背景填色、人物定位和封面等画面变化。
- 生成单个或批量任务，并调用剪映顺序导出 MP4。
- 管理公共素材、个人素材、母版、任务、输出和回收站。
- 以单机嵌入模式运行，或由中央服务把任务交给一台或多台 Windows Agent。

必须明确的技术边界：

- 真正的剪映导出只能运行在 Windows 桌面会话中，目标电脑必须安装兼容版本的剪映。
- UI 自动化会占用剪映窗口，不适合在同一桌面会话中并行操作多个剪映实例。
- Web 服务可以集中部署，但实际执行导出的 Agent 仍然必须是可交互的 Windows 电脑。
- 草稿中的本地绝对路径必须在执行任务的电脑上可访问，或在导入、采集阶段被复制和重定位。

## 2. 总体架构

```text
浏览器
  |
  v
Processor Web/API (FastAPI, :8000)
  |-- 用户页面、批量编辑器、素材管理、任务结果
  |-- SQLite 控制库和任务状态
  |-- embedded: 进程内顺序执行 Render Job
  `-- agent: Agent 领取任务、回传状态和结果
          |
          v
      Windows Render Agent
          |
          v
      render_job.py
          |-- 草稿创建/复制/解密
          |-- JSON 修改和资源应用
          `-- 剪映 UI 自动导出 MP4

Local Collector (:8765)
  |-- 扫描本机 JianyingPro Drafts
  |-- 分析、解密、采集素材和字体
  |-- 选择本机视频及输出目录
  `-- 向 Processor 上传母版或素材包
```

### 2.1 三个主要应用

| 应用 | 源码入口 | 默认地址 | 职责 |
| --- | --- | --- | --- |
| Processor | `apps/processor/run_web_api.py` | `127.0.0.1:8000` | 网站、API、账户、素材、任务和嵌入式执行 |
| Collector | `apps/collector/run_local_collector.py` | `127.0.0.1:8765` | 本机草稿扫描、采集、文件选择和上传 |
| Agent | `apps/agent/run_agent.py` | 主动连接 Processor | 领取任务并控制本机剪映导出 |

`apps/auth_center` 是独立账户中心，用于多工作台统一登录。它不是剪映渲染链路的一部分，单机开发不需要启动。

### 2.2 两种执行模式

- `embedded`：Processor 自己执行任务。适合本机开发、单机安装包和大部分功能调试。
- `agent`：Processor 只调度任务，Windows Agent 主动领取并执行。适合中央网站连接多台剪映处理机。

切换模式不会改变 Render Job 的业务结构，只改变任务由哪个进程执行。

## 3. 目录结构

```text
apps/                         可运行、可打包的应用入口和前端
  processor/frontend/         主工作台、批量编辑器、素材页、登录页
    new/                      `/app/new` 新版工作台静态页面
  collector/frontend/         独立采集器调试页面
  auth_center/                可选的统一账户服务
src/jyd_probe/                核心 Python 代码
data/
  libraries/                  随正式安装包发布的公共素材
  personal_libraries/         当前安装实例采集的个人素材
  template_library/           已导入的剪辑母版
  web_storage/                数据库、任务、上传、输出和会话数据
docs/                         开发、接口、部署及素材专题文档
examples/                     Render Job JSON 示例
runtime/                      解密副本、测试环境和临时运行数据
scripts/                      开发环境及 Windows 打包脚本
tests/                        自动化测试
tools/                        草稿诊断、素材提取和任务调试工具
vendor/jy-draftc/             高版本剪映草稿解密程序
release/                      最终交付 ZIP
```

新版统一项目数据由 `src/jyd_probe/project_store.py` 管理。它与渲染队列共用
`control.db`，但只创建 `project_*` 表和独立的 `project_schema_meta`，不会修改既有
`schema_meta`、`batches`、`jobs` 或 `agents`。`Project` 包含多条 `ProjectItem`；
音频、原始片段、画面合成视频、上传视频和变体都按不可覆盖的素材版本保存。
模块 2 把 `project_schema_meta` 升级到版本 2：为脚本行增加当前输入图片指针，并增加
`project_input_images` 项目图片池。模块 3 升级到版本 3，增加按数字人账号保存的默认
音色和语音参数；逐行音色、音频素材版本、MiniMax 时间戳、数字人批次关联和异步操作
继续复用现有项目表。模块 4A 升级到版本 4，增加当前基础视频指针；基础视频与最终
`composition_video` 分离，云端有序分段继续按不可覆盖版本保存；自 2026-08-10 起默认
为 SeedVR2 清晰结果。模块 4B 升级到
版本 5，增加浏览器预览配方、按需导出和字幕渲染状态绑定；旧版
`POSTPROCESS_RUNNING` 剪映任务仍可同步完成。升级只执行
`CREATE TABLE IF NOT EXISTS` 和缺失列
`ALTER TABLE ADD COLUMN`，不会重建或清空既有项目及剪映任务表。

智能内容分析模块 5 将项目 schema 升级到版本 7，为 `project_items` 增加
`content_analysis_json`。快照按当前脚本 SHA-256 绑定，分别保存音乐、字幕分支状态和
结果；脚本变化只重置该行快照，音色、音频、字体或宽度变化不删除语义分析。工作台通过
`project_content_analysis.py` 把一个项目拆成逐行请求，单批并发最多 10；每行失败独立
落盘并继续其余行。内容分析状态不参与原有音频、4A、4B 或变体状态机，不得借分析失败
清空 MiniMax `raw_cues`、当前音视频指针或历史素材。

智能内容分析模块 6 将项目 schema 升级到版本 8，但不新增数据库列。字幕 JSON 增加
`semantic_mapping`；`semantic_subtitles.py` 负责严格原文复核和 MiniMax cue 锚点内的确定性
字符时间映射，`project_postprocess.py` 负责语义组真实字宽排版和失败降级。新音频素材的
metadata 保存脚本 SHA-256/长度；只有脚本、分析、音频和 raw cues 绑定四方一致时使用
`subtitle_units`，否则继续使用既有 raw cues 排版。任何路径都不得覆盖 `raw_cues`。

精确字幕时间校准由 `caption_alignment.py` 完成。FunASR 仅产生候选字词时间，随后必须与
原脚本 token 做顺序精确匹配；全局命中率至少 90%，每个 MiniMax raw cue 也必须通过局部
质量门。`project_postprocess.py` 先完成语义断句和真实字宽排版，再把最终 `render_cues`
重新绑定到 ASR 时间，并始终用 raw cue 作为硬边界。成功结果保存在
`subtitles.asr_alignment`，缓存键由脚本 SHA-256、音频素材 ID 和版本组成；不得把 ASR
识别文本作为字幕落盘，也不得覆盖 `raw_cues`。默认工作台要求精确对齐，服务故障或质量门
失败时标记 `REVIEW_REQUIRED`；只有测试或显式关闭配置允许旧插值路径。
完整请求仅在明确缺少字词时间戳时启用分块回退：先规范为 16k 单声道 PCM，再用 20 秒核心区
加前后 1 秒上下文识别；合并时按 token 中点归属非重叠核心区并恢复绝对时间。其他 HTTP、结构、
脚本命中率或 raw cue 错误不触发该回退。单行失败保存 `REVIEW_REQUIRED` 后继续同批其他行。

日志第一阶段将项目 schema 升级到版本 9，为 `project_operations` 增加独立
`correlation_id`。项目操作、云端声音批次、4A 画面生成和本地渲染都应传递该字段；
`idempotency_key` 只负责防重复提交，不得兼作关联号。历史操作以原 `operation_id` 回填。

Processor 日志位于 `data/logs/workbench.log`，本地渲染位于 `data/logs/render.log`，内嵌
Collector 位于 `data/logs/collector.log`，`server.log` 只保留启动和致命错误。独立 Collector
使用其状态目录下的 `logs/collector.log`；独立 Agent 使用
`%LOCALAPPDATA%/JianyingRenderAgent/logs/agent.log`。本地日志默认单文件 10 MB、保留 14 天，
写入前统一脱敏，不得记录访问令牌、API Key、完整脚本或完整请求体。

AuthCenterError 必须保留云端返回的业务正文，并用稳定错误码区分失败类型：
DIGITAL_HUMAN_REQUEST_REJECTED 表示 4xx 业务拒绝，不能伪装成断网；
DIGITAL_HUMAN_AUTH_EXPIRED / DIGITAL_HUMAN_FORBIDDEN 表示账号问题；
DIGITAL_HUMAN_CONNECTION_FAILED 和 DIGITAL_HUMAN_SERVER_UNAVAILABLE 才允许按
服务器暂时不可用处理。4A 状态轮询结束后，前端显示最近一次失败操作的真实正文；只有连接
失败或 5xx 使用“数字人服务器暂时不可用”标题。

`GET /api/new/projects/{project_id}/diagnostics` 仅允许项目所有者下载临时 ZIP。摘要不得包含
脚本文本、素材路径、操作 `payload/result` 或错误正文；日志仅从 14 天内的 `workbench.log`、
`render.log`、`collector.log` 及其轮转文件中选取与当前 `project_id`、`operation_id` 或
`correlation_id` 精确匹配的行，并在打包前再次脱敏。独立 Agent 的 `agent.log` 不在本机包内。

不要把以下数据混为一类：

- `data/libraries`：公共、长期保留、可随完整安装包分发。
- `data/personal_libraries`：某个运行实例采集的个人素材，更新包默认不会覆盖；需要迁移时复制整个目录。
- `data/web_storage`：运行状态，不应从开发机直接覆盖生产机。
- `runtime`：临时数据，通常不参与正式发布。

## 4. 开发环境

### 4.1 前置条件

- Windows 10 或 Windows 11。
- Python 3.11，当前开发机也可显式指定已有 Python。
- 已安装兼容版本剪映，并能正常手工打开草稿和导出视频。
- PowerShell 5.1 或更高版本。
- `vendor/jy-draftc/jy-draftc.exe` 存在。

安装运行依赖：

```powershell
python -m pip install -r .\requirements.txt
python -m pip install pytest
```

当前主要依赖包括 `pyJianYingDraft==0.3.0`、FastAPI、Uvicorn、FontTools、OpenCV 和 NumPy。

### 4.2 启动单机开发环境

在项目根目录执行：

```powershell
.\start_processor.ps1 `
  -Python "D:\Myanaconda\python.exe" `
  -ProcessingMode standalone `
  -ExecutionMode embedded
```

该正式源码入口默认连接 `https://video.lanyingjk01.com`，并与生产对口型入口
`http://127.0.0.1:8791` 成对。需要显式联调其他数字人服务或对口型地址时，使用
`-DigitalHumanServerUrl` 和 `-LtxWorkbenchUrl` 参数覆盖，不要把正式入口写到测试端口。

访问：

```text
工作台：http://127.0.0.1:8000/app
高级页面：http://127.0.0.1:8000/app/advanced
素材管理：http://127.0.0.1:8000/app/assets
接口文档：http://127.0.0.1:8000/docs
健康检查：http://127.0.0.1:8000/api/health
```

如需读取本机草稿、弹出文件夹选择器或采集个人素材，再开一个 PowerShell：

```powershell
.\start_collector.ps1 `
  -Python "D:\Myanaconda\python.exe" `
  -ServerUrl "http://127.0.0.1:8000"
```

默认端口是 `8765`。主页面会通过本地接口检测 Collector 是否在线。

### 4.3 前端开发

前端主体是原生 HTML、CSS 和 JavaScript，修改后刷新浏览器即可：

- `apps/processor/frontend/product.*`：普通用户工作台和批量任务主流程。
- `apps/processor/frontend/advanced.*`：高级任务页面。
- `apps/processor/frontend/assets.*`：素材和母版管理。
- `apps/processor/frontend/app.*`：旧版/通用页面逻辑，修改前先确认路由实际加载的脚本。

`apps/processor/frontend/new/` 的 Tailwind 与 Font Awesome 必须随工作台本地提供，运行时
不得依赖 CDN，否则断网或 CDN 不可达时会导致 `hidden`、布局和弹层样式整体失效。页面新增
或修改 Tailwind class 后，在项目根目录重新生成并提交 `tailwind.generated.css`：

```powershell
npx --yes tailwindcss@3.4.17 -c apps/processor/frontend/new/tailwind.config.cjs -i apps/processor/frontend/new/tailwind.input.css -o apps/processor/frontend/new/tailwind.generated.css --minify
```

Font Awesome 的 CSS 和字体位于 `apps/processor/frontend/new/vendor/fontawesome/`；打包与迁移
不得遗漏该目录。`tests/test_new_frontend.py` 会校验四个新版页面只引用本地关键样式、静态
路由可访问，并确保 `.woff2` 以 `font/woff2` 返回。

浏览器缓存导致代码未更新时，先使用 `Ctrl+F5` 强制刷新，再检查开发者工具 Network 中返回的 JS 是否为当前文件。不要通过复制同一份逻辑到多个页面解决缓存问题。

### 4.4 隔离测试环境

测试网站使用独立端口和独立数据副本，不修改正式 `data`：

```powershell
.\start_test_processor.ps1 -Python "D:\Myanaconda\python.exe"
.\start_test_collector.ps1 -Python "D:\Myanaconda\python.exe"
```

访问 `http://127.0.0.1:8001/app`。首次创建或需要重新从正式素材初始化时：

```powershell
.\start_test_processor.ps1 -ResetData
```

测试环境位于 `runtime/test_environment`。正式环境和测试环境仍会控制同一个本机剪映，不要同时提交真实导出任务。

## 5. 核心调用链

### 5.1 从网页提交到导出

1. 前端提交 `/api/render` 或 `/api/render/batch`。
2. `src/jyd_probe/web_api.py` 校验媒体、母版和素材引用。
3. 批量请求通过维度候选展开、去重和数量限制生成子任务。
4. `src/jyd_probe/task_store.py` 持久化批次及任务状态。
5. `RenderJobQueue` 顺序执行，或等待 Agent 领取。
6. `src/jyd_probe/render_job.py` 准备视频源或母版副本。
7. 按顺序应用字幕、文字、音频、特效、贴纸、画面套装和封面。
8. 保存草稿并调用剪映 UI 自动化导出 MP4。
9. 状态和输出写回 `data/web_storage`，前端轮询展示结果。

### 5.2 Render Job

稳定结构版本为 `jyd.render_job.v1`。主要入口：

```python
from jyd_probe.render_job import run_render_job, run_render_job_file

result = run_render_job_file("job.json")
print(result.as_dict())
```

命令行调试：

```powershell
D:\Myanaconda\python.exe .\tools\jobs\run_render_job.py `
  --job .\examples\render_job_video.example.json
```

完整字段说明见 `docs/RENDER_JOB_SCHEMA.md`。修改任务结构时应同时更新：

- `render_job.py` 的解析与校验。
- `web_api.py` 的媒体和素材引用解析。
- 前端任务构造逻辑。
- `examples` 中至少一个示例。
- 对应自动化测试和 `docs/RENDER_JOB_SCHEMA.md`。

### 5.3 草稿处理顺序

`render_job.py` 是业务编排入口，底层职责分散在以下模块：

| 模块 | 主要职责 |
| --- | --- |
| `draft_crypto.py` | 高版本草稿检测和解密 |
| `draft_factory.py` | 从单个 MP4 或按顺序排列的多个原始视频创建基础草稿 |
| `draft_transfer.py` | 草稿复制、重定位和元数据处理 |
| `draft_compat.py` | 剪映版本兼容字段处理 |
| `content_replace.py` | 视频、文字、音频和特效的基础修改 |
| `subtitles.py` | 长文本切分、SRT 生成和字幕导入 |
| `text_asset_apply.py` | 花字和复合文字素材应用 |
| `sticker_apply.py` | 全屏贴纸和四角贴纸应用 |
| `visual_variant.py` | 镜像、人物定位、裁剪和背景填充 |
| `cover_apply.py` | 封面帧、矩形和文字轨道 |

新增处理步骤时，要先确定它在时间线中的顺序以及是否会改变草稿总时长。不要在多个模块中重复修改同一轨道。

## 6. 素材库与母版

### 6.1 公共素材与个人素材

所有素材类型都应通过统一的清单或 bundle 结构被读取，避免运行时依赖原电脑的剪映缓存绝对路径。

```text
data/libraries/<kind>/              公共素材
data/personal_libraries/<kind>/     本机采集素材
```

常见 `<kind>`：

- `audio_library`
- `effect_library`
- `font_library`
- `sticker_library`
- `corner_sticker_library`
- `text_effect_library`
- `text_style_library`
- `text_template_library`

四角贴纸示例结构：

```text
corner_sticker_library/
  bundles/<素材标识>/...
  manifest/sticker_manifest.json
```

迁移个人素材时复制整个 `data/personal_libraries`，不能只复制资源文件而遗漏 manifest。完整安装包目前主要自动带入 `data/libraries`；运行后采集到 `personal_libraries` 的内容需要明确决定是否随交付包分发。

### 6.2 素材管理状态

素材管理支持重命名、分类、启用/停用、软删除和恢复。软删除不会立即物理清除文件，而是在元数据中标记为回收站状态；默认保留 7 天后由生命周期清理器删除。

涉及素材删除时必须使用 `asset_admin.py` 和现有 API，不要在前端直接拼路径删除文件。

### 6.3 母版

母版位于：

```text
data/template_library/<template_id>/
  draft/
  template_meta.json
```

母版可能来自高版本加密草稿。导入阶段负责解密、分析、复制依赖和生成元数据；生成任务应引用 `template_id`，不要重新读取用户原始草稿路径。

复合文字模板通常依赖人工排版，剪辑母版默认应保持原样，不能作为普通字体、BGM 或特效一样自动参与随机变化。

## 7. 配置与环境变量

源码启动脚本会设置大部分变量。常用变量如下：

| 变量 | 作用 | 常见默认值 |
| --- | --- | --- |
| `JYD_WEB_STORAGE_ROOT` | 任务、上传和输出根目录 | `data/web_storage` |
| `JYD_RESULT_LIBRARY_ROOT` | 新版最终变体成果归档根目录 | `D:\auto` |
| `JYD_DATABASE_PATH` | SQLite 控制库 | `data/web_storage/control.db` |
| `JYD_TEMPLATE_LIBRARY_ROOT` | 母版库 | `data/template_library` |
| `JYD_PERSONAL_LIBRARY_ROOT` | 个人素材根目录 | `data/personal_libraries` |
| `JYD_WEB_DRAFT_ROOT` | 实际剪映草稿目录 | 自动检测或手工指定 |
| `JYD_DRAFTC_EXE` | `jy-draftc.exe` 路径 | `vendor/jy-draftc` 或打包资源 |
| `JYD_EXECUTION_MODE` | `embedded` 或 `agent` | `embedded` |
| `JYD_ALLOW_LOCAL_FILE_ACCESS` | 是否允许网页引用本机路径 | 源码单机启动为 `true` |
| `JYD_AUTH_SERVER_URL` | 统一账户中心地址 | 由部署配置决定 |
| `JYD_AUTH_TIMEOUT_SECONDS` | 数字人网站普通 API 请求超时 | `15` |
| `JYD_SHARED_PROCESSOR_URL` | 公用工作台地址 | 空 |
| `JYD_AGENT_TOKEN` | Agent 接入令牌 | 空 |
| `JYD_MAX_ACTIVE_JOBS` | 单批最大任务数 | `500` |
| `JYD_MEDIA_RETENTION_HOURS` | 上传媒体保留时间 | `24` |
| `JYD_TEMPLATE_RETENTION_HOURS` | 临时母版保留时间 | `48` |
| `JYD_DRAFT_RETENTION_HOURS` | 生成草稿保留时间 | `48` |
| `JYD_OUTPUT_RETENTION_HOURS` | 成功输出保留时间 | `72` |
| `JYD_ASSET_TRASH_RETENTION_DAYS` | 素材回收站保留时间 | `7` |

发布包优先使用 `data/processor_config.json` 和 Windows 启动器保存的配置。开发环境不要把真实令牌、管理员密码或生产地址提交到源码。

### 7.1 数字人清晰片段契约

- `ProjectCompositionCoordinator.REMOTE_COMPOSITION_ACTIVE`、`PROJECT_ITEM_STATUSES`、
  `ACTIVE_ITEM_STATUSES`、前端进度和轮询集合必须同时包含 `VIDEO_ENHANCING`。
- 云端主视频下载已经是 SeedVR2 清晰片段。本地仍登记为 `original_video_segment`，因为它
  表示进入项目的原始有序分段；必须通过 metadata 区分 `seedvr2_upscaled`。
- `source_download_url` 只表示云端保留了数字人源片段，本地 4A 不自动下载该文件。
- 4B、字幕、BGM、变体与成果库继续消费工作台已落盘的清晰分段或 `base_video`，不得再次
  调用 SeedVR2。

### 7.2 RunningHub 取消后的阶段重建

- 工作台重试时把项目当前 `settings.digital_human.resolution` 传给云端，但该值只用于数字人
  阶段取消后的新数字人命令；它不决定取消发生在哪个阶段，也不触发 SeedVR2 回退。
- 数字人阶段取消：重新创建数字人 RunningHub 任务；SeedVR2 阶段取消：复用已保存数字人 MP4，
  只重新创建 SeedVR2 48G 任务。两种情况都不能复用被取消的外部任务 ID。
- 本地必须在云端接受请求后才把操作改为 `RUNNING`。远端返回 4xx/5xx 或抛出异常时，应立即把
  刚创建的操作改为 `FAILED/COMPOSITION_FAILED`，避免页面永久显示“完整成片生成中”。

### 7.3 RunningHub 双池费用确认与本地快照

- `/api/new/runninghub-execution-accounts` 只代理云端安全摘要。`same_account_v1` 沿用一组数字人
  ID；`dual_pool_v1` 必须同时展示并提交数字人、SeedVR2 两组非空内部 ID，不得接收或落盘 Key。
- `ProjectCompositionCoordinator` 在每个 `COMPOSITION_GENERATE` 操作中冻结 `execution_mode`、
  `runninghub_execution_account_ids` 和 `seedvr2_execution_account_ids`。同一幂等键改变任一项均
  拒绝；升级前缺少模式字段的操作按 `same_account_v1` 继续恢复。
- HTTP 请求只创建持久化 `PENDING` 行并快速返回，后台线程逐行交接。云端响应的权威模式若与
  本地快照不一致，该行失败；不得静默切分支或重提。重启恢复继续使用原两组快照和行级幂等键。
- 费用确认显式显示当前“一控多/双池”。云端 `composition.execution_assignments` 的安全逐分段
  摘要在启动与每次轮询时复制到 `COMPOSITION_GENERATE.result`；表格据此显示实际账号，未预留
  阶段显示“待分配”。账号名称仅用于操作定位，Key、指纹、Base URL 和 App ID 不进入本地。
- 账号清单中的 `account.balance` 是云端 `accountStatus` 缓存的安全摘要。前端只显示
  `remain_coins` 和缓存新鲜度；缺失时显示“RH 币未知”，不得自行调用 RunningHub、推算余额，
  也不得因为摘要未知而改变本次默认勾选。

## 8. API 与状态存储

开发时以 FastAPI 自动文档 `/docs` 为当前接口事实来源，专题说明见 `docs/WEB_API.md`。

主要 API 分组：

- `/api/auth/*`：用户登录和登录接力。
- `/api/admin/*`：账户、素材和测试批次管理。
- `/api/media/*`：视频和音频上传。
- `/api/draft-imports`、`/api/templates/*`：母版导入和读取。
- `/api/assets/*`、`/api/local-assets/*`：公共及个人素材。
- `/api/render`、`/api/render/batch`：单任务和批量任务。
- `/api/jobs/*`、`/api/batches/*`：状态、结果、重试、取消和下载。
- `/api/agents/*`：处理机注册、心跳、领取和回传。
- `/api/new/projects*`：新版统一项目、脚本行、素材版本、状态和可执行操作；`POST /{id}/items`
  采用追加语义，已有行进入生成后仍可新增独立草稿行，不得退化为整项目 `PUT inputs`。
- `/api/new/script-imports/preview`：严格解析两列 `.xlsx`/`.csv` 脚本。
- `/api/new/projects/{id}/images*`、`image-mapping`：项目图片池、逐行图片版本和后端分配策略；
  文件选择器本次返回的每个文件都创建新的项目图片记录，不按文件名或 SHA-256 跳过；删除已
  分配图片时对非运行行自动改用剩余图片。
- `/api/new/projects/{id}/image-mapping-scope`：把选中脚本行保存为本次人物图换图范围，空数组
  清除范围。范围保存于既有行级 `settings_json.image_mapping_target`，不升级 schema；范围非空时
  批量映射只处理范围内行，且可用 `image_ids` 限制为刚上传的一组项目内部图片 ID。
- `/api/new/voices*`、`/api/new/voice-creations*`：当前数字人账号的官方/自定义音色、试听和声音制作。
- `/api/new/voices/{id}/activate`、`DELETE /api/new/voices/{id}`：显式激活或移除自定义音色卡。
- `/api/new/projects/{id}/voice`：原子地把已保存音色设为项目默认值并应用到全部脚本行。
- `/api/new/projects/{id}/items/{item_id}/voice`：覆盖单个脚本行的音色。
- `/api/new/projects/{id}/audio*`：项目声音生成、状态同步、单行重试、试听和下载。
- `/api/new/h3/accounts`：读取安全 H3 执行账号摘要。
- `/api/new/projects/{id}/h3/settings`：H3 默认参数；历史 `identity_image_ids` 仅兼容读取，正式前端
  不再维护第二套人物图选择。
- `/api/new/projects/{id}/items/{item_id}/h3/reference-video`：逐行参考视频素材版本。
- `/api/new/projects/{id}/items/{item_id}/h3/overrides`：逐行 H3 差异参数。
- `/api/new/projects/{id}/h3/audio-review`：保留给兼容调用的音频锁定接口；正式前端不暴露
  独立按钮，`h3/prepare` 会在“生成视频”后自动锁定所选 MiniMax 音频，且不触发 H3 费用。
- `/api/new/projects/{id}/h3/prepare|confirm|status`：所选行 H3 费用快照、确认和结果回填；
  `/h3/segments/*` 提供段级重试、主动重生成、取消和下载。
- `/api/new/projects/{id}/ltx/state|generate|refresh`：同步 JYD 权威原稿与最新 MiniMax 音频，启动或
  刷新隐藏 LTX/SeedVR2 引擎；`PUT /items/{item_id}/ltx/source-video` 版本化上传源视频并只失效
  该行画面与后期，`POST /items/{item_id}/ltx/retry` 只重试该行失败引擎阶段。
- `/api/new/projects/{id}/composition*`：4A 画面启动、真实状态同步和失败阶段重试。
- `/api/new/projects/{id}/items/{item_id}/base-video`：下载当前标准化基础视频。
- `/api/new/postprocess/options`：返回实际可读的真实字体和现有 BGM 素材。
- `/api/new/projects/{id}/postprocess*`：4B 浏览器预览配方生成与状态查询。
- `/api/new/projects/{id}/items/{item_id}/postprocess/export`：用户明确下载时按需启动一次剪映导出。
- `GET/POST /api/new/projects/{id}/items/{item_id}/current-video`：下载当前视频或上传本地视频并切换版本。
- `GET /api/new/projects/{id}/videos/download`：一次性 ZIP 下载项目所有未变体当前成片。
- `/api/new/projects/{id}/items/{item_id}/original-materials`：下载单个原始片段或包含顺序清单的多片段 ZIP。
- `POST /api/new/projects/import-h3-handoff`：新交接导入 `h3.jyd_handoff.v2`，历史读取兼容 v1。
  v2 必须声明完整 H3 原生音画母版、`separate_h3_generated_audio` 分轨策略、H3 权威音频和
  `h3_segment_windows_then_funasr` 字幕来源。交接编号检查和项目、H3 权威音频、H3 分段字幕
  窗口、静音 `base_video`、外部关联写入必须位于同一个 `BEGIN IMMEDIATE` 事务；相同账号和
  `handoff_id` 的并发请求返回同一项目，任何中途异常整体回滚。输入 MiniMax 音频/raw cues
  只保留 provenance，不能进入 v2 最终播放时间线。
- 4B Coordinator 复用 `create_app()` 启动时已校验并计算摘要的语义视觉 catalog；生成、预览和
  导出不得对整套媒体重复做 SHA-256。素材 catalog 更新后通过重启服务加载新快照。

新版浏览器把同一项目拆成两个主工作页：`/app/new` 负责脚本导入、声音生成、试听与审核，
`/app/new/generate` 负责图片、逐行参考视频、画面参数、生成状态和结果检查。两页复用同一
`index.html` 和项目 API，以路径选择显示职责，不复制前端状态或数据库记录。成果库为
`/app/new/gallery`，声音中心为 `/app/new/voices`。这些页面均受普通站点会话保护；公开的 `/app/new/login` 调用现有
`/api/auth/login`，由工作台后端向数字人账号中心验证账号并把令牌保存在 HTTP-only
Cookie 中。前端只通过 `/api/auth/session` 读取用户摘要，通过 `/api/auth/logout`
退出，不得读取或保存数字人访问令牌。

任务和批次会同时涉及 SQLite 元数据及 `data/web_storage` 下的 JSON/媒体文件。调试数据异常前先停止服务并备份整个 `data/web_storage`，不要只复制或修改 `control.db`。

新版项目 API 只允许普通数字人账号访问，技术管理员会话不能代替普通账号成为项目
所有者。项目详情中的 `allowed_actions` 是页面按钮权限的唯一业务来源；前端不得根据
显示文本或本地定时器自行推进项目状态。新版页面已经完成登录、脚本/图片输入、声音和
画面 4A/4B 模块。声音编排由 `project_audio.py` 完成：工作台只把脚本、音色和语音参数提交
给数字人后端 MiniMax 批次能力，强制停在 `AWAITING_REVIEW`，下载音频和原始时间戳后
创建本地不可覆盖素材版本；声音阶段不上传图片。`project_composition.py` 只有在用户再次
确认费用后才上传该行当前图片，调用数字人后端把图片与已审核音频绑定并放行既有任务，
保存全部成功原始分段及标准化 `base_video`，并按真实后端状态驱动页面。
它不添加字幕/BGM、不创建最终 `composition_video`，也不生成变体。音频完成后到 4A
启动前，图片仍可替换；4A 上传的永远是提交时的当前图片。

H3 画面链由 `project_h3.py` 单独编排，入口位于 `/app/new/generate`。它只消费调用时显式
选择的 `item_ids`：读取每行最新 MiniMax 历史音频（即使当前播放音频已经是上一版 H3 权威
音频），在 `h3/prepare` 内自动把 `AWAITING_REVIEW` 锁定为本次输入；原稿使用当前音频元数据
哈希所绑定的 raw cues 文本，不再拿可后改的表格标点做远端严格比较。每行人物图直接读取
`inputs.image`，相同图片只上传一次，云端 row 合同只为该行提交一个
`reference_image_asset_ids`；图片的逐行循环或一图多行完全沿用现有 `image_mapping`。然后上传
该行冻结参考视频，把批次默认参数及 `settings.h3.overrides` 交给云端。正式前端只
保留声音预览和“生成视频”，不展示第二个审核操作；`h3/confirm` 仍只确认 H3 费用，不改写
声音版本。云端成功后，`project_h3_media.py` 保留并
合并每段 H3 原生音画、归零时间戳、拆出静音基础视频和 H3 权威音频，并以实际分段窗口调用
FunASR；回填只更新原 `ProjectItem`。未选行不继承批次状态，前端自动后期也只提交当前
`remote_batch_id` 对应且已回填的行。换人物图/批次参数使项目 H3 画面失效，换行级覆盖或
参考视频只使该行失效；待确认、排队或运行期间一律禁止修改输入或用新幂等键重复准备。

H3 权威音频和静音基础视频的 metadata 必须同时保存冻结原稿的 `script_sha256` 与
`script_length`。4B 只有在当前音频、raw cues、内容分析和原稿版本一致时才使用语义字幕单元；
缺少该绑定会退回粗粒度 H3 分段窗口，可能把“第一…、第二…”等相邻枚举项排进同一字幕。
历史 H3 项目在同步同一 `h3_segment_signature` 且远端分段能重建当前冻结原稿时，可原子补齐
这两个字段；修复只解除旧的派生成片引用并清空旧 `render_cues`，保留 H3 音视频、旧成片文件
和素材历史，随后重新生成本地 4B 预览，不得重新发起付费 H3 任务。

自 2026-08-25 起，`main` 的 `/app/new` 只展示“多参考”入口，新项目固定写入
`settings.generation_mode=minimax_h3_ref2va`；前端不再提供普通数字人和视频对口型切换控件，
面向用户的文案也不显示内部模型简称。三路前端完整快照保存在
`archive/three-generation-modes-20260825`。后端仍兼容读取历史项目中的
`runninghub_digital_human`、`minimax_h3_ref2va` 和 `ltx_lip_sync`，但只有多参考是 `main`
的正式入口。

历史视频对口型链仍由 `project_ltx.py` 读取当前原稿、最新 READY MiniMax 历史音频引用和每行源视频，并交给
本机回环 LTX 引擎；该引擎按 20 秒以内分段执行 LTX，再固定逐段执行 SeedVR2 48G。完成的基础
视频回填为 `source_type=ltx`，之后继续走与另外两条路线完全相同的字幕、BGM、语义视觉、剪映
模板、完整预览、变体和成果链。普通数字人的服务商临时 2 秒静音不能进入 LTX：LTX 始终使用
MiniMax 原音；从 H3 切回普通数字人或 LTX 时，`ProjectStore.set_generation_mode` 恢复最新
MiniMax 音频及其保存的 raw cues。替换 LTX 源视频只清除该行当前基础视频和后期结果；LTX 或
SeedVR2 运行期间，隐藏引擎拒绝原稿、声音版本和源视频变化，避免已计费任务与当前输入脱节。
源码联调必须从 `ltx_lip_sync_workbench/START-LOCAL.cmd` 统一启动；该入口生成一次性管理令牌并
同时传给 `8010` 与 `8791`。分别手工启动而没有共享令牌时，JYD 不创建内部引擎客户端，也不会
接受视频上传。
新配置默认 `continuity_mode=loop_anchor`、`generation_tail_seconds=0.1`。`loop_anchor` 要求目标行
已有图片分配；该图由云端固定映射为 `<Picture 1>` 并同时描述为每段首帧和尾帧，不得沿用普通
多图模式把参考视频抽帧插到第一槽。`fast` 与 `soft_chain` 仍是合法行级覆盖。
项目分辨率变更使既有基础视频失效时，`project_composition.py` 检测
`DIGITAL_HUMAN_RESOLUTION_CHANGED`。已有云端数字人源片段的行只调用
`AuthCenterClient.backfill_workbench_video_enhancement()`，不上传图片、不调用数字人启动接口；
云端对整行补建或重试 SeedVR2 48G 阶段。本地继续轮询同一 `remote_item_id`，下载清晰片段和
新基础视频后由 `ProjectStore` 清除失效原因。补跑操作的 `scope` 固定为
`seedvr2_backfill_only`，便于日志和费用审计。
若该行在数字人阶段被 RunningHub 手动取消、因而没有任何已下载源片段，则不能误走 SeedVR2
补跑：重新生成改为上传当前图片，并通过原声音关联调用画面启动接口；云端保留已审核音频，
按当前分辨率创建全新的数字人命令。失败行即使同时具备 start/retry 能力，前端批量分组也只
进入 retry 一次，防止同一行重复提交和重复计费。
`project_audio.py` 不提交本地硬编码的 RunningHub 提示词；行级未显式设置时由数字人网站
配置的默认提示词接管，避免工作台旧值截断服务器配置。

管理员首次 4A 启动前，前端使用统一账号会话的 `is_admin` 决定是否读取
`/api/new/runninghub-execution-accounts`。管理员每次费用弹窗按服务端默认列表重新全选，至少
选择一项，只把内部 ID放入 `runninghub_execution_account_ids`；普通用户请求不含该字段。
已有流水线的 retry/backfill 继续用云端锁定账号，不允许前端重新选择。

批量 4A 不得在请求线程逐行上传。`ProjectCompositionCoordinator.start()` 只校验并创建逐行
`PENDING` 操作；`ProjectCompositionStartDispatcher` 使用最多 4 个线程调用
`start_pending_operation()`。`ProjectStore.claim_pending_operation()` 以 SQLite 条件更新原子
认领为 `STARTING`，云端接受幂等请求后才转 `RUNNING`。重复轮询还受内存 scheduled set 去重，
但正确性不能只依赖内存。进程初始化调用 `recover_interrupted_composition_starts()`，只把
`STARTING` 恢复为 `PENDING`；登录令牌只作为内存参数，严禁写入 payload、结果或日志。
后台按 payload 的图片资产 ID读取历史版本并复核 SHA-256，不能改用行当前图片。云端 5xx
保留 PENDING 供原幂等键恢复，明确/本地错误只失败当前行。实际 RunningHub 容量仍由云端
Worker 控制，本地 4 线程不是账号并发配额。

当前批次的 4A 状态仍按 3 秒轮询。为了避免用户切换批次后旧项目永久保留本地 `RUNNING`，
新版页面使用同一已登录会话每 60 秒重新读取项目列表，并顺序调用其他活动批次既有的
`composition/status`。页面首次打开时立即执行一次补同步；关闭浏览器后不持久化访问令牌，
下次登录再恢复。该补同步只驱动既有幂等操作、查询云端终态和下载结果，不创建新的费用确认，
单个旧项目同步失败也不得打断当前项目的编辑与轮询。

`project_postprocess.py` 负责 4B：保存 MiniMax 原始 cues 不变，使用所选真实字体文件的
glyph advance 测量宽度，把过长文本在原 cue 时间内派生为连续的单行 render cues。
语义排版先修复过短逗号前缀，再把剩余软/硬标点边界视为不可跨越的分句边界；局部字宽
切分统一保护数字与量词表达式，并按词性硬保护动词后的结果/趋向补语（如“排/出”、
“拿/出来”“做/完”“改/掉”），避免出现语义未完成的字幕断裂。
普通 4B 先把 `base_video`、render cues、字体和 BGM 登记为浏览器预览配方，再向
`RenderJobQueue` 提交 `skip_export=true` 的草稿生成任务；只有草稿目录和
`draft_content.json` 均存在时才进入 `COMPOSITION_READY`。此阶段不编码 MP4。固定参数为居中、
画面宽度 `0.8`、`transform_y=-850/1920`
（剪映 1080×1920 参考位置 Y=-850）、`DouyinSansBold` 14 号、默认白字、黑色 `0.06` 描边。
BGM 不使用固定音量：`bgm_loudness.py` 通过 FFmpeg `loudnorm` 测量人声和曲目综合响度，目标
为普通音乐低于人声 11 dB、强人声音乐低于人声 15 dB；两者线性音量分别限制在
`0.08..0.25`、`0.05..0.16`，失败分别回退 `0.18`、`0.1136`。结果冻结到
`postprocess.bgm_volume` / `bgm_loudness`，浏览器预览、普通导出和变体共用；不接受前端人工音量参数。
4B 与变体的 BGM 任务固定 `align_to_end=true`、`crossfade_us=200000`。渲染器从正文视频结尾
向前规划源音乐：音乐更长时裁取尾部等长区间；音乐更短时最后一轮必须完整播放 `0..end`，
前面的轮次再从后向前补足，最早一轮允许只取音乐尾部。相邻轮次放到两条交替音轨并使用
0.2 秒淡入淡出重叠，最后一轮不淡出，确保停在素材自身自然结尾。浏览器动态预览必须镜像
同一反向计划，不能恢复为从 0 开始的 `% duration` 循环。
项目成片另用两个集中参数控制收尾：`BGM_FADE_IN_US=5000000` 传入
`audios[].fade_in_us`，让最早的 BGM 片段用 5 秒渐起；`VIDEO_FADE_OUT_US=2000000` 传入
`source.fade_out_us`，只给最后一个主视频片段添加 2 秒“渐隐”。普通 4B 与变体共用这两个
参数，后续调整时不得在草稿 JSON 写入层另设硬编码。
浏览器直接读取同一冻结样式，不得为溢出字幕临时缩字；
无法可靠排版时状态为 `REVIEW_REQUIRED`，不会静默显示溢出字幕。只有用户明确下载普通
成片时才调用 `postprocess/export`，并使用 `existing_draft` 对已冻结草稿执行 MP4 编码。正常路径不
重建字幕、BGM、封面和视觉时间线；升级前没有草稿结果的旧预览先提交独立 `skip_export=true`
草稿准备任务，准备成功后由客户端再次调用同一导出接口。唯一恢复例外是：剪映首页连续 5 次
返回 `DraftNotFound` 时，导出任务使用提交时一并冻结的完整重建配方创建一个新名称草稿，保留且
不覆盖旧草稿，再尝试识别 5 次；第二轮仍失败才停止。已知 UIA `COMError` 可重建控制器重试，
但不得误判成草稿丢失并触发时间线重建。后续变体必须把基础/上传视频与已
冻结的字幕、BGM 配方合并到同一个变体任务中一次导出，不能依赖一个预先导出的普通成片。
若该按需导出失败但冻结草稿、`base_video` 和 `PREVIEW_READY` 配方仍在，行级失败重试必须直接以新的
幂等键再次调用 `postprocess/export`；不得把全项目行重新提交给 `postprocess/generate`。
若 4A 返回多个 RunningHub 原始片段，浏览器预览使用已按音频时长标准化的 `base_video`；
4B 按需导出和模块 6 使用按 `video_index` 排序的 `video_sequence`，让剪映草稿保留真实分段。
每段目标时长来自原分段计划：素材过长裁尾，素材略短则对该段画面轻微放慢到目标时长；
所有片段原声静音，并从 0 写入一条完整、已审核的 MiniMax 音频。字幕仍直接使用 MiniMax
绝对时间戳，因此供应商 MP4 容器时长误差不会在后续片段中累计。相邻片段继续使用 250000
微秒剪映原生叠化，画面、权威语音、字幕、BGM 和封面共享同一绝对时间轴。

项目封面属于 4B 后处理冻结配方，不属于模块 6。`postprocess.cover_title` 保存两行、每行最多
5 字的文案；普通导出与所有变体统一调用 `build_project_cover()`。存在基础视频时按其冻结的
`input_image_sha256` 从当前图和 `asset_history.input_image` 回溯原图作为底图，
匹配原图缺失时拒绝生成可能错配的封面；没有冻结哈希的旧数据才使用当前上传人物图。
固定 3 帧、思源粗宋和受控视觉参数。变体请求不能自定义封面。标题为空时不生成占位封面。
`normalize_cover_title()` 是 AI 标题、人工保存、历史数据、浏览器预览、普通导出和变体共用的
输出安全门。结构或长度不合法仍按原契约报错；低风险体重管理词做自然中性改写，硬风险整组
回退为“生活提醒/理性看待”。不得用谐音字、近形字、emoji、拼音或符号实现审核规避。
浏览器动态预览在时间轴开头显示同一人物图、两行标题和固定视觉参数；“刷新动态预览”只
重算浏览器配方，不伪装成 MP4 导出。标题或后处理设置使旧成片指针失效时，页面明确显示
“旧成片已过期”，并提供“重新导出带封面 MP4”入口调用 `postprocess/export`。旧素材版本保留。
参数和后续统一内容分析返回契约见 [AI_TITLE_AND_COVER_20260810.md](AI_TITLE_AND_COVER_20260810.md)。
正文视频顶部与封面标题解耦：`build_top_title_texts()` 固定生成一行“世界冠军带你自律”，字号
19、Y=1535、红字白描边；历史 `top_title` 只保留接口兼容，不再影响浏览器预览或剪映导出。

建草稿前必须用真实画面时长裁剪所有定时视觉和来源文字。时长优先读取
`base_video.metadata.duration_us`，旧行回退原始分段结束时间，再回退当前音频或其绑定 raw cues；
起点已在画面外的项丢弃，跨越片尾的项裁短。不能用脚本文字估算时长直接写入草稿。
4A 基础视频下载完成后会用与剪映建草稿相同的媒体探测器覆盖 `duration_us`，并把云端分段计划值
保存在 `planned_duration_us` 供诊断。历史数据或容器帧取整仍可能与草稿相差不足一帧；仅新增文字
允许在 30fps 一帧（33334 微秒）内裁到实际片尾，更大的越界继续作为错误绑定失败。

`ProjectPostprocessCoordinator.sync()` 必须扫描全部仍为 `PENDING/RUNNING` 的 4B 操作，
不能只检查每行最新一条。更新操作时通过 `operation_id` 精确定位；被新尝试取代的旧操作只
回收自身终态，不覆盖当前行状态、字幕或成片指针。

新版页面把字幕效果卡直接放在表格“字幕样式”列，点击效果卡才打开字体和颜色配置；BGM
继续在相邻列直接选择。修改任一设置只把对应脚本行退回 `BASE_VIDEO_READY` 并保留
`base_video`、付费任务和历史成片。前端用同一个 `POST /postprocess/generate` 仅提交该行
`item_id`，即可重新派生字幕、刷新浏览器 BGM 预览并重建该行剪映草稿；服务端只处理请求中明确列出的脚本行。
批量工具栏的“刷新预览”复用同一契约：有勾选时使用选中行，否则使用当前批次全部
`base_video` 已存在且不在运行中的行。前端先逐行以 `force_retry=true` 失效旧 4B 配方，再用
一个 `/postprocess/generate` 请求提交明确的 `item_id` 列表，因此字幕断句、ASR 时间绑定、
自动 BGM 选择和封面会按当前代码重算，并重建可编辑剪映草稿，但不会调用 MiniMax、RunningHub
或编码 MP4。
同一工具栏的“下载视频”把目标行 ID 编码为 `GET /videos/download?item_ids=id1,id2`。后端校验
所有 ID 都属于当前项目并按项目行顺序打包；省略参数继续打包项目全部当前普通成片。
姿态或字幕样式保存提交 `preserve_auto_bgm=true`。当新旧模式都是 `auto` 时，Store 保留当前
`bgm_identity`、`music_selection`、`bgm_volume` 与 `bgm_loudness`，但仍使旧成片失效；批量刷新
预览不提交该标志，因此会按当前算法重新选择和测量音乐。
字幕效果卡固定显示“这是字幕预览”，不绑定脚本或 render cue。BGM 下拉框隐藏内部 `auto`
哨兵：自动 Top1 成功时直接显示解析后的具体曲目，尚无解析结果时显示“无音乐”；提交时仍
保留既有 `bgm_selection_mode=auto`。单行分析按钮在请求开始后立即切换为“AI 分析中”，成功
后直接保存并展示 Top1；手动曲目或手动无音乐不被覆盖。
每条表格任务在任务 ID 下提供 `DELETE /api/new/projects/{project_id}/items/{item_id}` 入口。
后端拒绝删除运行中或内容分析中的任务；其他状态删除时级联清理该行素材版本、操作、外部
关联和未被其他行引用的本地生成文件，再重新排列 `position`。任务可删到 0 行并通过“添加
分段”重新创建。共享图片池及其他任务不删除，前端必须先提示本地记录删除和第三方费用不可撤销。
从音频已就绪点击“生成完整成片”时，只对 RunningHub 费用确认一次，4A 完成后自动执行
4B 并进入视频预览，不再弹出字幕/BGM 二次确认。

模块 5 直接复用 `ProjectStore` 的素材版本和用户归属校验。上传视频以原始请求体写入当前
用户的项目目录，限制为 MP4/MOV/AVI/MKV/WebM 和 `JYD_MAX_VIDEO_UPLOAD_BYTES`；新增
`source_type=user_upload` 的 `composition_video` 并设为当前版本。`ProjectStore` 会保留
旧成片和 RunningHub 原始片段，同时解绑并失效原 MiniMax 字幕。原始素材下载按
`external_ref.video_index` 排序；单片段直接返回文件，多片段使用一次性 ZIP 并附加
`片段顺序清单.json`，响应结束后删除临时 ZIP。
底部“一键下载未变体视频”按用户选中的真实 `item_id` 集合工作，不以点击瞬间 DOM 的 ready
子集重新取样。前端先复用已有 MP4，等待选中行正在执行的草稿任务；旧预览则通过第一次
`POST /postprocess/export` 补建冻结草稿，完成后第二次调用只编码 MP4。每条目标必须进入成功
或失败清单，不能静默跳过。最终只把已有或本轮成功的普通成片通过项目级
`GET /videos/download` 打包；variant 素材不参与，临时 ZIP 在响应结束后删除，全部失败时不发空包。

`project_content_analysis.py` 负责新增智能内容分析模块 5。Excel/CSV 导入、添加分段和编辑
脚本只把相应快照置为 `NOT_REQUESTED`，不发起分析。用户点击“生成声音预览和脚本分析”时，前端在
提交声音任务的同时，对本批声音目标中 `NOT_REQUESTED` 的行调用
`POST /api/new/projects/{project_id}/content-analysis`；协调器为每个需要分析的

`ProjectItem` 单独调用数字人后端 `/api/workbench/content-analysis`，不会把多条脚本
拼成一个模型输入。脚本哈希未变化时，声音重生成不会重做文本分析；普通调用跳过已有
`PENDING`/`SUCCESS`/`PARTIAL`/`FAILED` 尝试，单行显式重试使用 `force_refresh=true`。
协调器在同一次请求中加入本地生成的 `visual_context`，只包含 catalog 版本、概念描述和
原文字符锚点，不含素材路径或时间。服务端响应在工作台再次核对脚本哈希、长度、三个分支
状态、字幕完整覆盖以及视觉 anchor/concept 后才落盘。刷新重试时，新失败不得覆盖同一脚本
此前已经成功的内容分支。项目批量调用最多并发 10 行，失败按行保存并继续。模块 5 不进行字符到时间轴映射或字体
排版；音乐分支成功后会调用 `ProjectMusicSelector.resolve_for_analysis` 产生不依赖音频时长的
初步 Top1，并通过 `save_item_auto_music_selection` 保存，但不会覆盖显式 manual 设置。

新版工作台把既有多项目能力暴露为批次选择器。`GET /api/new/projects?limit=100` 只用于
列出当前账号自己的项目，切换时再读取目标项目；“新建批次”只清空浏览器当前视图，原项目
及素材仍保留。`POST /api/new/projects/{project_id}/items/batch` 在一个事务内校验容量、已有
任务 ID 和批内重复项，再统一追加 `DRAFT` 行并沿用当前图片映射策略；任何一行失败时全部
回滚。追加表格不执行模型、MiniMax、RunningHub 或剪映操作。

配置表的文章类型筛选直接读取 `settings.source_metadata.article_type`，不复制分类字段；切换
筛选会清空旧勾选，表头全选、序号快速选择、刷新预览和下载视频都只作用于当前可见类型。
`DELETE /api/new/projects/{project_id}` 允许删除没有运行中操作的草稿、失败或完成批次，事务内
级联删除项目记录，并返回不再被其他项目引用的受管文件和成果目录候选。Web 层再次校验文件
必须位于工作台存储根目录，成果目录必须严格位于“成果根/日期/数字批次号”，才执行物理删除；
云端数字人任务和既有第三方费用不受影响。

表格的选择状态只保存在当前浏览器内存，并使用不可见的 `item_id` 调用现有子集接口；界面
可按显示序号、原 `row_key` 或数字范围快速建立选择。选中统一分析使用 `force_refresh=true`；
选中声音通过 audio `item_ids`；选中画面通过 composition `item_ids`，已有 `base_video` 的行
直接进入选中 4B 参数列表。图片、音频或执行条件不足的选中行会在提交付费请求前整体提示。
表格选择还可把所选行锁定为本次人物图换图范围，例如将第 11-30 行设为目标后，前 10 行不再
参与本批分配。人物图上传按当前表格选择分支：未勾选任何行时清除残留换图范围，继续按原有
全项目图片池规则分配给全部可编辑行；勾选行时则先把当前所选行保存为精确换图范围，再为文件
选择器返回的每个本地文件创建新图片，并只把这些新 `image_ids` 交给该范围的批量映射。后端从
目标范围第 1 行重新计数并保存本批图片 ID，刷新或修改 count/loop 时不会混入此前图片池。

`semantic_subtitles.py` 负责智能内容分析模块 6。工作台再次拒绝带大模型时间字段、未连续
覆盖原文或语义属性非法的 `subtitle_units`；MiniMax cue 文本允许省略原文空格/换行，但
所有非空白字符必须精确一致，`~` 不作通配符。每条 cue 的真实 `start_us/end_us` 是唯一
时间锚点，cue 内字符时间只做确定性比例派生。Prompt v19 及更早的普通断点进入排版时仍是软偏好；
本地只把高置信的“类别/问题/评价 → 答案”和较长编号项提升为强语义边界，允许它们在整句
未超宽时增加一条字幕。Prompt v20+ 的字幕已经过云端原文、长度和安全词边界硬校验；v23
默认仍使用最少断点，但三个以上同构并列项允许多一个结构断点。其 `prefer` 断点全部作为
不可跨越边界，本地只能在单个语义段实际超宽时继续细分。旧模型偏好不能仅为节奏增加字幕数量，避免旧分析产生
“第一｜脂肪”或“世界冠军｜张雒”一类短碎片。`project_postprocess.py` 先按每条 raw cue、
段落和除顿号外的显式标点建立不可跨越子句，
再用 `jieba==0.42.1` 的确定性词典分词（`HMM=False`）、词性、数字单位、结构助词和真实字体
宽度对每个子句做全局排版。脚本、分析、当前音频脚本摘要或 raw cues 音频绑定不一致，以及
映射/排版失败时，4B 记录
`semantic_mapping.status=FALLBACK` 并调用原有 `layout_one_line_captions`；raw cues 永久保留。
本模块不执行音乐 Top1。

站姿字幕 `clip_scale` 从 `1.351709192276617` 降为 `1.32`，保持 80% 画面安全宽度，
使当前生产字体的一行容量由约 9.7 个全宽汉字提升到稳定 10 个。Prompt v19 及更早模型返回的
多余断点在不超宽子句内仍可删除，超宽子句只遍历通用
分词允许的字符位置，并以模型断点作为小权重偏好；结构助词不得位于行首/行尾，动词与
结果/趋向补语之间属于硬禁切边界，量词、连接词
和名词组合使用通用语法罚分；完整名词主语后接“已经/正在”等副词引导的谓语时，优先在
主谓之间断开，并禁止把副词单独留在上一字幕末尾。短标点片段不得跨普通逗号或句号强制吞并后文，任何重新分配的
时间都必须留在当前子句和 raw cue 范围内。不能为了行宽均衡拆成“情｜绪”“弯｜路”
“四十｜多”“破罐子｜破摔”或让一条字幕同时包含相邻 raw cues。

`project_music.py` 负责智能内容分析模块 7。内容分析完成时先从当前行已校验的
`music_intent` 和本地 `music_profiles.v1.json` 返回可见的初步唯一 Top1；同一项目批量处理时
按脚本行顺序传递 `recent_identity_counts`，在语义评分之后施加确定性的已使用次数惩罚，
让分数接近的合格曲目适度轮换。4B 自动模式按相同项目计数加入当前 MiniMax 音频真实时长
复核并保存最终 `jyd.project-music-selection.v1` 快照，不保存候选列表或 Top3。声音版本变化
保留已选 identity 并标记 `STALE`，避免界面退回“无音乐”，4B
会按新音频时长刷新绑定。音乐分支失败按项目默认音乐或无 BGM 降级；手动曲目及手动无 BGM
始终优先。变体只冻结继承 4B 最终 BGM。

智能内容分析模块 8 的跨项目验收位于数字人项目
`tests/test_content_analysis_workbench_integration.py`。它直接把数字人服务端
`analyze_content` 的实际响应传入本项目 `_validated_remote_result`、
`map_subtitle_units_to_raw_cues` 和 `MusicProfileMatcher`，覆盖双成功、两种部分成功、安全
索引重算、空格、换行和 `~`。新增测试 `3 passed`；最终完整 mock 回归为数字人
`216 passed`、本项目 `260 passed`。本轮没有真实第三方请求或生产变更。

新版 `/app/new` 不再提供模块 6 的变体生成入口；原表格三列与底部按钮改为 H3 原始分段人工
检查入口。弹层使用响应式换行网格，桌面端每行五张、窄屏依次降为四/三/二/一张，只允许
纵向滚动；视频使用本地已下载的 H3 原始音画且 `preload=metadata`。成功但主观不合格的分段
复用 H3 主动重生成，失败分段复用失败重试，均沿用现有费用确认与历史 attempt 保护。
`ProjectH3Coordinator.sync()` 在保存云端快照前按成功分段增量下载，单次最多三路并发；缓存键绑定
批次、远端 item 和 `segment_id`，结果版本再绑定云端标准化视频 SHA-256 与完成时间。状态响应中的
`local_preview_ready` / `local_preview_is_current` 只描述本机缓存，不能替代云端任务状态。重生成期间
稳定的 `current.mp4` 保留上一版本，新版本校验后原子切换；只有全部当前版本缓存齐全才调用
`prepare_h3_media()` 合并和登记 `base_video`，不得在最终阶段重复下载整行。
`project_variants.py` 和既有 API 暂时作为历史项目兼容能力保留，不删除历史 `variant_video`。

历史 `project_variants.py` 模块推荐设置启用视频特效、全屏贴纸和画面变化套装，组合
选择使用确定性的加权 maximin，而不是随机抽样：裁剪比例、视频特效、全屏贴纸和四角贴纸
的权重大于背景色，并把已有成功签名作为补充生成的距离参照。每行冻结基础视频（用户上传
视频则冻结上传版本）、模块 4B 的 render cues/字体/BGM、项目固定封面和素材身份；项目级生成
可合并为一次 `submit_batch`，行级生成则只提交指定 `item_id`，不会先导出普通
`composition_video`。封面固定 3 帧，封面
视频片段并入主视频轨道首段，临时视频轨道随后删除；底层统一后移所有正文轨道。操作类型为
`VARIANT_GENERATE`、`VARIANT_SUPPLEMENT` 和
`VARIANT_RETRY`；成功文件保存为不可覆盖的 `variant_video`，失败项可原样重试。

`project_results.py` 负责模块 7 的物理归档和成果查询。`project_script_sources` 保存用户原始
XLSX/CSV 的版本与校验信息；`project_result_batches` 使用 SQLite 当日计数器原子分配
`D:\auto\月.日\批次号`。脚本文件先复制到批次目录，模块 6 的 MP4 随后直接输出到该目录。
成果页不把目录扫描结果当作用户归属，而是按 `owner_user_id` 查询项目/素材/剪映批次索引，
再检查 `managed_path` 是否真实存在。这样手工移动或删除文件会显示为缺失，但不会跨账号泄露。

新版表格采用版本化修改而不是完成后永久锁定。非运行中脚本行可以随时修改：脚本或音色
清空当前音频/基础视频/成片指针并回到 `DRAFT`；图片只清空基础视频和成片指针并保留当前
音频；BGM 或字幕设置只清空当前成片并保留基础视频。历史资产、操作和外部链接不删除。
声音总按钮在存在待生成行时只处理待生成行；全部行均已有音频时再次点击会为全部行创建
新的声音批次和音频素材版本。同一秒创建的多批外部链接按数据库插入顺序选择最新版本。

核心工作台只能选择数字人账号中已经保存的音色。项目默认音色由后端统一写入全部脚本
行，前端逐行下拉框只负责展示和提交单行覆盖。声音中心的克隆/融合是两阶段流程：先生成
可试听结果，用户试听确认后再保存。保存后的自定义音色仍是 `READY`，必须由用户二次
确认激活，后端执行第一次正式 TTS 并切换到 `ACTIVE` 后才能进入核心工作台。删除音色卡
只从可用音色库移除，不破坏历史任务和历史音频；当前项目仍引用时拒绝删除。

生成语速保存在 `project_user_preferences.voice_settings_json.speed`，范围为 `0.5–2.0`，
默认 `1.0`，前端滑杆步长为 `0.01`。核心工作台默认声音区负责读写该偏好；批量、选中、单行新生成和单行重新生成
都必须提交同一份 `voice_settings`。调整语速不使已有素材失效，只有明确发起下一次付费
MiniMax 生成时才生效；切换默认音色不重置语速。

## 9. 自动化测试

运行全部不依赖真实剪映导出的测试：

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python -m pytest -q
```

运行单个测试文件：

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python -m pytest -q .\tests\test_visual_variant.py
```

建议按修改范围选择测试：

- Render Job 或草稿结构：`test_mother_draft_rendering.py`、`test_draft_*`。
- 批量组合：`test_batch_dimensions.py`、`test_web_batch_naming.py`。
- 画面、贴纸、封面：`test_visual_variant.py`、`test_sticker_library.py`、`test_cover_apply.py`。
- 素材管理：`test_asset_admin.py`、`test_personal_asset_management.py`。
- 多处理机：`test_multi_processor_api.py`。
- 前端批量流程：`test_batch_editor_frontend.py`、`test_batch_result_center.py`。

自动化测试不能代替真实剪映回归。涉及 UI 导出、剪映版本兼容、缓存资源或字体渲染的修改，最终必须在目标剪映版本上完成一次真实 MP4 闭环。

## 10. Windows 打包与发布

### 10.1 初始化构建环境

```powershell
.\scripts\setup_build_environment.ps1 `
  -Python "D:\Myanaconda\python.exe"
```

构建缓存位于项目同级 `.jyd-build-cache`，不属于源码。

### 10.2 常用构建命令

```powershell
# 本机完整工作台、采集器和 Agent
.\build_release.ps1

# 当前单机交付所需包
.\build_deployment.ps1

# 只构建公用处理机
.\build_shared_processor.ps1

# 完整可迁移源码包，可选同时重建发布包
.\build_portable_project.ps1 -BuildReleases
```

底层单独构建 Processor：

```powershell
.\scripts\build\build_processor.ps1 -CompressionLevel Fastest
.\scripts\build\build_processor.ps1 -DeploymentMode shared -CompressionLevel Fastest
.\scripts\build\build_processor.ps1 -UpdateOnly -CompressionLevel Fastest
```

`UpdateOnly` 是纯程序更新包，只携带重新构建的 Processor、内嵌前端、工具和更新说明；
构建脚本会清除复用 dist 中残留的 `data` 并在压缩前做硬断言。它不会携带或删除目标机的
语义素材、音乐、字体、模板、账户、任务、配置、数据库、个人素材库或 ASR 运行时。
公共素材首次交付使用完整包；素材增量应使用独立、可审核的素材包。

完整 Processor 构建会复制 `data/libraries` 中受支持的公共素材和 `data/template_library`。运行实例后来采集的 `data/personal_libraries` 默认属于实例数据，交付前如需预装，必须显式复制并验证 manifest 和 bundle 都存在。

### 10.3 发布前检查

1. 运行相关自动化测试。
2. 启动全新的测试数据目录，确认页面和 API 能打开。
3. 检查 ZIP 根目录包含 EXE、`_internal`、`tools`、说明文件和预期的 `data`。
4. 检查 `jy-draftc.exe` 已进入 `tools`。
5. 检查公共素材、个人素材和母版是否符合本次发布范围。
6. 在非开发电脑上解压运行，验证不依赖开发机 Python。
7. 最后执行一次真实草稿生成和 MP4 导出。

## 11. 常见扩展方法

### 11.1 新增一种素材类型

1. 定义素材目录和 manifest/bundle 格式。
2. 编写或扩展 `tools/library` 下的采集工具。
3. 在 Web API 的素材列表、引用校验和个人素材导入中注册类型。
4. 在 `asset_admin.py` 注册管理类型，确保启停、软删除和恢复一致。
5. 在 Render Job 中实现应用逻辑。
6. 增加前端选择和批量维度构造。
7. 更新打包脚本的素材复制列表。
8. 增加提取、API、应用和打包覆盖测试。

遗漏第 4 或第 7 步通常会导致“网页可以采集，但管理接口报不支持”或“开发机可用，安装包缺素材”。

### 11.2 新增一个可排列组合元素

组合维度由 `web_api.py` 的批量展开逻辑处理。候选项应包含稳定 `id`、简短 `label`，以及用于覆盖任务的 `patch` 或追加数组的 `append`。

修改时需要保证：

- 在生成草稿前完成筛选，不能先生成大量无用草稿再删除。
- 同一批内组合不重复。
- 任务数不超过服务端上限。
- 固定、参与组合、不使用三种模式语义一致。
- 组合名称简短但任务 ID 保持唯一。
- 批量失败不阻断已成功结果的预览和下载。

### 11.3 新增一个画面处理步骤

先决定处理对象是视频素材、轨道片段还是整个画布，再确定与镜像、裁剪、贴纸、字幕和封面的执行顺序。时间轴切段会影响后续片段索引，优先让一个模块统一完成切段并返回稳定结果。

## 12. 调试与故障定位

### 后端返回 500

先看启动 Processor 的控制台 traceback，再根据请求路径定位 `web_api.py` 对应路由。不要只根据前端的 `Internal Server Error` 修改 UI。

### 网页显示旧版本

确认启动的是源码目录还是旧发布包；使用 `Ctrl+F5`，并在 Network 中检查脚本响应。发布包必须重新构建，修改源码不会自动改变已解压的 EXE。

### 本地采集器已连接但处理机离线

Collector 和 Render Agent 是两个不同角色。Collector 在线只表示网页可以读取本机文件；是否能执行任务取决于 Processor 使用 `embedded`，或 `agent` 模式下是否有 Agent 注册并持续心跳。

### 开发环境有素材，发布包没有

依次检查：

1. 素材位于 `data/libraries` 还是运行实例的 `data/personal_libraries`。
2. 构建是否使用了 `UpdateOnly`。
3. manifest 和 bundles 是否一起复制。
4. 页面当前连接的是本机工作台还是另一台公用处理机。

### 剪映导出失败

确认剪映已安装、桌面未锁屏、没有遮挡导出对话框、草稿目录正确、素材路径有效。自动化找到
草稿但单击后没有进入编辑页时，会重新创建控制器、聚焦剪映并重试点击；最终错误明确报告
“点击草稿后未进入编辑页”，不再误报为普通的“找不到导出按钮”。保留失败任务的 job JSON、
草稿副本、Processor 日志和剪映版本号，这四项是定位兼容问题的最低信息集合。

## 13. 开发约定

- 先读现有模块和测试，再扩展已有模式，避免重新创建平行实现。
- 草稿 JSON 使用结构化读写，不使用大段字符串替换。
- 公共路径通过 `runtime_paths.py` 和环境变量解析，不在核心模块硬编码开发机盘符。
- 临时文件必须进入受管理目录，并明确到期清理规则。
- 永久素材和一次性视频必须分开存储。
- 修改 API、任务 schema、素材格式或部署步骤时同步更新文档和测试。
- 不覆盖用户已有的 `data`，程序更新优先使用 UpdateOnly 包。
- 涉及用户本地文件的接口只在明确允许本地访问的单机模式启用。

## 14. 专题文档索引

| 主题 | 文档 |
| --- | --- |
| 项目目录 | `docs/PROJECT_LAYOUT.md` |
| 当前状态 | `docs/PROJECT_STATUS.md` |
| Web API | `docs/WEB_API.md` |
| Render Job | `docs/RENDER_JOB_SCHEMA.md` |
| 本地采集器 | `docs/LOCAL_COLLECTOR.md` |
| 多处理机 | `docs/MULTI_PROCESSOR.md` |
| 处理机部署 | `docs/PROCESSOR_DEPLOYMENT.md` |
| 公用机快速部署 | `docs/SHARED_PROCESSOR_QUICK_START.md` |
| 程序更新 | `docs/PROCESSOR_UPDATE.md` |
| 快速打包 | `docs/FAST_BUILD.md` |
| 母版导入分析 | `docs/DRAFT_IMPORT_ANALYZER.md` |
| 音乐库 | `docs/AUDIO_LIBRARY.md` |
| 特效库 | `docs/EFFECT_LIBRARY.md` |
| 字体库 | `docs/FONT_LIBRARY.md` |
| 贴纸库 | `docs/STICKER_LIBRARY.md` |
| 花字库 | `docs/FLOWER_TEXT_LIBRARY.md` |
| 复合文字模板 | `docs/TEXT_TEMPLATE_LIBRARY.md` |
| 语音标点停顿配方（跨项目、待开发） | `D:\工作内容\轻盈健\数字人\语音标点停顿配方开发文档.md` |

遇到文档与代码不一致时，以当前测试、FastAPI `/docs` 和实际入口代码为准，并在修复代码的同一个改动中更新文档。

## 15. 新版工作台 2026-08-05 细节修正

- 4A 通过数字人工作台接口启动时使用 `exact_timestamps` 内部模式。云端把口播分段限制为
  32.8 秒，并仅在 RunningHub 临时上传音频中为每段追加 2 秒静音；工作台同步的已审核音频、
  raw cues 和字幕时间轴不变。原始分段元数据携带 `speech_duration_seconds` 与
  `generation_tail_seconds`：剪映主轨裁掉中间段生成尾，只在最后一段保留生成尾供 2 秒渐隐。
- `/api/new/postprocess/options` 返回 `default_font_identity`；当前默认是
  `resource_id:7244518590332801592`（`DouyinSansBold`）。前端新配置以此初始化，历史行的
  `settings.postprocess.font_identity` 优先级更高。
- 成果库首页只渲染批次缩略卡，批次弹层才渲染全部视频卡；选择状态使用真实变体
  `asset_id`，支持当前查询结果的总全选、单批次全选、ZIP 下载和删除选中。删除接口先对
  整批 ID 做账号归属校验，再原子删除数据库记录和对应受管导出文件。
- 核心工作台脚本列采用固定表格布局和 `overflow-wrap:anywhere`，编辑区最大高度内纵向
  滚动，避免长文本改变其他列宽度。
- 核心工作台左侧输入区可通过表格标题栏按钮收起；状态保存在本机
  `localStorage`，收起时右侧表格跨满工作区。表头类 `table-header-input` 使用低饱和深色
  底配靛蓝标线和圆点表示输入/操作列，`table-header-output` 使用深青色底配青绿标线和
  圆点表示三个预览输出列；标题栏显示对应图例，只改变表头，不改变正文单元格状态配色。

## 16. 语义视觉图片与视频（2026-08-10）

- `semantic_visuals.py` 负责受控目录校验、内容哈希版本、最长别名召回、复合词排除、稳定字符
  候选、FunASR 优先/MiniMax 回退时间映射、素材选择和密度规则；本地路径从不发送到云端。
- `project_content_analysis.py` 通过既有 `/api/workbench/content-analysis` 一次取得音乐、字幕
  和 selected-only `visual_plan`；`unified_visual_plan.py` 只把合法 anchor 映射回本地候选。
  项目 Web 主流程不再调用 `/api/workbench/visual-analysis`，旧客户端方法和独立协调器仅用于
  兼容测试及迁移期读取。
- 每行 `visual_analysis` 保存原始三字段计划、兼容决策、映射状态和最终 `recipe`。用户保存后条目标记
  `selection_mode=manual`；重新分析只能保留并尊重锁定人工项。
- MiniMax raw cues 尚未产生时允许先保存语义计划并将 mapping 标为失败；时间轴到达或变化后，
  `project_audio.py` 只重新执行本地字符时间映射和冻结配方，不再次调用 Ark。连续空格或换行
  被 MiniMax 省略时，每个原文字符仍保留；同一空白间隙内的后续字符压为零时长锚点，保证
  时间单调。服务启动后及运行中每分钟检查一次内容/视觉分析状态，`PENDING` 超过 15 分钟
  标记为中断并恢复显式重试权限。
- 启动时使用当前 catalog 扫描已有成功视觉计划。旧 anchor/concept 仍属于新候选集合时，
  `unified_visual_plan.py` 直接用新 catalog 重选素材、重绑 raw cues 并冻结配方；不兼容时清空
  自动计划并标记 `VISUAL_CATALOG_CHANGED`，等待用户显式重试。两条路径都不得请求 Ark。
- 浏览器播放预览和 `project_postprocess.py` / `project_variants.py` 的 4B 冻结任务读取同一
  `mixed` 配方。`image_apply.py` 写入真实 photo 轨道，`video_overlay_apply.py` 写入原生 video
  material/segment，支持源片截取、静音、cover/contain；单项失败按 optional 跳过。语义视频
  始终只播放一次，源片不足目标区间时按剩余可用时长提前结束。
- 自动贴图先把命中关键词扩展到它所在的标点分句（逗号、句号、问号、感叹号、分号、冒号
  或换行），再优先使用当前音频绑定的 FunASR 字词时间取得该分句的真实开始和结束；未完成
  ASR 时使用 MiniMax raw cue 字符插值回退。最终单行 `render_cues` 生成后，自动明确语义贴图
  的开始时间再吸附到包含关键词的最终字幕起点，结束时间仍不早于原句段结尾。手工锁定项、
  泛氛围 `seam_broll` 和列举速切不参与吸附；成片尾部仍会裁切超出的显示区间。
  同句至少两个入选语义且相邻项目由顿号连接时，整个句段按关键词语音中心点顺序速切，单项
  不做 2 秒保底。相邻分句只要求时间不重叠；每 60 秒全部自动视觉最多 24 条，同 concept 按
  `semantic_overlay/full_screen_broll` 展示角色分别保留 20 秒密度冷却，同一 `asset_id` 仍为
  整条成片最多自动使用一次。边缘重叠不超过 0.5 秒时裁短或顺延新项，超过后仍整项跳过。后处理得到精确
  ASR 后会只在本地重绑
  未锁定自动配方，不再次调用 Ark。未人工锁定的自动项在预览和渲染时
  刷新素材库当前默认资源、位置、缩放和透明度，人工/锁定项保持冻结值。
- `semantic_visual_library/fixed/nameplate_standing` 和 `nameplate_seated` 是每条视频自动携带的
  两套固定人名板。渲染任务通过 `fixed_overlays` 写入剪映原生贴纸轨道并覆盖完整正文时长；
  站姿/坐姿的原始贴纸缩放、旋转、位置及三层文字参数由 `layout_profiles.py` 分别冻结，避免
  透明方形 PNG 按照片缩放造成底板与文字错位。
- 项目独立 MiniMax 语音统一带 `fit_to_video=true`；渲染入口按源草稿主视频时长同时裁切语音
  和 `duration_us=0` 的固定贴层，不能再以音频文件的原生编码时长反向延长成片。
  字幕仍为最高层。`layer_order` 统一保证
  `下方图片/小窗视频 < 固定人名牌 < 全屏 B-roll < 字幕`；全屏 B-roll 自然覆盖人名牌，不生成
  隐藏和恢复状态。浏览器使用同一层级和鉴权视频内容接口。
- 新版表格把“语义视觉”保持在 BGM、字幕的配置区域，并将“单条生成”移到最右侧；审核
  弹窗的“移除本行”只修改当前行配方，不删除素材库文件。全局图库新增、停用和物理删除
  保护规则见 `docs/SEMANTIC_VISUAL_LIBRARY.md`。
- catalog v3 严格按用途选材：普通句只接受 `semantic_overlay/action_demo/knowledge_card`，
  顿号速切只接受 `list_quick_cut`，通用空镜只接受 `full_screen_broll`；拼接点只接受明确带
  `seam_broll` 用途的连接处素材，不得把仅有 `full_screen_broll/enrichment` 的普通空镜并入候选。
  同一素材同时带 `seam_broll` 和其他用途时仍可用于拼接点。v2 继续兼容
  `空镜/相关素材/b-roll/enrichment` tags。v3 的
  `semantic_roles.related` 是非自动关系，不能作为空镜开关。锚点输入显式携带
  `usage=enrichment/seam_broll` 和所在短语上下文；自动空镜必须是脚本直接召回的同一具体对象、
  动作或明确场景并返回 priority 2。priority 1、编辑型氛围及分类宽回退只供审核。通用空镜由
  `VISUAL_BROLL_TARGET_INTERVAL_SECONDS=10` 控制约每 10 秒一次的目标尝试；
  本地在目标点附近只提交确有获准素材支撑的相关短句，普通空镜之间仍至少留 6 秒空窗；接缝
  空镜不重置这个间隔。该值是
  尝试间隔而非配额，匹配不到即保留数字人口播。普通全屏空镜先于显式小窗占位；本地按精确
  动作、对象或明确场景的直接概念选材，不再自动进入编辑型空镜池或宽分类回退。
  首轮显式语义和通用空镜仍在统一内容分析的一次模型调用内完成；数字人真实分段尚未生成，
  因此接缝候选不能在首轮可靠产生。
- `project_video_source.py` 从当前 `source_task_ids` 绑定的最新原始数字人分段读取下一段脚本，并用
  与剪映建草稿相同的媒体探测器读取每个实际 MP4 时长，按使用顺序累计真实边界；下载新分段时
  同时冻结 `actual_duration_us`，历史分段在使用时现场探测。`POST .../postprocess/generate` 在 4B
  冻结配方前自动调用一次轻量视觉接口，只提交新增的
  `seam_broll` 候选，不重跑音乐、标题、字幕或普通空镜，也不覆盖首轮视觉方案。候选集合摘要
  成功落盘后重复 4B 不再请求；旧多段项目重刷 4B 会自动补齐。候选上下文固定为上一段末句加
  下一段首句，不得向下一段后续句子扩张；云端只允许 `direct_concept_ids` 中同一具体对象、动作
  或明确场景的强匹配。本地同时拒绝编辑型原因码、低于 0.90 的置信度和非直接概念；没有通过时
  不再生成任何本地氛围兜底。4B 在 ASR/raw cues
  已就绪后的本地重映射阶段把边界传给统一配方。接缝有对应未用视频时生成覆盖真实边界前后各
  0.5 秒的 `seam_broll`，否则不新增 overlay；底层 `video_sequence` 和 250ms 溶解始终
  保留。补分析失败或没有达到语义门槛时继续 4B 并保留原溶解。配方先登记手工项，再按接缝、
  通用全屏空镜、显式语义的顺序占位和更新
  `used_asset_ids`。普通语义画面和普通空镜均直接使用对应说话短句的真实时间，不再补足到 2 秒；
  与接缝碰撞时也只能在原句段的前后剩余区间内裁短，不能移到无关台词。原句段没有空间才跳过。
  视频源短于冻结目标区间时，浏览器预览和渲染器都会让该 overlay 提前结束，
  不循环也不定格补足；该规则同时覆盖自动、人工锁定和历史冻结配方，旧
  `loop_to_target=true` 在 API 保存、配方消费及渲染任务三层都会被强制关闭。
- 工作台加载器同时支持严格 catalog v2 和完整 catalog v3。v3 强制
  `concept_ids == auto_trigger_concept_ids`，自动关系只能来自互斥的 depicts/expresses，且每项
  必须给出 `trigger_basis`；`auto_eligible=false` 的概念不会进入模型候选或本地选材。未知或
  受限授权的素材不得自动全屏。迁移使用 `semantic_visual_migration.py` 校验源库、备份和候选
  SHA-256，并提供哈希保护的原子 apply/rollback；manifest 默认 `approval.status=pending`，只有
  人工填写批准人、批准时间并改为 `approved` 后才能 apply。
- 默认库现有 191 张图片和 19 条视频。首批人工审片素材按图片像素指纹和视频 SHA-256 去重；
  原有胯下击掌与 42.766341 秒腹部核心源片只合并概念/标签，没有重复复制。腹部核心源片仍
  通过 `source_start_us=12000000` 截取 5 秒全屏 B-roll，且未导入 `爆款动作.mp4`。
- 完整候选池逐条审核并完成视频分层后的本地 catalog 现为 1378 个资产、921 个概念；新增审核
  资产包含 432 条视频和 737 张图片。451 条视频保存 `video_taxonomy`，927 张图片不含该字段。
- 以 `SEMANTIC_VISUAL_LIBRARY.md` 为权威合同：视频标签分 L1 领域、L2 类别、L3 精确，
  并另存动作、场景事实。图片只能 L3 精确触发；L1 永不自动触发；视频先走 L3，缺少精确
  视频时，普通空镜和接缝空镜才可使用人工批准的 L2、动作或场景回退。
- L2 回退必须由显式白名单关系声明，禁止根据 concept ID 前缀或任意父概念自动扩散；
  `nutrition.protein` 之类抽象营养概念不得回退成鱼、肉、鸡蛋等具体素材。没有合格视频时
  保留数字人口播或原接缝。食物、菜品、饮品的 L2 仅用于视频归档，不做同类自动替换。本地
  catalog、选择器与回归已更新，生产环境尚未部署。
- 人工验收后的口播小窗统一使用 `bottom_center`：语义图片默认宽度 56%，动作视频默认宽度
  61.5%，水平中心为画面中轴。高素材最多显示下方约 37% 并允许底边裁出；全屏 B-roll 规则不变。
- 本机需要自动操作剪映界面时，固定使用桌面“剪映专业版6.01破”对应的 6.0.1 独立程序，
  不使用普通“剪映专业版”入口指向的 8.9 版本；用户正在操作电脑时不得抢占界面。

## 17. 语音标点停顿配方（方案已确认，尚未实施）

- 权威方案位于 `D:\工作内容\轻盈健\数字人\语音标点停顿配方开发文档.md`。它是独立的
  本地确定性语音配方，不复用内容分析或语义配图的大模型结果。
- 工作台负责解析人工控制语法、保存原始脚本和紧凑覆盖、显示生成前预检；数字人服务端
  负责按同一规则版本复核并编译 MiniMax 专用标记。
- 普通空格不代表停顿；真实换行参与规则，页面自动折行不参与。字幕、字数、内容分析和
  语义配图始终读取不含控制标记的原始脚本。
- 当前 `project_audio.py` 仍提交原始 `script_text`。在项目 schema、API 契约、幂等摘要和
  跨项目测试完成前，不得把本节写成已经上线的能力。
