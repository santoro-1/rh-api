# 媒体链路排错

| 现象或错误码 | 含义与处理 |
| --- | --- |
| `MEDIA_OSS_DISABLED` / 503 | 功能默认关闭。不要为了试上传就切生产；先配置隔离 Bucket 与 FC。 |
| `MEDIA_KEY_CONFLICT` / 409 | 相同上传键绑定了不同声明；客户端应换真正新文件的键，不能覆盖原对象。 |
| `MEDIA_PARTS_INCOMPLETE` / 409 | 用状态接口查已传分片，只续传缺失部分。 |
| `MEDIA_PARTS_INVALID` / 422 | 分片编号或字节数不符，检查客户端 `Blob.slice` 起止位置。 |
| `MEDIA_OBJECT_SIZE_MISMATCH` / 422 | OSS 合并后大小不等于预声明，停止后续处理并调查。 |
| OSS 签名失败 | 检查 Bucket、杭州 Region、公网 Endpoint、V4 凭据及 CORS；绝不打印签名 URL 或 AK/SK。 |
| 上传完成却不能生成 | `UPLOADED` 需由核验任务变为 `READY`；即使 `READY`，业务消费者尚未接通，仍不能提交付费任务。 |
| 核验任务停在 `PENDING` | 检查独立 Worker 开关、令牌、FC 触发器和活动租约上限。当前没有部署 FC 触发器，不要把队列积压当作已上线故障。 |
| 核验任务变为 `REJECTED` | 查看任务 `error_code`；大小、摘要或格式不符须重传新素材。暂时错误重复 3 次会拒绝，须查 OSS 权限、临时空间、FFmpeg 能力后在隔离环境处理，不得回退网站搬文件。 |

诊断时先读 `../../HANDOFF.md` 和 `../PROJECT_MAP.md`；生产切换、旧通道封口和历史迁移均未获本阶段验收。
