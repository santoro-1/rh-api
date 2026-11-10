# 复合文字模板批量采集

这个工具采集剪映的复合文字模板，对应草稿中的 `materials.text_templates`。
它与普通字体样式、花字 `text_effect` 是三种不同的素材结构。

一个复合文字模板可以包含：

- 一个或多个可替换文字槽；
- 每个文字槽独立的字体、花字、位置和动画；
- 贴纸、形状和背景；
- 模板整体的缩放、位置与持续时间；
- 剪映缓存中的字体、贴纸、花字和动画资源。

## 制作采集草稿

1. 在剪映中新建复合文字模板采集草稿。
2. 每个时间段放一个文字模板，模板之间不要嵌套或重叠也可以正常识别。
3. 模板可以位于同一条或多条文字轨道，工具不依赖固定下标。
4. 确认模板及其字体、贴纸和动画已经下载完成，然后保存草稿。

## 批量提取

```powershell
cd "D:\工作内容\轻盈健\公寓\jyd_plain_json_probe"

D:\Myanaconda\python.exe .\tools\library\export_text_template_library.py `
  --draft-dir "D:\剪映草稿\JianyingPro Drafts\花字测试" `
  --force-decrypt
```

默认输出到工作区的 `text_template_library`。需要重新复制已有模板资源时增加
`--replace`。

## 输出结构

```text
text_template_library/
  bundles/
    模板名称_资源ID/
      text_template.json
      resources/
        路径哈希_资源目录或文件
        ...
  manifest/
    text_template_manifest.json
```

每个 `text_template.json` 中包含：

- `template`：原始 `text_templates` 素材；
- `segment_template`：时间线片段布局；
- `text_slots`：按顺序排列的可替换文字槽；
- `referenced_materials`：花字、文字动画等附加素材；
- `resources`：原始路径、素材库路径和 JSON 引用位置映射；
- `sources`：模板来自哪个草稿和片段。

后续生成逻辑必须给模板、文字槽和附加素材重新生成 UUID，再把资源路径重写为
`resources[*].library_path`，不能把原始 ID 原样插入同一个草稿多次。
