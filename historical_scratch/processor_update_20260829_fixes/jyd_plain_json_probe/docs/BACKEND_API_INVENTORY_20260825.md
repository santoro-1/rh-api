# 三条视频生成路线后端接口盘点

> 盘点日期：2026-08-25  
> 文档性质：仅记录当前后端接口，不包含删除方案、改造计划或部署操作。  
> 涉及服务：JYD 本地后端、LTX 本地隐藏引擎、RunningHub 云端后端。

## 1. 服务边界

| 服务 | 默认地址 | 代码仓库 | 职责 |
| --- | --- | --- | --- |
| JYD 本地后端 | `http://127.0.0.1:8010` | `jyd_plain_json_probe` | 浏览器唯一业务入口、项目数据、声音同步、三条视频路线协调、本地后期和成果 |
| LTX 本地隐藏引擎 | `http://127.0.0.1:8791` | `ltx_lip_sync_workbench` | 对口型源视频、本地影子项目、云端 LTX 状态和基础视频下载 |
| RunningHub 云端后端 | `https://video.lanyingjk01.com` | `runninghub_mvp` | 账号、MiniMax、素材暂存、三条生成路线、RunningHub 任务和云端结果 |

三条路线的内部标识：

| 页面业务名称 | `generation_mode` |
| --- | --- |
| 普通数字人 | `runninghub_digital_human` |
| 多参考 | `minimax_h3_ref2va` |
| 视频对口型 | `ltx_lip_sync` |

## 2. 通用鉴权合同

### 2.1 浏览器访问 JYD

- 浏览器使用 JYD 的 HTTP-only 登录会话 Cookie。
- 普通用户账号由 RunningHub 云端账号中心验证。
- JYD 不保存普通用户密码。
- 本地接口通过 `current_project_user()` 校验当前项目所有权。

### 2.2 JYD 访问 RunningHub 云端

- JYD 在进程内保存当前账号的短期访问令牌。
- JSON 接口把令牌放入请求体的 `access_token`。
- 文件下载接口使用 `Authorization: Bearer <token>`。
- 云端响应不返回 RunningHub API Key、工作流密码或其他执行凭据。

### 2.3 JYD 访问 LTX 本地隐藏引擎

JYD 同时发送：

```text
Authorization: Bearer <数字人账号短期令牌>
X-Workbench-Manager-Token: <本机工作台管理令牌>
```

隐藏引擎只接受本机回环地址调用。

## 3. JYD 本地后端：账号与运行时接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/auth/center/login` | 使用云端数字人账号登录 |
| `POST` | `/api/auth/center/verify` | 验证当前云端账号令牌 |
| `POST` | `/api/auth/center/handoff` | 创建跨本地页面的一次性交接码 |
| `GET` | `/api/auth/session` | 返回当前 JYD 登录状态 |
| `POST` | `/api/auth/logout` | 退出 JYD 登录会话 |
| `GET` | `/api/auth/handoff` | 消费交接码进入 JYD |
| `POST` | `/api/auth/local-handoff` | 创建本机页面交接 |
| `GET` | `/api/auth/local-handoff` | 消费本机页面交接 |
| `GET` | `/api/auth/handoff-to` | 跳转到指定本地服务 |
| `GET` | `/api/auth/handoff-to-center` | 返回云端账号中心 |
| `POST` | `/api/runtime/pages` | 登记一个打开的工作台页面 |
| `POST` | `/api/runtime/pages/{lease_id}` | 刷新页面租约 |
| `POST` | `/api/runtime/pages/{lease_id}/close` | 释放页面租约 |
| `GET` | `/api/runtime/status` | 返回本地运行状态 |
| `POST` | `/api/runtime/shutdown` | 由管理器令牌请求本地服务退出 |

## 4. JYD 本地后端：项目、脚本与输入接口

