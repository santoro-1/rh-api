# 开发说明

## 边界

本地工作台只保存用户可见项目、素材引用、分段、费用确认快照和后期状态。RunningHub Key、
workflowId、账号池选择与付费 attempt 由云端负责。

`8792` 当前只承担技术验收，不是最终生产前端。现有 JYD `/app/new` 已在本地实现
`Project/ProjectItem` 级 H3 模式，复用声音中心、MiniMax 生成与独立审核、Excel/CSV 批量、
参考视频范围映射和整批后期；H3 结果回填原项目行，不再依赖这里按行创建孤立 JYD 项目。
生产部署和新增真实付费验收仍未授权。完整状态与完成定义见上级目录
`MiniMax H3多参考工作台开发文档.md` 第 19 节。

## 输入层级

```text
project
  batch_identity_images[0..5]
  defaults: continuity_mode / resolution / generation_tail_seconds
  items[1..n]
    script_text
    reference_video
    full_audio
    raw_cues
    optional user_direction / continuity_mode / resolution override
```

人物图在批次内共用；参考视频属于脚本行。`soft_chain` 的 continuity anchor 是运行产物，不是
用户输入，也不计入 0～5 张人物图。

## 报价合同

`remote_batch_id` 和费用快照只对生成报价时的素材、逐行输入、默认参数与账号选择有效。报价
尚未确认时，只要这些输入发生变化，本地必须同时清空批次 ID 和快照，要求重新执行
`prepare`；无变化保存不作废报价。批次确认进入执行后，费用相关输入锁定，避免清除远端批次
引用导致运行结果失联。项目名称不属于费用相关输入。

本地上传文件只用于转发给云端素材接口，成功或失败后均立即删除；项目数据库只保存云端
`asset_id` 和用于界面展示的原始文件名。

MiniMax 音频通过云端 `h3-audio-sources` 摘要选择，不允许页面手工编辑批次 ID、行 ID 或版本。
摘要只包含当前账号音频/raw cues 完整的来源：既包括 `AWAITING_REVIEW`，也包括已有审核记录的
`SUCCESS`。后者可以重复绑定新的 H3 批次，确认时不得改写原审核时间、来源行状态或重新产生
MiniMax 费用。`AWAITING_REVIEW` 必须先通过独立 review 接口显式审核，`confirm` 不得顺带批准。
选中后原稿只读。试听由本地受登录保护的代理下载到随机临时文件，响应完成后删除，
云端访问令牌不进入音频 URL 或浏览器存储。

## 适配器能力声明

`/api/workbench/h3-execution-accounts` 同时返回公开的 `adapter_capability`。本地前端从该声明
读取用户人物图上限、衔接模式、画面比例和 MP 范围；本地保留兼容旧云端的 v1 默认值。能力
声明不得包含 ComfyUI 节点 ID、workflowId、工作流访问密码或 RunningHub Key。

## 分段

分段器只在 raw cue 边界切分，使用动态规划优先选择 8～12 秒和强标点边界，同时保证
`segment_audio_duration + 0.5` 落在 4～15 秒。它不会改写台词，也不会用 ASR 重新猜原稿。

## 余量与后期

生成端可比输入音频多请求 0.5 秒。这是 H3 推理窗口的安全余量，不代表输出里存在一个可以按
输入时长机械裁掉的静音尾巴。标准化必须保留 H3 返回的完整音画；连续模式从完整 H3 成片的
最后可见帧生成 anchor，任何流程都不得从片段中间删除 0.5 秒。

`h3.jyd_handoff.v2` 同时登记完整 `h3_master` 和 JYD 分轨布局。分轨布局把同一份 H3 成片无损拆成
从 0 秒开始的静音 `base_video` 与一次性的 H3 权威音频，防止基础视频与独立音轨重复发声。
MiniMax 输入音频和它的 raw cues 只用于生成条件、冻结原稿与来源审计，不得覆盖 H3 解码音轨。
字幕先用每个 H3 实际输出段的完整时长建立宽窗口，再让 FunASR 在这些窗口内将冻结原稿对齐到
H3 实际语音；因此字幕时间跟随最终成片，而不是跟随输入音频。

多段合并使用两次 stream-copy：第一遍拼接 H3 原生音画，第二遍按探测到的音视频起始时间分别
归零。该过程不重编码段内样本，避免因为 AAC priming 或重新编码引入可见口型偏移。
