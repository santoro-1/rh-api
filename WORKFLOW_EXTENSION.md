# 工作流扩展说明

当前 `digital_human` 是第一个适配器。通用层不保存任何 RunningHub 节点 ID，也不假设输入一定是图片和音频。

## 分层

- `app/services/runninghub.py`：通用 HTTP 客户端，只负责上传文件、提交、查询和下载。
- `app/models.py`、`app/routes/tasks.py`、`app/workers/task_worker.py`：本地用户、权限、任务、文件和状态系统。
- `app/workflows/`：每个工作流自己的输入校验、默认值、节点映射、请求体和输出选择。

`generation_tasks.input_payload` 保存适配器序列化后的通用素材和参数；`workflow_type` 指向适配器键。`workflow_configs` 保存每个用户、每个工作流自己的 AI App ID、实例类型、默认提示词与可选 JSON 设置。RunningHub API Key 仍只保存在账户级 `runninghub_configs` 中。

## 新增一个工作流

以“图生视频”为例：

1. 新建 `app/workflows/image_to_video.py`，实现 `WorkflowAdapter` 的六个方法。
2. 在适配器内定义该工作流的节点 ID、字段名、默认参数、所需素材类型以及 `select_output` 规则。
3. 在 `app/workflows/registry.py` 注册唯一键，例如 `image_to_video`。
4. 为其增加创建页面/API 表单；表单保存的文件转换为 `WorkflowAsset`，再调用适配器的 `validate_parameters` 和 `serialize_input`。
5. 管理员为用户保存该工作流的 `WorkflowConfig`。Worker 无需增加节点、素材字段或轮询逻辑。

不要在 `task_worker.py`、`RunningHubClient` 或通用路由中写 AI App ID、节点 ID、`nodeInfoList` 细节或工作流专属输出解析。
