# Web API

## 新版页面入口与登录（模块 1）

```text
GET  /app/new
GET  /app/new/templates
GET  /app/new/gallery
GET  /app/new/voices
GET  /app/new/login
POST /api/auth/login
GET  /api/auth/session
POST /api/auth/logout
```

前四个业务页面要求有效的数字人普通账号会话；未登录访问时以 `303` 跳转到
`/app/new/login?next=原地址`。登录接口仍由工作台代理数字人网站账号验证，并设置
HTTP-only Cookie。`next` 只接受受工作台保护的站内路径，新版登录页面额外把返回范围
限制在 `/app/new` 内，避免开放跳转。静态页面不得保存数字人访问令牌。

模块 1 只负责登录、会话、导航和退出；脚本/图片与声音分别已由模块 2、3 接入。画面
合成、变体和成果库已分别由模块 4、6、7 接入真实状态；不能把界面定时器的显示结果当成
后端业务状态。

## 脚本与图片输入（模块 2）

脚本文件正式模板固定为四列：`任务ID`、`脚本内容`、`文章类型`、`分配账号`；历史两列表
继续兼容。可从新版页面下载模板：

```text
GET  /api/new/script-template
POST /api/new/script-imports/preview?filename=脚本.xlsx
```

预览接口使用原始请求体接收 `.xlsx` 或 `.csv`，不创建项目。服务端验证文件大小、XLSX
压缩包路径和解压大小、固定表头、空 ID、空脚本、四列表分类字段、重复 ID、额外列和
500 行上限。任何
一行失败时整体返回 `422`，不会产生半个项目。旧版 `.xls` 需先另存为 `.xlsx` 或 `.csv`。

解析通过后使用模块 0 的 `POST /api/new/projects` 创建项目；重新导入或调整脚本行使用：

```text
PUT    /api/new/projects/{project_id}/inputs
POST   /api/new/projects/{project_id}/items
PATCH  /api/new/projects/{project_id}/items/{item_id}
DELETE /api/new/projects/{project_id}/items/{item_id}
DELETE /api/new/projects/{project_id}
```

`PUT inputs` 在一个数据库事务内完成 ID、脚本内容和顺序的整体替换，失败时保留更新前
数据。只有全部脚本行仍为 `DRAFT` 才能整体替换输入。删除整个项目允许草稿、失败或已完成
批次，但会拒绝仍在内容/视觉分析、声音、画面、后期、变体或成果导出中的批次；成功后级联
删除本地项目记录，并只物理清理工作台受管根目录内且不再被其他项目引用的文件。成果目录还
必须符合“成果根目录/日期/数字批次号”结构，云端数字人任务不会远程删除。`POST items` 只追加
一条新的 `DRAFT` 行，不改写已有行，因此已有声音或视频版本时仍可
用“添加分段”继续增加测试脚本；若项目已有图片池，新行按当前映射策略取得图片。单行删除允许
草稿、失败或已完成任务，但禁止删除正在执行内容分析、声音、画面、后期
或变体异步操作的行。任务可以删到 0 行，之后仍可通过“添加分段”重新创建；删除会级联移除
该行本地素材版本、操作和外部关联，并清理该行不再被引用的本地生成文件，项目公共图片池
不受影响。

已有项目补充分类信息使用：

```text
PUT /api/new/projects/{project_id}/metadata-import?filename=脚本.xlsx
```

请求体为四列 `.xlsx`/`.csv` 原始内容。服务端要求任务 ID 集合与当前项目完整一致，以任务 ID
写入 `settings.source_metadata.article_type` 和 `assigned_account`；Excel 中的旧脚本文字不会
覆盖当前项目脚本。该事务允许项目处于生成中，只更新分类元数据和项目修订，不清除或失效
声音、视频、字幕、内容分析、语义视觉及变体，并把四列表保存为新的脚本源文件版本。

`POST /api/new/projects/{project_id}/items/batch` 接收 `{ "items": [{ "row_key": "...",
"script_text": "..." }] }`，用于“追加表格”。项目容量、已有任务 ID、批内重复 ID 和全部
脚本文字在同一事务中校验，成功后一次追加并按当前图片策略分配；任何一项无效时整批回滚。
该接口不会替换旧行，也不会启动内容分析或任何付费生成。

智能内容分析模块 5 增加：

```text
POST /api/new/projects/{project_id}/content-analysis
POST /api/new/projects/{project_id}/items/{item_id}/content-analysis/retry
```

项目接口请求体可选：

```json
{
  "item_ids": ["可选的工作台脚本行 ID"],
  "force_refresh": false
}
```

该项目接口由“生成声音预览和脚本分析”动作触发，但浏览器只提交本批声音目标中首次导入或脚本内容
变化、状态为 `NOT_REQUESTED` 的行；单纯重新生成声音不调用该接口。工作台把目标行拆成
一条脚本一次数字人后端请求，单批最多并发 10 行。普通请求对脚本哈希未变化且已有
`PENDING`、`SUCCESS`、`PARTIAL` 或 `FAILED` 尝试的行保持幂等跳过。单行 retry 固定强制
刷新，用于用户显式重试失败分支。项目响应的每个 item 新增
`content_analysis`，项目顶层新增 `content_analysis_summary`。音乐、字幕和标题分别保存
`NOT_REQUESTED | SUCCESS | FAILED`，请求执行期间顶层 `overall_status=PENDING`，最终为
`SUCCESS | PARTIAL | FAILED`。脚本修改只失效该行分析快照；分析失败返回合法项目状态，
不重新调用 MiniMax/RunningHub/剪映，也不清空 `raw_cues` 或已有音视频。前端在请求发出时立即把
目标行显示为“AI 分析中”；音乐分支成功后，工作台会用本地标签库预选唯一 Top1 并保存到
`settings.postprocess`，因此同一次响应即可在背景音乐下拉框显示具体曲目。

工作台对每行先从本地 catalog 召回字符候选，再把不含路径、素材 ID 和时间的
`visual_context` 随同一条 `/api/workbench/content-analysis` 请求发送。响应中的
`visual_plan` 每项只有 `anchor_id`、`concept_id`、`priority`；工作台复核引用范围后保存到
该行 `visual_analysis`，具体素材选择和 MiniMax raw cues 时间绑定仍完全在本地执行。

智能内容分析模块 6 不增加独立外部 API；它在既有
`POST /api/new/projects/{project_id}/postprocess/generate` 生成浏览器 4B 预览时执行。每个
item 的 `subtitles` 增加：

```json
{
  "raw_cues": ["MiniMax 原始时间戳，永久保留"],
  "render_cues": ["语义映射或安全降级后的派生字幕"],
  "asr_alignment": {
    "schema": "jyd.asr-caption-alignment.v1",
    "status": "SUCCESS",
    "script_sha256": "...",
    "audio_asset_id": "...",
    "audio_version": 1,
    "provider": "funasr_http",
    "device": "cpu",
    "exact_match_ratio": 0.98,
    "ranges": ["按原脚本 token 保存的字词时间范围"]
  },
  "semantic_mapping": {
    "schema": "jyd.semantic-caption-mapping.v1",
    "status": "NOT_REQUESTED | SUCCESS | FALLBACK",
    "reason_code": null,
    "reason_summary": null,
    "script_sha256": "...",
    "analysis_script_sha256": "...",
    "audio_asset_id": "...",
    "audio_version": 1,
    "mapped_unit_count": 0
  }
}
```

智能内容分析模块 7 同样不增加外部模型请求。内容分析完成后先使用合法 `music_intent` 和
本地 46 首标签库计算可见的唯一 Top1；4B 请求中的每一行仍可显式传
`bgm_selection_mode=auto|manual`：`auto` 会结合当前音频真实时长复核唯一 Top1，`manual`
使用用户指定曲目或明确的无 BGM。声音版本变化时保留当前推荐供界面显示并标记 `STALE`，
4B 前再按新时长复核。
选择结果保存在 `settings.postprocess.music_selection`，包含选择来源、当前脚本摘要、音频
素材 ID、时长、matcher/taxonomy/profile 版本与标签库哈希；不会返回 Top3 或候选列表。
音乐分析失败或本地匹配失败时按“项目明确默认音乐 → 无 BGM”降级，不影响字幕映射或
4B 预览。手动曲目和手动无 BGM 都不会被后续 AI 分析覆盖。

模块 8 没有增加 API。跨项目 mock 验收直接使用数字人服务端实际内容分析响应，确认本节
状态、字段和两种部分成功结果均能被工作台消费；错误字符索引只在文字完整重建原文时由
服务端重算，空格、换行和 `~` 仍按精确字符处理。

