# Render Job JSON

当前稳定版本：`jyd.render_job.v1`

后端入口：

```python
from jyd_probe.render_job import run_render_job, run_render_job_file

result = run_render_job_file("job.json")
print(result.as_dict())
```

命令行调试入口：

```powershell
D:\Myanaconda\python.exe .\tools\jobs\run_render_job.py --job .\examples\render_job_video.example.json
```

## 顶层结构

```json
{
  "schema": "jyd.render_job.v1",
  "source": {},
  "output": {},
  "captions": {},
  "texts": [],
  "text_templates": [],
  "audios": [],
  "effects": [],
  "stickers": [],
  "visual_variant": {},
  "export": {}
}
```

## source

上传 MP4 直接生成基础草稿，不需要解密：

```json
{
  "type": "video",
  "media_path": "D:/素材/input.mp4",
  "work_root": "D:/工作目录/generated_video_drafts",
  "fade_out_us": 2000000,
  "canvas": {
    "width": 0,
    "height": 0,
    "fps": 30
  }
}
```

`fade_out_us` 是主视频片尾“渐隐”时长，单位微秒；`0` 表示关闭。单视频作用于唯一主片段，
`video_sequence` 只作用于最后一个主视频片段，不会给语义覆盖层或字幕重复添加动画。

按需导出已冻结草稿时使用 `source.type=existing_draft`。项目工作台会在
`source.recovery.rebuild_job` 中一并冻结一个 `output.skip_export=true` 的恢复任务：剪映首页连续
5 次返回 `DraftNotFound` 后，保留原草稿并用该任务创建一个新名称草稿，再识别 5 次；第二轮
仍失败才停止。普通调用可以省略 `recovery`，省略后发现失败不会重建草稿。

多个视频需要在剪映主轨道中保持为独立素材片段时，使用 `video_sequence`：

```json
{
  "type": "video_sequence",
  "items": [
    {"media_path": "D:/素材/segment-001.mp4", "target_duration_us": 4200000, "transition_after_us": 250000, "volume": 0.0},
    {"media_path": "D:/素材/segment-002.mp4", "target_duration_us": 3800000, "volume": 0.0}
  ],
  "canvas": {"width": 0, "height": 0, "fps": 30}
}
```

`items` 按数组顺序进入同一条主视频轨道，不会先合并成一个媒体文件。目标时长短于素材时
直接裁尾；目标时长长于素材时用剪映片段速度把现有画面轻微放慢到目标时长，不生成尾帧
素材。可选 `volume` 控制该视频片段原声，范围 `0..2`。字幕、独立语音、BGM、特效和封面
仍使用既有绝对时间轴。可选 `transition_after_us` 为当前真实视频片段与下一真实视频
片段直接添加剪映原生“叠化”，单位微秒；工作台多片段固定请求 250000 微秒，仅在任一真实
片段本身不足 250000 微秒时按安全时长缩短。转场不会额外生成时间线片段。

新版项目工作流的浏览器预览仍播放 4A 标准化的单个 `base_video`；4B 按需导出和模块 6 则
提交 RunningHub 原始片段组成的 `video_sequence`。有 `generation_tail_seconds` 的数字人
分段，中间片段目标时长只取口播时长，最后片段最多取口播时长加生成尾；片段原声静音，
完整、已审核的 MiniMax 音频作为独立音轨从 0 开始写入。这样接缝没有静音停顿，末尾又保留
真实运动画面用于渐隐，并且不会因供应商 MP4 容器时长误差逐段累计字幕偏移。

历史数字人任务若没有 `generation_tail_seconds`，且原始分段实际总长比 4A 记录的批准音频
时间轴短 50 毫秒以上，4B 自动改用已经按批准音频标准化的 `base_video`。这项兼容保护避免
`fit_to_video` 把累计的供应商/SeedVR2 容器短差转化为片尾口播截断；带生成尾的新任务仍保留
独立 `video_sequence`，不会进入该回退分支。

模板模式用于套用已经存在的剪映草稿。模板可能是明文，也可能是高版本加密草稿；加密草稿会按 `decrypt` 配置自动解密到工作副本：

```json
{
  "type": "template",
  "template_draft_dir": "D:/剪映草稿/JianyingPro Drafts/模板名"
}
```

更推荐的生产方式是先把模板导入模板库，然后渲染时只传 `template_id`：

```powershell
D:\Myanaconda\python.exe .\tools\draft\manage_templates.py import `
  --source-draft-dir "D:\剪映草稿\JianyingPro Drafts\模板名" `
  --template-id "demo_template" `
  --name "演示模板"