### 4.1 项目与脚本

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/new/projects` | 新建项目/批次 |
| `GET` | `/api/new/projects` | 分页列出当前账号项目 |
| `GET` | `/api/new/projects/{project_id}` | 读取完整项目状态 |
| `PATCH` | `/api/new/projects/{project_id}` | 修改项目名称、脚本等通用属性 |
| `DELETE` | `/api/new/projects/{project_id}` | 删除项目 |
| `POST` | `/api/new/projects/{project_id}/items` | 新增脚本行 |
| `POST` | `/api/new/projects/{project_id}/items/batch` | 批量新增脚本行 |
| `PATCH` | `/api/new/projects/{project_id}/items/{item_id}` | 修改单行脚本 |
| `DELETE` | `/api/new/projects/{project_id}/items/{item_id}` | 删除单行脚本 |
| `GET` | `/api/new/script-template` | 下载脚本导入模板 |
| `POST` | `/api/new/script-imports/preview` | 预览 Excel/CSV 脚本导入 |
| `PUT` | `/api/new/projects/{project_id}/metadata-import` | 导入项目元数据 |
| `PUT` | `/api/new/projects/{project_id}/script-source` | 保存脚本源文件 |
| `PUT` | `/api/new/projects/{project_id}/inputs` | 批量更新项目输入 |
| `GET` | `/api/new/projects/{project_id}/diagnostics` | 下载当前项目脱敏诊断包 |

### 4.2 生成路线设置

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `PUT` | `/api/new/projects/{project_id}/generation-mode` | 在三个内部模式之间切换 |
| `PUT` | `/api/new/projects/{project_id}/digital-human-settings` | 保存普通数字人的项目级分辨率 |
| `PUT` | `/api/new/projects/{project_id}/h3/settings` | 保存多参考人物图和项目级默认参数 |

### 4.3 项目图片与人物图映射

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/new/projects/{project_id}/images` | 上传项目图片 |
| `GET` | `/api/new/projects/{project_id}/images/{image_id}` | 读取图片 |
| `DELETE` | `/api/new/projects/{project_id}/images/{image_id}` | 删除图片 |
| `PUT` | `/api/new/projects/{project_id}/image-mapping` | 保存图片到脚本行的整体映射 |
| `PUT` | `/api/new/projects/{project_id}/image-mapping-scope` | 保存映射作用范围 |
| `PUT` | `/api/new/projects/{project_id}/items/{item_id}/image` | 修改单行图片绑定 |

## 5. JYD 本地后端：声音接口

### 5.1 音色与声音制作

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/new/voices` | 读取官方及当前账号已保存音色 |
| `PUT` | `/api/new/voices/default` | 保存项目默认音色 |
| `POST` | `/api/new/voices/import` | 导入云端声音资产 |
| `POST` | `/api/new/voices/{voice_asset_id}/preview` | 创建官方音色试听 |
| `GET` | `/api/new/voices/{voice_asset_id}/preview` | 下载/播放音色试听 |
| `POST` | `/api/new/voices/{voice_asset_id}/activate` | 激活已保存音色 |
| `DELETE` | `/api/new/voices/{voice_asset_id}` | 删除当前账号音色 |
| `POST` | `/api/new/voice-creations` | 创建克隆/融合声音任务 |
| `POST` | `/api/new/voice-creations/{task_id}/save` | 保存声音制作结果 |
| `GET` | `/api/new/voice-creations/{task_id}/preview` | 下载声音制作试听 |

### 5.2 项目声音生成

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `PUT` | `/api/new/projects/{project_id}/voice` | 保存项目默认音色和声音参数 |
| `PUT` | `/api/new/projects/{project_id}/items/{item_id}/voice` | 保存单行音色覆盖 |
| `POST` | `/api/new/projects/{project_id}/audio/generate` | 创建 MiniMax 声音批次 |
| `GET` | `/api/new/projects/{project_id}/audio/status` | 同步声音批次并下载 READY 音频与 raw cues |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/audio/retry` | 重试单行声音 |
| `GET` | `/api/new/projects/{project_id}/items/{item_id}/audio` | 播放或下载当前行声音 |
| `GET` | `/api/new/projects/{project_id}/audios/download` | 批量打包项目声音 |