`subtitle_units` 只决定语义断句，模型返回的时间字段仍会被拒绝。最终排版完成后，本机
FunASR 只识别字词位置，再与原脚本文字做确定性对齐；MiniMax `raw_cues` 继续作为不可跨越
的句级硬边界。ASR 精确命中率低于 90%、单个 raw cue 命中率过低、脚本/音频版本变化或
本机服务不可用时标记 `REVIEW_REQUIRED`，不会静默退回会累计漂移的等字数插值，也不会
覆盖 `raw_cues`。成功缓存按脚本 SHA-256、音频素材 ID 和版本复用，改字体或重试 4B 不会
再次识别。

项目图片池和映射接口：

```text
POST   /api/new/projects/{project_id}/images?filename=画面.png
GET    /api/new/projects/{project_id}/images/{image_id}
DELETE /api/new/projects/{project_id}/images/{image_id}
PUT    /api/new/projects/{project_id}/image-mapping
PUT    /api/new/projects/{project_id}/image-mapping-scope
PUT    /api/new/projects/{project_id}/items/{item_id}/image
```

图片上传使用原始请求体，单张最大 200 MB，只接受内容与扩展名一致的 JPG、PNG、WEBP。
文件选择器本次返回的每个文件都会创建新的项目图片记录和 `image_id`，不按文件名或 SHA-256
跳过；同名、同内容或改名后的文件再次上传，也视为用户明确选择的本批新图片。SHA-256 仍用于
后续任务输入快照校验，不用于项目图片上传判重。
前端上传时若表格没有勾选行，会先以空 `item_ids` 清除可能残留的局部范围，再不传
`image_ids` 执行原有全项目图片池映射；若存在勾选行，则以这些行覆盖范围，并只传本次上传
返回的新 `image_ids`，因此旧图片池不会进入该局部批次。
`image-mapping-scope` 接收 `{"item_ids":["脚本行 ID"]}`，用请求中的完整 ID 集合覆盖本次
人物图换图范围；空数组清除范围。范围状态写入现有行级设置，刷新和工作台重启后继续生效，
但不会冻结行的当前图片，也不阻止范围内单行换图。历史项目默认没有范围。

`image-mapping` 由后端按图片上传顺序计算：`count` 表示每张图片连续复用 `reuse_count`
行，脚本超出后从第一张继续；`loop` 表示每行依次取下一张并循环。最终映射保存为每条
脚本的 `input_image` 素材版本，页面刷新只读取后端结果。单行替换创建新版本并切换当前
图片，不覆盖旧版本。换图范围非空时，批量映射只处理范围内行并从范围第 1 行重新计数；没有
范围时处理全部行。请求可选传 `image_ids`，且这些 ID 必须属于当前项目，用于只将本次新上传
的图片在目标范围内循环；成功后该 ID 集合保存在项目 `settings.image_mapping.image_ids`，页面
刷新或切换 count/loop 时继续使用，不会退回整个旧图片池。存在换图范围时，仍被范围外脚本
引用的图片不能删除，避免间接改写
已经生成的前序行；其他删除继续按当前策略处理。删除最后一张时清空可处理行的当前图片并使
后续画面结果失效。对应旧输入图片版本和本地文件一并清理。
若图片仍被正在执行声音、画面、后期或变体任务的行使用，返回 `409`，待该行完成后再删除。

## 音色与声音生成（模块 3）

浏览器始终只访问工作台。工作台使用 HTTP-only 会话中的短期令牌代理数字人后端，
MiniMax Key、官方 voice ID 校验、声音制作任务和付费音频任务仍由数字人后端管理。

```text
GET  /api/new/voices
PUT  /api/new/voices/default
POST /api/new/voices/import
POST /api/new/voices/{voice_asset_id}/preview
GET  /api/new/voices/{voice_asset_id}/preview
POST /api/new/voices/{voice_asset_id}/activate
DELETE /api/new/voices/{voice_asset_id}

POST /api/new/voice-creations
POST /api/new/voice-creations/{task_id}/save
GET  /api/new/voice-creations/{task_id}/preview

PUT  /api/new/projects/{project_id}/voice
PUT  /api/new/projects/{project_id}/items/{item_id}/voice
PUT  /api/new/projects/{project_id}/digital-human-settings
POST /api/new/projects/{project_id}/audio/generate
GET  /api/new/projects/{project_id}/audio/status
POST /api/new/projects/{project_id}/items/{item_id}/audio/retry
GET  /api/new/projects/{project_id}/items/{item_id}/audio
```

`GET /api/new/voices` 返回三个经产品确认且当前 MiniMax 账号实际可用的官方音色、账号
已保存的克隆/融合音色、最近声音制作任务和工作台默认音色。三个官方 voice ID 为：

```text
Chinese (Mandarin)_Reliable_Executive
Chinese (Mandarin)_Warm_Girl
Chinese (Mandarin)_Unrestrained_Young_Man
```

官方音色首次试听是一次真实 MiniMax 合成，必须提交 `cost_confirmed: true`；成功后缓存，
以后直接读取缓存。声音制作使用 `multipart/form-data`，克隆需要 `source_a`，融合还需要
`source_b`；费用确认、时长、格式、保存状态和失败恢复继续执行数字人后端既有规则。

`PUT /api/new/voices/default` 的 `voice_settings.speed` 支持 `0.5–2.0`。核心工作台把生成
语速控件放在“选择项目默认配音声音”下方，滑杆按 `0.01` 调整，并提供 `0.8×`、`0.9×`、`1.0×` 快捷值和完整
范围滑杆。该值是当前工作台账号的生成偏好，切换音色后沿用；只影响之后的新生成或
重新生成，不改写已有音频。缓存试听在浏览器按同一倍率播放。
新音频素材会冻结本次生成的 `speed`，单条下载和项目声音 ZIP 都使用
`{任务序号}_{speed}倍速.mp3`，例如 `2_0.9倍速.mp3`；内部素材路径和任务 ID 不改变。

`PUT /api/new/projects/{project_id}/voice` 接收 `voice_asset_id`，验证它属于当前数字人账号
且已保存，然后原子地写入项目默认音色和全部脚本行。若任一需要修改的脚本行正在异步
处理中，整次请求返回 `409`，不会只更新一部分。已生成音频的脚本行切换音色后会清空
当前音频指针并回到 `DRAFT`，但历史素材版本仍保留。单行下拉框使用
`PUT /api/new/projects/{project_id}/items/{item_id}/voice` 覆盖项目默认值。

声音制作任务生成成功后进入 `PREVIEW_READY`，此时可通过 preview 接口试听；只有调用
save 接口成功并进入 `SAVED` 后，才会生成自定义音色卡。新卡最初为 `READY` 且
`selectable: false`；`POST /api/new/voices/{voice_asset_id}/activate` 必须携带
`cost_confirmed: true`，数字人后端用该克隆音色执行一次短 TTS，成功后标记 `ACTIVE`
和 `selectable: true`。这次调用是 MiniMax 的首次正式使用，会触发音色复刻费和短文本
合成费。保存本身不触发首次使用费用。

`POST /api/new/voices/import` 接收
`{"voice_id": "...", "name": "...", "already_activated": true}`。工作台只代理当前登录
账号，数字人后端会向当前配置的 MiniMax Key 查询 `voice_cloning` 列表并精确核对 ID。
导入始终不调用 T2A：若用户确认该 ID 已在同一 MiniMax 账号成功调用过 T2A，则直接登记为
`ACTIVE`、`selectable: true`；否则登记为 `READY`、`selectable: false`，之后仍需在音色卡
上明确点击“激活”并确认费用。

`DELETE /api/new/voices/{voice_asset_id}` 只允许删除自定义音色卡，并由前端二次确认。
若工作台任一当前项目的默认音色或脚本行仍引用该音色，返回 `409`；删除后历史声音制作
任务及已生成音频继续保留。MiniMax 官方音色不可从声音中心删除。

项目声音生成请求至少包含 `default_voice_asset_id`、`idempotency_key` 和
`cost_confirmed: true`，可用 `voice_assignments` 按脚本行覆盖默认音色。工作台按音色
分组创建数字人音频批次，保存批次/批次行关联和 `AUDIO_GENERATE` 操作。后端只处理
`DRAFT` 或 `AUDIO_FAILED` 行，不重新生成已就绪音频。此阶段只提交脚本、音色和语音
参数，不上传或校验项目图片；内部远程幂等键使用固定长度哈希。
单行 `audio/retry` 同时提交当前 `voice_settings`；数字人后端在创建下一版 attempt 前更新
任务语速，因此不会继续沿用旧版本的 `speed`。

请求可选传 `item_ids: ["项目脚本行 ID"]` 只处理指定行。显式单条请求采用智能复用：
指定行仍有当前 `audio` 时直接返回项目，不创建 MiniMax 批次；脚本或音色修改后当前指针
已失效，同一入口才会生成新音频版本。省略 `item_ids` 时保留原项目级批量行为。

