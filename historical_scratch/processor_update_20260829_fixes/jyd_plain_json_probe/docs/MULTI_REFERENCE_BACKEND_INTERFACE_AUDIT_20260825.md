# 单一“多参考”入口后端接口梳理

> 审计日期：2026-08-25  
> 适用仓库：`jyd_plain_json_probe`、`runninghub_mvp`、`ltx_lip_sync_workbench`  
> 当前结论：本文只做接口盘点和拆除规划，不删除接口、不迁移历史数据、不部署云端。

## 1. 目标与术语

新版 JYD 主界面只保留“多参考”这一条生成路线。它在代码和接口中的内部标识仍然是：

```text
generation_mode = minimax_h3_ref2va
接口路径片段 = /h3/
```

内部标识暂时不改名。它们已经进入本地项目数据、云端任务、幂等键、状态机和测试合同，强行重命名
只会增加迁移风险；用户可见页面继续只显示“多参考”。

后续希望从本地正式包中断开的两条路线是：

```text
runninghub_digital_human  # 普通数字人
ltx_lip_sync              # 视频对口型
```

本文把接口分为四类：

1. 多参考专属：必须保留；
2. 三路线共享：必须保留；
3. 普通数字人专属：本地可逐步断开，但不能直接误删共享能力；
4. 对口型专属：本地可逐步断开，同时会影响 8791 隐藏引擎、启动器和更新包。

## 2. 当前真实架构

```text
浏览器
  -> JYD 本地服务 127.0.0.1:8010
       -> 多参考协调器 ProjectH3Coordinator
       -> 云端 https://video.lanyingjk01.com/api/workbench/h3-*
       -> RunningHub / MiniMax / 云端 Worker
       -> JYD 下载多参考分段并生成本地基础视频、权威音频和字幕
       -> JYD 本地字幕、BGM、语义视觉、剪映模板、变体和成果库

历史对口型链路：
浏览器 -> JYD 8010 -> LtxWorkbenchClient -> 本机隐藏引擎 8791
       -> 云端 /api/workbench/ltx-* -> RunningHub LTX/SeedVR2

历史普通数字人链路：
浏览器 -> JYD 8010 -> ProjectCompositionCoordinator
       -> 云端 /api/workbench/audio-batches/.../composition
       -> RunningHub 数字人/SeedVR2
```

### 当前不能直接删后端的原因

前端入口已经收口，但前端源码尚未完成物理清理：

- 仍有 `/ltx/state`、`/ltx/generate`、`/ltx/refresh`、源视频上传和重试函数；
- 仍有 `/composition/generate`、`/composition/status`、普通数字人失败重试和 SeedVR2 补跑函数；
- 项目列表/项目恢复逻辑仍引用 `/composition/status`；
- `generation-mode` 后端仍接受三种模式；
- JYD 启动时仍创建 `LtxWorkbenchClient` 和普通数字人的后台启动调度器；
- 统一启动器仍启动 8010 和 8791，代码更新包仍编译并携带 LTX 引擎。

因此，安全顺序必须是“先证明多参考页面不再发旧请求，再停用本地写接口，最后才移除引擎和打包
依赖”。

