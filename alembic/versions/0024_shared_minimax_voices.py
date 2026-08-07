"""backfill shared custom voices for identical MiniMax credentials

Revision ID: 0024_shared_minimax_voices
Revises: 0023_batch_correlation_id
"""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa


revision = "0024_shared_minimax_voices"
down_revision = "0023_batch_correlation_id"
branch_labels = None
depends_on = None


def _backfill_shared_custom_voices(connection) -> None:
    # A stable account binding survives an API-key rotation. Bring voices that
    # still belong to the owner's current binding onto the current credential
    # fingerprint before matching other website users with that exact key.
    connection.execute(
        sa.text(
            "UPDATE minimax_voice_assets SET credential_fingerprint=("
            "SELECT credential_fingerprint FROM minimax_configs "
            "WHERE minimax_configs.id=minimax_voice_assets.config_id"
            ") WHERE account_binding_id=("
            "SELECT account_binding_id FROM minimax_configs "
            "WHERE minimax_configs.id=minimax_voice_assets.config_id"
            ") AND (SELECT credential_fingerprint FROM minimax_configs "
            "WHERE minimax_configs.id=minimax_voice_assets.config_id) IS NOT NULL"
        )
    )
    configs = connection.execute(
        sa.text(
            "SELECT id, user_id, account_binding_id, credential_fingerprint "
            "FROM minimax_configs WHERE credential_fingerprint IS NOT NULL "
            "ORDER BY id"
        )
    ).mappings().all()
    voices = connection.execute(
        sa.text(
            "SELECT id, user_id, config_id, name, voice_id, account_binding_id, "
            "credential_fingerprint, status, method, category, is_saved, "
            "source_relative_path, source_original_name, remote_file_id, "
            "preview_relative_path, activated_at, expires_at, created_at, updated_at "
            "FROM minimax_voice_assets ORDER BY created_at, id"
        )
    ).mappings().all()
    representative: dict[tuple[str, str], dict] = {}
    existing = {(int(voice["config_id"]), str(voice["voice_id"])) for voice in voices}
    for raw_voice in voices:
        voice = dict(raw_voice)
        if (
            voice["method"] == "system"
            or not voice["is_saved"]
            or voice["status"] not in {"READY", "ACTIVE"}
        ):
            continue
        key = (str(voice["credential_fingerprint"]), str(voice["voice_id"]))
        current = representative.get(key)
        if current is None or (
            current["status"] != "ACTIVE" and voice["status"] == "ACTIVE"
        ):
            representative[key] = voice

    for config in configs:
        fingerprint = str(config["credential_fingerprint"])
        for (source_fingerprint, provider_voice_id), source in representative.items():
            if source_fingerprint != fingerprint:
                continue
            local_key = (int(config["id"]), provider_voice_id)
            if local_key not in existing:
                connection.execute(
                    sa.text(
                        "INSERT INTO minimax_voice_assets ("
                        "id, user_id, config_id, name, voice_id, account_binding_id, "
                        "credential_fingerprint, status, method, category, is_saved, "
                        "source_relative_path, source_original_name, remote_file_id, "
                        "preview_relative_path, activated_at, expires_at, created_at, updated_at"
                        ") VALUES ("
                        ":id, :user_id, :config_id, :name, :voice_id, :account_binding_id, "
                        ":credential_fingerprint, :status, :method, :category, 1, "
                        ":source_relative_path, :source_original_name, :remote_file_id, "
                        ":preview_relative_path, :activated_at, :expires_at, :created_at, :updated_at"
                        ")"
                    ),
                    {
                        **source,
                        "id": str(uuid.uuid4()),
                        "user_id": config["user_id"],
                        "config_id": config["id"],
                        "account_binding_id": config["account_binding_id"],
                        "credential_fingerprint": fingerprint,
                    },
                )
                existing.add(local_key)
            if source["status"] == "ACTIVE":
                connection.execute(
                    sa.text(
                        "UPDATE minimax_voice_assets SET status='ACTIVE', "
                        "activated_at=COALESCE(activated_at, :activated_at), "
                        "preview_relative_path=COALESCE(preview_relative_path, :preview), "
                        "expires_at=COALESCE(:expires_at, expires_at) "
                        "WHERE config_id=:config_id AND voice_id=:voice_id "
                        "AND is_saved=1"
                    ),
                    {
                        "activated_at": source["activated_at"],
                        "preview": source["preview_relative_path"],
                        "expires_at": source["expires_at"],
                        "config_id": config["id"],
                        "voice_id": provider_voice_id,
                    },
                )


def upgrade() -> None:
    _backfill_shared_custom_voices(op.get_bind())


def downgrade() -> None:
    # Shared rows are valid user-owned voice records and may already be referenced
    # by audio tasks. A downgrade must not guess which copy is safe to delete.
    pass