数字人音频批次强制启用审核门：MiniMax 成功后停在 `AWAITING_REVIEW`，不会自动创建
RunningHub 画面任务。状态同步会把 MP3 下载到工作台项目目录，创建新的 `audio` 素材
版本并切换当前音频，同时保存 MiniMax 原始 cue 为 `raw_cues` 和第一版
`render_cues`。重新生成显式确认可能再次计费，旧音频版本不覆盖、不删除。

## 新版画面接口（模块 4A）

模块 4A 只负责 RunningHub 生成结果和基础视频，不执行字幕/BGM 剪映后处理：

```text
POST /api/new/projects/{project_id}/composition/generate
GET  /api/new/projects/{project_id}/composition/status
POST /api/new/projects/{project_id}/items/{item_id}/composition/retry
GET  /api/new/projects/{project_id}/items/{item_id}/base-video
GET  /api/new/projects/{project_id}/items/{item_id}/preview-video
GET  /api/new/runninghub-execution-accounts
```

`base-video` 保持返回后期时间线使用的当前基础画面；H3 项目中的该文件按合同不含声音。
`preview-video` 专供浏览器动态预览：当前基础画面来自 H3 时，返回与其
`h3_segment_signature` 一致的有声 H3 母版；其他画面模式回退到当前基础画面。这样浏览器可以
直接试听 H3 原声，同时不改变剪映草稿和导出的“静音底片 + 独立权威音频”轨道结构。

H3 单分段重试前，页面必须先调用 `GET /api/new/projects/{project_id}/h3/status` 取得当前批次
快照。若原按钮携带的 `segment_id` 已被后续批次替换，页面刷新后停止，不得自动映射到同序号
的新分段，也不得进入费用确认；服务端对并发期间失效的分段返回 `409`，避免重试另一份冻结输入。

H3 原始分段人工检查使用：

```text
GET /api/new/projects/{project_id}/items/{item_id}/h3-segments/{segment_number}/preview
```

`segment_number` 从 1 开始。H3 状态轮询发现某段 `SUCCESS` 后立即把该段下载到项目受管的
`h3/segment-cache`，最多并发三路；接口读取当前批次、当前远端 item 和当前 `segment_id` 对应的
本地缓存，不等待同一行全部成功，也不依赖 `base_video` 已经合并完成。云端返回的
`normalized_video_sha256` 与 `completed_at` 共同参与结果版本绑定；同一分段主动重生成时旧缓存继续
可预览，新文件校验摘要后原子替换，不同批次或不同分段 ID 不按序号复用旧片。所有当前版本均已
落盘后才运行 H3 母版合并与静音底片/权威音频拆分。

接口要求项目和脚本行属于当前登录账号，并再次限制路径位于工作台受管目录。它不代理云端下载
地址、不返回访问令牌，也不会触发 H3 生成、重试或任何费用；缓存缺失或越界时返回 `404`。
浏览器以 `preload=metadata` 使用该接口，弹层不会同时自动播放多条视频。

`digital-human-settings` 当前保存项目级 `resolution`，含义为数字人画面的最长边像素。
新版页面允许直接输入任意正整数，默认值为 `1024`。修改时会保留历史视频文件，但解除旧分辨率
基础视频的当前绑定；已生成的 MiniMax 声音继续复用。`audio/generate` 与
`composition/generate` 都会携带同一项目值，后者负责在真正创建 RunningHub 画面前冻结
最终分辨率。正在生成的项目不能修改该设置。

启动请求必须包含 `cost_confirmed: true` 和 `idempotency_key`。工作台根据模块 3 保存的
`digital_human_audio_item` 关联调用数字人后端；前端不取得数字人令牌，也不直接访问
数字人服务。启动时工作台才读取并上传该行当前图片，由数字人后端把图片绑定到已审核
音频后创建 RunningHub 子任务。脚本行状态按真实任务依次使用 `COMPOSITION_QUEUED`、
`DIGITAL_HUMAN_RUNNING`、`VIDEO_ENHANCING`、`VIDEO_MERGING`、`BASE_VIDEO_READY` 或
`COMPOSITION_FAILED`。

`GET /api/new/runninghub-execution-accounts` 返回云端权威模式。`same_account_v1` 对管理员或
受控测试授权用户返回 `pool_access=true`、原单池摘要和非空
`runninghub_execution_account_ids: number[]`；每个分段的数字人与 SeedVR2 使用同一执行账号。
无授权普通用户的 `pool_access=false`，继续自己的单账号。`dual_pool_v1` 返回
`digital_human`、`seedvr2` 两组安全摘要，首次启动必须同时提交非空
`runninghub_execution_account_ids`、`seedvr2_execution_account_ids`，并把
`execution_mode: "dual_pool_v1"` 冻结在本地持久化操作中。两组按各自
`default_selected_account_ids` 每次重新默认全选。摘要只含内部 ID、名称、健康/冷却/启用、
运行数和容量，不含 API Key、指纹、Base URL 或 App ID。云端仍重新判权和决定模式；工作台
不会把本地模式字段作为云端授权开关。受控非管理员只有云端明确授权才会收到单池选择或双池
摘要；显示哪一种由云端管理员网页开关决定。失败阶段重试和 SeedVR2 补跑不重新选择或修改快照。

4A 费用确认明确显示“一控多”或“双池”及本次候选账号。任务清单的
`composition.execution_assignments[]` 是云端实际分配结果，按 `segment_index` 提供
`digital_human.account` 与 `seedvr2.account` 的安全内部 ID和备注名称以及阶段状态。一控多的
两个阶段显示同一账号；双池分别显示独立账号；尚未原子预留时账号为 `null`，页面显示“待分配”。
工作台把这组字段写入逐行 `COMPOSITION_GENERATE.result`，使刷新和重启后仍可恢复显示；接口
不得返回 API Key、凭据指纹、Base URL 或 App ID。

`composition/generate` 只同步校验并为目标行创建持久化 `COMPOSITION_GENERATE/PENDING`
操作，然后快速返回。每行快照保存声音批次、远程行、图片资产 ID及 SHA-256、分辨率、执行
模式、两组账号 ID范围和稳定 `请求幂等键:行 ID`。最多 4 个后台线程原子执行
`PENDING -> STARTING -> RUNNING` 的图片上传和云端交接；`RUNNING` 才表示云端已接受。
单行失败不阻断其余行；进程启动把中断的 `STARTING` 恢复为 `PENDING`，下一次携带有效登录
Cookie 的状态请求按原幂等键继续。已经 `RUNNING` 的付费任务不得退回重提。

`VIDEO_ENHANCING` 表示云端已有数字人源片段，正在逐段执行固定 48G 的 SeedVR2。该状态
属于活动状态，工作台必须继续轮询，不能提前下载、进入 4B 或显示为失败。云端任务清单中
每个视频可返回 `quality_variant: "seedvr2_upscaled"`、`enhancement_status` 和
`source_download_url`；主 `download_url` 始终指向清晰片段。

修改项目分辨率会设置 `composition_invalidated_reason=DIGITAL_HUMAN_RESOLUTION_CHANGED`。
若该行云端已经保存成功数字人源片段，即使本地基础视频因归一化失败而尚未建立，工作台也会
开放显式 SeedVR2 高清补跑；下一次补跑不再上传图片或调用数字人画面启动接口，而是由工作台服务端代理调用云端
`POST /api/workbench/tasks/{remote_item_id}/enhancement/backfill`。该动作只补跑 SeedVR2 48G，
操作快照使用 `scope=seedvr2_backfill_only`；原 `remote_item_id` 和数字人付费任务保持不变。
补跑完成后的状态同步、清晰片段下载和 `base_video` 下载沿用现有流程。任一源片段缺失时
云端整行拒绝，工作台不得把旧清晰度视频冒充新结果。

若本地没有保存任何数字人源片段（典型情况是用户在 RunningHub 手动取消了数字人阶段），
则同一失效标记不会调用 enhancement backfill。工作台上传该行当前图片并调用原 4A 启动接口，
云端从已审核音频重建全新数字人任务并采用当前项目分辨率；此路径不会重新生成 MiniMax 声音。

内部云端请求同时提交当前输入图片 SHA-256。相同摘要是幂等重试；摘要变化表示用户明确
换图，云端保留已批准的 MiniMax 音频和原始时间戳，仅清除旧图片的画面子任务与合并结果后
重新排队。云端清单的 `composition.image_sha256` 必须与本地 `COMPOSITION_GENERATE`
操作快照一致，否则本地以 `REMOTE_IMAGE_VERSION_MISMATCH` 拒绝下载旧视频。

请求可选传 `item_ids` 只启动指定行。指定行已有当前 `base_video` 时直接复用，不再次调用
RunningHub；若只修改字幕/BGM，则单条控制直接把该行交给 4B。4B 的 `items` 本来就是
显式子集，其他行未完成不会阻止已具备基础视频的当前行生成完整浏览器预览。

