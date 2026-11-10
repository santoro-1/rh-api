# 全屏贴纸素材库

全屏贴纸通过一个专用剪映草稿采集。该草稿只放允许自动铺满整段视频的贴纸；局部贴纸不应放入采集草稿。

## 采集

```powershell
D:\Myanaconda\python.exe .\tools\library\export_sticker_library.py `
  --draft-dir "D:\剪映草稿\JianyingPro Drafts\全屏贴纸草稿"
```

工具会自动解密高版本草稿，并把每个贴纸的完整缓存目录复制到工作区 `sticker_library`。再次采集时会按 `resource_id` 去重；需要刷新已有资源时增加 `--replace`。

## 素材结构

每个贴纸包包含：

- `sticker.json`：剪映贴纸素材和片段模板。
- `resources/sticker`：PNG、Lua、JSON 等完整运行资源。
- `preview_file`：从资源目录选出的本地预览图。

普通产品页将素材库中的每个贴纸视为一个组合候选项。每条输出视频只添加一个候选贴纸，开始时间为 0，持续到视频结尾。

## 语义前景图片目录

`data/libraries/semantic_visual_library` 是独立产品目录，不属于全屏贴纸库或四角装饰贴纸库。
`catalog.json` 使用 `jyd.semantic-visual-catalog.v1`，登记概念、别名、稳定 `asset_id`、贴纸包、
预览图和左上/右上默认安全区。加载时会拒绝路径越界、缺失文件、重复素材 ID、未知概念、
非法位置/缩放/透明度；目录版本同时哈希清单、`sticker.json` 和预览图片内容。

当前 MVP 使用 7 个已打包素材：鸡蛋 2 个，以及玉米、红薯、燕麦、豆浆、蔬菜各 1 个。
后续替换图片时保留稳定 `asset_id` 和贴纸包结构，重新启动工作台即可得到新的目录版本。
