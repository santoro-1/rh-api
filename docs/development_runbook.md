# 媒体链路本地开发与测试

1. 使用隔离 Python 环境按 `requirements.txt` 安装依赖；不要指向生产数据库或生产 OSS。
2. 默认 `MEDIA_OSS_ENABLED=false`、`MEDIA_CLOUD_WORKER_ENABLED=false`，新路由返回 503。开发时用 Mock OSS 测试，不需要真实云凭据。隔离环境才配置独立 `MEDIA_CLOUD_WORKER_TOKEN`（至少 32 字符）、租约秒数与活动任务上限。
3. 执行 `python -m pytest -q tests/test_media_uploads.py tests/test_media_verification.py tests/test_production_config.py tests/test_migrations.py tests/test_h3_input_uploads.py`。2026-09-24 本地为 68 passed；先核对本地专项结果，再逐阶段联调，不能把 Mock 结果当云端验收。
4. 隔离环境将 Bucket 设为私有、启用网站 Origin 的 PUT/GET/HEAD CORS，并设置允许暴露 ETag；网站只用窄权限 OSS 凭据，FC 使用独立角色。必须真实验证 multipart、续传、分片页、超时和容量后，才能准备业务切换。

补充回归：`test_media_uploads.py` 与 H3 页面/工作台/Worker/直达相关集合为 52 passed。全量测试在跨项目 JYD 合同模块收集阶段因本次隔离环境缺 `fontTools` 等 JYD 依赖中断，不能声称全量通过。

FC 镜像中需包含 `oss2`、`requests`、FFprobe、FFmpeg，并以执行角色提供 `ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET/SECURITY_TOKEN`。单次调用 `python -m app.cloud_media_verifier`，从 `MEDIA_CLOUD_API_BASE` 的 HTTPS 网站领取一单；执行前在隔离环境确认这些变量实际由 FC 注入且角色仅能读取指定 Bucket。任务临时空间 `MEDIA_CLOUD_SCRATCH_LIMIT_MB` 默认为 1024 MiB，真实素材的耗时、空间与成本必须压测。当前没有 FC 镜像、触发器、真实 OSS 联调或自动按需唤醒。

尚未接通 H3/旧业务的 FC ASR/切片、RunningHub 文件上传、结果归档、客户端、旧接口封口和历史迁移。不能把此模块的单元测试解释为全站验收通过。