所有成功 SeedVR2 清晰分段下载为 `original_video_segment` 历史素材；这里的名称表示项目
时间轴中的原始有序分段，不表示未经清晰化。素材 metadata 保存 `quality_variant`、
`enhanced_by=runninghub_seedvr2` 和云端源片段可用标记。工作台默认不额外下载数字人源片段，
避免本地磁盘翻倍。标准化/拼接结果保存
为当前 `base_video`，不会设置 `composition_video`，因此 `generate_variants` 仍为
`false`。重试只处理数字人后端判定为失败或已取消的 RunningHub/下载任务或拼接阶段；成功的
付费子任务不重做。

RunningHub 手动取消后的“生成视频”按取消时所处阶段创建新的外部命令：数字人阶段取消时，
工作台把项目左侧当前 `resolution` 随重试请求提交，云端清除被取消的数字人任务 ID，并用保存的
图片、音频和该分辨率重新创建数字人任务；SeedVR2 阶段取消时，云端保留已完成的数字人 MP4，
仅清除被取消的 SeedVR2 ID 并创建新的 SeedVR2 48G 任务。`1024` 只是项目当前输入值，不是恢复
分支的判断条件。若云端拒绝重试，本地本次 `COMPOSITION_GENERATE` 操作必须立即落为 `FAILED`，
不得保留假的 `PENDING/COMPOSITION_QUEUED` 状态。

图片权限按脚本行隔离：某一行进入异步生成后只锁定该行，其他未运行的脚本行仍可上传新图
并替换当前图片。上传未分配图片和删除未被任何脚本当前引用的图片不会改变运行中任务，
因此允许继续操作；设置换图范围后，重新映射只要求范围内脚本行均可编辑，范围外的运行中或
历史行自动保持原图；未设置范围时仍要求全部脚本行可编辑。4A 读取提交瞬间该行的当前
图片，进入 `COMPOSITION_QUEUED` 后只锁定该行图片。

图片映射按项目图片 ID 判断是否真实变化。对某行重复分配同一个图片 ID属于幂等操作，不创建
新版本、不清空 `base_video` / `composition_video`，也不改变该行状态。4A 操作会冻结
`input_image_asset_id` 与 `input_image_sha256`；后续失败阶段重试继承这份快照，若当前人物图哈希
已不同则拒绝重试旧远程任务。任何被 `COMPOSITION_GENERATE` 快照引用的项目图片都不能删除，
避免旧任务恢复、下载或再次导出时出现视频与封面人物不一致。

## 账号剪映模板

新版提供独立的 `/app/new/templates`“剪映模板中心”，支持上传、筛选、封面展示、重命名、
资源修复、删除和跳回核心工作台复用；“上传与生成”页底部仍保留快速选择弹窗。模板按数字人账号隔离，项目只
保存 `template_id` 和显示名称；4B 提交时服务端会再次按当前账号解析成受信任的草稿路径，前端
不能传服务器本地路径。

纯网页上传使用 Chrome/Edge 的目录授权。用户应选择具体草稿目录，通常位于
`%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\草稿名`。网页只上传
`draft_content.json`、`draft_meta_info.json` 等必要描述文件，不会上传整个草稿目录。服务端自动
识别可选的主视频占位轨和语音字幕轨；没有视频轨的纯字幕、花字、贴纸或特效模板也可保存，
生成时会自动加入当前项目主视频。已知字幕轨名称优先，其次按连续性、覆盖率、短文本比例综合判断。
时间和文案一致的描边、阴影等重复口播字幕轨会归入同一字幕组；其中一条作为当前项目字幕样式，
其余轨在预览中隐藏、生成时清空，避免同一句文案叠出两份。标题、人物介绍等固定文字不归入字幕组。

```text
GET    /api/new/jianying-templates
GET    /api/new/jianying-templates/{template_id}/cover
GET    /api/new/jianying-templates/{template_id}/browser-preview
GET    /api/new/jianying-templates/{template_id}/browser-assets/{asset_id}
POST   /api/new/jianying-template-import-tickets
POST   /api/new/jianying-template-imports/{ticket}
POST   /api/new/jianying-templates
PUT    /api/new/jianying-templates/{template_id}/draft-files?path=...
POST   /api/new/jianying-templates/{template_id}/analyze
PUT    /api/new/jianying-templates/{template_id}/resource-files?resource_key=...&path=...
POST   /api/new/jianying-templates/{template_id}/resources/complete
PATCH  /api/new/jianying-templates/{template_id}
DELETE /api/new/jianying-templates/{template_id}
PUT    /api/new/projects/{project_id}/jianying-template
```

本机采集器导入时，已登录浏览器先调用 `jianying-template-import-tickets`，获得绑定当前 `user_id`
和模板名称的十分钟一次性凭证；采集器再把 `template_center` 模式迁移包上传到凭证地址。上传接口不读取
浏览器 Cookie，也不接受客户端指定用户编号，凭证消费后立即失效。

若花字/字体/贴纸资源未进入服务端中央素材库，分析返回 `NEEDS_RESOURCES`、资源标识和剪映
`User Data\Cache` 下的候选相对目录。网页经用户再次授权后，只读取这些精确资源 ID 目录中的
白名单素材文件；找不到时明确提示先在剪映中使用一次该资源。服务端不会根据草稿中的任意绝对
路径读取服务器文件，也不会把上传者电脑的原路径返回给浏览器。

`browser-preview` 返回按当前账号鉴权的脱敏草稿时间轴：保留画布、轨道、片段、普通文字样式、
受控图片/视频素材 URL 和效果名称，但移除上传机与部署机的绝对路径。核心工作台保留并绘制新模板
除字幕组以外的文字、贴图、动画和特效；只把所识别主字幕轨的示例字幕替换为当前项目的
`render_cues`，并继承该轨的位置、字号、颜色、粗斜体、下划线、描边、阴影、缩放及可访问字体资源。
接口同时返回 `caption_track_id` 和完整 `caption_track_ids`，浏览器不会绘制字幕组内的模板旧文案，
也不会在成功后把模板解析状态框压到画面中。
字幕阴影按草稿的 `shadow_alpha`、`shadow_distance`、`shadow_smoothing` 和方向向量换算到网页画布；
`shadow_point` 不作为字号倍数直接偏移，避免网页把正常剪映阴影误画成第二份黑色字幕。
旧默认模板的标题、人名牌、免责声明、三帧封面和语义视觉层不会叠加。该动态预览不需要先编码新的 MP4，
也不代表剪映逐像素输出；剪映私有花字、动画和 shader 仍以最终草稿/导出结果为准。
最终效果仍以冻结草稿或正式导出为准。`browser-assets` 只能读取模板目录或中央素材库中由草稿实际
引用且属于预览白名单格式的文件，客户端不能传服务器路径。

模板进入 4B 后作为完整视觉母版：原音轨全部删除，主视频替换为当前 `base_video`，语音和当前 BGM
作为独立轨道加入，自动字幕内容替换到已识别主字幕轨并复制其花字、位置和动画，同组的重复字幕轨
会清空。新模板的其他文字、
贴图和效果继续保留；工作台不再加入旧默认模板的语义贴图、固定名牌、固定标题、免责声明或封面。
最终草稿时长强制跟随当前视频时长；模板中
抵达原片尾的固定视觉随片尾伸缩，新生成的字幕不会被误拉长。

## 新版字幕与 BGM 接口（模块 4B）

4B 普通预览不访问 RunningHub，也不在浏览器预览阶段启动剪映：

```text
GET  /api/new/postprocess/options
POST /api/new/projects/{project_id}/postprocess/generate
GET  /api/new/projects/{project_id}/postprocess/status
POST /api/new/projects/{project_id}/items/{item_id}/postprocess/export
GET  /api/new/projects/{project_id}/items/{item_id}/current-video
PATCH /api/new/projects/{project_id}/items/{item_id}/postprocess-settings
```

`options` 返回实际可读的真实字体文件和现有 BGM 素材。启动请求示例：

```json
{
  "idempotency_key": "postprocess-20260804-001",
  "items": [
    {
      "item_id": "项目脚本行 ID",
      "font_identity": "system:simhei.ttf",
      "bgm_identity": "",
      "bgm_selection_mode": "auto",
      "text_color": "#FFFFFF",
      "top_title": {
        "label": "减肥大实话",
        "headline": "只有坚持才能达成目标"
      },
      "cover_title": {
        "line_1": "健康真相",
        "line_2": "别再踩坑"
      }
    }
  ]
}
```