声音接口由三条视频生成路线共用。多参考在进入画面生成前还会执行单独的声音审核绑定。

## 6. JYD 本地后端：多参考接口

| 方法 | 路径 | 用途 | 下游 |
| --- | --- | --- | --- |
| `POST` | `/api/new/projects/import-h3-handoff` | 导入已有多参考交接结果 | 本地项目库 |
| `GET` | `/api/new/h3/accounts` | 读取多参考执行账号摘要 | 云端 `h3-execution-accounts` |
| `PUT` | `/api/new/projects/{project_id}/h3/settings` | 保存人物参考图和默认参数 | 本地项目库 |
| `PATCH` | `/api/new/projects/{project_id}/items/{item_id}/h3/overrides` | 保存单行参数覆盖 | 本地项目库 |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/h3/reference-video` | 上传单行参考视频 | 本地托管素材 |
| `POST` | `/api/new/projects/{project_id}/h3/audio-review` | 审核并冻结 MiniMax 音频版本 | 云端 `h3-audio-sources/approve` |
| `POST` | `/api/new/projects/{project_id}/h3/prepare` | 上传输入、冻结合同并计算分段费用 | 云端 `h3-batches/prepare` |
| `POST` | `/api/new/projects/{project_id}/h3/confirm` | 确认费用并启动云端任务 | 云端 `h3-batches/{id}/confirm` |
| `GET` | `/api/new/projects/{project_id}/h3/status` | 查询状态、下载分段、回填音画 | 云端批次查询和分段下载 |
| `POST` | `/api/new/projects/{project_id}/h3/segments/{segment_id}/regeneration/prepare` | 计算主动重生成范围与费用 | 云端同名接口 |
| `POST` | `/api/new/projects/{project_id}/h3/segments/{segment_id}/regeneration/confirm` | 确认主动重生成 | 云端同名接口 |
| `POST` | `/api/new/projects/{project_id}/h3/segments/{segment_id}/retry/prepare` | 计算失败阶段重试范围与费用 | 云端同名接口 |
| `POST` | `/api/new/projects/{project_id}/h3/segments/{segment_id}/retry/confirm` | 确认失败阶段重试 | 云端同名接口 |
| `POST` | `/api/new/projects/{project_id}/h3/segments/{segment_id}/cancel` | 取消多参考分段 | 云端同名接口 |

本地协调器：

```text
src/jyd_probe/project_h3.py
src/jyd_probe/project_h3_media.py
```

## 7. JYD 本地后端：普通数字人接口

| 方法 | 路径 | 用途 | 下游 |
| --- | --- | --- | --- |
| `GET` | `/api/new/runninghub-execution-accounts` | 查询普通数字人及 SeedVR2 账号池 | 云端账号池接口 |
| `POST` | `/api/new/projects/{project_id}/composition/generate` | 创建普通数字人 4A 操作 | 云端 composition 接口 |
| `GET` | `/api/new/projects/{project_id}/composition/status` | 轮询普通数字人、SeedVR2 和拼接状态 | 云端任务查询/下载 |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/composition/retry` | 重试失败的普通数字人阶段 | 云端 composition retry |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/composition/seedvr2-backfill` | 对历史数字人结果补跑 SeedVR2 | 云端 enhancement backfill |
| `GET` | `/api/new/projects/{project_id}/items/{item_id}/base-video` | 读取本地基础视频 | 本地托管文件 |

普通数字人协调器：

```text
src/jyd_probe/project_composition.py
```

### 7.1 云端数字人任务收件箱接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/digital-human/tasks` | 拉取当前账号的云端数字人任务 |
| `POST` | `/api/digital-human/tasks/{item_id}/import` | 把云端任务导入本地项目 |
| `GET` | `/api/digital-human/tasks/{item_id}/videos/{video_index}` | 下载指定云端原始视频片段 |

## 8. JYD 本地后端：视频对口型接口

