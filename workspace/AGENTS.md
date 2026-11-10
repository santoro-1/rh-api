# Codex 工作区入口

## 文档职责（忘记时先看这里）

```text
AGENTS.md                       项目规则、任务路由、长期边界
runninghub_mvp/PROJECT_MAP.md   模块地图、代码位置、专项文档入口
模块开发文档                     模块当前怎么工作
runninghub_mvp/CHANGELOG.md     历史上什么时候改了什么
```

忘记模块或文档在哪里时，先打开 `runninghub_mvp/PROJECT_MAP.md`；不知道当前任务应该读什么时，
回到本文件和 `runninghub_mvp/AGENTS.md`。模块开发文档按模块长期维护，同一模块的小改动优先更新
原文档，不为每个任务重复新建一份 Markdown。

本工作区的主要项目 `runninghub_mvp` 负责第一版数字人网站和 RunningHub 生成服务；第二版
FlashCut 客户端不属于这个仓库。开始第一版相关任务前，先完整读取：

1. `runninghub_mvp/AGENTS.md`
2. `runninghub_mvp/PROJECT_MAP.md`

后续按 `runninghub_mvp/AGENTS.md` 的任务分类补读文档。不要让所有任务默认加载全部历史文档。
涉及剪映工作台时，还必须读取 `D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\AGENTS.md`
及其中规定的工作台文档。

当前产品边界：普通数字人生成路线和 LTX 对口型路线均已废弃，MiniMax H3 是唯一继续维护的
画面生成路线。除非用户明确要求恢复、修复历史任务或清理遗留数据，不再为前两条路线新增功能、
优化上传、迁移服务、打包发布或安排验收。旧代码、迁移和历史数据暂作兼容与追溯保留；“废弃”
不等于授权删除。账号、MiniMax 声音、字幕/BGM、后期、成果库等被 H3 复用的公共能力仍属有效范围。

涉及 MiniMax 文本合成、脚本标点、人工停顿、语音配方、音频幂等或 raw cues 绑定时，还必须读取 `语音标点停顿配方开发文档.md`。

只有用户明确要求处理 LTX 历史兼容、恢复或清理时，才读取
`ltx_lip_sync_workbench/LTX对口型独立工作台开发文档.md` 和 `ltx2.3对口型api文档.md`。

涉及 MiniMax H3、多参考图片/视频/音频、H3 Prompt、动态图工作流、H3 自动分段、尾帧连续
或 H3 接入 JYD 时，还必须读取 `MiniMax H3多参考工作台开发文档.md`，并按任务读取
`C:/Users/san/.codex/skills/h3-prompt-writing/SKILL.md` 及其指定的参考文件。

不要只根据聊天摘要、旧状态文档或历史实现修改代码。未经用户明确要求，不部署生产环境，不修改云端账号数据库。

新增会影响开发或 Agent 判断的 Markdown 文档时，必须同时把它加入
`runninghub_mvp/PROJECT_MAP.md`；需要作为任务前置资料时，还必须加入
`runninghub_mvp/AGENTS.md` 的任务路由。