字幕使用 MiniMax `raw_cues` 派生 `render_cues`：固定居中且禁止换行、字号 `14`、最大宽度 `0.8`、
`transform_y=-850/1920`（1080×1920 参考参数 Y=-850）。过长文本按真实字体 glyph advance 测量后，
在原始 cue 时间范围内拆成连续字幕；原始 cues 不修改。缺字、字体损坏或无法满足安全
宽度/最短显示时长时返回 `409`，并把该行字幕标记为 `REVIEW_REQUIRED`，不会静默提交
溢出字幕。BGM 可不选；选择时使用音乐库 identity，并适配视频时长。服务端用 FFmpeg 测量
人声和曲目的综合响度：普通音乐以低于人声 11 dB 为目标自动计算 `bgm_volume`，限制在
`0.08..0.25`、失败回退 `0.18`；强人声音乐以低于人声 15 dB 为目标，限制在
`0.05..0.16`、失败回退 `0.1136`。该字段是服务端冻结结果，不接受客户端手工音量。

`postprocess/generate` 会先登记 `PREVIEW_READY` 配方，再向本地剪映队列提交
`skip_export=true` 的草稿生成任务；草稿结构完整后才进入 `COMPOSITION_READY`。浏览器仍直接
使用内部 `base-video`、render cues、真实字体和 BGM 预览，不创建 `composition_video`，草稿
生成阶段也不编码 MP4。只有用户明确下载普通成片时才调用单行 `postprocess/export`，此时以
`existing_draft` 复用已冻结草稿，只启动剪映编码。升级前已生成、没有冻结草稿的旧预览第一次
调用该接口时只提交 `skip_export=true` 的冻结草稿准备任务；准备完成后再次调用才会以
`existing_draft` 编码，禁止在同一个导出任务里重建时间线。后续变体仍直接把基础视频和同一配方放进变体任务一次导出，不以
普通成片作为必需中间产物。若导出因剪映窗口状态等本地原因失败，冻结草稿、`base_video`、
`PREVIEW_READY` 配方和 render cues 均继续保留；客户端只需为该行使用新幂等键重调
`postprocess/export`，不得把其他已就绪行一并提交到 `postprocess/generate`。
同一请求批量规划多个草稿时，名称在任务提交前即按批内集合预留；即使草稿目录尚未由队列
创建，同名基础名也会依次取得 `-02`、`-03` 后缀，不能把文件系统的瞬时空缺当作名称可用。
对于已有两个及以上真实数字人分段的行，`postprocess/generate` 会在冻结配方前自动用现有
工作台令牌调用一次 `/api/workbench/visual-analysis`，只判断连接处 `seam_broll` 候选并与首轮
配方合并。成功候选摘要用于幂等复用；云端失败、无合格素材或旧服务暂不支持新字段时不阻塞
4B，继续保留原 250ms 溶解。旧多段项目重新生成 4B 也会走同一补齐路径。
真实画面时长优先取 `base_video.metadata.duration_us`，旧数据回退到原始分段边界，最后才使用
当前音频或与当前音频绑定的 raw cues。所有语义视觉和来源文字在建草稿前按该时长裁边：完全
落在片尾外的项丢弃，跨越片尾的项裁短。因此历史 SeedVR2 画面短于音频时也不会因字幕或贴层
越过主视频而使整条草稿失败。浏览器预览、4B 和模块 6 使用同一绝对时间轴。
RunningHub 原始 MP4 分段继续作为不可覆盖历史素材保存，但不直接作为上述时间线画面源；
这样字幕、BGM 和视频使用完全相同的绝对时间轴，不会因供应商分段的容器实际时长偏短而
使末尾字幕越界。`base_video` 已包含 4A 生成的 250000 微秒保时长叠化。

声音、普通成片、原始分段和变体下载统一读取 `item.settings.source_metadata`。后两列齐全时，
文件名以 `账号{分配账号}-{文章类型}-{任务ID}` 开头，例如
`账号5-鸡汤文-2_0.9倍速.mp3`、`账号5-鸡汤文-2-composition.mp4` 和
`账号5-鸡汤文-2-变体-001.mp4`。该规则在下载响应时动态生效，因此给旧项目回填四列表后，
既有文件也会立即使用新名称；历史两列表继续返回原文件名。

统一内容分析的 `title` 分支返回唯一 `{"line_1":"减脂真相","line_2":"坚持更关键"}`：第一行
最多 5 个字符，新 AI 标题第二行最多 5 个字符；历史手工/已保存标题读取时兼容到 14 个字符，
均不得含空白或重复。工作台将其保存为 `postprocess.title`，只用于项目封面两行标题。
正文视频顶部不再使用模型标题，而是始终渲染单行固定文案“世界冠军带你自律”：字号 19、
1080×1920 参考 Y=1535、红色填充和白色描边，浏览器预览、普通导出与变体一致。
`postprocess-settings` 仍兼容读取历史 `top_title` 字段，但该字段不再改变正文固定标题。标题或
后处理设置变化时只取消当前成片指针并回到 `BASE_VIDEO_READY`；旧成片仍保留在素材历史，
随后重新生成浏览器预览配方即可，不会自动再次导出。
姿态或字幕样式等非音乐编辑可提交 `preserve_auto_bgm=true`；当当前设置和请求都为自动模式时，
接口保留已解析的 BGM、选择快照和冻结响度。省略该字段时，`auto + 空 bgm_identity` 继续表示
明确清空旧推荐、等待 4B 重新匹配。
`cover_title={"line_1":"健康真相","line_2":"别再踩坑"}` 必须两行同时存在；第一行最多 5 字、
新 AI 标题第二行最多 5 字且不含空白，历史已保存标题读取时兼容到 14 字。非空时普通导出和
变体都按当前 `base_video` 保存的 `input_image_sha256` 从图片历史回溯原图并生成固定 3 帧封面；
冻结图片缺失时返回冲突错误，不会用后来映射的当前图片静默生成错配封面；
视觉参数不由接口传入。
同理，脚本或音色修改会保留旧音频/视频但回到 `DRAFT`，图片修改会保留当前音频但回到
`AUDIO_READY`。再次调用 `/audio/generate` 时，若没有待生成/失败行，则为全部已完成行
创建新的声音版本，而不是返回“当前项目没有待生成声音”。

## 新版视频预览与上传替换接口（模块 5）

```text
GET  /api/new/projects/{project_id}/items/{item_id}/current-video
POST /api/new/projects/{project_id}/items/{item_id}/current-video?filename=人工粗剪.mp4
GET  /api/new/projects/{project_id}/items/{item_id}/original-materials
GET  /api/new/projects/{project_id}/videos/download?item_ids={item_id_1},{item_id_2}
```

`POST current-video` 使用视频文件原始二进制作为请求体，支持 MP4、MOV、AVI、MKV、WebM，
大小上限沿用 `JYD_MAX_VIDEO_UPLOAD_BYTES`。接口要求脚本行的
`allowed_actions.upload_current_video=true`，成功后创建 `source_type=user_upload` 的
`composition_video` 素材版本并切换当前视频；旧自动成片、基础视频和 RunningHub 原始
片段不覆盖、不删除。上传文件视为用户已处理好的完整视频，原 MiniMax cues 继续保留，
但字幕绑定和状态改为 `INVALIDATED`。

`GET /videos/download` 只打包项目中每一行当前的 `composition_video`，也就是生成变体前的
普通成片，不包含任何 variant。目标脚本行必须已有实际导出的当前成片；浏览器动态预览
尚未导出时返回 `409`。工作台的一键下载按用户选择的完整 `item_id` 集合核对：复用已有 MP4、
等待正在生成的草稿、为旧预览补冻结草稿，再顺序调用单行 `postprocess/export` 补齐文件；
每条目标明确记为成功或失败，不按点击瞬间的 ready 子集静默漏项。之后再请求 ZIP，响应结束后
立即删除。可选的逗号分隔 `item_ids` 只打包指定项目行，
其中任何 ID 不属于当前项目时返回 `422`；省略该参数时保持全项目下载。

`original-materials` 不改变任何项目状态。只有一个 RunningHub 原始片段时直接返回 MP4；
存在多个片段时按 `video_index` 排序，返回包含全部片段及 `片段顺序清单.json` 的一次性
ZIP。浏览器预览固定使用 9:16 容器；播放时隐藏中央按钮，点击画面可以暂停。

## 新版变体接口（模块 6）

```text
GET    /api/new/variant-options
PATCH  /api/new/projects/{project_id}/variant-settings
POST   /api/new/projects/{project_id}/variants/generate
GET    /api/new/projects/{project_id}/variants/status
POST   /api/new/projects/{project_id}/items/{item_id}/variants/supplement
POST   /api/new/projects/{project_id}/items/{item_id}/variants/retry
GET    /api/new/projects/{project_id}/items/{item_id}/variants/{asset_id}
DELETE /api/new/projects/{project_id}/items/{item_id}/variants/{asset_id}
```