## 3. JYD 本地 8010：多参考专属接口（保留）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/new/projects/import-h3-handoff` | 兼容导入既有多参考交接结果 |
| `GET` | `/api/new/h3/accounts` | 读取多参考执行账号安全摘要 |
| `PUT` | `/api/new/projects/{project_id}/h3/settings` | 保存人物参考图和项目级默认参数 |
| `PATCH` | `/api/new/projects/{project_id}/items/{item_id}/h3/overrides` | 保存单行参数覆盖 |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/h3/reference-video` | 上传单行参考视频 |
| `POST` | `/api/new/projects/{project_id}/h3/audio-review` | 将当前 MiniMax 声音版本确认为多参考输入 |
| `POST` | `/api/new/projects/{project_id}/h3/prepare` | 冻结输入、计算分段和费用，不启动付费任务 |
| `POST` | `/api/new/projects/{project_id}/h3/confirm` | 确认费用并启动多参考任务 |
| `GET` | `/api/new/projects/{project_id}/h3/status` | 同步云端状态、下载结果并回填本地项目 |
| `POST` | `/api/new/projects/{project_id}/h3/segments/{segment_id}/regeneration/prepare` | 计算主动重生成影响范围和费用 |
| `POST` | `/api/new/projects/{project_id}/h3/segments/{segment_id}/regeneration/confirm` | 确认主动重生成 |
| `POST` | `/api/new/projects/{project_id}/h3/segments/{segment_id}/retry/prepare` | 计算失败阶段重试范围和费用 |
| `POST` | `/api/new/projects/{project_id}/h3/segments/{segment_id}/retry/confirm` | 确认失败阶段重试 |
| `POST` | `/api/new/projects/{project_id}/h3/segments/{segment_id}/cancel` | 取消指定多参考分段 |

主要实现：

- `src/jyd_probe/web_api.py`
- `src/jyd_probe/project_h3.py`
- `src/jyd_probe/project_h3_media.py`
- `src/jyd_probe/auth_center.py`
- `src/jyd_probe/project_store.py`

## 4. JYD 本地 8010：共享接口（不能随旧路线删除）

### 4.1 登录、项目与脚本

保留以下接口族：

```text
/api/auth/*
/api/new/projects
/api/new/projects/{project_id}
/api/new/projects/{project_id}/items*
/api/new/script-template
/api/new/script-imports/preview
/api/new/projects/{project_id}/metadata-import
/api/new/projects/{project_id}/script-source
/api/new/projects/{project_id}/inputs
```

### 4.2 人物参考图

下面这些路径名字没有 `h3`，但多参考仍在使用，不能当作普通数字人残留删除：

```text
POST   /api/new/projects/{project_id}/images
GET    /api/new/projects/{project_id}/images/{image_id}
DELETE /api/new/projects/{project_id}/images/{image_id}
PUT    /api/new/projects/{project_id}/image-mapping
PUT    /api/new/projects/{project_id}/image-mapping-scope
PUT    /api/new/projects/{project_id}/items/{item_id}/image
```

多参考的 `identity_image_ids` 引用的就是项目图片资产。

### 4.3 声音

MiniMax 声音仍然是多参考的条件输入，以下接口全部保留：

```text
/api/new/voices*
/api/new/voice-creations*
/api/new/projects/{project_id}/voice
/api/new/projects/{project_id}/items/{item_id}/voice
/api/new/projects/{project_id}/audio/generate
/api/new/projects/{project_id}/audio/status
/api/new/projects/{project_id}/items/{item_id}/audio/retry
/api/new/projects/{project_id}/items/{item_id}/audio
/api/new/projects/{project_id}/audios/download
```

`project_audio.py` 中部分历史关系名仍叫 `digital_human_audio_batch`、
`digital_human_audio_item`。它们实际上承载共享的 MiniMax 音频版本，当前不能按名字删除。

### 4.4 后期、预览、变体和成果

多参考生成基础音画后仍依赖：

```text
/api/new/projects/{project_id}/items/{item_id}/base-video
/api/new/postprocess/options
/api/new/projects/{project_id}/postprocess/*
/api/new/projects/{project_id}/items/{item_id}/postprocess/*
/api/new/variant-options
/api/new/projects/{project_id}/variant-settings
/api/new/projects/{project_id}/variants/*
/api/new/projects/{project_id}/items/{item_id}/variants/*
/api/new/projects/{project_id}/items/{item_id}/current-video
/api/new/projects/{project_id}/videos/download
/api/new/gallery*
/api/new/semantic-visuals/*
/api/new/projects/{project_id}/content-analysis
/api/new/projects/{project_id}/visual-analysis
/api/new/jianying-templates*
```

`base-video` 是共享的本地读取接口，不等于普通数字人专属接口。

## 5. JYD 本地 8010：普通数字人专属接口（候选断开）

| 方法 | 路径 | 当前作用 | 风险 |
| --- | --- | --- | --- |
| `PUT` | `/api/new/projects/{project_id}/digital-human-settings` | 保存普通数字人分辨率 | 前端还保留隐藏兼容字段和函数 |
| `GET` | `/api/new/runninghub-execution-accounts` | 普通数字人/SeedVR2 账号池 | 与多参考的 `/api/new/h3/accounts` 不是同一接口 |
| `POST` | `/api/new/projects/{project_id}/composition/generate` | 创建普通数字人 4A 操作 | 前端源码仍有多处引用 |
| `GET` | `/api/new/projects/{project_id}/composition/status` | 恢复/轮询普通数字人任务 | 当前项目恢复逻辑仍会调用，必须先改前端 |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/composition/retry` | 重试普通数字人失败阶段 | 前端源码仍有引用 |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/composition/seedvr2-backfill` | 历史数字人补跑 SeedVR2 | 多参考不使用 |

对应实现：

```text
src/jyd_probe/project_composition.py
src/jyd_probe/web_api.py 中 ProjectCompositionCoordinator 路由
src/jyd_probe/auth_center.py 中普通 composition 客户端方法
```

以下三条是“云端数字人任务收件箱/导入”接口，不属于多参考生成主链，但也不应和 4A 接口一起
盲删。只有确认本地以后完全不需要导入网站历史数字人任务时，才能单独停用：

```text
GET  /api/digital-human/tasks
POST /api/digital-human/tasks/{item_id}/import
GET  /api/digital-human/tasks/{item_id}/videos/{video_index}
```

## 6. JYD 本地 8010：对口型专属接口（候选断开）

```text
GET  /api/new/projects/{project_id}/ltx/state
PUT  /api/new/projects/{project_id}/items/{item_id}/ltx/source-video
POST /api/new/projects/{project_id}/ltx/generate
POST /api/new/projects/{project_id}/ltx/refresh
POST /api/new/projects/{project_id}/items/{item_id}/ltx/retry
```

同时存在以下非业务主链依赖：

```text
/api/auth/handoff-to?target=ltx
WebApiSettings.ltx_workbench_url
JYD_LTX_WORKBENCH_URL
processor_config.json: ltx_workbench_url
app.state.ltx_workbench_client
```

对应实现：

```text
src/jyd_probe/project_ltx.py
src/jyd_probe/web_api.py 中 LTX 路由和客户端初始化
apps/processor/processor_windows.py 中 JYD_LTX_WORKBENCH_URL
apps/processor/processor_config.example.json
```

## 7. 本机 8791 隐藏对口型引擎接口（整组候选移除）

JYD 目前通过管理令牌和数字人 Bearer Token 调用以下回环接口：

```text
POST /api/integrations/jyd/projects/{external_project_id}/sync
POST /api/integrations/jyd/projects/{external_project_id}/state
PUT  /api/integrations/jyd/projects/{external_project_id}/items/{external_item_id}/source-video
POST /api/integrations/jyd/projects/{external_project_id}/start
POST /api/integrations/jyd/projects/{external_project_id}/refresh
POST /api/integrations/jyd/projects/{external_project_id}/items/{external_item_id}/retry
GET  /api/integrations/jyd/projects/{external_project_id}/items/{external_item_id}/base-video
```

8791 还保留自己的旧页面、项目、声音、生成、渲染和变体接口，但主 JYD 只依赖上面的
`/api/integrations/jyd/*` 桥接族。只要 JYD 确认不再接对口型，整个 8791 程序都可以从正式本地包
中退出，而不必逐条删除它自己的页面 API。

## 8. 云端 RunningHub：多参考专属接口（保留）

JYD 的 `AuthCenterClient` 实际调用：

```text
POST /api/workbench/h3-execution-accounts
POST /api/workbench/h3-audio-sources/approve
POST /api/workbench/h3-batches/prepare
POST /api/workbench/h3-batches/{batch_id}/confirm
POST /api/workbench/h3-batches/{batch_id}
POST /api/workbench/h3-segments/{segment_id}/regeneration/prepare
POST /api/workbench/h3-segments/{segment_id}/regeneration/confirm
POST /api/workbench/h3-segments/{segment_id}/retry/prepare
POST /api/workbench/h3-segments/{segment_id}/retry/confirm
POST /api/workbench/h3-segments/{segment_id}/cancel
GET  /api/workbench/h3-segments/{segment_id}/video
```

云端还提供以下多参考素材读取接口，应作为同一接口族保留：

```text
POST /api/workbench/h3-audio-sources
GET  /api/workbench/h3-items/{item_id}/raw-cues
GET  /api/workbench/h3-items/{item_id}/audio
```

实现位置：

```text
runninghub_mvp/app/routes/workbench_h3.py
runninghub_mvp/app/services/h3_workbench.py
```

## 9. 云端 RunningHub：共享接口（保留）

多参考仍依赖以下云端能力：

```text
/api/auth/center/login
/api/auth/center/verify
/api/auth/center/handoff*
/api/workbench/voices*
/api/workbench/voice-creations*
/api/workbench/batch-assets
/api/workbench/audio-batches
/api/workbench/audio-batches/{batch_id}
/api/workbench/audio-batches/{batch_id}/items/{item_id}/audio
/api/workbench/audio-batches/{batch_id}/items/{item_id}/retry
/api/workbench/content-analysis
/api/workbench/visual-analysis
```

特别注意：`batch-assets` 同时承担多参考图片、参考视频等暂存上传，不能因为旧路线也曾使用就删除。

## 10. 云端 RunningHub：普通数字人接口（本地可不调用，云端先保留）

```text
POST /api/workbench/runninghub-execution-accounts
POST /api/workbench/runninghub-dual-pool-accounts
POST /api/workbench/audio-batches/{batch_id}/items/{item_id}/composition
POST /api/workbench/tasks/{item_id}/composition/retry
POST /api/workbench/tasks/{item_id}/enhancement/backfill
GET  /api/workbench/tasks/{item_id}/base-video
POST /api/workbench/tasks
POST /api/workbench/tasks/{item_id}
GET  /api/workbench/tasks/{item_id}/videos/{video_index}
GET  /api/workbench/tasks/{item_id}/videos/{video_index}/source
```

建议当前阶段只让 JYD 不再调用，不从 `video.lanyingjk01.com` 删除。原因：

- 云端网站本身仍有普通数字人页面和历史任务；
- 旧版工作台、三路归档分支或其他客户端可能继续调用；
- 普通数字人、SeedVR2 与多参考共用部分任务模型、Worker 和 RunningHub 基础设施；
- 删除云端路由不等于删除模型，反而容易造成历史查询和取消/恢复失败。

## 11. 云端 RunningHub：对口型接口（本地可不调用，云端先保留）

对口型工作台专属接口族：

```text
POST /api/workbench/ltx-batches/validate
POST /api/workbench/ltx-batches
POST /api/workbench/ltx-batches/{batch_id}
POST /api/workbench/ltx-items/{item_id}
POST /api/workbench/ltx-items/{item_id}/retry
POST /api/workbench/ltx-items/{item_id}/cancel
POST /api/workbench/ltx-items/{item_id}/segments/{segment_index}/retry
POST /api/workbench/ltx-items/{item_id}/segments/{segment_index}/cancel
POST /api/workbench/ltx-segments/{segment_id}/retry
GET  /api/workbench/ltx-items/{item_id}/segments/{segment_index}/video
GET  /api/workbench/ltx-items/{item_id}/segments/{segment_index}/source-video
GET  /api/workbench/ltx-items/{item_id}/base-video
```

实现位置：

```text
runninghub_mvp/app/routes/workbench_ltx.py
runninghub_mvp/app/services/ltx_workbench.py
```

云端旧网站的 `/api/tasks/ltx-lip-sync`、长音频对口型及 Worker 不是 JYD 单入口改造的删除范围。
当前阶段同样只停止本地调用，不删云端接口。

## 12. 数据兼容边界

即使主入口只剩多参考，也应继续读取而不是删除以下历史数据：

- 项目 `settings_json.generation_mode` 的三种历史值；
- `settings_json.h3` 和单行 `settings_json.h3`；
- `generation_mode_views` 中保存的各路线当前声音、基础视频和字幕绑定；
- `project_assets` 中历史 `base_video`、`audio`、`h3_reference_video` 及其来源类型；
- `project_links` 中历史云端批次、行和分段 ID；
- 已经完成、失败、取消或仍在云端执行的历史操作快照。

第一阶段不要删除表、列、迁移或历史文件。正确策略是：

```text
新项目只创建 minimax_h3_ref2va
新前端不再产生旧路线写请求
历史旧项目仍可被数据库读取和导出
旧路线写接口先返回明确的 410/409，再考虑物理删除
```

## 13. 启动器、安装包和更新包影响

只删 JYD 路由还不等于从本地产品移除对口型。当前正式包还存在以下依赖：

| 位置 | 当前行为 | 后续变化 |
| --- | --- | --- |
| `ltx_lip_sync_workbench/apps/processor/workbench_manager.py` | 同时启动 8010 和 8791 | 改为只启动 8010 |
| `ltx_lip_sync_workbench/build_code_update.ps1` | 强制构建并打包 LTX EXE | 改为只构建 JYD 和单服务启动器 |
| `deploy/update-assets/APPLY-CODE-UPDATE.ps1` | 检查 LTX 进程和 8791 端口 | 移除 LTX 检查 |
| `PublicVideoWorkbenchLauncher.exe` | 监控两个子服务 | 重建为单服务启动器 |
| `processor_config.json` | 保存 `ltx_workbench_url` | 新安装不再写入；旧配置可忽略 |
| 完整首次安装包 | 包含 `lip-sync` 目录 | 新单入口包不再携带该目录 |

在这些打包点修改前，不能直接删除目标电脑的 `lip-sync` 目录，否则旧启动器会把“缺少 LTX
可执行文件”当成启动失败。

## 14. 推荐实施顺序

### 阶段 A：先切断前端旧请求

1. 删除前端中普通数字人和对口型的残留函数、弹窗、轮询和事件绑定；
2. 项目恢复只调用 `/h3/status`，不再调用 `/composition/status` 或 `/ltx/state`；
3. 新项目和旧项目进入主页面时统一使用 `minimax_h3_ref2va` 视图；
4. 增加测试：加载、生成、刷新、失败重试、取消、切换批次全过程不得请求 `/ltx/`、
   `/composition/` 或 `/api/new/runninghub-execution-accounts`。

### 阶段 B：停用 JYD 旧路线写接口

1. `generation-mode` 只接受 `minimax_h3_ref2va`，或直接移除前端调用并固定服务端默认值；
2. 普通数字人和对口型的生成、上传、重试、补跑接口先返回 `410 Gone`；
3. 保留历史项目 GET、素材读取和导出；
4. 移除 `ProjectCompositionStartDispatcher` 和 `LtxWorkbenchClient` 的启动初始化；
5. 运行声音、多参考、后期、变体、成果库和历史项目读取回归。

### 阶段 C：移除 8791 和双服务打包

1. 改单服务启动器；
2. 改代码更新包构建脚本和安装脚本；
3. 首次安装包不再携带 `lip-sync`；
4. 升级包先更新启动器，再由清理步骤移除旧 `lip-sync` 目录；
5. 清理必须显式限定在 `PublicVideoWorkbench/lip-sync`，不能影响 `data`、ASR、素材库或其他项目。

### 阶段 D：云端保持兼容，另行审计

1. `video.lanyingjk01.com` 先保持不变；
2. 通过日志按接口和 `source_channel` 观察旧路线真实调用量；
3. 只有确认网站、旧客户端、运行中任务、历史下载和管理功能都不再使用后，才单独提出云端下线方案；
4. 云端下线必须有独立备份、迁移、回滚和生产部署授权。

## 15. 本地旧路线真正“断干净”的验收标准

- `/app/new/generate` 的网络请求中不出现 `/ltx/`；
- 不出现 `/composition/generate`、`/composition/status`、普通数字人重试和 SeedVR2 补跑；
- 不请求 `/api/new/runninghub-execution-accounts`，只请求 `/api/new/h3/accounts`；
- 新项目固定为 `minimax_h3_ref2va`；
- 多参考声音审核、费用预览、确认、状态恢复、失败重试、主动重生成、取消、结果回填全部通过；
- H3 权威音频、精确字幕、BGM、语义视觉、模板、完整成片、变体和成果库全部通过；
- 关闭并重启页面后，多参考运行中任务继续恢复；
- 旧普通数字人/对口型项目数据仍可读取，不被迁移脚本删除；
- 正式启动器只启动 8010，不再要求 8791；
- 代码更新包和首次安装包不再包含 LTX EXE，且更新脚本不再检查 LTX 进程和 8791 端口；
- 云端普通数字人和 LTX 网站功能不受本地单入口包影响。

## 16. 结论

本次前端收口只完成了“入口不可见”，尚未完成“本地后端不连接旧路线”。最安全的最终边界是：

```text
本地 JYD：只保留多参考生成 + 全部共享声音/后期能力
本机服务：只保留 8010，移除 8791 和 LTX 打包依赖
云端：继续保留普通数字人和 LTX 接口，避免影响网站与历史任务
数据库：保留三路线历史数据，只禁止新写入旧路线
```

按照阶段 A → B → C 执行，可以在不碰云端生产和历史数据的前提下，把新部署的本地项目稳定收口
成单一“多参考”工作台。
