请基于我提供的 RunningHub API 文档和已经成功运行的 Python 测试代码，实现一个可以部署到公网服务器的数字人视频生成中转站。

不要只给我架构建议、伪代码或零散示例。请直接检查现有项目文件，设计并实现一个完整可运行的 MVP，运行测试并修复发现的问题。

# 一、项目背景

我已经使用 Python 测试代码成功验证了 RunningHub API 的完整调用流程：

1. 从本地路径读取图片。
2. 从本地路径读取音频。
3. 调用 RunningHub 文件上传接口。
4. 获取上传结果中的 `data.fileName`。
5. 将图片和音频的 `fileName` 写入工作流对应节点。
6. 调用 RunningHub AI 应用接口提交任务。
7. 获取 RunningHub 返回的 `taskId`。
8. 定时查询任务状态。
9. 任务成功后获取生成结果 URL。
10. 下载生成的视频。

当前测试已经成功，因此请优先复用和重构现有测试代码，不要重新猜测 RunningHub API 的调用方式。

需要特别注意：

* 24G 的 `default` 实例生成速度较慢。
* 我平时实际使用的是 48G 实例。
* 正式系统默认必须使用：

```text
instanceType = plus
```

# 二、项目定位

这个项目本质上是一个 RunningHub API 中转站。

用户只需要在网页中：

1. 登录账号。
2. 上传一张参考图片。
3. 上传一个音频文件。
4. 查看系统自动识别的音频时长。
5. 根据需要修改开始时间和结束时间。
6. 查看并修改默认提示词。
7. 提交视频生成任务。
8. 查看任务进度。
9. 生成完成后在线播放或下载视频。

所有 RunningHub 调用必须由后端完成。

前端不能直接访问 RunningHub，也不能获得 RunningHub API Key。

整体流程：

```text
用户浏览器
    ↓
我的前端页面
    ↓
我的后端服务
    ↓
RunningHub API
    ↓
我的后端保存结果
    ↓
用户预览或下载视频
```

# 三、部署目标

整个服务第一版可以全部部署在一台公网 Linux 服务器上，包括：

* 前端页面
* 后端 API
* 用户账号系统
* 数据库
* 后台任务 Worker
* 上传文件
* 生成结果文件
* Nginx
* HTTPS
* 域名访问

服务器和域名不是问题，请重点完成应用本身。

第一版优先保证：

* 简单
* 稳定
* 可维护
* 可直接部署
* 后续方便扩展

不要一开始引入没有必要的微服务和复杂基础设施。

# 四、账号和 RunningHub 配置

系统需要有网站账号。

网站账号由管理员创建，不需要开放用户自行注册。

每个网站账号都可以配置对应的 RunningHub 信息。

至少需要支持以下配置：

* RunningHub API Key
* RunningHub API Base URL
* RunningHub AI App ID
* RunningHub 实例类型
* 默认提示词
* 最大同时运行任务数量
* 账号是否启用

每个 RunningHub 账号本身只有一个 API Key，因此系统可以采用：

```text
一个网站账号对应一套 RunningHub 配置
```

用户登录后，后端根据当前用户查找对应的 RunningHub API Key，再调用 RunningHub。

普通用户不能在前端修改：

* RunningHub API Key
* RunningHub API 地址
* AI App ID
* 工作流节点 ID
* 实例类型
* 任意远程 URL

这些由管理员在后台配置。

API Key 不能写入前端 JavaScript，不能通过接口返回给浏览器，也不能出现在普通日志中。

数据库中的 API Key不能直接以明文展示。请采用适合服务端解密使用的加密方式保存。

# 五、管理员后台

管理员至少可以：

1. 创建网站用户。
2. 修改用户名和密码。
3. 启用或禁用用户。
4. 配置用户对应的 RunningHub API Key。
5. 配置用户对应的 RunningHub AI App ID。
6. 配置 RunningHub Base URL。
7. 配置实例类型，默认值为 `plus`。
8. 配置用户默认提示词。
9. 配置用户允许同时生成的任务数量。
10. 查看所有用户的任务记录。