`variant-settings` 在不清空任何音视频素材的前提下保存全局规则和逐行数量，刷新页面后可恢复。
`variants/generate` 可提交项目全部行，也可只提交一行；每个提交项只包含 `item_id` 和 `count`。
封面来自该行已经保存的 `postprocess.cover_title` 和当前输入图片，不属于变体参数。单批总任务数上限
500。推荐配置默认启用视频特效、全屏贴纸、`1:1`/`3:4` 裁剪、四种背景色、人物居中和
四角贴纸；后端用加权最大差异算法选择不重复组合。字幕字体和 BGM 只从 4B 冻结配方读取，
接口不接受它们作为变体维度。项目存在合法封面标题时，封面强制 `frame_count=3`，封面图段插入主视频轨道首段而非
额外视频轨道；所有正文轨道从封面结束后开始。
状态接口将完成文件登记为 `variant_video`；批次允许部分成功，`retry` 只重提失败签名，
`supplement` 避开已有成功签名，删除一个素材不会删除同一行的其他变体。

新版表格的“单条生成”同时覆盖声音、完整视频和变体。当前产物与输入配置仍匹配时按钮显示
“复用”，前端不提交付费/渲染任务；脚本、音色、图片、字幕/BGM、项目标题、变体数量或规则
变化后显示“重新生成”。历史素材不覆盖，新生成版本成为当前版本或追加为新的变体成果。

## 新版成果库接口（模块 7）

```text
PUT  /api/new/projects/{project_id}/script-source?filename=原始脚本.xlsx
GET  /api/new/gallery?project_id=&date_key=&batch_no=&status=&keyword=
POST /api/new/gallery/downloads
POST /api/new/gallery/deletions
```

前端解析并保存项目后，用原始二进制调用 `script-source`；后端再次校验 XLSX/CSV 内容与当前
项目脚本完全一致后保存源文件版本。每次变体生成、补充或失败重试分配独立成果批次，默认
直接输出到 `D:\auto\月.日\批次号`，并在提交剪映前把最新源脚本复制到该目录。日期目录
按本机时区生成且不补零，例如 8 月 5 日为 `8.5`；批次号由数据库按日全局递增。

成果查询返回项目编号、脚本行、源视频素材 ID、成果批次、剪映批次、状态、时间、文件存在
状态和鉴权下载地址。查询、ZIP 打包与批量删除都校验数字人账号归属；物理目录仅保存文件，
不承担权限和业务状态。`deletions` 接收 `{"asset_ids": [...]}`，最多 500 个；服务端先在
同一事务中校验整批素材归属，任一 ID 不存在或无权访问时不会部分删除。模块 6 弹窗及成果库
视频容器均固定为 `9:16`。

## 新版统一项目接口（模块 0）

新版页面统一使用工作台后端的 `/api/new/*`。模块 0 只建立项目、脚本行、素材版本、
操作记录、外部批次关联、字幕占位和后端可执行操作，不会调用 MiniMax、RunningHub
或剪映。

```text
POST  /api/new/projects
GET   /api/new/projects?limit=50&offset=0
GET   /api/new/projects/{project_id}
GET   /api/new/projects/{project_id}/diagnostics
PATCH /api/new/projects/{project_id}
PATCH /api/new/projects/{project_id}/items/{item_id}
```

所有接口要求数字人普通账号登录。项目记录绑定数字人 `user_id`；其他账号查询同一
项目编号时返回 `404`，避免泄露项目是否存在。工作台技术管理员会话不能代替普通
账号创建或读取项目。

创建项目示例：

```json
{
  "name": "八月数字人口播",
  "items": [
    {"row_key": "001", "script_text": "第一条口播。"},
    {"row_key": "002", "script_text": "第二条口播。"}
  ],
  "settings": {}
}
```

成功返回 `201` 和 `jyd.project.v1` 项目详情。项目编号格式为
`DH-YYYYMMDD-0001`，同一天在当前工作台实例内递增。一个项目最多包含 500 条脚本，
项目内 `row_key` 不能重复。

`GET diagnostics` 返回一次性 ZIP 下载，并在响应完成后删除临时文件。它只包含当前项目的
安全摘要和 14 天内可按项目、操作或关联号精确关联的本机脱敏日志；不包含脚本文本、素材
文件及路径、操作负载、错误正文、凭据或其他项目日志。独立 Agent 日志不在该包内。

详情中的每条脚本行包含：

```json
{
  "item_id": "...",
  "row_key": "001",
  "position": 1,
  "script_text": "第一条口播。",
  "status": "DRAFT",
  "outputs": {
    "audio": null,
    "base_video": null,
    "composition_video": null,
    "original_video_segments": [],
    "variants": []
  },
  "subtitles": {
    "source": null,
    "raw_cues": [],
    "render_cues": [],
    "bound_audio_asset_id": null,
    "bound_video_asset_id": null,
    "style": {
      "font_id": null,
      "font_size": 15,
      "max_width_ratio": 0.82,
      "max_lines": 2
    },
    "status": "NOT_AVAILABLE",
    "overflow_risk": false
  },
  "allowed_actions": {}
}
```

项目粗粒度状态为：

```text
DRAFT
PROCESSING
AUDIO_READY
BASE_VIDEO_READY
COMPOSITION_READY
VARIANT_READY
PARTIAL_FAILED
FAILED
```

声音、RunningHub、拼接、字幕/BGM 和变体的详细阶段保存在脚本行与 `operations` 中。
项目只做聚合，不复制第三方状态机。

项目和脚本行都返回 `allowed_actions`，包括输入编辑、音频生成/重试/下载、画面合成、
当前视频下载、原始片段下载、上传当前视频和生成/重试变体。按钮是否可用必须以该
字段为准。

新版工作台保持当前项目的短间隔轮询，并每分钟扫描一次其余项目。未打开项目若存在活动
声音、H3、4A、剪映后期或变体状态，页面只调用相应的既有状态接口并回写终态，不重新创建
任务；单个云端阶段暂时不可用时，仍继续核对同项目的本地剪映任务。

项目更新支持 `expected_revision` 乐观并发检查。版本过期返回 `409`；格式或状态不
允许返回 `422`。当前模块只允许 `DRAFT` 脚本行直接修改脚本和行编号。

## 数字人任务收件箱

普通用户账号由数字人网站统一验证。工作台使用服务端保存的短期会话令牌拉取当前用户自己的任务：

```text
GET  /api/digital-human/tasks
POST /api/digital-human/tasks/{item_id}/import
GET  /api/digital-human/tasks/{item_id}/videos/{video_index}
```

- `GET /api/digital-human/tasks` 返回当前账号的数字人任务列表。
- `POST .../import` 只接受 `AUTO_READY`、`AUTO_POSTPROCESS` 且只有一个成功视频的任务，返回本地媒体引用和精确字幕 cue。
- `GET .../videos/{video_index}` 用于下载上传音频或多片段任务的原始生成片段。
- 三个接口都要求先登录工作台；工作台不会自动导入、渲染或发布任务。
- 本地数字人服务默认是 `http://127.0.0.1:8000`，工作台默认是 `http://127.0.0.1:8010`。

完整设计与验收记录见 [DIGITAL_HUMAN_INTEGRATION_20260803.md](DIGITAL_HUMAN_INTEGRATION_20260803.md)。

## 网页入口

```text
普通用户生成页：http://127.0.0.1:8010/app
脚本与声音：      http://127.0.0.1:8010/app/new
上传与生成：      http://127.0.0.1:8010/app/new/generate
新版登录页：      http://127.0.0.1:8010/app/new/login
管理员登录页：  http://127.0.0.1:8010/admin/login
高级设置页：    http://127.0.0.1:8010/app/advanced
素材管理页：    http://127.0.0.1:8010/app/assets
接口文档：      http://127.0.0.1:8010/docs
```

普通生成页和业务 API 使用管理员在 `/admin` 创建的内测账号。高级设置、素材管理、用户管理、存储清理和接口文档使用管理员账号 `admin / admin123`。停用、删除或重置普通账号密码会立即撤销原会话。

素材管理页读取处理机上的永久素材库，支持重命名、音乐分类、管理标签、启用/停用、预览、移入回收站和恢复。移入回收站只写入 `data/web_storage/asset_admin.json`，不会立即删除素材文件；已停用或已回收的素材不会出现在普通产品页候选项中。

```text
GET    /api/admin/assets
PATCH  /api/admin/assets/{asset_kind}/{asset_identity}
DELETE /api/admin/assets/{asset_kind}/{asset_identity}
POST   /api/admin/assets/{asset_kind}/{asset_identity}/restore
```

启动后端：

```powershell
cd "D:\工作内容\轻盈健\公寓\jyd_plain_json_probe"
D:\Myanaconda\python.exe .\apps\processor\run_web_api.py --host 127.0.0.1 --port 8010
```

打开接口文档：

```text
http://127.0.0.1:8010/docs
```

打开最小网页前端：

```text
http://127.0.0.1:8010/app
```

## 环境变量

可选：