```

导入后会生成：

```text
jyd_plain_json_probe/data/template_library/demo_template/
  draft/
    draft_content.json
    draft_meta_info.json
  template_meta.json
```

渲染任务里使用：

```json
{
  "type": "template",
  "template_id": "demo_template",
  "library_root": ""
}
```

`library_root` 留空时使用 `jyd_plain_json_probe/template_library`。模板库里的草稿应当已经是明文，所以渲染时不会重复解密。

已经完成时间线构建、只需要编码 MP4 时，使用 `existing_draft`：

```json
{
  "source": {
    "type": "existing_draft",
    "draft_dir": "D:/剪映草稿/JianyingPro Drafts/已冻结草稿",
    "draft_name": "已冻结草稿"
  },
  "output": {"mp4_path": "D:/输出/result.mp4"},
  "export": {"resolution": "1080P", "framerate": "30fps"}
}
```

该模式校验草稿目录及 `draft_content.json` 后直接调用剪映导出，不重新执行字幕、音频、封面、
贴层或其他时间线修改。项目 4B 在 `postprocess/generate` 阶段先用 `skip_export=true` 生成冻结
草稿，下载阶段再用此模式编码。

## output

```json
{
  "draft_root": "D:/剪映草稿/JianyingPro Drafts",
  "draft_name": "",
  "mp4_path": "D:/输出/result.mp4",
  "skip_export": false
}
```

`draft_name` 留空时自动生成。真实导出 MP4 时，`draft_root` 应该是剪映实际的 `JianyingPro Drafts` 目录。

## captions

上传 MP4 时，可以把长文案自动切分为一条字幕轨道上的多个片段：

```json
{
  "text": "与视频口播对应的完整长文案……",
  "start_us": 0,
  "duration_us": 0,
  "max_chars": 16,
  "style_json_path": "D:/项目/text_style_library/抖音美好体测试.json",
  "size": 15,
  "color": "#FFFFFF",
  "stroke_color": "#000000",
  "stroke_width": 0.06,
  "transform_x": 0.0,
  "transform_y": -0.8,
  "line_max_width": 0.82
}
```

`duration_us=0` 表示覆盖上传视频从 `start_us` 开始的剩余时长。程序按换行、标点和 `max_chars` 生成 SRT，再通过 `ScriptFile.import_srt` 导入。每条字幕的时间按文字显示宽度比例分配。

`captions` 会在视频替换和其他草稿修改完成后写入最终草稿，因此直接上传 MP4 和“上传 MP4 + 套用模板”两种流程都可使用。`texts` 继续负责单个标题的新增和模板已有文字替换。

已有语音服务时间戳时，不要再按文字长度估时。可以直接提交精确字幕轨道；每条支持 `end_us` 或 `duration_us`：

```json
{
  "captions": {
    "cues": [
      {"start_us": 0, "end_us": 1250000, "text": "第一句。"},
      {"start_us": 1400000, "duration_us": 1600000, "text": "第二句。"}
    ],
    "track_name": "MiniMax 精确字幕"
  }
}
```

也可以传入内联的 `srt_text`。`cues` / `srt_text` 与 `text` 同时存在时，优先使用精确时间轴。允许供应商字幕结尾比视频长不超过 1 秒，程序会把最后一条裁到视频结尾；更大的越界或字幕重叠会拒绝任务。

## texts

新增文字：

```json
{
  "type": "add",
  "scope": "top",
  "track_name": "auto_title",
  "text": "新的标题",
  "start_us": 0,
  "duration_us": 5000000,
  "style_json_path": "D:/项目/text_style_library/style.json",
  "text_effect_json_path": "D:/项目/text_effect_library/bundles/双描边紫色渐变花字/text_effect.json",
  "apply_clip": true,
  "transform_x": 0.0,
  "transform_y": 0.703125,
  "size": 13,
  "line_max_width": 0.92,
  "color": "#FFFFFF",
  "stroke_color": "#000000",
  "stroke_width": 0.04,
  "font_id": "7244518590332801592",
  "font_path": "D:/项目/font_library/DouyinSansBold.otf",
  "font_title": "DouyinSansBold"
}
```

`text_effect_json_path` 是可选的花字素材，只应用于这条新增文字。新增文字可直接指定填充、描边、
字体、单行宽度与坐标；这些字段在样式预设之后应用，因此固定标题可锁定最终参数。
`start_us=0` 表示从视频开头开始，`duration_us=0` 表示从开始时间持续到视频结尾。
新增文字的结束点若只因媒体帧取整而超出草稿实际时长不超过一个 30fps 帧（33334 微秒），
渲染器会把它裁到片尾；起点越界或结束点超出一帧仍会拒绝任务，防止错误视频/文字绑定被掩盖。

替换已有文字：

```json
{
  "type": "replace",
  "scope": "top",
  "track_index": 0,
  "segment_index": 0,
  "text": "替换后的文字",
  "style_json_path": "D:/项目/text_style_library/style.json"
}
```

## text_templates

复合文字模板会复制模板中的多段文字、花字、贴纸和动画资源，并按文字槽顺序替换内容：

```json
{
  "template_json_path": "D:/项目/text_template_library/bundles/新年愿望清单标题模板/text_template.json",
  "texts": [
    "· 一夜暴富\n· 减肥成功\n· 工作顺利\n· 厄运退散",
    "「新年愿望」"
  ],
  "start_us": 0,
  "duration_us": 0,
  "track_name": "new_year_wishes"
}
```

`texts` 按模板元数据里的文字槽顺序对应；少传的槽保留模板原文，显式传空字符串会清空对应槽。`duration_us=0` 同样表示持续到视频结尾。

## audios

给上传 MP4 添加 BGM 时用 `add` 或 `bgm`，不会静音原视频人声：

```json
{
  "type": "bgm",
  "media_path": "D:/素材/bgm.mp3",
  "target_start_us": 0,
  "target_duration_us": 0,
  "fit_to_video": true,
  "align_to_end": true,
  "crossfade_us": 200000,
  "fade_in_us": 5000000,
  "volume": 0.3
}
```

`type=bgm`、`fit_to_video=true` 且 `target_duration_us=0` 时，BGM 自动覆盖视频剩余时长；
默认兼容行为仍从音乐开头正向循环并裁切最后一轮。新版 4B/变体另传 `align_to_end=true`：
渲染器从视频结尾向前铺设，音乐长于视频时取曲目末尾等长区间；音乐短于视频时最后一轮完整
播放到曲目自然结尾，最早一轮允许只取曲目尾部。`crossfade_us` 控制相邻轮次的交叉衔接，
4B/变体固定为 `200000`；实现使用两条交替音轨，最后一轮不淡出。
`fade_in_us` 控制整条 BGM 从时间线开头渐起的时长，单位微秒；只应用于最早的音乐片段，
并可与该片段用于循环衔接的淡出同时存在。`0` 表示关闭。
普通 `type=add` 默认仍只播放一次；确实需要循环时可显式传 `loop_to_video=true`。
`volume` 支持 `0.0` 到 `2.0`。通用提交页未指定时使用页面默认值；数字人 4B 流程则保存
响度分析得到的冻结音量，普通音乐范围为 `0.08..0.25`，强人声音乐范围为 `0.05..0.16`。

通过 Web API 提交时，`media_path` 还可以替换成以下任意一种引用：

```json
{"type": "add", "library_identity": "固定音乐素材 ID", "fit_to_video": true}
```

```json
{"type": "add", "library_category_id": "分类 ID", "selection_mode": "next", "fit_to_video": true}
```

`library_category_id` 会在任务入队时按音乐清单顺序选择下一首并推进持久化游标。`render_job.py` 的直接 Python/命令行入口仍使用已经解析好的 `media_path`。

模板里已有独立音频轨道时，也可以替换指定音频片段：

```json
{
  "type": "replace-segment",
  "media_path": "D:/素材/bgm.mp3",
  "track_index": 0,
  "segment_index": 0
}
```

## effects

```json
{
  "effect_json_path": "D:/项目/effect_library/流星雨.json",
  "target_video_track_index": 0,
  "target_video_segment_index": 0,
  "start_us": -1,
  "duration_us": 0
}
```

`start_us=-1`、`duration_us=0` 表示跟随目标视频片段。

## visual_variant

画面变化套装会先按全局时间轴切开视频片段并交替镜像，再做人脸居中裁剪和纯色画布填充：

```json
{
  "enabled": true,
  "mirror_interval_seconds": 10,
  "crop_ratio": "1:1",
  "background_color": "#000000",
  "face_centered": true,
  "face_sample_count": 3,
  "video_track_index": 0
}
```

`mirror_interval_seconds=10` 表示 `0-10s` 保持原方向、`10-20s` 水平镜像、`20-30s` 再恢复。当前网页提供 `1:1`、`3:4` 两种裁剪比例；底层接受任意合法的 `宽:高` 正数比例。检测不到人脸或视频路径不可读时自动使用画面中心，不中断任务。

## stickers

普通全屏贴纸仍不传 `corner`。四角贴纸为同一个任务追加四项：

```json
[
  {"sticker_json_path": "D:/项目/sticker_library/a/sticker.json", "corner": "top_left", "visible_ratio": 0.05},
  {"sticker_json_path": "D:/项目/sticker_library/b/sticker.json", "corner": "top_right", "visible_ratio": 0.05},
  {"sticker_json_path": "D:/项目/sticker_library/c/sticker.json", "corner": "bottom_left", "visible_ratio": 0.05},
  {"sticker_json_path": "D:/项目/sticker_library/d/sticker.json", "corner": "bottom_right", "visible_ratio": 0.05}
]
```

`visible_ratio` 当前网页可选 `0.05` 或 `0.10`。四角贴纸沿用已采集并打包进贴纸库的资源，不依赖原电脑的剪映缓存绝对路径。

## export

```json
{
  "resolution": "1080P",
  "framerate": "30fps",
  "timeout": 1200
}
```

导出依赖剪映客户端处于打开状态，并停在草稿首页。自动化找到草稿但单击后未进入编辑页时，
渲染器会重新聚焦并重试进入；连续失败最终报告“点击草稿后未进入编辑页”，避免把入口点击
失败误判为导出按钮缺失。

新版工作台模块 4B 的默认字幕字体为字体库资源
`resource_id:7244518590332801592`（`DouyinSansBold` / 抖音美好体）。这是工作台配置默认值，
Render Job 的 `captions.font_id` 与 `captions.font_path` 契约没有变化；历史任务仍使用其冻结
配方中的字体。

新版 4B 与模块 6 的冻结字幕样式固定为：字号 `14`、默认白色填充 `#FFFFFF`、黑色描边
`#000000`、描边宽度 `0.06`、单行、画面宽度 `0.8`、`transform_y=-850/1920`（1080×1920
参考参数 Y=-850）。断句由服务端按
真实字体字宽对整个原 cue 做平衡切分，不允许新字幕以标点开头，也不会为了靠近标点生成
只有一两个正文字符的孤句。显示字幕会隐藏逗号、句号、问号等断句标点，但保留 `24.4`
和 `8:30` 这类数字内部符号；“那么、但是、所以、然后”等承接词优先放到下一条字幕开头，
不会单独留在上一条末尾。过短逗号前缀修复后不再跨越其余逗号分句，数字量词表达式（如
“十年”“近 5 万名”）内部也不得切分。浏览器预览直接使用这些参数，不得另行自动缩小字号。

