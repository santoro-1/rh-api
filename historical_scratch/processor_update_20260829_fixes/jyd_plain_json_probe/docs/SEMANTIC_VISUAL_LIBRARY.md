# 语义视觉素材库维护

语义图片与视频共用同一素材库：

```text
data/libraries/semantic_visual_library/
  catalog.json
  bundles/
  videos/
  fixed/nameplate_zhangluo/
```

`catalog.json` 使用统一图片/视频协议；工作台启动时一次性读取并严格校验目录，
因此修改素材库后必须重启 Processor。`fixed/nameplate_zhangluo` 不属于语义 catalog；当前固定图为
`人名板4.png` 的受控副本，作为
每条项目视频的固定人名牌从正文第 1 帧显示到结束，默认位于左侧胸口区域、宽度约占画面
46%；封面 3 帧不显示，也不得引用桌面原始路径。

## 项目行内增加或移除素材

表格“语义视觉”列进入审核窗口：

- “加入本行”把模型候选加入当前脚本行。
- “关闭显示”保留配方但不在预览和成片中显示。
- “移除本行”从当前脚本行配方中移除，不删除本地图库文件。
- 更换素材、位置、时间、缩放、透明度和开关修改均自动切换为人工配方并锁定。
- 图片和视频使用同一占用表；保存时任何启用项时间重叠都会被拒绝。

修改后点击“保存配方”。取消或直接关闭窗口不会写入项目。

## 向整个图库新增图片

素材库存储仍使用完整 bundle，不能只把一张 PNG 随意放进目录。这样目录可整体搬迁、版本可
校验，也能兼容已经冻结的历史项目。新增素材至少包含：

```text
bundles/<新目录>/
  sticker.json
  resources/sticker/singleImage.png
```

普通语义贴图会把 `resources/sticker/singleImage.png` 写成剪映 `photo` 素材并放在独立视频
轨道上。站姿/坐姿固定人名板是例外：它们使用本地 bundle 的 `sticker.json`、`config.json`、
`heycanInfo.json`、`infoSticker.lua` 和 `singleImage.png` 写成剪映原生 `sticker` 轨道，以保持
规范草稿的原始缩放语义。若 `sticker.json` 还引用其他资源，仍须连同整个目录一起复制。然后在
`catalog.json` 的 `assets` 数组增加素材项。下面的 catalog v2 是旧库兼容格式；新入库和再次
打标应使用后文 catalog v3 完整字段：

```json
{
  "asset_id": "food.egg.new_unique_id",
  "concept_ids": ["food.egg"],
  "name": "新鸡蛋图片",
  "description": "图片内容的简短事实描述",
  "media_type": "image",
  "renderer": "jyd_sticker_bundle",
  "tags": ["食物", "照片"],
  "resource": {
    "bundle": "bundles/<新目录>",
    "preview": "bundles/<新目录>/resources/sticker/singleImage.png"
  },
  "defaults": {
    "corner": "bottom_center",
    "scale": 0.56,
    "opacity": 1.0,
    "duration_us": 1800000
  }
}
```

`asset_id` 必须永久唯一，不能复用旧 ID。若新增概念，还要在 `concepts` 中添加唯一
`concept_id`、名称、描述和非空关键词别名；别名应具体，并按实际语言补齐长词。

## 向素材库新增视频

每条视频使用独立、发布后不覆盖的目录：

```text
videos/<新目录>/
  video.mp4
  poster.png
  metadata.json  # 可选
```

catalog v2 项示例：

```json
{
  "asset_id": "activity.running.video.01",
  "concept_ids": ["activity.running"],
  "name": "户外跑步视频",
  "description": "人物在户外持续跑步的动作画面",
  "media_type": "video",
  "renderer": "video_overlay",
  "tags": ["运动动作", "动态"],
  "resource": {
    "video": "videos/<新目录>/video.mp4",
    "preview": "videos/<新目录>/poster.png",
    "metadata": "videos/<新目录>/metadata.json",
    "duration_us": 6000000,
    "width": 1920,
    "height": 1080,
    "has_audio": true
  },
  "defaults": {
    "corner": "center",
    "scale": 1.0,
    "opacity": 1.0,
    "duration_us": 3000000,
    "source_start_us": 0,
    "mute": true,
    "loop": false,
    "fit": "cover"
  }
}
```

## catalog v3 语义与用途合同

