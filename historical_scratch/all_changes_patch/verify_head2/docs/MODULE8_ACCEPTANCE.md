# 模块 8：完整闭环与发布前验证

本文记录新版工作台模块 8 的自动化验收入口，以及需要用户明确授权后才能执行的本地真实联调。

## 自动化验收矩阵

| 验收项 | 自动化证据 |
| --- | --- |
| 旧工作台数据库升级后保留既有任务 | `tests/test_project_api.py::test_project_tables_do_not_modify_existing_render_queue_schema_or_rows` |
| 项目、素材、操作、外部关联和成果批次按账号隔离 | `tests/test_project_api.py::test_project_contract_is_owned_and_contains_backend_actions`、`tests/test_project_results.py::test_gallery_bulk_delete_is_account_scoped_and_atomic` |
| 幂等键不重复创建付费任务或外部关联 | `tests/test_project_api.py::test_operations_are_idempotent_and_external_links_are_preserved` |
| 进程重启后可从持久化外部关联继续同步异步任务 | `tests/test_project_audio.py::test_pending_audio_operation_resumes_after_application_restart` |
| MiniMax、RunningHub 不产生真实测试费用 | 项目音频与画面合成测试全部 patch 客户端；完整回归禁止配置真实凭据 |
| 精确字幕、BGM、剪映任务结构与最大差异变体组合 | `tests/test_project_audio.py`、`tests/test_project_postprocess.py`、`tests/test_project_variants.py` |
| 新版前端不以模拟定时器决定业务成功 | `tests/test_new_frontend.py::test_new_workspace_and_voice_center_use_real_voice_apis`、`test_module_6_uses_real_variant_api_and_manual_three_frame_cover` |
| 成果库真实文件闭环与安全删除 | `tests/test_project_results.py` |

定向验收命令：

```powershell
$env:PYTHONPATH='src'
$env:PYTHONDONTWRITEBYTECODE='1'
D:\Myanaconda\python.exe -m pytest -q -p no:cacheprovider tests\test_project_api.py tests\test_project_audio.py tests\test_project_video.py tests\test_project_postprocess.py tests\test_project_variants.py tests\test_project_results.py tests\test_new_frontend.py
```

发布前仍需执行两个项目各自的完整测试集，不能只跑上述定向测试。

2026-08-05 自动化结果：剪映工作台完整测试 `219 passed`，数字人后端完整测试
`163 passed`。浏览器级成果库检查确认“删除选中”初始禁用，选中真实可用成果后启用且选择
摘要同步为 `已选 1 个`。

## 本地真实验收

1. 启动数字人后端 `http://127.0.0.1:8000`。
2. 启动剪映工作台 `http://127.0.0.1:8010/app/new`。
3. 使用同一测试账号登录，导入脚本和图片。
4. 生成、试听并下载一次真实音频。
5. 各执行一次单片段、多片段画面合成，并下载原始片段。
6. 上传本地视频，确认当前视频版本切换。
7. 生成变体，验证部分失败重试、成果库预览、ZIP 下载和删除选中。

真实 MiniMax、RunningHub 调用可能产生费用，必须先获得用户明确授权。真实剪映闭环要求目标
Windows 桌面会话可用。未获得这两项条件时，只能完成 mock 自动化验收，不能宣称真实联调已经执行。

## 完成边界与后续新增范围

截至 2026-08-05，模块 8 的自动化闭环、旧任务隔离和自动化回归已经完成；本页“本地真实
验收”仍待用户安排无人占用时段并明确授权。智能音乐匹配、关键词语义局部前景素材和多段
数字人人物跳变转场属于原计划完成后的新增功能与质量优化，不作为模块 8 自动化完成的前置
条件。智能音乐的当前规范见 `docs/MUSIC_MATCHING_V1.md`，语义前景图片的当前规范见
`D:\工作内容\轻盈健\数字人\语义前景图片功能开发文档.md`；多片段 0.25 秒保时长叠化的
现行边界以 `docs/DEVELOPER_GUIDE.md` 和 `docs/RENDER_JOB_SCHEMA.md` 为准。