## visual_overlays

新版工作台 4B 冻结任务可同时包含图片和视频。以下路径不是模型返回值，而是工作台在提交
渲染前，依据已冻结 recipe 的素材库相对路径解析得到的本机受控路径：

```json
{
  "visual_overlays": [
    {
      "asset_id": "food.egg.boiled.image.01",
      "media_type": "image",
      "renderer": "jyd_sticker_bundle",
      "bundle_path": "受控语义图片 bundle 路径",
      "enabled": true,
      "start_us": 500000,
      "duration_us": 1800000,
      "corner": "top_right",
      "scale": 0.28,
      "opacity": 1.0
    },
    {
      "asset_id": "activity.aerobic.core_broll.video.01",
      "media_type": "video",
      "renderer": "video_overlay",
      "video_path": "受控语义视频路径",
      "enabled": true,
      "start_us": 15000000,
      "duration_us": 5000000,
      "source_start_us": 12000000,
      "corner": "center",
      "scale": 1.0,
      "opacity": 1.0,
      "mute": true,
      "fit": "cover",
      "timing_mode": "seam_broll",
      "segment_boundary_us": 15000000
    }
  ]
}
```

字段来自项目已冻结的 `jyd.semantic-visual-recipe.v2`，不是渲染时重新分析；v1 历史图片配方
仍可兼容读取。图片 bundle 继续作为可搬迁、可校验的素材容器，渲染时读取
`resources/sticker/singleImage.png`，写成 `type=photo` 的真实图片素材和独立视频轨道，而不是
剪映贴纸轨道。视频写成原生 video material/segment，支持目标时间、源片截取、静音、
`cover/contain`、位置、缩放和透明度。图片和视频使用同一占用表，随后一起应用封面时间偏移。
两者均为 optional：素材缺失或单项写入失败会跳过该项，不改变语音、字幕、BGM、主视频、
总时长或已有输出。

