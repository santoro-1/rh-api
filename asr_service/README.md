# 本地 FunASR 服务

该服务与主站 Python 环境隔离，只监听 `127.0.0.1:18084`。主站通过
`funasr_http` Provider 获取字词时间戳，再将识别结果与原脚本对齐。

## 本地安装

在项目根目录执行：

```powershell
python -m venv .asr-runtime\venv
.\.asr-runtime\venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
.\.asr-runtime\venv\Scripts\python.exe -m pip install -r asr_service\requirements.txt -i https://mirror.sjtu.edu.cn/pypi/web/simple
```

CPU 版先用于验证准确度，也对应无 GPU 服务器的运行方式。需要改用本机
RTX 5060 时，按照 PyTorch 官方命令将隔离环境中的 `torch`、`torchaudio`
替换为匹配显卡驱动的 CUDA wheel，并设置 `ASR_DEVICE=cuda`。

## 启动

双击项目根目录的 `启动ASR服务.cmd`。首次提交识别请求时会下载
`paraformer-zh` 和 `fsmn-vad` 模型。模型缓存位于
`.asr-runtime\models`。

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18084/healthz
```

服务启动并完成首次模型下载后，另开 PowerShell，在项目根目录运行真实项目
只读基准：

```powershell
python -m scripts.benchmark_long_audio_asr
```

脚本默认读取数据库中最新的长音频项目，输出 ASR 耗时、常驻内存、GPU 峰值
以及每个切点前后的原脚本。它不会保存方案或创建视频任务。

本地开发可以不配置令牌。服务器部署时必须同时为 ASR 服务和主站配置独立的
`ASR_SHARED_TOKEN`，并继续只监听环回地址。