工作台同时兼容 catalog v1、v2 和 v3。v2 保持严格 9 字段校验，不允许把新字段直接追加到
旧 schema；v3 在相同图片/视频资源字段上增加以下必填字段：

```json
{
  "schema": "jyd.semantic-visual-catalog.v3",
  "concept_ids": ["food.fish"],
  "semantic_roles": {
    "depicts": ["food.fish"],
    "expresses": [],
    "related": ["nutrition.protein"]
  },
  "auto_trigger_concept_ids": ["food.fish"],
  "trigger_basis": {"food.fish": "exact_subject"},
  "visual_actions": [],
  "usage_modes": ["semantic_overlay", "list_quick_cut"],
  "cleanliness_grade": "A",
  "auto_eligible": true,
  "requires_clip": false,
  "loop_allowed": false,
  "rights_status": "cleared",
  "person_status": "none",
  "brand_status": "none",
  "health_claim_status": "none",
  "platform_ui_status": "none"
}
```

上例只展示 v3 新字段，仍须保留 v2 的 `asset_id/name/description/media_type/renderer/tags/resource/defaults`。
强制约束如下：

- `concept_ids` 必须与 `auto_trigger_concept_ids` 完全一致；不得由所有 depicts/expresses 自动求并集。
- `auto_trigger_concept_ids` 必须来自 `depicts` 或 `expresses`，且每项都有合法 `trigger_basis`。
- `related` 永不参与自动匹配；三种语义角色互斥。
- `auto_eligible=false` 的素材不会产生内容分析候选，也不会被本地选材器选中。
- `manual_only` 必须是唯一 usage mode，可以保留 depicts/related，并允许自动 concept 列表为空。
- v3 只根据 `usage_modes` 判断全屏/接缝空镜；接缝优先 `seam_broll`，也可从同语义下已批准的
  `full_screen_broll` 补充候选。旧 `相关素材/b-roll/enrichment` tags 仅作为 v2 回退。
- `rights_status=unknown/restricted` 的自动素材不得声明 `full_screen_broll` 或 `seam_broll`。
- 图片不得声明全屏 B-roll 或接缝 B-roll；`seam_broll` 只能是视频。历史 `loop` / `loop_allowed`
  字段只为旧目录兼容保留，不再决定语义视频的播放行为；小窗视频、普通全屏空镜和接缝空镜
  一律只播放一次，目标区间长于获准源片窗口时提前结束。

## 分级语义标签与图片/视频差异召回（本地已实现）

> 状态：2026-08-14 已在本地工作台完成 catalog v3 分层迁移、编辑型空镜池与调度回归，尚未部署生产环境。
> 927 张图片不含 `video_taxonomy`，继续只按 L3 精确概念召回；482 条视频保存 L1/L2/L3、
> 动作和场景元数据，其中 92 条视频进入至少一个编辑型空镜池。

素材语义不能只使用一棵宽泛标签树。正式打标分为“内容层级”和“画面侧标签”两部分：

```text
内容层级
L1 领域：食物 / 饮品 / 运动 / 日常活动 / 居家生活
L2 类别：汤类 / 早餐 / 水果 / 轻活动 / 居家做饭 / 购物
L3 精确：西兰花豆腐汤 / 白灼虾 / 骑自行车 / 倒牛奶

独立画面侧标签
动作：煮、盛、倒、切、散步、骑车、打球
场景：厨房、餐桌、客厅、超市、公园、步道
```

L1 只用于管理、统计和人工筛选，永不直接产生自动素材。L2 是视频专用分类字段；只有映射到
显式 `fallback_concept_ids` 的安全宽语义才可作为空镜回退，食物、菜品和饮品的 L2 只归档、不
自动触发。L3 是图片和视频共同的精确命中层。动作、场景不是 L1-L3 的子级，一个视频可以同时具有
“汤类 + 盛汤 + 厨房”三个事实标签。

编辑型接缝空镜另设五个受控池：`editorial.home_daily`、`editorial.meal_daily`、
`editorial.leisure_daily`、`editorial.family_life`、`editorial.mood_atmosphere`。它们只写入
视频的 `fallback_concept_ids`，不是画面中对象的 L3 标签，也不依赖脚本逐字出现“居家阳光”等
素材描述。这五类池保留给人工审核和检索，不参与普通 10 秒 enrichment，也不再自动补数字人
片段接缝。自动接缝只接受脚本直接召回的同一具体对象、动作或明确生活场景；仅有健康、坚持、
变好、生活、心态或相近情绪不算高相关。没有高相关素材时保留原接缝。图片不进入这些池。

