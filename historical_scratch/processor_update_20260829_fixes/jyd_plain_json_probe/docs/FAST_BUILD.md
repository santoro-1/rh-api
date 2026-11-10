# 快速打包

## 第一次准备环境

只需要运行一次：

```powershell
cd "D:\工作内容\轻盈健\公寓\jyd_plain_json_probe"
.\scripts\setup_build_environment.ps1 -Python "D:\Myanaconda\python.exe"
```

这一步只用 Anaconda 创建项目专用的精简打包环境。以后执行打包命令时不要再传 `-Python`，否则 PyInstaller 会扫描整个 Anaconda，速度慢且发布包容易变大。

## 日常快速打包

当前“用户电脑 + 完整处理电脑”的部署只需要服务器包和采集器包：

```powershell
.\build_deployment.ps1
```

正式交付且本地工作台需要使用云端数字人账号与任务时，必须在完整构建中显式写入正式地址：

```powershell
.\build_deployment.ps1 `
  -DigitalHumanServerUrl "https://video.lanyingjk01.com" `
  -CompressionLevel Optimal
```

该参数只改变生成包内的 `data\processor_config.json`，不会把生产地址写入源码默认配置。
完整 Processor 包会复制 `data\libraries`，因此包含当前公共音乐、字体及其他素材库；
`-UpdateOnly` 是纯程序更新包，不包含任何 `data` 内容，也不能和
`-DigitalHumanServerUrl` 同时使用。

新电脑首次安装、但公共素材库另行交付时，使用首次安装无素材模式：

```powershell
.\build_deployment.ps1 `
  -WithoutLibraries `
  -DigitalHumanServerUrl "https://video.lanyingjk01.com" `
  -CompressionLevel Optimal
```

它仍包含首次启动需要的程序、启动说明、ASR 运行时、空数据目录和
`processor_config.json`，只排除 `data\libraries` 以及它的短路径映射 `data\l`。
输出的 Processor 包为：

```text
release\JianyingRenderServer-no-libraries-windows-x64.zip
```

该模式与 `-UpdateOnly` 不同：前者可用于新电脑首次安装，后者只能覆盖已有安装。

脚本默认保留 PyInstaller 分析缓存，并使用 `Fastest` ZIP 压缩。输出为：

```text
release\JianyingDraftCollector-windows-x64.zip
release\JianyingRenderServer-windows-x64.zip
```

只修改了其中一端时，可以单独构建：

```powershell
.\scripts\build\build_processor.ps1
.\scripts\build\build_collector.ps1
```

## 什么时候完整重建

升级 Python、PyInstaller、依赖，或者增量包出现无法解释的问题时才使用：

```powershell
.\build_deployment.ps1 -Clean
```

## 已部署电脑的快速更新包

处理电脑已经有完整服务器目录，并且只是修改代码或网页时，不需要再次压缩素材库：

```powershell
.\scripts\build\build_processor.ps1 -UpdateOnly
```

输出：

```text
release\JianyingRenderServer-update-windows-x64.zip
```

先关闭处理服务器，把更新包解压到原来的 `JianyingRenderServer` 目录并覆盖同名文件。更新包
不含 `data` 目录，不会复制、删除或覆盖素材、模板、任务、登录数据和配置。

如果服务器和采集器代码都修改了：

```powershell
.\build_deployment.ps1 -UpdateOnly
```

需要体积更小、但压缩更慢的最终归档时使用：

```powershell
.\build_deployment.ps1 -CompressionLevel Optimal
```

不要删除项目同级的 `.jyd-build-cache`；它保存专用环境和增量构建缓存。`build`、`dist` 和项目里的临时目录不是这套正式缓存。

## Windows 解压路径

完整服务器包包含剪映复合文字模板资源。新版发布包已缩短其内部素材目录，但仍建议把服务器解压到短目录，例如：

```text
D:\JYD
F:\JYD
```

如果 Windows 资源管理器提示 `0x80010135：路径太长`，不是压缩包损坏；取消当前操作并改到上述短目录重新解压即可。
