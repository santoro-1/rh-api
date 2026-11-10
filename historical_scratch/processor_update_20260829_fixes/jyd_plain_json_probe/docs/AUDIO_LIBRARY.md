# 音频素材批量采集

## 新版工作台音乐采集入口

新版工作台顶部提供“音乐采集”入口。先在剪映中建立音乐采集草稿，把需要入库的歌曲放到
顶层音频轨道并保存；启动本机草稿采集器后，在入口中选择草稿并点击“采集并加入音乐库”。
采集器复用 `/api/drafts/collect-personal-assets`，固定只提交 `audio` 类型，并把提取结果通过
`/api/personal-assets/import` 上传到处理机的永久个人音乐库。完成后新版工作台会立即刷新 BGM
候选，不需要重启服务或刷新页面。

本入口当前只负责正式入库：新音乐可以手动选择，但不会写入 `music_profiles.v1.json`，也不会
参与自动选歌。标签审核和自动匹配准入留待后续实现。

当前工具负责采集和持久化音频文件。采集完成后，可以在网页中分类、试听、指定音乐，或让任务在某个分类中按导入顺序轮换取下一首。

## 推荐的剪映采集草稿

1. 从高版本剪映收藏中把音乐添加到采集草稿。
2. 每首音乐放在独立音频轨道，并全部从 0 秒开始，避免时间线变成所有歌曲时长之和。
3. 每个采集草稿建议放 20 到 50 首，数量较多时拆成多个草稿。
4. 确认每首音乐已经下载到本机并能正常播放，然后保存草稿。

工具会读取所有顶层 `audio` 轨道，轨道下标不会作为音频身份。

## 批量提取

```powershell
cd "D:\工作内容\轻盈健\公寓\jyd_plain_json_probe"

D:\Myanaconda\python.exe .\tools\library\export_audio_library.py `
  --draft-dir "D:\剪映草稿\JianyingPro Drafts\音乐采集01"
```

高版本加密草稿会自动调用 `jy-draftc` 解密。默认输出到项目内的 `data/libraries/audio_library`。

### 提取时自动分类

推荐把剪映草稿命名为 `音乐采集_分类名`，然后使用：

```powershell
D:\Myanaconda\python.exe .\tools\library\export_audio_library.py `
  --draft-dir "D:\剪映草稿\JianyingPro Drafts\音乐采集_轻松" `
  --category-from-draft-name
```

程序会自动创建或复用“轻松”分类，并把本次草稿中的全部音乐加入该分类。也支持
`音效采集_转场` 这样的名称。

草稿名称不符合上述格式时，可以直接指定分类：

```powershell
D:\Myanaconda\python.exe .\tools\library\export_audio_library.py `
  --draft-dir "D:\剪映草稿\JianyingPro Drafts\音乐采集01" `
  --category "轻松"
```

分类采用追加方式：重复导入不会重复归类，音乐原来已有的其他分类也会保留。

需要重新复制已收录文件时：

```powershell
D:\Myanaconda\python.exe .\tools\library\export_audio_library.py `
  --draft-dir "D:\剪映草稿\JianyingPro Drafts\音乐采集01" `
  --replace
```

## 输出结构

```text
audio_library/
  catalog.json
  files/
    音乐名称_music-id.mp3
  metadata/
    音乐名称_music-id.json
  manifest/
    audio_manifest.json
```

工具通过 `audio segment.material_id` 关联 `materials.audios[*].id`，复制 `material.path` 指向的完整音频文件。时间线上的 `source_timerange` 和 `target_timerange` 只作为来源记录，不会截断导出的音乐。

素材按 `music_id`、`resource_id` 和文件 SHA-256 去重。多次运行不同采集草稿时，会累积更新同一个清单，不会删除之前已经收录的音乐。

## 分类与网页选择

启动 Web API 后打开 `http://127.0.0.1:8010/app`，在“音乐与特效”区域可以：

1. 创建音乐分类。
2. 给已采集音乐指定分类。
3. 试听并固定选择某一首音乐。
4. 选择一个分类，让每次提交的任务按清单顺序取下一首。

分类、素材归属和每个分类的轮换游标保存在 `catalog.json`。任务提交时会立即把分类选择解析为具体音乐文件，并在 job 记录中保存 `selected_library_audio`，因此后续分类变化不会改变已经排队的任务。

相关接口：

```text
GET  /api/audio-library
POST /api/audio-library/categories
POST /api/audio-library/assign
GET  /api/audio-library/file?identity=...
```

## 迁移到其他电脑

采集完成后复制整个 `audio_library` 目录。实际渲染使用 `files` 中的独立音频文件，不再依赖原电脑的剪映收藏和缓存路径。一起复制 `catalog.json` 可以保留分类和轮换位置；`metadata` 中保留原剪映素材信息。

如果某首收藏音乐没有下载到本机、缓存路径为空或文件受版权限制，工具会报告缺失，无法仅凭草稿 JSON 恢复音频内容。

## 智能音乐标签与 Top1 匹配

首批 46 首音乐的受控语义标签保存在 `manifest/music_profiles.v1.json`，运行时通过稳定
`music_id:*` 与音频 manifest 对应，不依赖 Excel、显示名称或分类游标。原表已确认的
42 首允许自动选择，4 首未确认曲目保留但不进入自动候选。

本地 `MusicProfileMatcher` 先检查文件、审核、使用权限和禁用特征，再按固定 100 分权重
确定语义近似候选，并只在近似候选内做轻量重复降权，直接返回最优一首，不返回 Top3。
较短音乐由浏览器和剪映导出从成片结尾向前循环补足，最后一轮完整落在曲目自然结尾，
轮次间使用 0.2 秒交叉衔接；音乐长于视频时直接使用其末尾等长区间。完整字段、算法、降权和异常规则见
[`MUSIC_MATCHING_V1.md`](MUSIC_MATCHING_V1.md)。