文章类型先限制可见池，避免宽匹配跨题材：

- 鸡汤文：居家、休闲、家庭、状态氛围，不默认开放三餐；
- 干货类：居家、三餐、休闲、状态氛围，不默认开放家庭；
- 带人设介绍的干货类：五池均可见；
- 缺失或未知类型：五池均可见，但仍由模型按整句判断，允许全部跳过。

### 媒体触发规则

| 媒体与用途 | L3 精确 | L2 类别回退 | L1 领域 | 动作/场景 |
| --- | --- | --- | --- | --- |
| 图片普通贴图 | 允许且必须优先 | 禁止 | 禁止 | 仅画面确实是动作示意图时精确命中 |
| 图片列举速切 | 允许 | 禁止 | 禁止 | 不参与宽泛回退 |
| 视频明确语义 | 优先 | 精确素材不存在时可回退 | 禁止 | 文案明确提到时允许 |
| 视频普通空镜 | 优先 | 允许白名单回退 | 禁止 | 允许白名单回退 |
| 视频接缝空镜 | 优先 | 允许白名单回退 | 禁止 | 允许白名单回退 |

图片的“精确”是画面事实精确，不等于文件名必须逐字相同。比如西兰花豆腐汤图片可以审核
“西兰花豆腐汤 / 西兰花汤 / 豆腐汤”等确实与画面一致的完整对象短语，但不能因为画面里有
西兰花就响应“蔬菜”，也不能因为豆腐含蛋白质就响应“蛋白质”。同理，鱼、鸡蛋、肉类与
`nutrition.protein` 只能是知识关联，不能建立自动父子回退；“蛋白质”只有蛋白质知识卡、
成分示意等直接表达该抽象概念的素材才可自动命中。

视频允许比图片宽，但必须是经过审核的“可代表性”回退。例如：

- 文案“西兰花豆腐汤”只命中同菜品或同义完整对象的精确素材，不因“汤类”归档回退到其他汤。
- 文案“喝点汤”只有存在直接登记为该宽语义的获准视频时才可命中，不从任意具体菜品向上推导。
- 文案“饭后轻活动一下”可使用散步、逛超市等明确登记为轻活动代表的视频，不能回退到
  跑步、深蹲、健身房高强度训练。
- 文案“运动一下”不会仅凭 L1“运动”随机插入视频；缺少更具体的 L2、动作或场景时保留
  数字人原画面。

### 回退顺序与失败行为

一次视频选材依次尝试：

1. 同一语句或接缝对应语句中的 L3 精确概念；
2. 文案明确出现的动作或场景概念；
3. 该 L3/L2 关系中人工批准的 L2 视频回退白名单；
4. 编辑型空镜池只提供人工审核，不进入自动选材；
5. 没有可用、未重复且用途相符的直接强匹配视频时，不插空镜，保留原始拼接或数字人口播画面。

二级回退不得通过字符串前缀或任意祖先自动推导，必须由受控关系或素材自身获准的回退概念
明确声明。素材仍受 `usage_modes`、版权、人物、品牌、健康声明、平台 UI、全视频
`used_asset_ids` 和时间密度共同约束；层级命中不能绕过任何安全门槛。

### 本地迁移验收结果

- 所有自动图片只能从 L3 精确概念召回，抽样中不得出现抽象营养词命中具体食物图片。
- 所有 L2 回退资产必须是视频，且明确获准 `full_screen_broll` 或 `seam_broll` 等对应用途。
- 同一短语只有一个自动概念所有者；同义词归并后仍保留多个素材轮换，不复制概念。
- 日常轻活动、备餐、办公、通勤、居家、城市、自然和常用运动具有可验证的视频回退池；汤类、
  早餐、喝水等食物饮品只验证 L3 精确池，不开放 L2 同类替换。
- 验收同时覆盖精确优先、二级命中、禁止跨类回退、无素材保留原拼接、全视频素材去重和单次播放。

`corner=center + scale>=0.95` 作为全屏 B-roll：轨道位于固定人名牌上方、字幕下方，因此会
自然遮住人名牌，不需要隐藏/恢复事件。其他位置作为小窗视频，位于固定人名牌下方。视频默认
静音；动作 concept 在同时存在图片和视频时优先视频，无视频时回退图片。

