# 媒体资产数据与接口

`0053_media_assets` 新增 `media_assets`：`id/user_id` 标识归属，`source_channel + upload_key` 在用户内唯一；记录类型、用途、原始名称、预期大小与 SHA-256、OSS object key、multipart upload ID、分片大小、状态和过期时间。旧 `staged_assets` 原样保留。`MediaAsset.object_key` 绝不能送进 `safe_relative_path()`。

`0054_media_verification_jobs` 增加每素材唯一的 `media_verification_jobs`，保存任务编号、状态、租约、尝试次数、错误码和已接受回执摘要。相同租约的相同回执可安全重发；不同回执不能覆盖。状态：`UPLOADING -> UPLOADED -> VERIFYING -> READY`，失败可到 `REJECTED`。`READY` 仅代表离线核验通过；尚未接入任何付费预检。

网页（Cookie + `X-CSRF-Token`）：

- `POST /api/media-assets/uploads`：JSON `{kind,source_channel,asset_role,upload_key,filename,size_bytes,source_sha256}`；返回 `{asset_id,status,part_size,part_count,...}`。
- `GET /api/media-assets/uploads/{asset_id}`：返回状态及已上传分片号。
- `POST /api/media-assets/uploads/{asset_id}/parts`：JSON `{start,count}`，最多一次签发 32 个待上传分片的 10 分钟 PUT URL。
- `POST /api/media-assets/uploads/{asset_id}/complete`：网站从 OSS `ListParts` 核验编号、尺寸并合并，`HEAD` 核实总大小；置 `UPLOADED` 且同一数据库事务创建核验任务。

工作台以 `/api/workbench/media-assets/uploads...` 使用同一协议，状态请求是 `POST .../{asset_id}/status`，每次 JSON 可带 `access_token`，并沿用设备业务准入。网页来源仅可为 `h3_web/web_legacy`；工作台来源仅可为 `h3_workbench/workbench_legacy/ltx_workbench`。H3 来源须有 H3 权限。

云端核验端点（独立 `MEDIA_CLOUD_WORKER_TOKEN`，默认关闭）：`POST /api/cloud-media/jobs/claim` 返回一条任务或 `null`；`POST /api/cloud-media/jobs/{job_id}/heartbeat` 传 `lease_id` 续租；`POST /api/cloud-media/jobs/{job_id}/complete` 传 `lease_id/outcome`，成功时加 `observed_size/observed_sha256/detected_kind`，失败时加受限 `error_code`。网站再次比较声明的大小、摘要和类型。最多重试 3 次，并限制活动租约数；过期租约的旧回执不能覆盖新结果。这里没有上传或下载文件正文的接口。

所有 URL 都按指定对象/分片签发，浏览器收到的只有短期签名，没有 AK/SK。OSS 分片上传是原始 PUT，不是 `FormData`。完成接口不接受客户端自报的 ETag、大小或摘要作为可信事实。云端核验实现已落代码，但尚未在真实 OSS/FC 上验证。