第一版不需要复杂的角色权限系统，只需要：

* 管理员
* 普通用户

# 六、用户登录

实现基础账号登录功能。

要求：

* 密码必须安全哈希保存。
* 未登录用户不能进入生成页面。
* 未登录用户不能查看任务。
* 用户只能查看自己的任务。
* 用户只能下载自己的生成结果。
* 管理员可以查看全部任务。
* 登录状态应使用安全的 Session 或 HttpOnly Cookie。

# 七、视频生成页面

用户登录后进入视频生成页面。

页面至少包含：

## 1. 参考图片

用户上传一张本地图片。

支持的常用格式：

* JPG
* JPEG
* PNG
* WEBP

页面显示图片预览。

## 2. 参考音频

用户上传一个本地音频文件。

支持的常用格式：

* MP3
* WAV
* M4A
* AAC
* FLAC

页面显示：

* 音频文件名
* 检测到的音频总时长

## 3. 开始时间和结束时间

音频上传后，系统自动读取音频长度。

默认填写：

```text
开始时间：0:00
结束时间：音频总时长
```

例如音频长度是 15 秒，自动填写：

```text
开始时间：0:00
结束时间：0:15
```

例如音频长度是 75 秒，自动填写：

```text
开始时间：0:00
结束时间：1:15
```

用户可以手动修改开始和结束时间。

后端需要校验：

* 开始时间不能小于 0。
* 结束时间必须大于开始时间。
* 结束时间不能超过音频实际时长。
* 输入格式必须正确。

音频长度可以通过服务器上的 `ffprobe` 或其他稳定方式读取。

需要正确处理：

* 音频损坏
* 格式不支持
* 无法读取时长
* 用户输入时间超出音频长度

## 4. 提示词

页面提供提示词文本框。

提示词从当前账号配置的默认提示词中自动填入，例如：

```text
人物自然地说话，表情自然，动作自然，镜头保持稳定。
```

用户可以根据不同视频自由修改。

提交任务时使用用户最终填写的提示词。

提示词不需要做得很复杂，第一版只需要：

* 有默认值
* 可以修改
* 可以在管理员后台修改账号默认值

## 5. 提交按钮

用户点击“开始生成”后，创建一个后台任务。

页面不应该一直等待 RunningHub 完成才返回。

提交成功后立即显示任务已经创建，并跳转或进入任务状态页面。

# 八、RunningHub 当前工作流配置

当前 RunningHub AI App ID：

```text
2062251097452007426
```

当前已验证的节点配置：

```text
总体模式：
nodeId = 752
fieldName = select
可选 fieldValue = 0（灵动模式）、1（稳定模式 v1）、2（稳定模式 v2）
默认 fieldValue = 2

单双人模式：
nodeId = 753
fieldName = select
可选 fieldValue = 0（双人模式）、1（单人模式）
默认 fieldValue = 1

最长分辨率：
nodeId = 503
fieldName = value
默认 fieldValue = 1024
由用户填写正整数，不设置业务上限

参考图片：
nodeId = 240
fieldName = image

总参考音频：
nodeId = 339
fieldName = audio

音频开始时间：
nodeId = 341
fieldName = start_time

音频结束时间：
nodeId = 341
fieldName = end_time

左边人物音频：
nodeId = 739
fieldName = audio
默认 fieldValue = "None"

右边人物音频：
nodeId = 738
fieldName = audio
默认 fieldValue = "None"

提示词：
nodeId = 422
fieldName = text
```

任务配置默认使用：

```text
instanceType = plus
usePersonalQueue = false
```

不要默认使用 `default`。

第一版不要传：

```text
retainSeconds
```

除非以后明确配置了 RunningHub 支持的合法值。

