# Codex 项目指令

本文件用于告诉 Codex 在新任务开始时应先读取哪些项目文档。不要只根据聊天摘要、早期验证文档或旧认证中心说明修改代码。

## 每个新任务的必读文档

在分析、修改或运行本项目代码前，必须完整读取：

1. `README.md`
2. `docs/DEVELOPER_GUIDE.md`
3. `docs/DIGITAL_HUMAN_INTEGRATION_20260803.md`

如果任务涉及数字人生成、任务分流、账号、后处理状态、片段下载或数字人服务器接口，还必须读取数字人项目中的：

1. `D:\工作内容\轻盈健\数字人\runninghub_mvp\README.md`
2. `D:\工作内容\轻盈健\数字人\runninghub_mvp\DEVELOPER_GUIDE.md`
3. `D:\工作内容\轻盈健\数字人\runninghub_mvp\数字人网站与剪映工作台集成说明.md`

## 按任务补读

- 修改 Web 接口、登录、数字人任务收件箱或下载：读 `docs/WEB_API.md`。
- 修改 `VIDEO_ENHANCING`、SeedVR2 清晰片段、数字人源片段或其质量元数据：同时读
  `D:\工作内容\轻盈健\数字人\runninghub_mvp\SeedVR2视频清晰化流程开发文档.md`。
- 修改渲染参数、字幕 cue、SRT、BGM、视频源或保存目录：读 `docs/RENDER_JOB_SCHEMA.md`。
- 修改项目结构或跨模块代码：读 `docs/PROJECT_LAYOUT.md` 和 `docs/PROJECT_STATUS.md`。
- 修改本地安装和处理机使用：读 `START_HERE.md`、`docs/PROCESSOR_DEPLOYMENT.md`。
- 修改更新包或打包：读 `docs/PROCESSOR_UPDATE.md`、`docs/FAST_BUILD.md`。
- 修改公用机或多处理机：读 `docs/SHARED_PROCESSOR_QUICK_START.md`、`docs/MULTI_PROCESSOR.md`。
- 修改本地草稿读取或采集器：读 `docs/LOCAL_COLLECTOR.md`；分析已有草稿时再读 `docs/DRAFT_IMPORT_ANALYZER.md`。
- 修改素材库时，只读对应专项文档：`AUDIO_LIBRARY.md`、`FONT_LIBRARY.md`、`FLOWER_TEXT_LIBRARY.md`、`TEXT_TEMPLATE_LIBRARY.md`、`STICKER_LIBRARY.md` 或 `EFFECT_LIBRARY.md`。
- 修改 MiniMax 文本合成、脚本标点、人工停顿、语音配方、音频幂等或 raw cues 绑定时，
  还必须读取 `D:\工作内容\轻盈健\数字人\语音标点停顿配方开发文档.md`。

## 历史文档的使用方式

- `apps/auth_center/README.md` 是旧认证中心资料，不得作为当前普通账号设计依据。
- 当前普通用户账号来自数字人网站；工作台的 `/local-admin/login` 只用于本地技术管理。

## 信息优先级

发生冲突时按以下顺序判断：

1. 当前代码、数据结构和自动化测试
2. `docs/DIGITAL_HUMAN_INTEGRATION_20260803.md` 中已经确认的产品决策
3. `docs/DEVELOPER_GUIDE.md`
4. `README.md`、`START_HERE.md` 和专项操作文档
5. `docs/PROJECT_STATUS.md` 与历史文档

发现文档与代码不一致时，先核对代码和测试，不要静默采用旧描述；完成修改后同步更新对应文档。

## 当前不可破坏的边界

- 工作台普通账号复用数字人网站账号，不保存用户密码；本地联调默认连接 `http://127.0.0.1:8000`。
- 工作台服务默认监听 `8010`，不得改回与数字人网站冲突的 `8000`。
- 任务列表可以每 15 秒刷新，但不能在用户未确认时自动导入、自动渲染或自动发布。
- 只有 `AUTO_READY`、`AUTO_POSTPROCESS` 且只有一个成功视频的任务允许一键导入。
- 上传音频和多片段任务提供原始片段下载，由人工粗剪后继续处理。
- 数字人精确字幕只在原视频、原文本和原时间轴未被修改时有效。
- 本轮视频保存在本地，可选择并记忆输出目录；未经明确要求，不增加阿里云上传或自动发布。
- 未经用户明确要求，不修改或迁移云端数字人账号数据库，也不部署生产环境。
