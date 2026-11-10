# 字体素材库

`tools/library/export_font_library.py` 用于从剪映草稿中找到实际使用的字体，并把仍然存在的字体文件复制到统一素材库。

默认素材库目录：

```text
D:\工作内容\轻盈健\公寓\font_library
```

目录结构：

```text
font_library/
  files/                 # 实际字体文件
  metadata/              # 每个字体的来源和引用信息
  manifest/
    font_manifest.json   # 字体索引和去重信息
```

字体优先按剪映资源 ID 去重，没有稳定资源 ID 时按 SHA-256 去重。高版本草稿会先在临时副本上自动解密。

本地采集页面已经提供“提取本草稿字体”按钮，普通用户不需要运行命令。开发排错时可以使用：

```powershell
D:\Myanaconda\python.exe .\tools\library\export_font_library.py `
  --draft-dir "D:\剪映草稿\JianyingPro Drafts\字体来源草稿"
```

采集器会优先读取字体文件内部的完整名称，而不是直接使用剪映缓存中的 `font.ttf` 文件名。已有素材库可以原地刷新名称，不需要重新提取草稿或重新复制字体：

```powershell
D:\Myanaconda\python.exe .\tools\library\export_font_library.py --refresh-library
```

注意：

- `tools/library/export_text_style.py` 负责保存字号、颜色、位置、字体 ID 和字体路径等样式数据。
- `tools/library/export_font_library.py` 负责复制字体文件本身。
- JSON 中有字体路径但本机文件已经被删除时，只能标记为缺失，无法从草稿 JSON 恢复字体文件。
- 将来上传到服务器后，服务端会把样式中的旧字体路径重写为渲染机字体库路径。
