> 当前工作状态（2026-11-10）：用户已明确将项目阶段收尾，要求提交并推送本地代码和文档，之后由用户自行删除本地目录。下方原媒体迁移任务仅为历史基线，不自动继续开发、部署或付费联调。恢复项目时先查看同仓库 `archive/workspace-closeout-20261110` 分支的 `README.md`。

# 工作基线：全站媒体直传 OSS、云端按需处理

> 本文件是当前任务交接入口。先读本文件，再按 `runninghub_mvp/PROJECT_MAP.md` 路由到代码和专项文档。`[verified]` 为本地证实，`[inferred]` 为推导，`[unknown]` 为未验证。不要把目标架构误读为已上线。

## 🎯 我们在做什么任务

用户批准的目标是：全站新图片/音频/视频与结果不走网站大文件通道；客户端直传杭州私有 OSS，同地域函数计算按需做 FFmpeg/FFprobe/ASR、RunningHub 文件上传和结果归档；网站仍管账号、任务、费用确认、付费提交和远程 ID。包含旧网页数字人/LTX、H3、JYD 和独立 LTX 工作台。历史本地文件限速迁移且暂不删除。本阶段不得部署生产、改真实账号库或调用真实付费生成。

## ✅ 已经完成了什么

- [verified] 核查现有 H3 幂等上传改动，并保留其 `source_channel/upload_key/source_sha256/transport_sha256` 及 `0052_h3_upload_metadata`；未覆盖用户未提交修改。
- [verified] 新增 `0053_media_assets` 和独立 `MediaAsset`，OSS object key 不进入旧本地路径解析。
- [verified] 新增默认关闭的网页登录与工作台 OSS 分片会话 API：用户/来源隔离、来源用途/大小/配额检查、上传键幂等、缺片续传 URL、OSS ListParts 尺寸核查、HEAD 最终大小确认。完成后仅为 `UPLOADED`，不进入费用预检。
- [verified] 使用 OSS Python SDK 的 V4 短期分片签名，网页控制请求走公网 HTTPS Endpoint，以支持当前网站不在杭州的灰度阶段；FC 内网端点留待后续。
- [verified] `tests/test_media_uploads.py` 有 13 个 Mock/离线签名用例；与配置、迁移、现有 H3 上传回归合计 49 项通过，H3 页面/工作台/Worker/直达相关集合 52 项通过（两集合有重叠，不相加；2026-09-24）。未进行真实 OSS 联调。
- [verified] 本轮新增 `0054_media_verification_jobs`：分片合并后同事务建立唯一核验任务；独立云端令牌、领取/续租/回执 API、活动租约上限与 3 次重试。新 Worker 入口使用 FC 执行角色临时凭据从 OSS 内网读取，限额落临时盘并以 SHA-256、FFprobe/FFmpeg 做内容检查；网站复核大小/摘要/类型才将素材置 `READY`。当前仅本地 Mock 测试，未部署 FC 镜像/触发器。
- [verified] `tests/test_media_verification.py` 18 项、迁移 19 项在内的 5 组专项组合回归共 68 项通过（2026-09-24）；包含重复失败回执、过期租约、并发上限、OSS 合并超时和原 H3 上传合同。没有真实 OSS/FC 联调，也未跑通跨 JYD 的全量测试。

## 🛑 当前卡点与根本原因

- [verified] 现行 H3/旧数字人/LTX 在费用预检、切片、RunningHub 上传和下载阶段直接读取本机 `Path`。只切换浏览器上传会导致任务找不到文件，因此未把新 `MediaAsset` 交给现有付费流程，也未关闭旧 multipart。
- [verified] 已有核验任务/租约/回执与单次 Worker 代码，但没有真实 FC 镜像、按需唤醒和 OSS 联调；业务级 ASR/切片、RunningHub 文件交接、结果归档和历史迁移仍缺。新接口只是可测试底座，不是完整迁移。
- [verified] JYD 源码位于工作区可写根之外（`D:\工作内容\轻盈健\公寓\jyd_plain_json_probe`），本轮未改动；后续编辑需获沙箱授权。独立 LTX 也未改动。
- [unknown] 杭州 OSS Bucket/FC 尚未提供隔离环境、资源规格和凭据，真实大文件性能、CORS、成本和权限未验证。不可开启 `MEDIA_OSS_ENABLED` 给真实用户。
- [verified] 全量 `pytest -q` 在收集 9 个跨 JYD 合同模块时因隔离环境缺少 JYD 的 `fontTools` 等依赖而中断；未得到全量通过结论。上述专项回归已通过。

## 📋 下一步行动计划