| 方法 | 路径 | 用途 | 下游 8791 接口 |
| --- | --- | --- | --- |
| `GET` | `/api/new/projects/{project_id}/ltx/state` | 同步项目到隐藏引擎并读取状态 | `POST .../sync` |
| `PUT` | `/api/new/projects/{project_id}/items/{item_id}/ltx/source-video` | 上传单行源视频 | `PUT .../source-video` |
| `POST` | `/api/new/projects/{project_id}/ltx/generate` | 确认费用并启动指定行 | `POST .../start` |
| `POST` | `/api/new/projects/{project_id}/ltx/refresh` | 刷新云端状态并回填基础视频 | `POST .../refresh`、`GET .../base-video` |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/ltx/retry` | 重试指定对口型行 | `POST .../retry` |

本地协调器与客户端：

```text
src/jyd_probe/project_ltx.py
```

## 9. JYD 本地后端：内容分析与语义视觉接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/new/semantic-visuals/catalog` | 获取本地语义视觉素材目录 |
| `GET` | `/api/new/semantic-visuals/{asset_id}/preview` | 获取素材预览 |
| `GET` | `/api/new/semantic-visuals/{asset_id}/content` | 获取语义视频内容 |
| `GET` | `/api/new/fixed-visuals/nameplate/preview` | 获取固定人名牌预览 |
| `POST` | `/api/new/projects/{project_id}/content-analysis` | 统一分析音乐、字幕、视觉和标题 |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/content-analysis/retry` | 重试单行统一分析 |
| `POST` | `/api/new/projects/{project_id}/visual-analysis` | 兼容的独立视觉分析 |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/visual-analysis/retry` | 重试单行视觉分析 |
| `PUT` | `/api/new/projects/{project_id}/items/{item_id}/visual-overlays` | 保存本地视觉叠加配方 |

## 10. JYD 本地后端：后期、预览、变体和成果接口

### 10.1 后期与当前视频

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/new/postprocess/options` | 获取字幕、BGM、布局等后期选项 |
| `POST` | `/api/new/projects/{project_id}/postprocess/generate` | 生成字幕/BGM 成片 |
| `PATCH` | `/api/new/projects/{project_id}/items/{item_id}/postprocess-settings` | 保存单行后期配置 |
| `GET` | `/api/new/projects/{project_id}/postprocess/status` | 查询后期任务状态 |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/postprocess/export` | 导出浏览器预览/当前后期结果 |
| `GET` | `/api/new/projects/{project_id}/items/{item_id}/current-video` | 下载当前完整成片 |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/current-video` | 上传并替换当前完整成片 |
| `GET` | `/api/new/projects/{project_id}/videos/download` | 批量下载当前成片 |
| `GET` | `/api/new/projects/{project_id}/items/{item_id}/original-materials` | 下载原始生成片段 |

### 10.2 变体与成果库

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/new/variant-options` | 获取变体参数选项 |
| `PATCH` | `/api/new/projects/{project_id}/variant-settings` | 保存项目变体设置 |
| `POST` | `/api/new/projects/{project_id}/variants/generate` | 批量生成变体 |
| `GET` | `/api/new/projects/{project_id}/variants/status` | 查询变体任务状态 |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/variants/supplement` | 补生成单行变体 |
| `POST` | `/api/new/projects/{project_id}/items/{item_id}/variants/retry` | 重试单行变体 |
| `GET` | `/api/new/projects/{project_id}/items/{item_id}/variants/{asset_id}` | 下载变体 |
| `DELETE` | `/api/new/projects/{project_id}/items/{item_id}/variants/{asset_id}` | 删除变体 |
| `GET` | `/api/new/gallery` | 查询成果视频库 |
| `POST` | `/api/new/gallery/downloads` | 批量打包成果 |
| `POST` | `/api/new/gallery/deletions` | 批量删除成果 |

## 11. JYD 本地后端：剪映模板接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/new/jianying-templates` | 列出当前用户模板 |
| `POST` | `/api/new/jianying-templates` | 创建模板记录 |
| `PUT` | `/api/new/jianying-templates/{template_id}/draft-files` | 上传模板草稿文件 |
| `POST` | `/api/new/jianying-templates/{template_id}/analyze` | 分析模板字幕与资源引用 |
| `PUT` | `/api/new/jianying-templates/{template_id}/resource-files` | 上传模板资源文件 |
| `POST` | `/api/new/jianying-templates/{template_id}/resources/complete` | 完成资源上传 |
| `PATCH` | `/api/new/jianying-templates/{template_id}` | 修改模板名称等元数据 |
| `DELETE` | `/api/new/jianying-templates/{template_id}` | 删除模板 |
| `PUT` | `/api/new/projects/{project_id}/jianying-template` | 给项目绑定模板 |

