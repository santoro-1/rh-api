# 画面特效批量采集

## 制作采集草稿

1. 在剪映中新建一个较长的测试视频草稿。
2. 每隔 2 到 3 秒放一个画面特效，同一时间只放一个。
3. 特效可以位于同一条或多条 effect 轨道，导出程序不会依赖固定轨道下标。
4. 确认每个特效已在当前渲染电脑上下载完成，然后保存草稿。

## 批量导出

```powershell
cd "D:\工作内容\轻盈健\公寓\jyd_plain_json_probe"

D:\Myanaconda\python.exe .\tools\library\export_effect_library.py `
  --draft-dir "D:\剪映草稿\JianyingPro Drafts\特效采集01"
```

加密草稿会自动调用 `jy-draftc` 解密。默认输出到项目内的 `data/libraries/effect_library`。

需要覆盖已经导出的同名特效时：

```powershell
D:\Myanaconda\python.exe .\tools\library\export_effect_library.py `
  --draft-dir "D:\剪映草稿\JianyingPro Drafts\特效采集01" `
  --replace
```

## 输出

```text
effect_library/
  星雨_634187.json
  模糊_200.json
  manifest/
    effect_manifest.json
```

程序通过 `effect segment.material_id` 关联 `materials.video_effects[*].id`，再按 `resource_id`、`effect_id` 去重。轨道和片段下标只写入来源信息，不作为特效身份。

当前批量工具采集的是顶层画面特效，也就是 `tracks[type=effect]` 和 `materials.video_effects`。音频素材、音效、转场、文字动画和嵌套模板特效需要使用各自的采集流程。
