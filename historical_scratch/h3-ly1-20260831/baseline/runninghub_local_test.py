"""Optional manual RunningHub smoke test.

This file deliberately contains no API key. Set RUNNINGHUB_API_KEY only in your
shell for an intentional real integration test; pytest never imports or calls it.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from app.services.runninghub import RunningHubClient
from app.services.workflow import build_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="手动验证 RunningHub 调用（会产生费用）")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--start", default="0:00")
    parser.add_argument("--end", required=True)
    parser.add_argument("--prompt", default="人物自然地说话，表情自然，动作自然，镜头保持稳定。")
    args = parser.parse_args()

    api_key = os.getenv("RUNNINGHUB_API_KEY", "")
    if not api_key:
        raise SystemExit("请先在当前 shell 设置 RUNNINGHUB_API_KEY；不要把 Key 写入文件。")
    client = RunningHubClient(
        api_key=api_key,
        base_url=os.getenv("RUNNINGHUB_BASE_URL", "https://www.runninghub.cn"),
        ai_app_id=os.getenv("RUNNINGHUB_AI_APP_ID", "2062251097452007426"),
    )
    image_file_name = client.upload_file(args.image)
    audio_file_name = client.upload_file(args.audio)
    task_id = client.submit_task(
        build_payload(
            image_file_name,
            audio_file_name,
            args.start,
            args.end,
            args.prompt,
            "plus",
        )
    )
    print(f"已提交真实任务：{task_id}。请到网站任务页或使用 API 查询结果。")


if __name__ == "__main__":
    main()
