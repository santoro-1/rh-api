# 剪映草稿导入分析

`tools/draft/analyze_draft_import.py` 是开发阶段的验证入口，不是最终交付给普通用户的操作方式。正式版本会由本地采集工具调用同一套 Python 函数，用户只需要在图形界面中选择草稿并点击“分析并上传”。

## 目标

本地采集工具需要先完成以下工作：

1. 读取用户选择的剪映草稿。
2. 如果草稿已加密，在临时副本上调用 `jy-draftc` 解密，不修改原草稿。
3. 识别可替换的 BGM、音效、视频特效、普通文字、花字和复合文字模板。
4. 扫描草稿引用的本地文件，并与服务器已有素材库进行匹配。
5. 先上传小型分析报告，等待服务器返回真正缺少的文件列表。
6. 只上传服务器缺少且生成任务仍会使用的文件。

当前业务不处理转场，因此 `materials.transitions`（包括叠化等转场资源）不会进入可替换槽位或依赖上传清单。

网页不能直接读取任意本地文件夹，因此“读取剪映草稿”必须由安装在用户电脑上的轻量本地工具完成。剪映控制和 MP4 导出仍在专用 Windows 渲染机上排队执行。

## 报告结构

报告 schema 为 `jyd_probe.draft_import_report.v1`，主要包含：

- `draft`：草稿名称、版本、画布、时长和轨道数量。
- `slots.audio`：可替换的 BGM/音效片段。
- `slots.video_effects`：可替换的视频特效片段。
- `slots.texts`：普通文字和花字片段。
- `slots.text_templates`：复合文字模板及其内部文字槽位。
- `dependencies`：草稿引用的本地视频、音频、字体、特效资源等。
- `summary`：槽位和依赖状态的汇总。
- `warnings`：缺失资源、空草稿等需要用户确认的问题。

每个槽位同时保存素材 ID、轨道 ID、片段 ID 和当前下标。后续修改优先使用稳定 ID 定位，下标只作为兼容信息，避免轨道顺序变化后替换错位置。

文字槽位不能只依赖轨道的 `type == "text"`。部分剪映版本会把普通文字片段放进 `mixed` 轨道；分析器、账号模板字幕轨检测和后续字幕替换会统一逐个检查 segment 的 `material_id`，命中 `materials.texts` 时按普通文字/花字处理，命中 `materials.text_templates` 时按复合文字模板处理。报告的 `summary.track_type_counts` 保留原始轨道类型统计，使用素材引用兼容识别时会同时写入 `warnings`。替换 mixed 轨道中的字幕时，轨道内引用其他素材集合的 segment 必须原样保留。

## 依赖状态

- `central_library`：服务器素材库已经存在，不需要上传。
- `upload_required`：本地存在，但服务器没有，需要按需上传。
- `missing`：草稿引用的路径在本机也不存在，需要用户补充或放弃该元素。
- `external`：目录、特殊协议或暂时无法打包的外部依赖。

`can_skip_if_replaced=true` 表示该依赖只有在保留当前元素时才需要。例如任务会替换原字体或原特效，就可以跳过对应旧缓存；固定源视频不能这样跳过。

## 代码调用

本地工具和后端代码直接调用函数，不需要拼接 PowerShell 命令：

```python
from jyd_probe.draft_import_analyzer import analyze_draft_import

report = analyze_draft_import(
    draft_json,
    source_draft_dir=source_draft_dir,
    analyzed_draft_dir=plain_draft_dir,
    was_decrypted=True,
    workspace_root=workspace_root,
)
```

开发排错时仍可使用：

```powershell
D:\Myanaconda\python.exe .\tools\draft\analyze_draft_import.py `
  --draft-dir "D:\剪映草稿\JianyingPro Drafts\草稿名称"
```

## 正式交互流程

1. 用户在网站获取设备配对码。
2. 用户打开本地采集工具并完成一次配对。
3. 本地工具自动列出剪映草稿，用户选择需要处理的成片草稿。
4. 工具显示草稿时长、文字、BGM、音效、特效和缺失文件摘要。
5. 用户点击上传，工具先发送报告，再按服务器返回结果上传必要文件。
6. 用户回到网页选择每类元素的策略：不使用、固定、参与组合或按顺序轮换。
7. 网站创建批量任务，渲染机逐个生成草稿并导出 MP4。

下一阶段需要实现本地采集工具的 HTTP 接口和最小图形界面，并在网站增加“已连接设备”和“选择本地草稿”入口。