```powershell
$env:JYD_WEB_STORAGE_ROOT="D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\web_storage"
$env:JYD_TEMPLATE_LIBRARY_ROOT="D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\template_library"
$env:JYD_WEB_DRAFT_ROOT="D:\剪映草稿\JianyingPro Drafts"
$env:JYD_AUDIO_LIBRARY_ROOT="D:\工作内容\轻盈健\公寓\audio_library"
$env:JYD_MEDIA_RETENTION_HOURS="24"
$env:JYD_TEMPLATE_RETENTION_HOURS="48"
$env:JYD_DRAFT_RETENTION_HOURS="48"
$env:JYD_OUTPUT_RETENTION_HOURS="72"
$env:JYD_FAILED_RETENTION_HOURS="24"
$env:JYD_METADATA_RETENTION_DAYS="30"
$env:JYD_CLEANUP_INTERVAL_MINUTES="30"
$env:JYD_ADMIN_USERNAME="admin"
$env:JYD_ADMIN_PASSWORD="请设置强密码"
$env:JYD_ADMIN_SESSION_SECRET="请设置长期固定的随机密钥"
$env:JYD_ADMIN_SESSION_HOURS="12"
$env:JYD_ADMIN_COOKIE_SECURE="false"
$env:JYD_SITE_USERNAME="operator"
$env:JYD_SITE_PASSWORD="自定义操作员密码"
$env:JYD_SITE_SESSION_SECRET="请设置长期固定的操作员会话密钥"
$env:JYD_AUTH_TIMEOUT_SECONDS="15"
$env:JYD_EXECUTION_MODE="embedded" # 多处理机中央服务改为 agent
$env:JYD_DATABASE_PATH="D:\JydServer\control.db"
$env:JYD_AGENT_TOKEN="请设置长期固定的处理机接入令牌"
$env:JYD_MAX_VIDEO_UPLOAD_BYTES="2147483648"
$env:JYD_MAX_AUDIO_UPLOAD_BYTES="209715200"
$env:JYD_MAX_DRAFT_IMPORT_BYTES="5368709120"
$env:JYD_MAX_ACTIVE_JOBS="500"
```

`JYD_DATABASE_PATH` 默认位于 `JYD_WEB_STORAGE_ROOT` 下。项目数据库 v12 内部使用
`storage://...` 保存受管素材的相对位置，但 API 响应中的 `managed_path` 继续返回解析后的
本机绝对路径，现有 H3、剪映后期和下载调用无需调整。不要把数据库单独复制到没有对应
`web_storage` 文件树的目录。

公网 HTTPS 部署时将 `JYD_ADMIN_COOKIE_SECURE` 设为 `true`。生产环境应显式配置管理员密码和会话密钥，不依赖自动生成文件。

真实导出 MP4 时，建议把 `JYD_WEB_DRAFT_ROOT` 指向剪映实际的 `JianyingPro Drafts` 目录，或者在 render job 的 `output.draft_root` 里显式传入。

## 上传 MP4

上传接口不用 multipart；前端直接把文件二进制作为 body，文件名放到 `filename` 查询参数里，中文文件名需要 URL 编码。

```powershell
$bytes = [System.IO.File]::ReadAllBytes("C:\Users\san\Desktop\测试\1.mp4")
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8010/api/media/video?filename=1.mp4" `
  -ContentType "application/octet-stream" `
  -Body $bytes
```

返回：

```json
{
  "media_id": "...",
  "kind": "video",
  "filename": "1.mp4",
  "path": "..."
}
```

音频上传：

```powershell
$bytes = [System.IO.File]::ReadAllBytes("D:\素材\bgm.mp3")
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8010/api/media/audio?filename=bgm.mp3" `
  -ContentType "application/octet-stream" `
  -Body $bytes
```

## 音乐库

查看音乐、分类和轮换位置：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8010/api/audio-library"
```

网页支持上传单次使用的 BGM、固定选择音乐库中的一首，以及在某个分类内按导入顺序轮换。分类轮换在提交任务时原子地推进游标，并把实际选中的音乐写入任务记录。

## 导入模板

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8010/api/templates/import" `
  -ContentType "application/json" `
  -Body '{
    "source_draft_dir": "D:/剪映草稿/JianyingPro Drafts/模板名",
    "template_id": "demo_template",
    "name": "演示模板",
    "replace": false
  }'
```

