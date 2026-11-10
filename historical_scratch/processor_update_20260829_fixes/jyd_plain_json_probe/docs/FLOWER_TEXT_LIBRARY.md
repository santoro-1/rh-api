# 花字素材批量采集

这个工具只采集普通文字片段直接使用的剪映“花字”，对应草稿 JSON 中的
`materials.effects[*].type == "text_effect"`。复合文字模板属于另一种结构，
不会混入花字素材库。

## 制作采集草稿

1. 在剪映中新建一个花字采集草稿。
2. 每个普通文字片段应用一种花字，文字内容可以保留默认值。
3. 可以把多个花字依次放在同一条文字轨道，也可以使用多条文字轨道。
4. 确认每个花字已经下载完成并能正常预览，然后保存草稿。

## 批量提取

```powershell
cd "D:\工作内容\轻盈健\公寓\jyd_plain_json_probe"

D:\Myanaconda\python.exe .\tools\library\export_flower_text_library.py `
  --draft-dir "D:\剪映草稿\JianyingPro Drafts\花字测试" `
  --force-decrypt
```

高版本加密草稿会自动调用 `jy-draftc` 解密。默认输出到工作区的
`text_effect_library`。需要重新复制已有资源时增加 `--replace`。

## 输出结构

```text
text_effect_library/
  bundles/
    双描边紫色渐变花字_7127684319300078885/
      text_effect.json
      resources/
        effect/
          config.json
          texture/
          xshader/
          ...
  manifest/
    text_effect_manifest.json
```

`text_effect.json` 保存剪映效果素材、文字中的 `effectStyle` 示例、原始片段引用和
资源迁移信息。`resources/effect` 是从剪映缓存复制出的完整花字资源，后续迁移到
渲染服务器时不再依赖采集电脑上的原始缓存路径。

工具按 `resource_id`、`effect_id` 等稳定标识去重，不依赖文字轨道或片段下标。
同一个花字出现在多个片段中时，只保存一份资源，并把所有来源累计到 metadata。

字体、字号、颜色、位置仍由文字样式库管理。花字库只负责花字效果本身，避免同一
花字被固定绑定到采集时碰巧使用的字体。
