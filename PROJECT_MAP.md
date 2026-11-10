# 第一版项目地图

本文件用于快速回答两个问题：第一版代码放在哪里，以及某类任务应该先读哪份文档。
它只做导航，不代替代码、数据库迁移或专项开发文档。

忘记代码位置时看第 3、4 节；忘记应该读哪份文档时看第 5 节；不知道任务需要哪些前置资料时，
回到 `AGENTS.md` 的“按任务补读”。本文件只维护当前地图，不在这里追加日期式修改记录。

## 1. 项目范围

- 当前仓库 `runninghub_mvp` 保留第一版数字人网站外壳和 RunningHub 生成服务；当前唯一继续维护的
  画面生成路线是 MiniMax H3。
- 普通数字人和 LTX 对口型路线已经废弃，相关路由、适配器、迁移和数据只作为历史兼容与追溯保留；
  除非用户明确要求处理历史问题，不再进入新功能、上传优化、服务迁移、发布或验收范围。
- 它同时保留给 JYD/剪映工作台使用的云端接口，但不包含第二版 FlashCut 客户端源码。
- 本地 Web 默认端口为 `8000`；生产由 Nginx 代理到本机 `18083`。
- 主体技术栈是 FastAPI、SQLAlchemy、SQLite、Jinja2，以及三个独立 Worker。

## 2. 从哪里启动

| 目的 | 入口 |
| --- | --- |
| 日常启动全部本地服务 | `启动系统.cmd` |
| 日常停止全部本地服务 | `停止系统.cmd` |
| 单独启动 Web | `python -m scripts.serve_web` |
| 单独启动语音 Worker | `python -m app.workers.audio_worker` |
| 单独启动媒体 Worker | `python -m app.workers.media_worker` |
| 单独启动视频 Worker | `python -m app.workers.task_worker` |
| 创建管理员 | `python -m scripts.create_admin` |
| 执行数据库迁移 | `python -m alembic upgrade head` |
| 运行测试 | `python -m pytest -q` |

## 3. 代码目录总览

```text
runninghub_mvp/
├─ app/                    网站和后台任务的主体代码
│  ├─ main.py              FastAPI 装配、中间件、路由注册、健康检查
│  ├─ config.py            环境变量、目录和生产安全配置
│  ├─ database.py          SQLAlchemy 引擎、Session 和 SQLite 设置
│  ├─ models.py            全部数据库模型和状态枚举
│  ├─ routes/              网页/API 入口、权限和请求响应组装
│  ├─ services/            可复用业务规则和第三方客户端
│  ├─ workers/             语音、媒体、视频三个持久 Worker
│  ├─ workflows/           RunningHub 工作流适配器
│  ├─ templates/           Jinja2 页面
│  └─ static/              浏览器 JS、CSS 和导入模板
├─ alembic/versions/       只增不改的数据库迁移链
├─ scripts/                本地启动、清理、管理员和专项诊断脚本
├─ media_node/             Windows 远程媒体节点及内置 ASR
├─ deploy/                 Nginx、systemd、备份、恢复和发布工具
├─ tests/                  全 mock 自动化测试
└─ data/                   数据库、上传、输出和日志等运行数据，不入库
```

## 4. 按功能找代码

### 网站入口与页面

| 要修改的内容 | 主要位置 |
| --- | --- |
| 应用启动、中间件、路由注册 | `app/main.py` |
| 登录、退出、会话 | `app/routes/auth.py`、`app/services/security.py` |
| 管理员用户和配置页 | `app/routes/admin.py` |
| 任务列表、详情、取消、重试 | `app/routes/tasks.py`、`app/services/task_*.py` |
| 批量生成、审核、拼接、下载 | `app/routes/batches.py`、`app/services/batch_*.py` |
| 长音频项目 | `app/routes/long_audio.py`、`app/services/long_audio.py` |
| 老网站多机位入口 | `app/routes/multi_camera.py`、`app/services/multi_camera.py` |
| 声音管理页 | `app/routes/voices.py`、`app/services/speech/` |
| 运行状态与日志页面 | `app/routes/operations.py`、`app/services/logging_config.py` |
| HTML 页面 | `app/templates/` |
| 页面脚本和样式 | `app/static/` |

### 第三方生成与后台任务

| 要修改的内容 | 主要位置 |
| --- | --- |
| RunningHub HTTP 客户端 | `app/services/runninghub.py` |
| 上传、尝试记录、余额 | `app/services/runninghub_uploads.py`、`runninghub_attempts.py`、`runninghub_balance.py` |
| 执行账号池和派发 | `app/services/runninghub_pool.py`、`runninghub_dispatch.py`、`runninghub_dual_pool.py` |
| SeedVR2 账号池和清晰化 | `app/services/seedvr2_pool.py`、`seedvr2_dispatch.py`、`video_enhancement.py` |
| MiniMax TTS、音色和异步结果 | `app/services/speech/`、`app/services/audio.py` |
| ASR 和原稿时间轴对齐 | `app/services/alignment/` |
| 豆包 Ark 请求治理 | `app/services/ark_request_manager.py`、`ark_operation_ledger.py` |
| 内容分析 | `app/services/content_analysis/` |
| 视觉分析 | `app/services/visual_analysis/` |
| 语音队列执行 | `app/workers/audio_worker.py` |
| 长音频调度 | `app/workers/media_worker.py` |
| RunningHub/SeedVR2 执行 | `app/workers/task_worker.py` |

### 工作流与工作台接口