## 12. LTX 本地隐藏引擎 8791：JYD 桥接接口

这些接口由 JYD 的 `LtxWorkbenchClient` 调用，不由浏览器直接调用。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/integrations/jyd/projects/{external_project_id}/sync` | 创建/更新 JYD 对应的影子项目和脚本行 |
| `POST` | `/api/integrations/jyd/projects/{external_project_id}/state` | 返回指定行的隐藏引擎状态 |
| `PUT` | `/api/integrations/jyd/projects/{external_project_id}/items/{external_item_id}/source-video` | 保存并上传对口型源视频 |
| `POST` | `/api/integrations/jyd/projects/{external_project_id}/start` | 校验输入并创建云端 LTX 批次 |
| `POST` | `/api/integrations/jyd/projects/{external_project_id}/refresh` | 查询云端批次并下载完成的基础视频 |
| `POST` | `/api/integrations/jyd/projects/{external_project_id}/items/{external_item_id}/retry` | 重试指定云端 LTX 行 |
| `GET` | `/api/integrations/jyd/projects/{external_project_id}/items/{external_item_id}/base-video` | 向 JYD 返回合并后的基础视频 |

实现位置：

```text
ltx_lip_sync_workbench/src/ltx_workbench/api.py
ltx_lip_sync_workbench/src/ltx_workbench/cloud_client.py
```

## 13. RunningHub 云端：账号与共享工作台接口

### 13.1 账号中心

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/auth/center/login` | 工作台账号登录 |
| `POST` | `/api/auth/center/verify` | 校验短期访问令牌 |
| `POST` | `/api/auth/center/handoff` | 创建一次性交接码 |
| `POST` | `/api/auth/center/handoff/consume` | 消费一次性交接码 |

### 13.2 音色与声音批次

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/workbench/voices` | 查询当前账号音色 |
| `POST` | `/api/workbench/voices/import` | 导入声音资产 |
| `POST` | `/api/workbench/voices/{voice_asset_id}/preview` | 创建官方音色试听 |
| `GET` | `/api/workbench/voices/{voice_asset_id}/preview` | 下载音色试听 |
| `POST` | `/api/workbench/voices/{voice_asset_id}/activate` | 激活声音资产 |
| `POST` | `/api/workbench/voices/{voice_asset_id}/delete` | 删除声音资产 |
| `POST` | `/api/workbench/voice-creations` | 创建声音克隆/融合任务 |
| `POST` | `/api/workbench/voice-creations/{task_id}/save` | 保存声音制作结果 |
| `GET` | `/api/workbench/voice-creations/{task_id}/preview` | 下载声音制作试听 |
| `POST` | `/api/workbench/batch-assets` | 暂存上传图片、视频、音频等批次素材 |
| `POST` | `/api/workbench/audio-batches` | 创建 MiniMax 声音批次 |
| `POST` | `/api/workbench/audio-batches/{batch_id}` | 查询声音批次 |
| `GET` | `/api/workbench/audio-batches/{batch_id}/items/{item_id}/audio` | 下载生成音频 |
| `POST` | `/api/workbench/audio-batches/{batch_id}/items/{item_id}/retry` | 重试声音行 |

### 13.3 内容分析

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/workbench/content-analysis` | 返回音乐、字幕、视觉和标题分析 |
| `POST` | `/api/workbench/visual-analysis` | 返回兼容的独立视觉分析结果 |

