from __future__ import annotations

import argparse
import getpass

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import RunningHubConfig, User
from app.services.security import hash_password
from app.services.workflow_configs import save_workflow_config


def main() -> None:
    parser = argparse.ArgumentParser(description="创建管理员账号或补全管理员配置")
    parser.add_argument("username")
    parser.add_argument(
        "--password",
        help="仅用于新建账号或主动重置密码；不建议在命令行中传递",
    )
    args = parser.parse_args()

    settings = get_settings()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.username.strip()))
        if user is None:
            password = args.password or getpass.getpass("管理员密码（至少 8 位）：")
            user = User(
                username=args.username.strip(),
                password_hash=hash_password(password),
                is_admin=True,
                is_active=True,
            )
            db.add(user)
        else:
            # Existing users keep their password unless --password is explicitly supplied.
            if args.password:
                user.password_hash = hash_password(args.password)
            user.is_admin = True
            user.is_active = True
        if user.runninghub_config is None:
            config = RunningHubConfig(
                user=user,
                base_url=settings.runninghub_base_url,
                ai_app_id=settings.default_runninghub_ai_app_id,
                instance_type=settings.default_runninghub_instance_type,
                default_prompt="人物自然地说话，表情自然，动作自然，镜头保持稳定。",
                max_concurrent_tasks=1,
            )
        else:
            config = user.runninghub_config
        # API Key is intentionally configured only through the authenticated
        # administrator page and is stored there as encrypted ciphertext.
        db.add(config)
        workflow_config = save_workflow_config(
            user,
            "digital_human",
            ai_app_id=config.ai_app_id,
            instance_type=config.instance_type,
            default_prompt=config.default_prompt,
        )
        db.add(workflow_config)
        db.commit()
    print(f"管理员账号 {args.username} 已创建或确认。请登录网页配置 RunningHub 信息。")


if __name__ == "__main__":
    main()