查看模板：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8010/api/templates"
Invoke-RestMethod -Uri "http://127.0.0.1:8010/api/templates/demo_template"
```

## 提交渲染任务

上传 MP4 模式：

```json
{
  "schema": "jyd.render_job.v1",
  "source": {
    "type": "video",
    "media_id": "上传接口返回的 media_id"
  },
  "output": {
    "draft_root": "D:/剪映草稿/JianyingPro Drafts",
    "skip_export": false
  },
  "captions": {
    "text": "与视频口播对应的完整长文案……",
    "start_us": 0,
    "duration_us": 0,
    "max_chars": 16,
    "style_json_path": "D:/项目/text_style_library/抖音美好体测试.json",
    "font_id": "剪映字体 resource_id",
    "font_path": "D:/项目/font_library/files/字体文件.ttf",
    "font_title": "优设标题黑",
    "size": 15,
    "color": "#FFFFFF",
    "transform_x": 0,
    "transform_y": -0.8,
    "line_max_width": 0.82
  },
  "texts": [],
  "audios": [
    {
      "type": "add",
      "library_category_id": "分类 ID，也可以改用 media_id 或 library_identity",
      "target_start_us": 0,
      "target_duration_us": 0,
      "fit_to_video": true,
      "volume": 0.3
    }
  ],
  "effects": [],
  "export": {
    "resolution": "1080P",
    "framerate": "30fps",
    "timeout": 1200
  }
}
```

字体与字幕样式是两类独立素材：

```text
GET /api/assets/fonts        读取 font_library/manifest/font_manifest.json
GET /api/assets/text-styles  读取 text_style_library/*.json
GET /api/assets/fonts/{font_identity}/file  返回字体文件，用于网页预览
```

剪辑母版仅更换普通字幕字体时使用 `existing_text_font`。它只更新字体引用，不修改字幕内容、时间、字号、颜色、描边和位置，也不会处理复合文字模板：

```json
{
  "existing_text_font": {
    "font_id": "剪映字体 resource_id",
    "font_path": "D:/项目/font_library/files/字体文件.ttf",
    "font_title": "优设标题黑"
  }
}
```

模板库模式：

```json
{
  "schema": "jyd.render_job.v1",
  "source": {
    "type": "template",
    "template_id": "demo_template"
  },
  "output": {
    "draft_root": "D:/剪映草稿/JianyingPro Drafts",
    "skip_export": false
  },
  "texts": [],
  "audios": [],
  "effects": []
}
```

提交：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8010/api/render" `
  -ContentType "application/json" `
  -Body (Get-Content ".\examples\render_job_video.example.json" -Raw)
```

查询任务：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8010/api/jobs/{job_id}"
```

下载 MP4：

```powershell
Invoke-WebRequest `
  -Uri "http://127.0.0.1:8010/api/jobs/{job_id}/download" `
  -OutFile "D:\输出\result.mp4"
```

`/api/render` 会把任务写入 SQLite 并立即返回 `job_id`。`embedded` 模式由本机内置 worker 顺序执行；`agent` 模式由多台 Windows Agent 原子领取任务。每台 Agent 仍然一次只控制一个剪映窗口。

## 批量排列组合

网页的“批量排列组合”模式会一次上传视频，再按通用素材维度生成子任务。BGM、视频特效、花字和复合文字模板都支持三种模式：

- `disabled`：不使用，不参与任务数量计算。
- `fixed`：固定使用一个候选项，应用到每个子任务。
- `product`：所选候选项参与笛卡尔积。

例如固定 1 首 BGM，选择 10 个视频特效和 5 个花字参与组合，会创建 `10 × 5 = 50` 个独立子任务：

```text
POST /api/render/batch
GET  /api/batches/{batch_id}
```

请求结构：

```json
{
  "job": {
    "schema": "jyd.render_job.v1",
    "source": {"type": "video", "media_id": "video-media-id"},
    "output": {"skip_export": false},
    "texts": [],
    "videos": [],
    "export": {"resolution": "1080P", "framerate": "30fps"}
  },
  "dimensions": [
    {
      "key": "bgm",
      "label": "BGM",
      "mode": "fixed",
      "candidates": [
        {
          "id": "music_id:1",
          "label": "固定音乐",
          "append": {
            "audios": [{"type": "add", "library_identity": "music_id:1", "volume": 0.3}]
          }
        }
      ]
    },
    {
      "key": "video_effect",
      "label": "视频特效",
      "mode": "product",
      "candidates": [
        {
          "id": "effect-1",
          "label": "特效 1",
          "append": {"effects": [{"effect_json_path": "D:/项目/effect_library/特效1.json"}]}
        },
        {
          "id": "effect-2",
          "label": "特效 2",
          "append": {"effects": [{"effect_json_path": "D:/项目/effect_library/特效2.json"}]}
        }
      ]
    }
  ],
  "max_jobs": 500
}
```

候选项的 `patch` 用于覆盖任务字段，`append` 用于向 `audios`、`effects`、`texts`、`text_templates` 等数组追加内容。固定维度必须且只能有一个候选项；参与组合的维度至少有一个候选项。每个子任务会生成唯一草稿名、MP4 输出路径和所选维度摘要。

当前默认单批上限为 500 个子任务。旧版 `music + effects` 请求结构暂时兼容。批量表示自动连续处理；一台电脑上的剪映 worker 仍然逐个导出，不会同时操控多个剪映窗口。

批次状态会返回 `counts.cancelled`、`average_job_seconds` 和 `estimated_remaining_seconds`。完成首个任务后，网页会按本批次实际平均耗时估算剩余时间。结果中心使用以下接口：

```text
POST /api/batches/{batch_id}/cancel
POST /api/batches/{batch_id}/retry-failed
POST /api/batches/{batch_id}/downloads
POST /api/batches/{batch_id}/delete-outputs
GET  /api/batch-downloads/{download_id}
```

- `cancel` 只取消尚未启动的任务，正在处理的任务会继续完成。
- `retry-failed` 把失败项复制到一个新批次，并重新生成任务 ID、草稿名和 MP4 路径。
- `downloads` 接收 `{"job_ids": ["..."]}`，生成一次性 ZIP；下载响应结束后服务器会删除 ZIP 临时文件。
- `delete-outputs` 接收相同结构，只删除 `storage_root/outputs` 下的 MP4 和 `default_draft_root` 下的生成草稿，不删除输入视频、母版或素材库。

## 存储生命周期

管理员可以在生产页“最近任务”中永久删除已经结束的测试批次。删除会同步清理该批次的任务记录、输出 MP4、生成草稿和受管临时目录；排队中或运行中的批次不能删除。

音乐、特效、字体、花字、复合文字和贴纸素材库属于永久数据。通过本地采集器上传的剪辑母版默认保留 48 小时；网页上传的视频、临时音频、输出 MP4、程序生成草稿和批量 ZIP 也属于临时数据：

- 上传素材默认保留 24 小时；排队中或正在运行的任务仍引用该素材时不会删除。
- 成功任务的 MP4 默认保留 72 小时。
- 成功、失败或取消任务在处理机剪映目录中生成的草稿统一保留 48 小时；草稿和 MP4 独立计时、独立清理。
- 失败或取消任务的其他临时结果默认保留 24 小时。
- ZIP 下载响应结束后立即删除；没有下载的遗留 ZIP 最多保留 24 小时。
- 任务和批次 JSON 元数据默认保留 30 天，之后整批删除。
- 服务每 30 分钟扫描一次。升级前的旧记录第一次只补到期时间，从升级时重新开始计时，不会立即删除。

查看占用与策略、手动扫描或只预演清理：

```text
GET  /api/storage
POST /api/storage/cleanup                 body: {}
POST /api/storage/cleanup                 body: {"dry_run": true}
```

清理器只删除本地采集器上传且已到期、未被运行中任务使用的剪辑母版，不删除内置或管理员手动导入的永久模板，也不会删除音乐库、特效库、字体、花字、复合文字模板或贴纸库。

长文案切分预览使用与渲染任务相同的后端算法：

```text
POST /api/captions/preview
```

普通用户页把字体与字幕样式分开：剪辑母版只选择字体；上传 MP4 时可选择字体，并单独设置基础样式、字号、颜色和位置。字体来自 `font_library`，完整字幕样式来自 `text_style_library`。

注意：SQLite 只能由一个中央 API 实例访问，启动后端时不要配置多个 uvicorn workers，也不要让 Agent 直接打开数据库文件。多处理机接口和部署方法见 [MULTI_PROCESSOR.md](MULTI_PROCESSOR.md)。

## 网页测试顺序

1. 启动后端并打开 `http://127.0.0.1:8010/app`。
2. 确认右上角显示“后端已连接”。
3. 选择 MP4 视频文件；需要套模板时勾选“套用模板”并选择模板。
4. 模板只作为可选加工方式，上传的 MP4 会自动替换第一个普通视频片段或第一个嵌套视频槽。
5. 输入长文案，选择字体样式，调整字号、颜色、最大宽度和位置，检查切分及视频叠加预览。
6. BGM 可选择分类内顺序轮换、固定音乐库素材、临时上传或不添加；这些配置与模板使用同一份渲染任务。
7. 第一次建议勾选“只生成草稿，不导出 MP4”，点击“开始生成”并确认状态为 `completed`。
8. 第二次取消勾选，并确保剪映已打开且停在草稿首页，再测试真实 MP4 导出。

网页始终以上传 MP4 为入口。文字样式来自 `text_style_library/*.json`，特效来自 `effect_library/*.json`；模板库管理只负责扫描、解密和导入剪映草稿。未勾选模板时从 MP4 新建草稿，勾选模板时先替换模板视频，再统一添加字幕、BGM 和特效。

## 新版工作台默认字幕与成果库选择（2026-08-05）

- `GET /api/new/postprocess/options` 除 `fonts`、`bgm` 和 `caption` 外返回
  `default_font_identity`。当前优先返回 `resource_id:7244518590332801592`
  (`DouyinSansBold`)；该素材不可用时才回退到其他可用真实字体。
- `POST /api/new/gallery/downloads` 的请求结构仍是 `{"asset_ids": [...]}`。前端总全选和
  批次全选只负责收集当前账号、当前筛选结果内实际可用视频的素材 ID，不扩大下载权限。
- `POST /api/new/gallery/deletions` 使用相同请求结构，执行不可撤销的成果记录与受管导出文件
  删除。前端必须二次确认，后端必须整批校验账号归属并保持数据库原子性。
## 服务器草稿扫描

网页模板库区域会调用 `/api/drafts` 扫描服务器电脑上的剪映草稿目录：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8010/api/drafts?root=D:/剪映草稿/JianyingPro%20Drafts"
```

返回里的 `plain_json=false` 表示这个草稿可能是高版本加密草稿；导入模板库时会自动调用解密流程。

## 新版工作台语义视觉 API（2026-08-10）

- `GET /api/new/semantic-visuals/catalog`：返回公开概念、素材元数据和当前内容哈希版本，不返回
  本地路径。
- `GET /api/new/semantic-visuals/{asset_id}/preview`：只返回目录内已登记素材的预览图片。
- `GET /api/new/semantic-visuals/{asset_id}/content`：鉴权返回已登记视频素材本体，供浏览器静音
  预览；图片或未知素材返回 404，不暴露本地路径。
- `GET /api/new/fixed-visuals/nameplate/preview`：鉴权返回工作台内置的张雒人名牌预览；该素材
  不进入语义 catalog、模型请求或逐行审核列。
- `POST /api/new/projects/{project_id}/visual-analysis`：仅作为迁移期兼容别名保留；生产前端不再
  调用该地址，别名内部仍复用统一 content-analysis 协调器，不会调用独立视觉模型接口。
- `POST /api/new/projects/{project_id}/items/{item_id}/visual-analysis/retry`：迁移期兼容别名，
  对当前行强制刷新统一内容分析；生产前端改用同一行的 `content-analysis/retry` 地址。
- `PUT /api/new/projects/{project_id}/items/{item_id}/visual-overlays`：请求体为
  `revision`、可选 `catalog_version` 和 `overlays`。保存时验证项目修订、素材/概念、画内
  安全区、时间、缩放、透明度、视频截取参数及统一不重叠约束，并冻结为人工配方。接口会保留
  可选的 `timing_source/timing_mode`、句段字符范围与文本、`list_index/list_size`、
  `segment_boundary_us` 和 `usage`，以便人工修改后仍可追溯自动编排来源。

工作台主流程向数字人网站发送 `/api/workbench/content-analysis`，本地候选被压缩为
`visual_context`；响应复用 `jyd.content-analysis.v1` 外包装并增加 `visual_plan`。旧的
`jyd.visual-analysis.request.v1` / `jyd.visual-analysis.v1` 只作为迁移期兼容接口保留。
任何一端都不信任模型返回的时间、本地路径或具体素材身份。

新生成或人工保存的 recipe 仍使用 `jyd.semantic-visual-recipe.v2`，并可包含：

```json
{
  "timing_policy_version": "sentence-v1",
  "used_asset_ids": ["food.egg.boiled.image.01"],
  "overlays": [{"timing_mode": "sentence", "segment_boundary_us": null}]
}
```

`used_asset_ids` 是当前已启用素材的冻结快照；自动重算时仍以实际 overlays 和已启用手工锁定项
重新建立集合。旧 recipe 缺少上述字段时继续按历史参数读取和导出。