## 14. RunningHub 云端：多参考接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/workbench/h3-execution-accounts` | 返回当前用户可用的多参考执行账号 |
| `POST` | `/api/workbench/h3-audio-sources` | 查询可作为输入的声音版本 |
| `POST` | `/api/workbench/h3-audio-sources/approve` | 审核并冻结指定声音版本 |
| `POST` | `/api/workbench/h3-batches/prepare` | 暂存并冻结人物图、参考视频、音频、原稿和参数；计算分段费用 |
| `POST` | `/api/workbench/h3-batches/{batch_id}/confirm` | 确认费用并把分段置入生成队列 |
| `POST` | `/api/workbench/h3-batches/{batch_id}` | 查询完整批次、行和分段状态 |
| `POST` | `/api/workbench/h3-segments/{segment_id}/regeneration/prepare` | 计算主动重生成影响范围和费用 |
| `POST` | `/api/workbench/h3-segments/{segment_id}/regeneration/confirm` | 创建主动重生成任务 |
| `POST` | `/api/workbench/h3-segments/{segment_id}/retry/prepare` | 计算失败阶段重试费用 |
| `POST` | `/api/workbench/h3-segments/{segment_id}/retry/confirm` | 创建失败阶段重试任务 |
| `POST` | `/api/workbench/h3-segments/{segment_id}/cancel` | 取消指定分段 |
| `GET` | `/api/workbench/h3-segments/{segment_id}/video` | 下载标准化后的多参考分段 |
| `GET` | `/api/workbench/h3-items/{item_id}/raw-cues` | 下载多参考行的 raw cues |
| `GET` | `/api/workbench/h3-items/{item_id}/audio` | 下载多参考输入音频 |

实现位置：

```text
runninghub_mvp/app/routes/workbench_h3.py
runninghub_mvp/app/services/h3_workbench.py
```

## 15. RunningHub 云端：普通数字人工作台接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/workbench/runninghub-execution-accounts` | 返回普通数字人单池账号摘要 |
| `POST` | `/api/workbench/runninghub-dual-pool-accounts` | 返回数字人与 SeedVR2 双池摘要 |
| `POST` | `/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition` | 以已审核声音和人物图片启动普通数字人 |
| `POST` | `/api/workbench/tasks` | 查询当前账号普通数字人任务列表 |
| `POST` | `/api/workbench/tasks/{item_id}` | 查询普通数字人行状态 |
| `GET` | `/api/workbench/tasks/{item_id}/videos/{video_index}` | 下载普通数字人处理后片段 |
| `GET` | `/api/workbench/tasks/{item_id}/videos/{video_index}/source` | 下载普通数字人源片段 |
| `POST` | `/api/workbench/tasks/{item_id}/composition/retry` | 重试普通数字人或 SeedVR2 阶段 |
| `POST` | `/api/workbench/tasks/{item_id}/enhancement/backfill` | 对历史数字人结果补跑 SeedVR2 |
| `GET` | `/api/workbench/tasks/{item_id}/base-video` | 下载普通数字人基础视频 |

实现位置：

```text
runninghub_mvp/app/routes/workbench.py
```

## 16. RunningHub 云端：视频对口型工作台接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/workbench/ltx-batches/validate` | 校验声音、源视频、原稿和分段输入 |
| `POST` | `/api/workbench/ltx-batches` | 确认费用并创建 LTX/SeedVR2 批次 |
| `POST` | `/api/workbench/ltx-batches/{batch_id}` | 查询 LTX 批次状态 |
| `POST` | `/api/workbench/ltx-items/{item_id}` | 查询 LTX 行状态 |
| `POST` | `/api/workbench/ltx-items/{item_id}/retry` | 重试准备失败的 LTX 行 |
| `POST` | `/api/workbench/ltx-items/{item_id}/cancel` | 取消尚未创建分段的 LTX 行 |
| `POST` | `/api/workbench/ltx-items/{item_id}/segments/{segment_index}/retry` | 按行与序号重试分段 |
| `POST` | `/api/workbench/ltx-segments/{segment_id}/retry` | 按分段 ID 重试 |
| `POST` | `/api/workbench/ltx-items/{item_id}/segments/{segment_index}/cancel` | 取消指定分段 |
| `GET` | `/api/workbench/ltx-items/{item_id}/segments/{segment_index}/video` | 下载清晰化后的分段 |
| `GET` | `/api/workbench/ltx-items/{item_id}/segments/{segment_index}/source-video` | 下载 LTX 原始分段 |
| `GET` | `/api/workbench/ltx-items/{item_id}/base-video` | 下载合并后的 LTX 基础视频 |