口播下方素材默认用 `corner=bottom_center`。当前产品默认值为语义图片 `scale=0.56`、动作视频
`scale=0.615`，水平中心固定在画面中轴；高素材最多显示下方约 37%，允许底边适度裁出。
`corner=center + scale>=0.95` 仍只表示全屏 B-roll。

自动明确语义素材使用 `sentence-v1` 分句时间规则：关键词字符范围先向两侧扩展到最近的逗号、
句号、问号、感叹号、分号、冒号或换行，先取得该句段实际发音区间；最终单行字幕生成后，普通
自动明确语义素材的 `start_us` 再吸附到包含关键词的最终字幕起点，结束时间不早于原句段结束，
且不得越过成片末尾。顿号不切句，同句多个入选
语义按关键词语音中心点在完整句段内速切，单项不做 2 秒保底。有 FunASR 时使用真实字词时间，
无 FunASR 时使用 MiniMax raw cue 字符插值。

冻结 recipe v2 可增加 `timing_policy_version`、`used_asset_ids`，overlay 可增加句段、列举和
`segment_boundary_us` 元数据；旧 recipe 缺少字段时仍兼容。自动素材在成片级按 `asset_id`
去重，同 concept 按 `semantic_overlay/full_screen_broll` 展示角色分别保留 20 秒密度冷却，
最终图片/视频仍不得重叠；新项边缘重叠不超过 0.5 秒时先裁短或顺延。通用空镜按
`VISUAL_BROLL_TARGET_INTERVAL_SECONDS=10` 约每 10 秒尝试且实际至少间隔 6 秒；接缝空镜按
下一段语境独立尝试，接缝空镜和通用全屏空镜先于明确语义占位。若冻结视频目标区间长于源片可用区间，浏览器和
渲染层都只播放从 `source_start_us` 起的剩余内容，随后提前结束，不循环或定格补齐。