| 要修改的内容 | 主要位置 |
| --- | --- |
| 工作流注册 | `app/workflows/registry.py` |
| 普通数字人适配器（已废弃，仅历史兼容） | `app/workflows/digital_human.py` |
| LTX 对口型适配器（已废弃，仅历史兼容） | `app/workflows/ltx_lip_sync.py` |
| MiniMax H3 适配器（当前有效路线） | `app/workflows/h3_ref2va.py` |
| SeedVR2 适配器 | `app/workflows/seedvr2_upscale.py` |
| JYD 通用工作台接口 | `app/routes/workbench.py` |
| JYD LTX 接口（已废弃，仅历史兼容） | `app/routes/workbench_ltx.py`、`app/services/ltx_workbench.py` |
| JYD H3 接口（当前有效路线） | `app/routes/workbench_h3.py`、`app/services/h3_workbench.py`、`app/services/h3/` |
| 工作台登录令牌 | `app/services/workbench_auth.py` |
| 工作台设备授权 | `app/routes/workbench_device_*.py`、`app/services/device_auth/` |
| 远程媒体节点 API | `app/routes/media_worker_api.py` |

### 数据、部署和测试

| 要修改的内容 | 主要位置 |
| --- | --- |
| 数据表和状态枚举 | `app/models.py` |
| 私有 OSS 素材、分片会话与签名 API（未接付费任务） | `app/services/media_uploads.py`、`app/services/media_oss.py`、`app/routes/media_assets.py` |
| OSS 云端核验任务、租约/回执与单次 FC 执行入口（默认关闭） | `app/services/media_verification.py`、`app/routes/cloud_media_jobs.py`、`app/cloud_media_verifier.py` |
| 数据库结构升级 | `alembic/versions/`，必须新增迁移 |
| Windows 媒体处理机 | `media_node/worker.py`、`media_node/launcher.py` |
| 本机 ASR 服务 | `media_node/asr_service/app.py` |
| 本地多进程启停 | `scripts/local_services.py` |
| Ubuntu 生产发布 | `deploy/` |
| 自动化验证 | `tests/`，通常按被改模块找同名测试 |

## 5. 文档地图

### 日常入口

- `../HANDOFF.md`：当前跨项目实施基线和未完成边界。
- `README.md`：产品能力、启动方法和使用说明。
- `PROJECT_MAP.md`：代码位置与文档路由，也就是本文件。
- `DEVELOPER_GUIDE.md`：当前架构、状态机、配置、迁移和发布规则。
- `MAINTENANCE.md`：模块边界、修改红线和检查清单。
- `CHANGELOG.md`：按时间记录已经发生的变化。
- `PROJECT_STATUS.md`：阶段快照和历史交接，不作为当前事实的最高来源。

### 专项文档

- `docs/architecture.md`：媒体链路现状与目标拓扑。
- `docs/data_and_api.md`：OSS 素材模型、状态和分片 API 合同。
- `docs/business_rules.md`：素材幂等、费用前校验和归档原则。
- `docs/troubleshooting.md`：媒体链路错误码和排查边界。
- `docs/development_runbook.md`：离线测试与隔离联调要求。
- `数字人网站与剪映工作台集成说明.md`：第一版服务与 JYD 工作台之间的产品和接口边界。
- `工作台设备授权与免重复激活开发文档.md`：设备身份、授权、执行准入和发布保护的技术依据。
- `工作台设备授权操作手册.md`：给管理员和使用者看的实际操作步骤。
- `工作台请求风暴与页面卡顿修复开发文档.md`：认证请求治理、历史任务分页和页面轮询。
- `SeedVR2视频清晰化流程开发文档.md`：清晰化状态机、费用和重试边界。
- `多机位图片单音频数字人流程开发文档.md`：老网站多机位受控入口。
- `语义视觉素材库与智能编排开发文档.md`：内容分析与视觉调度。
- `语义视觉素材统一协议设计.md`：catalog、asset 和 recipe 协议。
- `食物语义贴图素材库开发文档.md`：食物贴图素材接入方案。
- `WORKFLOW_EXTENSION.md`：新增 RunningHub 工作流的最短步骤。
- `deploy/README.md`：生产发布、备份和恢复。
- `media_node/README.md`：固定 Windows 媒体节点的安装与分发。

跨项目的语音停顿、LTX 和 H3 文档位于工作区根目录或相邻项目，具体路径由 `AGENTS.md` 路由。

个人技能入口（不属于网站代码）：
- `C:/Users/san/.codex/skills/h3-prompt-writing/SKILL.md`：H3 提示词及同目录参考资料，已从工作区整体迁出。
- `C:/Users/san/.codex/skills/koubo-edit/SKILL.md`：口播精剪；其 `HANDOFF.md` 与 `references/` 维护工作基线、剪辑方法、案例与回归清单。

## 6. 文档维护规则

1. 模块增加、删除、改名或移动代码位置时，同一次修改必须更新本文件。
2. 当前架构或流程改变时，更新对应模块开发文档；跨模块结论再同步到 `DEVELOPER_GUIDE.md`。
3. 有产品、接口、架构或运维影响的变化写入 `CHANGELOG.md`；历史过程不要堆到模块文档或
   `README.md` 顶部。
4. 新增专项 Markdown 时，同一次修改必须把路径补入本文件；如果它会影响任务准备，再补入
   `AGENTS.md` 的任务路由。
5. `README.md` 保持为使用入口，`PROJECT_STATUS.md` 只保留交接快照。
6. 文档与实现冲突时，按代码、迁移、测试、现行开发文档、历史记录的顺序核对。