节点配置应集中管理，不要将节点 ID 分散硬编码在不同业务文件中。

# 九、RunningHub 文件上传

用户上传到我方服务器的图片和音频，必须先由后端上传到 RunningHub。

上传接口返回后，工作流节点中必须填写：

```text
data.fileName
```

正确示例：

```text
openapi/xxxxxxxx.png
openapi/xxxxxxxx.mp3
```

不能填写：

* 用户电脑本地路径
* 我方服务器本地路径
* `download_url`
* 用户输入的外部 URL

将当前成功测试代码重构为独立的 `RunningHubClient`。

建议至少包含：

```text
upload_file
submit_task
query_task
download_result
```

需要处理：

* HTTP 超时
* 非 200 响应
* JSON 解析失败
* 返回字段缺失
* API Key 无效
* 上传失败
* 任务创建失败
* RunningHub 返回 FAILED
* RunningHub 返回未知状态
* `usage` 为 null
* 结果为空
* 文件下载失败

# 十、后台任务系统

视频生成通常需要较长时间，因此不能在一次 HTTP 请求中完成整个流程。

需要实现独立后台 Worker。

流程应为：

```text
用户提交生成请求
    ↓
Web 服务保存本地任务
    ↓
立即向前端返回本地任务 ID
    ↓
Worker 读取待执行任务
    ↓
上传图片和音频到 RunningHub
    ↓
提交 RunningHub 任务
    ↓
保存 RunningHub taskId
    ↓
定时查询 RunningHub
    ↓
成功后下载结果
    ↓
保存到服务器
    ↓
更新本地任务状态
```

不要只使用进程内的临时后台任务，因为服务重启后任务不能丢失。

MVP 可以使用数据库任务队列：

* Web 进程负责创建任务。
* Worker 进程负责领取任务。
* 使用数据库状态避免同一个任务被重复执行。
* Web 和 Worker 作为两个独立进程运行。

本地任务 ID 和 RunningHub taskId 必须分开保存。

# 十一、任务状态

本地任务至少包含这些状态：

```text
PENDING
UPLOADING
SUBMITTED
RUNNING
SUCCESS
FAILED
```

可以根据需要增加：

```text
CANCELLED
DOWNLOAD_FAILED
```

任务记录至少保存：

* 本地任务 ID
* 用户 ID
* RunningHub taskId
* 图片文件路径
* 音频文件路径
* 图片原始文件名
* 音频原始文件名
* 音频总时长
* 开始时间
* 结束时间
* 提示词
* 当前状态
* RunningHub 错误码
* 错误信息
* RunningHub usage
* 生成结果路径
* 创建时间
* 更新时间
* 完成时间

Worker 重启以后：

* 已经有 RunningHub taskId 的任务应继续查询。
* 不要重新提交同一个 RunningHub 任务。
* 避免重复生成和重复扣费。

# 十二、任务历史页面

用户可以查看自己的历史任务。

列表至少显示：

* 创建时间
* 图片缩略图
* 音频文件名
* 开始时间
* 结束时间
* 提示词
* 当前状态
* 生成耗时
* 错误信息
* 视频预览入口
* 下载按钮

用户只能查看自己的任务。

管理员可以查看所有用户的任务。

任务运行过程中，前端每隔约 5 秒查询一次我方后端状态。

浏览器不能直接查询 RunningHub API。

# 十三、生成结果保存

RunningHub 生成结果 URL 有有效期，因此任务成功后，Worker 必须立即把视频下载到我方服务器。

推荐目录结构：

```text
data/
  uploads/
    用户ID/
      本地任务ID/
        image.xxx
        audio.xxx

  outputs/
    用户ID/
      本地任务ID/
        result.mp4
```

用户播放和下载的都是我方服务器上的文件。

前端不能长期依赖 RunningHub 临时 URL。

下载接口必须验证：

