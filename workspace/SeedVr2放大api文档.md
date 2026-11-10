# 欢迎使用 RunningHub API，轻松调用 RunningHub 云端的 ComfyUI 工作流

## 1. 开始使用

### 注册用户

注册 RunningHub 账号并充值钱包后，即可开始使用 AI 应用 API 和 ComfyUI 工作流 API。
请注意：若您使用 消费级-会员 API Key，需拥有 基础版及以上会员 才能调用上述接口。
使用 企业级-共享 或 企业级-独占 API Key 的用户不受此限制。

### 获取您的 API Key

RunningHub 为每位用户自动生成一个独特的 32 位 API KEY

请妥善保存您的 API KEY，不要外泄，后续步骤将依赖此密钥进行操作

### 提交请求

提交 API 请求。RunningHub API 已为您处理 API Key，您只需提交请求即可

```python
import os
import requests
import json
import time

def main():
    print("Hello from RunningHubAPI!")
    API_KEY = RUNNINGHUB_API_KEY
    print(f"API_KEY: {API_KEY}")

    url = "https://www.runninghub.cn/openapi/v2/run/ai-app/2064116518987845634"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    payload = {
    "nodeInfoList": [
        {
            "nodeId": "46",
            "fieldName": "video",
            "fieldValue": "cbdd6e22fed5f2c302f3b05f7f061572b546c601a50a32b48fd4fe11f7ccbdc6.mp4",
            "description": "上传视频"
        },
        {
            "nodeId": "108",
            "fieldName": "select",
            "fieldValue": "1",
            "description": "模型切换"
        },
        {
            "nodeId": "112",
            "fieldName": "value",
            "fieldValue": "1920",
            "description": "最大分辨率（不建议超过1920）"
        }
    ],
    "instanceType": "plus",
    "usePersonalQueue": false
}

    begin = time.time()
    response = requests.post(url, headers=headers, data=json.dumps(payload))
    if response.status_code == 200:
        result = response.json()
        task_id = result.get("taskId")
        print(f"Task submitted successfully. Task ID: {task_id}")
    else:
        print(f"Error: {response.status_code}, {response.text}")
        return

    query_url = "https://www.runninghub.cn/openapi/v2/query"
    query_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    # Poll for results
    while True:
        query_payload = {"taskId": task_id}
        response = requests.post(query_url, headers=query_headers, data=json.dumps(query_payload))
        if response.status_code == 200:
            result = response.json()
            status = result["status"]

            if status == "SUCCESS":
                end = time.time()
                print(f"Task completed in {end - begin} seconds.")
                if result.get("results") and len(result["results"]) > 0:
                    output_url = result["results"][0]["url"]
                    print(f"Task completed. URL: {output_url}")
                else:
                    print("Task completed but no results found.")
                break
            elif status == "RUNNING" or status == "QUEUED":
                print(f"Task still processing. Status: {status}")
            else:
                # FAILED or other error status
                error_message = result.get("errorMessage", "Unknown error")
                print(f"Task failed: {error_message}")
                break
        else:
            print(f"Error: {response.status_code}, {response.text}")
            break

        time.sleep(5)


if __name__ == "__main__":
    main()
```

#### 请求参数说明

| 参数说明 | 类型 | 必填/可选 | AI 应用程序生成的结果。 |
| --- | --- | --- | --- |
| `nodeInfoList` | List | 必填 | 节点参数映射列表，用于动态修改工作流参数 |
| `instanceType` | String | 可选 | 指定运行实例的类型：default（24G 显存）、plus（48G 显存）。本项目的 SeedVR2 放大流程统一固定为 `plus`（48G），不得回落为 `default`。 |
| `usePersonalQueue` | Boolean | 可选 | 是否使用个人独占队列 |
| `retainSeconds` | Integer | 可选 | 实例保留时长（秒）。仅企业共享 API Key 生效；任务成功结束后会在指定时长内优先复用同用户同工作流实例，减少冷启动与排队。该保留时段会产生额外费用，按实际保留时长计费。可选范围：10~180 秒。 |
| `webhookUrl` | String | 可选 | Webhook 回调地址，任务完成时会向该地址发送 POST 请求 |

#### 响应示例

```json
{
  "taskId": "2013508786110730241",
  "status": "RUNNING",
  "errorCode": "",
  "errorMessage": "",
  "results": null,
  "clientId": "f828b9af25161bc066ef152db7b29ccc",
  "promptTips": "{\"result\": true, \"error\": null, \"outputs_to_execute\": [\"4\"], \"node_errors\": {}}"
}
```

#### 响应字段说明

| 参数说明 | 类型 | AI 应用程序生成的结果。 |
| --- | --- | --- |
| `taskId` | String | 任务ID，用于后续查询任务状态 |
| `status` | String | 当前任务状态，常见状态：QUEUED (排队中), RUNNING (运行中), SUCCESS (成功), FAILED (失败) |
| `errorCode` | String | 错误码，仅在失败时返回 |
| `errorMessage` | String | 错误具体信息 |
| `results` | List | 生成结果（提交时为 null） |
| ├ `url` | String | 重要提醒：该链接有效期仅为 24 小时。任务生成结束后，请务必在此时间窗口内将视频文件下载或转存至您的服务器。逾期后链接将永久失效且无法恢复。 |
| ├ `nodeId` | String | 生成该结果的工作流节点 ID |
| ├ `outputType` | String | 文件扩展名 (如 png, mp4, txt) |
| └ `text` | String | 如果输出是纯文本，内容将显示在此字段 |
| `clientId` | String | 客户端会话ID，用于标识本次连接 |
| `promptTips` | String (JSON) | ComfyUI 后端的校验信息，包含需执行的节点ID等调试信息 |

