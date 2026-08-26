"""share one configuration per registered RunningHub workflow

Revision ID: 0044_system_workflow_configs
Revises: 0043_h3_motion_reference_pool
Create Date: 2026-08-26
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "0044_system_workflow_configs"
down_revision = "0043_h3_motion_reference_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "system_workflow_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workflow_key", sa.String(length=100), nullable=False, unique=True),
        sa.Column("ai_app_id", sa.String(length=100), nullable=False),
        sa.Column("instance_type", sa.String(length=20), nullable=False),
        sa.Column("default_prompt", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "instance_type IN ('default', 'plus')",
            name="ck_system_workflow_config_instance_type",
        ),
    )
    bind = op.get_bind()
    legacy = sa.table(
        "workflow_configs",
        sa.column("user_id", sa.Integer()),
        sa.column("workflow_key", sa.String()),
        sa.column("ai_app_id", sa.String()),
        sa.column("instance_type", sa.String()),
        sa.column("default_prompt", sa.Text()),
        sa.column("is_enabled", sa.Boolean()),
        sa.column("settings_json", sa.Text()),
    )
    defaults = (
        (
            "digital_human",
            "2062251097452007426",
            "人物自然地说话，表情自然，动作自然，镜头保持稳定。",
        ),
        (
            "ltx_lip_sync",
            "2080551073030434817",
            "一名人物用中文说：“请填写与音频一致的完整台词。”",
        ),
    )
    for key, app_id, prompt in defaults:
        row = bind.execute(
            sa.select(legacy)
            .where(legacy.c.workflow_key == key)
            .order_by(legacy.c.user_id)
            .limit(1)
        ).mappings().first()
        bind.execute(
            table.insert().values(
                workflow_key=key,
                ai_app_id=(row["ai_app_id"] if row else app_id),
                instance_type=(row["instance_type"] if row else "plus"),
                default_prompt=(row["default_prompt"] if row else prompt),
                is_enabled=(bool(row["is_enabled"]) if row else True),
                settings_json=(row["settings_json"] if row else "{}"),
            )
        )
    h3 = bind.execute(
        sa.text(
            "SELECT workflow_id, access_password_encrypted "
            "FROM runninghub_h3_capabilities WHERE is_enabled = 1 "
            "ORDER BY execution_account_id LIMIT 1"
        )
    ).mappings().first()
    bind.execute(
        table.insert().values(
            workflow_key="minimax_h3_ref2va",
            ai_app_id=(h3["workflow_id"] if h3 else "2090147471501643778"),
            instance_type="plus",
            default_prompt="由 H3 PromptProfile 根据每段台词自动编译",
            is_enabled=True,
            settings_json=json.dumps(
                (
                    {"access_password_encrypted": h3["access_password_encrypted"]}
                    if h3 and h3["access_password_encrypted"]
                    else {}
                ),
                ensure_ascii=False,
            ),
        )
    )


def downgrade() -> None:
    op.drop_table("system_workflow_configs")