* 用户已经登录。
* 任务属于当前用户，或者当前用户是管理员。
* 任务已经成功。
* 文件真实存在。
* 不允许路径穿越。
* 不允许通过修改 URL 访问其他用户文件。

# 十四、文件校验

上传时至少检查：

* 文件是否为空
* 文件扩展名
* MIME 类型
* 实际文件内容
* 文件大小

文件大小上限应通过环境变量配置，例如：

```text
MAX_IMAGE_SIZE_MB=20
MAX_AUDIO_SIZE_MB=100
```

服务器保存文件时使用随机文件名或任务目录，不直接使用用户文件名作为真实磁盘路径。

# 十五、文件清理

实现一个可以定期运行的文件清理功能。

默认可以设置为：

```text
上传的原始图片和音频保留 3 天
生成视频保留 7 天
```

具体天数通过环境变量配置。

不能删除：

* 正在等待中的任务文件
* 正在上传中的任务文件
* 正在生成中的任务文件

# 十六、推荐技术方案

优先使用简单、容易部署和维护的技术栈。

建议：

```text
Python 3.12
FastAPI
SQLAlchemy
Alembic
SQLite
Jinja2
原生 JavaScript 或 HTMX
FFmpeg / ffprobe
pytest
Nginx
systemd 或 Docker Compose
```

第一版用户量不大，可以使用 SQLite，但应启用合理配置，例如 WAL 模式。

第一版不强制使用 React。

优先使用服务端模板或简单前端，避免引入没有必要的前端构建复杂度。

建议代码结构清晰，例如：

```text
app/
  main.py
  config.py
  database.py

  models/
    user.py
    task.py

  routes/
    auth.py
    admin.py
    tasks.py

  services/
    runninghub.py
    audio.py
    encryption.py
    storage.py

  workers/
    task_worker.py

  templates/
  static/

scripts/
  create_admin.py
  cleanup_files.py

tests/
```

可以根据实际情况调整，但不要把所有代码写在一个文件里。

# 十七、主要页面

第一版至少需要：

## 登录页

```text
/login
```

## 视频生成页

```text
/generate
```

包含：

* 图片上传
* 图片预览
* 音频上传
* 音频时长
* 开始时间
* 结束时间
* 默认提示词
* 开始生成按钮
* 错误提示

## 任务列表页

```text
/tasks
```

## 任务详情页

```text
/tasks/{task_id}
```

包含：

* 当前任务状态
* 创建时间
* 已运行时间
* 输入参数
* 错误信息
* 视频播放器
* 下载按钮

## 管理员用户管理页

```text
/admin/users
```

支持：

* 创建用户
* 修改用户
* 禁用用户
* 重置密码
* 配置 RunningHub 信息
* 配置默认提示词

# 十八、建议的后端接口

至少实现：

```text
GET  /login
POST /login
POST /logout

GET  /generate

POST /api/audio/inspect
POST /api/tasks
GET  /api/tasks/{task_id}
GET  /api/tasks/{task_id}/download

GET  /tasks
GET  /tasks/{task_id}

GET  /admin/users
GET  /admin/users/new
POST /admin/users
GET  /admin/users/{user_id}
POST /admin/users/{user_id}
```

音频分析接口返回类似：

```json
{
  "durationSeconds": 15.7,
  "durationText": "0:15",
  "suggestedStartTime": "0:00",
  "suggestedEndTime": "0:15"
}
```

创建任务接口使用 `multipart/form-data` 接收：

```text
image
audio
startTime
endTime
prompt
```

创建任务后立即返回：

```json
{
  "taskId": "本地任务ID",
  "status": "PENDING"
}
```

不能在创建任务的 HTTP 请求中一直等待 RunningHub 完成。

任务状态接口返回类似：

```json
{
  "taskId": "本地任务ID",
  "status": "RUNNING",
  "createdAt": "时间",
  "updatedAt": "时间",
  "errorCode": null,
  "errorMessage": null,
  "downloadUrl": null
}
```

