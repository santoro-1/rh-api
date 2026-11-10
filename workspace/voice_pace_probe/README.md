# 克隆音色自然语速测试工具

这是一个与数字人网站、剪映工作台完全分离的临时测试工具。它上传参考音频，调用音色
克隆接口并获取 `demo_audio`，再统计试听文案的中文汉字数和音频时长，判断保持自然语速
时，能否只靠增加停顿接近 220 字/分钟。

工具不会调用正式 T2A，也不会自动激活音色。

## 环境要求

- Python 3.11+
- `requests`
- `ffprobe` 在 `PATH` 中可用

当前机器可以直接使用 Anaconda Python：

```powershell
D:\Myanaconda\python.exe -m pip install -r requirements.txt
```

## 最简单的运行方式

在 Cursor 中打开 `run_probe.py`，直接点击运行，按提示输入参考音频路径即可。

也可以在 PowerShell 中运行：

```powershell
cd "D:\工作内容\轻盈健\数字人\voice_pace_probe"
$env:MINIMAX_API_KEY="你的 MiniMax API Key"
D:\Myanaconda\python.exe run_probe.py
```

API Key 只从环境变量或隐藏输入框读取，不会写入输出文件。

## 带参数运行

克隆试听模式：

```powershell
D:\Myanaconda\python.exe run_probe.py clone `
  --audio "D:\素材\参考声音.mp3" `
  --model speech-2.8-hd `
  --target-cpm 220
```

如果已经有克隆接口返回的试听音频，可以完全离线重新分析：

```powershell
D:\Myanaconda\python.exe run_probe.py analyze `
  --audio ".\outputs\某次测试\clone-preview.mp3" `
  --text-file ".\calibration_text.txt" `
  --target-cpm 220
```

## 输出内容

每次测试会建立独立目录，保存：

- `clone-preview.mp3`：克隆接口返回的原始试听；
- `calibration-text.txt`：本次实际使用的测试文案；
- `pace-report.json`：字数、时长、自然字速、停顿预算和判断结果。

报告中的“推荐语速”只是以后正式使用时的粗略参考。本工具不会自动变速，也不会为了
验证参数调用正式 T2A。