### 查询结果与 Webhook

如果在提交时添加了 "webhookUrl": "https://example.com/webhook" 请求体参数，RunningHub 会在任务完成时向您的URL发送POST请求

#### 请求示例

```python
import os
import requests
import json


API_KEY = RUNNINGHUB_API_KEY

url = "https://www.runninghub.cn/openapi/v2/query"
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}",
}
payload = {
    "taskId": "${RUNNINGHUB_TASKID}"
}

response = requests.post(url, headers=headers, data=json.dumps(payload))
if response.status_code == 200:
    result = response.json()
    print(json.dumps(result, indent=2))
else:
    print(f"Error: {response.status_code}, {response.text}")
```

#### 响应示例

```json
{
  "taskId": "2013508786110730241",
  "status": "SUCCESS",
  "errorCode": "",
  "errorMessage": "",
  "failedReason": {},
  "usage": {
    "consumeMoney": null,
    "consumeCoins": null,
    "taskCostTime": "0",
    "thirdPartyConsumeMoney": null
  },
  "results": [
    {
      "url": "https://rh-images-1252422369.cos.ap-beijing.myqcloud.com/b04e28cad0ee39193921a30a2eb4dc00/output/ComfyUI_00001_plhjr_1768892915.png",
      "nodeId": "2",
      "outputType": "png",
      "text": null
    }
  ],
  "clientId": "",
  "promptTips": ""
}
```

#### 响应字段说明

| 参数说明 | 类型 | AI 应用程序生成的结果。 |
| --- | --- | --- |
| `taskId` | String | 任务 ID |
| `status` | String | 任务最终状态，SUCCESS 表示生成成功 |
| `results` | List | 生成结果列表，包含图片、视频或文本等输出 |
| ├ `url` | String | 重要提醒：该链接有效期仅为 24 小时。任务生成结束后，请务必在此时间窗口内将视频文件下载或转存至您的服务器。逾期后链接将永久失效且无法恢复。 |
| ├ `nodeId` | String | 生成该结果的工作流节点 ID |
| ├ `outputType` | String | 文件扩展名 (如 png, mp4, txt) |
| └ `text` | String | 如果输出是纯文本，内容将显示在此字段 |
| `errorCode` | String | 错误码 (如有) |
| `errorMessage` | String | 错误信息 (如有) |
| `failedReason` | Object | ComfyUI 相关的失败原因 |
| `usage` | Object | 任务消耗信息 |
| ├ `thirdPartyConsumeMoney` | String | 三方API消费金额 |
| ├ `consumeMoney` | String | 运行时长消耗金额 |
| ├ `consumeCoins` | String | 运行消耗的RH币 |
| └ `taskCostTime` | String | 运行耗时（ComfyUI 工作流运行时长） |

#### 本项目真实 48G 冒烟记录（2026-08-10）

上面的 PNG 仅是 RunningHub 通用查询示例，不代表 SeedVR2 的真实媒体类型。经用户明确授权，使用 `instanceType=plus`（48G）、约 5 秒输入视频执行一次真实任务：

- SeedVR2 任务 ID：`2086736444621291521`
- 状态：`SUCCESS`
- 真实结果：`nodeId="97"`、`outputType="mp4"`
- 输入数字人源片段：H.264 + AAC，576×1024，4.84 秒
- 输出清晰片段：H.264 + AAC，1080×1920，4.84 秒
- 用量：`consumeCoins=97`、`taskCostTime=242`

业务适配器仍应按“唯一视频结果”选择输出，不应只依赖节点 `97`，以防 AI App 工作流以后调整输出节点。

### 文件上传

资源文件（如 imageUrls）参数支持传入文件 URL 或 Base64 Data URI。

#### 公共 URL

直接传递可公开访问的 URL：

```json
{
  "imageUrls": [
    "https://example.com/image.png"
  ]
}
```

#### Base64 data URI

以 Base64 格式嵌入图片：

```json
{
  "images": [
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA..."
  ]
}
```

#### RH 上传接口

上传本地文件以获取一个 URL。

**Endpoint:** `https://www.runninghub.cn/openapi/v2/media/upload/binary`

**请求**

```curl
curl --location --request POST 'https://www.runninghub.cn/openapi/v2/media/upload/binary' \
--header 'Authorization: Bearer [Your API KEY]' \
--form 'file=@/path/to/image.png'
```

**响应**

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "type": "image",
    "download_url": "xxxx.png",
    "fileName": "openapi/xxxx.png",
    "size": "3490"
  }
}
```

**备注:** 上传后获得的链接有效期为 1 天，超期将无法通过 URL 直接访问。