任务成功以后返回我方下载地址。

# 十九、安全要求

至少实现以下安全措施：

* 密码安全哈希
* API Key 加密保存
* HttpOnly Cookie
* 生产环境 Secure Cookie
* 登录鉴权
* 管理员权限检查
* 用户任务隔离
* 上传大小限制
* 文件类型校验
* 路径穿越防护
* 随机文件名
* 登录限流
* 创建任务限流
* 每个用户最大并发任务限制
* 日志中的 API Key 脱敏
* 错误页面不显示完整 Python 堆栈

不能把以下内容暴露给前端：

* 完整 RunningHub API Key
* 服务端加密密钥
* 任意其他用户配置
* 服务器真实文件路径

# 二十、错误处理

用户页面应显示容易理解的错误信息。

至少处理：

* 图片格式不支持
* 图片过大
* 音频格式不支持
* 音频过大
* 音频无法解析
* 时间范围不合法
* 用户没有配置 RunningHub API Key
* RunningHub API Key 无效
* RunningHub 上传失败
* RunningHub 任务提交失败
* RunningHub 工作流运行失败
* RunningHub 查询失败
* RunningHub 长时间无响应
* RunningHub 结果为空
* 视频下载失败
* 用户并发任务数量超限
* 服务器磁盘写入失败

详细错误可以存数据库和日志，但普通用户页面只显示安全、清楚的信息。

# 二十一、环境变量

请创建完整的 `.env.example`。

至少包括：

```text
APP_ENV=development
APP_SECRET_KEY=
APP_ENCRYPTION_KEY=

DATABASE_URL=sqlite:///./data/app.db
DATA_DIR=./data

RUNNINGHUB_BASE_URL=https://www.runninghub.cn
DEFAULT_RUNNINGHUB_AI_APP_ID=2062251097452007426
DEFAULT_RUNNINGHUB_INSTANCE_TYPE=plus
DEFAULT_USE_PERSONAL_QUEUE=false

POLL_INTERVAL_SECONDS=5
RUNNINGHUB_TASK_TIMEOUT_SECONDS=3600

MAX_IMAGE_SIZE_MB=20
MAX_AUDIO_SIZE_MB=100

UPLOAD_RETENTION_DAYS=3
OUTPUT_RETENTION_DAYS=7

COOKIE_SECURE=false
```

真实 API Key 不允许写进代码库或 `.env.example`。

# 二十二、测试要求

使用 pytest 为核心功能编写测试。

测试 RunningHub 调用时必须使用 mock，不能真实调用 RunningHub，不能产生费用。

至少测试：

* 时间字符串解析
* 秒数转 M:SS
* 开始和结束时间校验
* 音频时长处理
* 文件扩展名和大小校验
* 用户登录
* 管理员权限
* 用户任务隔离
* API Key 加密和解密
* RunningHub 文件上传成功
* RunningHub 文件上传失败
* RunningHub 任务创建成功
* RunningHub 返回 FAILED
* RunningHub `usage` 为 null
* Worker 恢复已有 RunningHub taskId 的任务
* Worker 不重复提交任务
* 下载权限
* 路径穿越防护

# 二十三、部署要求

项目需要支持在一台新的 Linux 服务器上部署。

请提供：

* `requirements.txt` 或 `pyproject.toml`
* `.env.example`
* 数据库迁移
* 创建管理员账号的脚本或命令
* Web 服务启动方式
* Worker 启动方式
* FFmpeg 安装说明
* Nginx 配置示例
* HTTPS 配置说明
* systemd 服务文件或 Docker Compose
* 数据目录权限说明
* 数据备份建议
* 日志查看方法
* 常见错误排查说明

如果使用 systemd，至少提供：

```text
runninghub-web.service
runninghub-worker.service
```

如果使用 Docker Compose，至少提供：