实现位置：

```text
runninghub_mvp/app/routes/workbench_ltx.py
runninghub_mvp/app/services/ltx_workbench.py
```

## 17. 三条路线的接口调用链

### 17.1 普通数字人

```text
JYD /audio/generate
  -> 云端 /api/workbench/audio-batches
JYD /composition/generate
  -> 云端 /api/workbench/audio-batches/{batch}/items/{item}/composition
JYD /composition/status
  -> 云端 /api/workbench/tasks/{item}
  -> 云端视频/基础视频下载接口
JYD /postprocess/generate
  -> 本地剪映后期
```

### 17.2 多参考

```text
JYD /audio/generate
  -> 云端 /api/workbench/audio-batches
JYD /h3/audio-review
  -> 云端 /api/workbench/h3-audio-sources/approve
JYD /h3/prepare
  -> 云端 /api/workbench/batch-assets
  -> 云端 /api/workbench/h3-batches/prepare
JYD /h3/confirm
  -> 云端 /api/workbench/h3-batches/{batch}/confirm
JYD /h3/status
  -> 云端 /api/workbench/h3-batches/{batch}
  -> 云端 /api/workbench/h3-segments/{segment}/video
JYD /postprocess/generate
  -> 本地剪映后期
```

### 17.3 视频对口型

```text
JYD /audio/generate
  -> 云端 /api/workbench/audio-batches
JYD /ltx/source-video
  -> 本机 8791 /api/integrations/jyd/.../source-video
  -> 云端 /api/workbench/batch-assets
JYD /ltx/generate
  -> 本机 8791 /api/integrations/jyd/.../start
  -> 云端 /api/workbench/ltx-batches/validate
  -> 云端 /api/workbench/ltx-batches
JYD /ltx/refresh
  -> 本机 8791 /api/integrations/jyd/.../refresh
  -> 云端 /api/workbench/ltx-batches/{batch}
  -> 云端 /api/workbench/ltx-items/{item}/base-video
  -> 本机 8791 /api/integrations/jyd/.../base-video
JYD /postprocess/generate
  -> 本地剪映后期
```

## 18. 主要后端实现文件索引

### JYD

```text
src/jyd_probe/web_api.py                 FastAPI 路由注册
src/jyd_probe/auth_center.py             RunningHub 云端 HTTP 客户端
src/jyd_probe/project_store.py           项目、素材、状态和三模式数据
src/jyd_probe/project_audio.py           MiniMax 声音协调器
src/jyd_probe/project_composition.py     普通数字人协调器
src/jyd_probe/project_h3.py              多参考协调器
src/jyd_probe/project_h3_media.py        多参考本地媒体准备与交接
src/jyd_probe/project_ltx.py             8791 对口型桥接客户端与协调器
src/jyd_probe/project_postprocess.py     字幕/BGM/剪映后期
src/jyd_probe/project_variants.py        变体生成
```

### RunningHub 云端

```text
app/routes/workbench.py                  共享、声音和普通数字人接口
app/routes/workbench_h3.py               多参考接口
app/routes/workbench_ltx.py              对口型接口
app/services/h3_workbench.py             多参考业务服务
app/services/ltx_workbench.py            对口型业务服务
```

### LTX 本地隐藏引擎

```text
src/ltx_workbench/api.py                 8791 FastAPI 路由
src/ltx_workbench/cloud_client.py        8791 到 RunningHub 云端客户端
src/ltx_workbench/store.py               本地影子项目和素材状态
```
