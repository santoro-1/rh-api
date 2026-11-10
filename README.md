# 轻盈健数字人项目阶段归档（2026-11-10）

本分支用于保存原工作区中未进入独立仓库的代码、文档和历史补丁。用户已将本阶段告一段落；不表示功能全部完成或已经生产验收。

## 当前代码与归档位置

| 内容 | 仓库与分支 |
| --- | --- |
| 数字人网站及 H3/OSS 最新开发改动 | [rh-api/main](https://github.com/santoro-1/rh-api/tree/main)，代码收尾提交 `219cfae` |
| 剪映工作台最新开发改动 | [jianji-auto/main](https://github.com/santoro-1/jianji-auto/tree/main)，`ec255c7` |
| LTX 历史工作台及更新工具 | [历史仓库/main](https://github.com/santoro-1/santoro-1-ltx-lip-sync-workbench/tree/main)，`807ac9f` |
| 工作台静态前端原型及原始 Git 历史 | [jianji-auto/archive/video-gongyu-20261110](https://github.com/santoro-1/jianji-auto/tree/archive/video-gongyu-20261110)，`05d889f` |
| 独立 H3 工作台、根级开发文档与辅助代码 | 本分支 `workspace/` |
| 历史临时脚本、补丁与 2026-08-29 处理机源码副本 | 本分支 `historical_scratch/`，仅追溯，不覆盖 main |
| 文件路径、SHA-256 与大小清单 | `MANIFEST.json` |
| 收尾前未提交文件与素材删除明细 | `git_audit_before_closeout.txt` |
| 完整历史交接基线 | `workspace/HANDOFF.md` |

## 四份原始 stash

都保存在 rh-api 的独立分支，原始 stash 提交及父提交完整保留，包括第三父提交中的未跟踪文件。普通分支浏览默认看不到第三父提交中的文件，恢复时需要按 stash 还原。

| 分支 | 原始用途 | 原提交 | 文件数（含未跟踪） |
| --- | --- | --- | --- |
| `archive/stash-pre-sync-20261110` | H3 同步前开发记录 | `cefd0ca` | 30 |
| `archive/stash-local-scripts-20261110` | 本地脚本 | `6cafcca` | 18 |
| `archive/stash-semantic-docs-20261110` | 语义素材库文档 | `efa2990` | 6 |
| `archive/stash-media-worker-20261110` | 远程媒体 Worker | `e302d03` | 2 |

恢复示例（先在干净的临时分支检查，避免覆盖新代码）：

```powershell
git fetch origin
git stash store -m "恢复历史本地脚本" origin/archive/stash-local-scripts-20261110
git stash show --include-untracked --stat 'stash@{0}'
# 确认需要后，再在合适的历史基线临时分支运行 git stash apply。
```

## 恢复原工作区结构

1. 克隆 `rh-api` 到 `数字人/runninghub_mvp`。
2. 克隆 `jianji-auto` 到相邻 `公寓` 目录。
3. 克隆 LTX 历史仓库到 `数字人/ltx_lip_sync_workbench`。
4. 单独克隆本分支，将 `workspace/` 的内容按原相对路径放回 `数字人/`，其中包含完整独立 H3 工作台源码。
5. 先读 `workspace/HANDOFF.md`、网站 `AGENTS.md` 与 `PROJECT_MAP.md`；需要历史补丁时再看 `historical_scratch/`。
6. 重新安装依赖并私下配置运行环境；不要把历史副本直接覆盖当前 main。

## 验证与范围

- [verified] 三个代码收尾提交、四份 stash 与前端原型已得到 GitHub 推送成功回执。
- [verified] 网站以下专项测试：`68 passed in 88.02s`。

```powershell
python -m pytest -q tests/test_media_uploads.py tests/test_media_verification.py tests/test_production_config.py tests/test_migrations.py tests/test_h3_input_uploads.py
```

- [verified] 307 个待备份 Python 文件通过语法检查；归档副本使用 SHA-256 与原文件核对。
- [verified] 扫描未发现真实私钥、GitHub/OpenAI/阿里云密钥；命中项为测试用占位内容。此项不代表对整个历史仓库的完整安全审计。
- [verified] 没有改动业务代码、部署生产、发起真实付费生成或修改云端账号数据库。
- [verified] 3480 个本地素材和模板删除未进入代码提交，远端继续保留原有文件与历史。
- [unknown] OSS/FC 联调、完整媒体迁移与其他历史模块的当前端到端功能状态未在本轮验收。
- 本次归档范围为数字人网站、H3/LTX 工作台、剪映工作台及相关代码/文档。轻盈健下其他独立业务目录不在本次处理范围。
- `.env`、真实账号数据库、媒体素材、生成结果、依赖环境、大型二进制安装包和缓存未新上传。删除本地后，这些未另行备份的本地内容无法由本次 Git 归档恢复。

补收检查：归档清单共 1589 个源文件条目（约 33.8 MB），包含各历史临时目录中的源码、文档及补丁；不包含测试运行数据和依赖目录。所有清单项已与原文件逐字节校验。

## 十项收尾核对

| 检查输入 / 场景 | 预期结果 | 实际结果 |
| --- | --- | --- |
| 网站 main 对比 GitHub | 提交一致 | [verified] `46442d3` 一致 |
| 剪映工作台 main 对比 GitHub | 提交一致 | [verified] `ec255c7` 一致 |
| LTX 历史仓库 main 对比 GitHub | 提交一致 | [verified] `807ac9f` 一致 |
| 仓库外源码和文档 | 全部清单项已上传 | [verified] 1589 个源文件条目已归档 |
| 从 GitHub 重新读取归档文件 | SHA-256 与原文件一致 | [verified] 全部清单项通过 |
| 四份 stash 的远端提交 | 与本地原始 stash 一致 | [verified] 四份全部一致 |
| stash 中未跟踪文件 | 第三父提交保留原文件树 | [verified] 6/18/6/0 个全部保留 |
| 无远端的前端原型 | 原始提交进入归档分支 | [verified] `05d889f` 一致 |
| 本地素材清理产生的删除 | 不误删远端历史素材 | [verified] 3480 个删除均未提交 |
| 网站专项测试 | Mock/离线回归通过 | [verified] 68 passed；不代表生产验收 |

`REMOTE_VERIFICATION.json` 保存独立远端读取的校验结果快照；随后仅同步本收尾文档、交接基线及校验清单。网站与 LTX 当前可见代码/文档状态已干净，工作台仅剩上述本地素材删除。