长视频不必先物理切割。若整段是同一种动作，保留原文件，通过 `source_start_us` 和
`duration_us` 只取适合入镜的区间；若一个文件包含多个动作，可登记多个永久唯一的逻辑
`asset_id`，分别引用同一 `video.mp4` 的不同源区间和标签。只有源文件难以稳定 seek、解码异常
或必须独立发布时，才另行物理切片。

catalog v3 需要参与普通全屏空镜或拼接点空镜时，分别声明 `full_screen_broll` 或
`seam_broll`；仅有知识相关关系时写入 `semantic_roles.related`，不得写空镜用途。catalog v2
仍使用 `空镜`、`相关素材`、`b-roll`、`broll` 或 `enrichment` tags 作为兼容开关，但这些
tags 在旧实现中的含义是“允许自动空窗”，不是 v3 的非自动 related。明确语义触发在存在普通
素材时不会误选空镜专用素材。普通周期空镜留在首轮统一内容分析；数字人真实分段生成后，4B
只为连接处新增一次轻量 seam 分析，不重做其他内容分支。

## sentence-v1 自动编排合同

- 普通句素材从关键词所在标点句段开头开始，到句段结束；不足 2 秒时延长到 2 秒，成片末尾
  可以自然截短。
- 顿号 `、` 不切句。同一句段至少两个入选项目时，按关键词语音中心顺序切分整个句段；每项
  可以短于 2 秒。缺少素材的列举项被跳过后，其余项重新分配完整句段。
- 每条最终成片的自动 overlays 按 `asset_id` 全局去重；已启用手工锁定项先登记，首选已用或
  资源文件失效时继续尝试同概念下一个获准候选，全部用尽则跳过。
- v3 用途严格隔离：`semantic_overlay/action_demo/knowledge_card` 用于普通句，
  `list_quick_cut` 用于列举，`full_screen_broll` 用于通用空镜，`seam_broll` 用于数字人接缝。
  v2 仅为兼容，继续用 enrichment tags 判断通用及接缝视频资格。
- 通用空镜由 `VISUAL_BROLL_TARGET_INTERVAL_SECONDS=10` 控制，约每 10 秒在附近寻找一个相关
  短句；实际调度至少留 6 秒空窗。普通 enrichment 只携带当前句直接召回的精确对象、动作或
  场景概念，不再携带编辑型空镜池；所选 concept 还必须属于该候选的 `direct_concept_ids`，
  `priority=1` 仅供审核，只有直接概念的 `priority=2` 自动使用。只有
  宽泛大类相同、多义词碰巧相同、同属健康主题或唯一可选项不构成合格弱匹配；没有
  自然且不误导的画面就不返回，因此 10 秒是候选节奏而不是强制配额。本地不再自动使用分类宽
  回退或编辑型空镜池。接缝空镜优先读取下一段开头语境，并可结合上一段末句判断；只有模型选择
  脚本直接召回的同一具体对象、动作或明确生活场景，且通过本地强相关安全门时才进入成片。
  模型省略、弱匹配或拒绝时不得生成本地氛围兜底。每个接缝最多一个素材，
  以边界为中心显示约 1 秒、静音、不循环、不增加淡入淡出；不占用 10 秒周期。普通空镜之间仍
  至少间隔 6 秒，但接缝不重置该间隔；两者实际
  重叠时仍由接缝优先，普通空镜只在原本对应句段内寻找不少于 2 秒的前后剩余区间，句内没有
  足够空间才跳过。手工锁定、接缝、通用全屏空镜、明确语义依次占位。
- 同 concept 的 20 秒密度冷却按 `semantic_overlay` 和 `full_screen_broll` 分角色计算，图片不会
  仅因概念相同而压掉后续全屏视频；同一 `asset_id` 仍整条成片只用一次。两个自动项仅在边缘
  重叠且重叠量不超过 0.5 秒时裁短或顺延新项，较大重叠仍跳过。
- 视频目标区间超过源片可用区间时，只播放从 `source_start_us` 起、长度不超过
  `defaults.duration_us` 的获准窗口并提前结束，不循环、不定格；该资产仍不允许在同一成片的
  其他时间再次自动出现。