1. 在隔离环境建立 FC 自定义镜像与安全的按需触发器，真实测试 OSS 内网读取、角色临时凭据、完整解码、临时空间/耗时/成本，补超时后自动唤醒及失败恢复；未压测通过不得开生产开关。
2. 将 H3/旧网页/工作台的现有 `Path` 处理按任务边界迁到 FC，RunningHub 文件上传回传 `fileName`，网站保留提交不明时不重提的保护；接 JYD 和独立 LTX 客户端直传，取得 JYD 目录写权限后再改。
3. 完成 MiniMax/RunningHub 输出归档、鉴权短期重定向、历史文件受控迁移与客户端升级提示。三类客户端及 10 项端到端验收通过后，才灰度并关闭旧 multipart、下调 Nginx 上限；迁站和生产部署另行授权。

本地复验命令：在 `runninghub_mvp` 的隔离 Python 环境运行 `python -m pytest -q tests/test_media_uploads.py tests/test_media_verification.py tests/test_production_config.py tests/test_migrations.py tests/test_h3_input_uploads.py`。相关合同见 `runninghub_mvp/docs/` 五份媒体文档。

## ⚠️ 踩过的坑绝对不要再踩

- [verified] OSS URL 不是 RunningHub 工作流接受的 `fileName`，不能直接塞进 H3 图节点；需要 FC 二进制上传并返回回执。
- [verified] `require_csrf` 的 Form 依赖会让 JSON 上传初始化请求被 FastAPI 解析成嵌套 body；JSON 路由须显式使用仅检查 Header 的包装依赖。
- [verified] 测试机全局 Python 缺少 `jieba` 等依赖且用户目录无写权限；本次使用 `runninghub_mvp/.codex_tmp/media-oss-venv` 的隔离环境运行测试。不要改全局 Python/生产配置来绕过。
- [verified] 新增迁移后，已有 `tests/test_migrations.py` 的 Alembic head 断言要同步至 `0054_media_verification_jobs`，但旧迁移和数据不改写。
- [inferred] 未接通所有消费者前直接封旧上传或把 `MediaAsset.object_key` 当本机 Path，会中断付费任务；任何失败都不回退到网站搬大文件。

## 本地磁盘清理（2026-09-25）
- [verified] 用户明确授权清理 `D:\工作内容\轻盈健` 中非代码/非运行环境的素材、本地数据和旧副本。
- [verified] 已删除 `公寓\jyd_plain_json_probe\data`、测试环境中的 `libraries` 与 `web_storage`、旧 pytest 副本和发布包；已删除 `数字人\runninghub_mvp\data`、旧 `patches` 包、指定旧发布压缩包及若干临时测试副本。轻盈健根目录的数据表格、数据目录和测试压缩包也已删除。
- [verified] 保留数字人源码、Git 历史、`HANDOFF.md`、`runninghub_mvp\dist`、`runninghub_mvp\media_node`、`jyd_plain_json_probe\src`、测试环境目录结构，以及 `.codex_tmp\processor_update_20260829_fixes`。
- [verified] D 盘清理前可用空间为 56,129,179,648 字节；清理后首次复核为 215,473,868,800 字节，随后复核为 210,950,807,552 字节（卷级可用空间会随系统运行波动）。按前后复核值计算，本次增加约 144 GiB 可用空间。
- [inferred] 依赖已删除本地素材库、测试数据或历史上传记录的测试/演示流程需要重新生成数据后才能复现；不要假定原有媒体数据仍在。

## 并行素材任务：徐博士浇水开场（2026-09-24）
- [verified] 用户已选择 C：户外米白衬衫站姿，并明确授权先生成一张浇水开场图。此项不改变上文软件工程任务基线。
- [verified] 已使用内置 image_gen，以 outputs/xu_selection/candidate_c.jpg 为人物参考生成一张竖屏图，保存至 outputs/xu_firstframe/xu_water_opening_v1.png；完整提示词在同目录 prompt_v1.txt。原素材未修改。
- [verified] 画面为米白衬衫、户外树荫步道、举起透明水容器的自拍构图，水尚未倒出。表情仍有轻微笑意，未完全达到皱眉的原设计。
- [unknown] 人物相似度是否满足用户要求、实际视频动作稳定性尚待评价；尚未提交视频生成。下一步由用户查看首帧，再决定微调或进入新台词与视频阶段。

- [verified] 徐博士素材任务追加：用户要求更夸张、更有感染力的表演，已用内置 image_gen 生成 outputs/xu_firstframe/xu_water_opening_v2.png。画面已改为皱眉、单眼眯起、张嘴喊话，保留举水容器与干燥衣服；v1 保留。完整提示词在 prompt_v2.txt。[unknown] 用户对身份相似度及表演强度的评价待确认，未提交视频生成。