正文顶部固定文字由 `texts` 自动带入单行“世界冠军带你自律”，字号 19、
`transform_y=1535/1920`、红色填充、白色描边，并持续整个正文；模型两行标题只用于封面。
底部两行免责声明同样由 `texts` 带入，字号 6、`transform_y=-1760/1920`，其
`opacity=0.5` 会写入剪映文字素材 `global_alpha`；浏览器预览使用同一 50% 透明度。

项目 4B 和变体任务还会自动带入 `fixed_overlays`。当前固定项为“张雒人名牌”：
正文时间 `start_us=0`、`duration_us=0`（解析为完整正文草稿时长），由任务的
`layout_profile` 选择站姿或坐姿规范。站姿使用原生贴纸缩放 `0.8941348042237189`，坐姿使用
`1.08873624376896`，并分别冻结对应规范草稿的旋转、位置和三层文字参数；
应用封面偏移后从正文第 1 帧开始，因此封面 3 帧不显示。统一层级从下到上为
`主视频 < 固定人名牌 < 语义图片/小窗视频/全屏 B-roll < 字幕`。任何后出现的语义贴图或视频
都可通过层级自然覆盖，结束后无需显隐事件即可恢复。人名板使用 bundle 内完整本地资源写成
剪映原生 sticker 轨道，同样为 optional，且不作为大模型输入。

### 项目固定封面

新版项目的 `cover` 由工作台根据 `postprocess.cover_title` 自动构建，普通导出和变体共用同一
对象。变体 API 不直接接收封面视觉参数。核心字段示例：

封面样式同样根据 `layout_profile` 选择站姿/坐姿规范；封面文字关闭 `auto_wrapping`，不复用
字幕的 80% 宽度约束，避免较长但仍符合字符上限的标题被拆成第三行。

```json
{
  "cover": {
    "enabled": true,
    "frame_source": "input_image",
    "image_path": "任务当前输入图片绝对路径",
    "frame_count": 3,
    "text_line_1": "健康真相",
    "text_line_2": "别再踩坑",
    "font": {"font_id": "6807742980271641102"},
    "line_1_size": 30,
    "line_2_size": 22,
    "line_1_y": -0.08333333333333333,
    "line_2_y": -0.3411458333333333,
    "overlay_y_ratio": 0.625,
    "overlay_height_ratio": 0.26
  }
}
```

`frame_source=input_image` 时，渲染器按实际 `canvas_config` 进行居中 cover 裁切，不读取视频帧。
黑框以 50% 黑色直接合成到封面 JPEG；两行文字仍是独立可编辑文本轨道，并写入固定颜色、
行间距和阴影参数。封面应用后，正文所有轨道统一后移 3 帧。

项目语音条目使用 `fit_to_video=true`，渲染时以封面插入前的正文主视频时长裁切 MiniMax
文件可能带有的编码尾巴；`duration_us=0` 的固定人名板等固定贴层也会在同一入口解析成该
明确时长，因此不会由多出的几十毫秒语音尾部继续撑长草稿。