```text
web
worker
nginx
```

# 二十四、界面要求

界面不需要复杂设计。

优先保证：

* 清晰
* 简单
* 手机和电脑均可正常使用
* 错误信息明确
* 任务状态容易理解

状态文本建议使用：

```text
等待处理
正在上传素材
已经提交到 RunningHub
正在生成
生成成功
生成失败
```

第一版不要投入过多时间做动画、图表或复杂交互。

# 二十五、MVP 验收标准

满足以下条件才算完成：

1. 管理员可以创建用户。
2. 管理员可以为用户配置 RunningHub API Key。
3. 普通用户可以登录。
4. 用户可以上传图片和音频。
5. 系统可以自动读取音频时长。
6. 系统可以自动填写开始时间和结束时间。
7. 用户可以手动修改开始时间和结束时间。
8. 用户可以看到并修改默认提示词。
9. 用户提交后可以立即获得本地任务 ID。
10. 后台 Worker 可以继续完成长任务。
11. 页面关闭后，任务仍能继续。
12. Worker 重启后，不会重复提交已经存在 RunningHub taskId 的任务。
13. RunningHub 默认使用 48G 的 `plus` 实例。
14. Worker 能正确上传图片和音频。
15. Worker 能正确提交 RunningHub 任务。
16. 页面可以查看任务状态。
17. RunningHub 成功后，视频会自动保存到我方服务器。
18. 用户可以在线播放和下载视频。
19. 用户不能访问其他用户的任务和视频。
20. API Key 不会出现在前端和普通日志中。
21. 自动测试不会真实调用 RunningHub。
22. 所有测试通过。
23. 按照 README 可以在新服务器上完成部署。
24. 项目不是只有页面或伪接口，必须实际接通现有成功的 RunningHub 调用逻辑。

# 二十六、暂时不做的功能

第一版暂时不需要：

* 开放注册
* 在线支付
* 网站内部余额结算
* 复杂角色权限
* 多租户计费
* 任意工作流编辑器
* 用户自行填写节点 ID
* 用户自行填写 RunningHub API 地址
* 复杂数据统计
* React 重型前端
* 微服务拆分
* Kubernetes

请不要因为这些非 MVP 功能拖慢第一版交付。

# 二十七、后续扩展方向

代码结构需要为以下功能保留扩展空间，但第一版不要求实现：

* 批量提交多个视频
* 提示词模板
* 多个 RunningHub 工作流
* 单人和双人模式切换
* 多音频输入
* 用户任务统计
* 消耗统计
* 多服务器 Worker
* 对象存储
* 邮件或消息通知
* Webhook

# 二十八、执行要求

请按下面顺序推进：

1. 检查现有 Python 测试代码和 RunningHub 文档。
2. 确认现有调用代码中哪些部分已经验证成功。
3. 建立项目目录和配置系统。
4. 实现数据库模型。
5. 实现管理员账号和普通用户登录。
6. 实现用户 RunningHub 配置。
7. 重构 RunningHubClient。
8. 实现音频时长检测。
9. 实现生成页面。
10. 实现本地任务创建。
11. 实现独立 Worker。
12. 实现任务状态查询。
13. 实现视频下载和保存。
14. 实现任务历史和详情页面。
15. 实现用户数据隔离。
16. 补充安全校验。
17. 编写自动化测试。
18. 运行测试并修复问题。
19. 编写部署配置和 README。

不要只停留在计划阶段。

请直接修改和创建项目文件，运行项目和测试，修复出现的问题，直到满足 MVP 验收标准。

完成后请汇报：

1. 已实现的功能。
2. 新增和修改的主要文件。
3. 数据库表结构。
4. Web 服务启动命令。
5. Worker 启动命令。
6. 管理员账号创建方式。
7. 测试运行结果。
8. 服务器部署步骤。
9. 当前仍存在的风险或未完成内容。
