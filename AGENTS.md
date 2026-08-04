# Codex 项目指令

本文件用于告诉 Codex 在新任务开始时应先读取哪些项目文档。不要只根据聊天摘要或旧的 `PROJECT_STATUS.md` 修改代码。

## 每个新任务的必读文档

在分析、修改或运行本项目代码前，必须完整读取：

1. `README.md`
2. `DEVELOPER_GUIDE.md`
3. `WORKBENCH_INTEGRATION_20260803.md`

如果任务涉及剪映工作台、账号共用、字幕、BGM、视频导入、人工粗剪、任务拉取或自动发布，还必须读取工作台项目中的：

1. `D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\README.md`
2. `D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\docs\DEVELOPER_GUIDE.md`
3. `D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\docs\DIGITAL_HUMAN_INTEGRATION_20260803.md`

## 按任务补读

- 修改任务生成、45 秒切分、状态机、取消、重试、数据库或 Worker：读 `MAINTENANCE.md`、`PROJECT_STATUS.md` 和 `CHANGELOG.md` 的最新日期。
- 修改或增加 RunningHub 工作流：读 `WORKFLOW_EXTENSION.md`。
- 修改生产部署、服务器配置、备份或恢复：读 `deploy/README.md`。
- 修改固定电脑媒体处理、FunASR 或便携媒体节点：读 `media_node/README.md`。
- 修改数字人与工作台之间的接口：同时读工作台的 `docs/WEB_API.md` 和 `docs/RENDER_JOB_SCHEMA.md`。

## 信息优先级

发生冲突时按以下顺序判断：

1. 当前代码、数据库迁移和自动化测试
2. `WORKBENCH_INTEGRATION_20260803.md` 中已经确认的产品决策
3. `DEVELOPER_GUIDE.md`
4. `README.md` 和专项操作文档
5. `PROJECT_STATUS.md`、旧 `CHANGELOG.md` 和历史文档

发现文档与代码不一致时，先核对代码和测试，不要静默采用历史描述；完成修改后同步更新对应文档。

## 当前不可破坏的边界

- 本地数字人测试账号与服务器生产账号是两套数据库；未经用户明确要求，不得修改、迁移或删除生产账号数据。
- 普通用户账号以数字人网站为唯一来源；工作台不另存普通用户密码。
- 上传音频和多片段数字人任务进入人工处理；多片段自动拼接仅供快速预览，原始片段必须保留。
- 只有文本语音且最终未切分的单片段任务可以标记为自动后期。
- 数字人网站本地端口为 `8000`，剪映工作台本地端口为 `8010`。
- 未经用户明确要求，不部署服务器、不自动发布、不启用阿里云上传。