## 个人技能整理（2026-10-09，独立于网站任务）
- [verified] 用户要求 H3 技能整体移动而非保留副本；现位于 `C:/Users/san/.codex/skills/h3-prompt-writing`，原 `.agents/skills/h3-prompt-writing` 已移除。四个文件迁移前后内容摘要一致，含两份完整参考文件与 UI 元数据。
- [verified] 根目录与 runninghub_mvp 的 AGENTS、PROJECT_MAP、H3 开发文档已更新现行入口；历史发布副本保持原样。
- [verified] 个人技能“口播精剪” v0.1.0 已创建于 `C:/Users/san/.codex/skills/koubo-edit`，包含工作流、手法、JSON 修改约束、案例、反馈更新和 10 场景预期结果。基础格式及内部引用已检查。
- [unknown] 新技能在下一条真实口播上的剪辑效果尚未实测；场景清单不代表已经完成视频测试。下一步使用用户今天提供的草稿及素材执行，再记录具体时间点反馈。
- 注意：H3 本次只迁移不改内容；不要重新在项目里复制安装。口播技能版本迭代记录在它自己的 HANDOFF.md，不影响上文网站任务。


## Git 推送核查（2026-11-10）
- [verified] 只读核对 GitHub 当前分支：`rh-api/main=f6693a1`、`jianji-auto/main=abf8d6d`、`santoro-1-ltx-lip-sync-workbench/main=9a601b0`，均与本地 HEAD 一致；三个主要仓库没有仅存在于本地分支的未推送提交。
- [verified] 仍未提交：网站 20 个修改 + 20 个未跟踪文件；公寓工作台 11 个修改 + 2 个未跟踪文件 + 3480 个删除；LTX 历史工作台 15 个修改 + 25 个未跟踪文件。未提交内容不包含在当前远端提交中。
- [inferred] 工作台删除范围与本文件 2026-09-25 磁盘清理记录一致；不自动恢复或提交这些删除。
- [verified] 网站另有 4 份本地 stash（含未跟踪文件分别涉及 30/18/6/2 个文件），普通 push 不会推送 stash；[unknown] 是否已在后续提交中实现，未逐项对比。
- [verified] 网站四个独立发布副本与提示词工作树均干净，其 HEAD 已包含在远端 main 的历史中。`video_gongyu/video_gongyu` 工作目录干净，但没有配置远端。数字人根目录 `.git` 为空，不是有效仓库，根级文档不受网站子仓库跟踪。
- [verified] 完整核查清单与删除明细保存在 `.codex_tmp/git_audit_20261110.txt`。本次未改代码、未 commit、未 push；后续备份需先区分有效开发改动、历史 stash 与已授权清理产生的删除。

## 阶段收尾归档（2026-11-10）

### 🎯 我们在做什么任务
- 用户要求结束本阶段，提交并推送代码和文档，之后自行删除本地项目。本次不执行本地删除。

### ✅ 已经完成了什么
- [verified] 三个仓库开发改动已提交并推送：网站 `219cfae`（40 文件），工作台 `ec255c7`（13 文件），LTX 历史工作台 `807ac9f`（40 文件）。
- [verified] 网站 4 份 stash 原始提交及父提交已推送到 `archive/stash-*-20261110` 分支，保留未跟踪文件的第三父提交；未应用到 main。
- [verified] 前端原型 `05d889f` 及历史已推送至 `jianji-auto` 的 `archive/video-gongyu-20261110` 分支。
- [verified] 网站专项 Mock/离线回归 `68 passed`；307 个待备份 Python 文件通过语法检查。其余历史模块未重新做功能验收。
- [verified] 工作台仅提交代码和文档，3480 个本地素材/模板删除未提交，远端保留原文件历史。

### 🛑 当前卡点与根本原因
- [verified] 无提交/推送阻塞。运行数据、真实环境变量、密钥、依赖环境、二进制发布包和缓存不属于本次 Git 代码归档范围。不能把源码归档当作可直接运行的生产备份。
- [unknown] OSS/FC 真实联调、完整迁移与生产验收仍未完成，保持默认关闭。

### 📋 下一步行动计划
1. 完成仓库外代码、根级文档、历史补丁的独立归档分支并从远端复核。
2. 恢复时按归档 README 克隆三个仓库，再按需要提取 H3 工作台或还原 stash；历史临时源码不作为现行主代码。
3. 用户自行决定何时删除本地项目；恢复开发前重新配置依赖和私密运行设置。

### ⚠️ 踩过的坑绝对不要再踩
- stash 包含未跟踪文件的第三父提交，不能仅导出普通 diff；本轮按原始 Git 提交归档。
- 旧素材清理产生的删除不应混入代码备份提交。
- `historical_scratch` 仅作历史追溯，不覆盖三个 main 中的新代码。
