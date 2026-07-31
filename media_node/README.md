# 固定电脑媒体节点

这个目录集中放置长音频所需的本地能力：

- FunASR 字词时间戳服务；
- FFmpeg 静音检测、切音频和切视频；
- 从主站主动领取任务、续租和回传结果的 Worker。

媒体节点只主动访问主站 HTTPS，不监听公网地址，不需要路由器端口映射，也不需要
在 Windows 防火墙开放入站端口。ASR 只监听 `127.0.0.1`。

## 推荐：生成独立便携包

在已经成功运行媒体节点的开发电脑上，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\media_node\build-portable-media-node.ps1
```

脚本会复用当前已经验证过的媒体节点 Python 环境，并把 Python、依赖、FFmpeg、程序
代码和现有 ASR 模型缓存打进 `dist\runninghub-media-node-windows-x64-*.zip`。目标电脑
不需要安装 Python、FFmpeg，也不需要完整仓库；解压后先双击“配置媒体节点.cmd”，
再双击“启动媒体节点.cmd”即可。

默认不会把真实 `.env` 和服务器令牌放进 ZIP。如确定只通过可信方式传给固定电脑，
可以显式携带当前配置：

```powershell
powershell -ExecutionPolicy Bypass -File .\media_node\build-portable-media-node.ps1 `
  -IncludeLocalConfig
```

即便携带当前配置，打包器也会把 `MEDIA_WORKER_ID` 清空，让新电脑自动使用自己的
电脑名，避免和原节点重名。

模型缓存约 1GB。希望减小压缩包、允许新电脑首次使用时联网下载模型，可加
`-WithoutModels`。希望打包后保留未压缩目录用于本机验证，可加 `-KeepExpanded`。

## 多节点如何分配任务

媒体节点每隔 `MEDIA_WORKER_POLL_SECONDS`（默认 10 秒）主动向服务器领取任务。服务
器按创建时间选择最早的待处理任务，并通过数据库原子更新保证一个任务只能由一个
节点领取。多台电脑同时运行时没有固定主从关系，也不会按硬件性能选择：哪台空闲
电脑先轮询成功，哪台就获得该任务。每个节点内部串行处理一个任务，因此两台节点
最多并行两个媒体任务。

`MEDIA_WORKER_ID` 留空时自动使用“电脑名-media”，便携包可以直接在多台电脑上区分
日志。如果节点意外断开，任务租约到期后可以被其他节点重新领取。

## 第一次安装

以下方式主要用于开发或重新制作便携包。固定电脑日常使用优先选择上面的独立包。

1. 在固定电脑拉取完整仓库。媒体节点会复用仓库中的切分与脚本对齐算法，但不启动
   Web、数据库、语音 Worker 或视频生成 Worker。
2. 安装 FFmpeg，并确认 PowerShell 中的 `ffmpeg -version` 和
   `ffprobe -version` 都能执行。
3. 在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\media_node\install-media-node.ps1
```

默认安装 CPU 版 PyTorch。需要指定镜像时：

```powershell
powershell -ExecutionPolicy Bypass -File .\media_node\install-media-node.ps1 `
  -PypiIndexUrl https://mirror.sjtu.edu.cn/pypi/web/simple
```

4. 编辑 `media_node\.env`：
   - `MEDIA_WORKER_SERVER_URL` 填主站地址；
   - `MEDIA_WORKER_TOKEN` 与服务器 `.env` 完全一致；
   - `MEDIA_WORKER_ID` 改成这台固定电脑的唯一名称；
   - 默认 `ASR_DEVICE=cpu`，不会占用其他 GPU ASR 的显存。
5. 双击 `启动媒体节点.cmd`。它会自动启动或复用兼容的本机 ASR，然后串行处理
   ASR、切音频和切视频任务。独立的“启动 ASR 服务”脚本不再需要。

第一次执行对口型分析时才会加载或下载模型，缓存位于
`media_node\.runtime\models`。数字人图生视频只做静音检测，不会触发模型加载。

## 从笔记本迁移到固定电脑

1. 先关闭笔记本上的媒体节点窗口，避免两台电脑使用同一 Worker ID。
2. 在固定电脑拉取相同版本代码并执行一次安装脚本。
3. 将旧 `.env.worker` 中的服务器地址和令牌复制到新电脑
   `media_node\.env`。`MEDIA_WORKER_ID` 留空即可自动使用新电脑名称，也可手动设置。
4. 可选：复制旧 `.asr-runtime\models` 或 `media_node\.runtime\models`
   到新电脑的 `media_node\.runtime\models`，可避免重新下载模型。
5. 双击新电脑的 `media_node\启动媒体节点.cmd`。主站无需改域名、Nginx或开放端口。

同一时刻只建议运行一个正式媒体节点。任务租约可以处理意外断线，但同时运行多个
节点会让任务在不同电脑之间分配，不利于观察资源占用。

## 与电脑现有 ASR / FFmpeg 共存

- FFmpeg 是命令行进程，不是常驻“服务”。多个程序可以同时调用，但会竞争 CPU、
  磁盘和显存；本节点内部保持单任务串行。
- 如果 `127.0.0.1:18084/healthz` 已经是兼容服务，启动器会直接复用。
- 如果 18084 被其他不兼容服务占用，请在 `.env` 同时修改 `ASR_BASE_URL`、
  `ASR_PORT`，例如改为 18085。
- 固定电脑已有高精度 GPU ASR 时，建议本节点保持 `ASR_DEVICE=cpu`；确认资源余量后
  再单独评估 CUDA 环境，不要共用另一个程序的 Python 虚拟环境。