当前本地审核库包含 931 个概念、1409 个资产，其中 927 张图片、482 条视频；467 条视频具有
接缝空镜用途，174 条视频具有显式回退白名单，92 条进入至少一个编辑型空镜池。2026-08-14
空镜批次的 34 个源视频已全部登记：3 个此前已在库，本次新增 31 个；其中 30 个可自动使用，
1 个低清重复版本仅保留为 `manual_only`。该状态仅应用于本地工作台，未部署生产。最初两条动作视频来自
`D:\迅雷下载\贴图素材-巧如\贴图1\视频素材\腹部核心燃脂操`，且未导入 `爆款动作.mp4`：

- `activity.aerobic.crotch_clap.video.01`：胯下击掌动作，源片从 0 秒取 4 秒，作为底部中轴小窗，
  供“胯下击掌/有氧操/燃脂操”等明确语义使用。
- `activity.aerobic.core_broll.video.01`：腹部核心燃脂动作，保留 42.766341 秒源文件，从 12 秒
  取 5 秒，作为全屏、带 `相关素材/b-roll/enrichment` 标签的空窗补充素材。

重启前运行：

```powershell
$env:PYTHONPATH='src'
D:\Myanaconda\python.exe -m pytest -q -p no:cacheprovider tests\test_semantic_visuals.py
```

## 从整个素材库停用或删除素材

catalog v2 没有 `enabled` 字段；要阻止新项目选择，只能从清单移除。catalog v3 可设置
`auto_eligible=false` 并把 `usage_modes` 收紧为 `manual_only`，这样保留人工检索与历史追溯，
但不产生自动候选。两种 schema 都应继续保留原 bundle 目录；历史冻结配方保存了 bundle
路径，提前删除物理目录会让旧项目缺图。

## v3 迁移与回滚

迁移产物必须包含原 catalog 备份、v3 候选和 migration manifest。先运行只读校验：

```powershell
$env:PYTHONPATH='src'
D:\Myanaconda\python.exe -m jyd_probe.semantic_visual_migration validate <migration-manifest.json>
```

人工补齐授权/人物/品牌/健康表达字段并确认候选后，把 manifest 的 `approval.status` 改为
`approved`，同时填写非空 `approved_by` 和 `approved_at`，才可执行 `apply`。默认生成值为
`pending`，工具会拒绝应用。工具还会核对正式 catalog、
备份和候选的 SHA-256；任一文件在审计后变化就拒绝覆盖。需要恢复时执行 `rollback`，同样要求
当前正式文件哈希仍等于 manifest 中记录的 v3 候选哈希。迁移使用同目录临时文件和原子替换，
不会在校验失败时留下半写 catalog。未经人工审核，不得直接把自动生成候选应用到正式库。

只有确认所有历史项目、历史版本和待执行任务都不再引用该 `asset_id` 后，才能物理删除
bundle。当前 MVP 没有跨全部项目的安全清理界面，因此默认不执行物理删除；需要彻底清理时
应先做引用审计和备份。

## 校验边界

- 路径必须位于语义素材库目录内，不能使用绝对路径或 `..` 越界。
- 每个 bundle 必须存在，预览图片必须存在，且 bundle 根目录必须含 `sticker.json`。
- 图片与小窗视频允许左上、右上、左下、右下、底部居中或居中。`bottom_center` 是口播默认：
  横向与画面中轴对齐，动作视频约占 61.5% 画面宽度；语义图片默认约占 56% 画面宽度，最多
  显示约 37% 画面高度，超出的底部允许裁出画面。浏览器与剪映使用同一换算。
- `default_scale` 范围是 `0.05` 到 `2.0`；透明度范围是 `0` 到 `1`。
- 视频文件、poster 和可选 metadata 必须都在素材库内；登记的时长、宽高、音轨必须来自实际探测。
- 视频默认 `mute=true`，`fit` 只允许 `cover` 或 `contain`；播放区间不能越过源视频结尾。
- 自动空镜的 `source_start_us + defaults.duration_us` 必须是完整复核过的连续干净窗口；暂停、加载、
  鼠标点击/回放图标、播放器控件不得进入该窗口。语义视频只播放一次该干净窗口，旧目录即使
  留有 `loop=true` / `loop_allowed=true` 也会被运行时强制忽略。
- 目录内容变化会改变 `catalog_version`，下一次统一内容分析会使用新候选；已有计划不能误命中
  旧 catalog 缓存，人工锁定项不会被静默覆盖。
