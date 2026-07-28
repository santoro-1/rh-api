"""Add stable MiniMax account binding and visible script segments."""

from alembic import op
import sqlalchemy as sa


revision = "0007_full_script_pipeline"
down_revision = "0006_voice_studio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("minimax_configs") as batch_op:
        batch_op.add_column(
            sa.Column("account_binding_id", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "account_label",
                sa.String(length=100),
                nullable=False,
                server_default="MiniMax 账号",
            )
        )
    op.execute(
        "UPDATE minimax_configs "
        "SET account_binding_id = 'legacy-config-' || CAST(id AS TEXT) "
        "WHERE account_binding_id IS NULL"
    )
    with op.batch_alter_table("minimax_configs") as batch_op:
        batch_op.alter_column(
            "account_binding_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_minimax_configs_account_binding_id",
            ["account_binding_id"],
        )

    for table_name in (
        "minimax_voice_assets",
        "audio_generation_tasks",
        "voice_creation_tasks",
    ):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "account_binding_id",
                    sa.String(length=64),
                    nullable=True,
                )
            )
        op.execute(
            f"UPDATE {table_name} "
            "SET account_binding_id = ("
            "SELECT account_binding_id FROM minimax_configs "
            f"WHERE minimax_configs.id = {table_name}.config_id"
            ") WHERE account_binding_id IS NULL"
        )
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                "account_binding_id",
                existing_type=sa.String(length=64),
                nullable=False,
            )
        op.create_index(
            f"ix_{table_name}_account_binding_id",
            table_name,
            ["account_binding_id"],
            unique=False,
        )

    op.create_table(
        "generation_segments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_item_id", sa.String(length=36), nullable=False),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("script_text", sa.Text(), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("audio_path", sa.String(length=500), nullable=False),
        sa.Column("video_path", sa.String(length=500), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "alignment_method",
            sa.String(length=30),
            nullable=False,
            server_default="punctuation_silence",
        ),
        sa.Column(
            "status",
            sa.String(length=30),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_item_id"],
            ["generation_batch_items.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_item_id",
            "segment_index",
            name="uq_generation_segments_item_index",
        ),
    )
    op.create_index(
        "ix_generation_segments_batch_item_id",
        "generation_segments",
        ["batch_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_segments_status",
        "generation_segments",
        ["status"],
        unique=False,
    )

    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("segment_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_generation_tasks_segment_id",
            "generation_segments",
            ["segment_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_generation_tasks_segment_id",
        "generation_tasks",
        ["segment_id"],
        unique=True,
    )

    with op.batch_alter_table("audio_generation_tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "alignment_method",
                sa.String(length=30),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("audio_generation_tasks") as batch_op:
        batch_op.drop_column("alignment_method")
    op.drop_index(
        "ix_generation_tasks_segment_id",
        table_name="generation_tasks",
    )
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.drop_constraint(
            "fk_generation_tasks_segment_id",
            type_="foreignkey",
        )
        batch_op.drop_column("segment_id")
    op.drop_index(
        "ix_generation_segments_status",
        table_name="generation_segments",
    )
    op.drop_index(
        "ix_generation_segments_batch_item_id",
        table_name="generation_segments",
    )
    op.drop_table("generation_segments")

    for table_name in (
        "voice_creation_tasks",
        "audio_generation_tasks",
        "minimax_voice_assets",
    ):
        op.drop_index(
            f"ix_{table_name}_account_binding_id",
            table_name=table_name,
        )
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_column("account_binding_id")

    with op.batch_alter_table("minimax_configs") as batch_op:
        batch_op.drop_constraint(
            "uq_minimax_configs_account_binding_id",
            type_="unique",
        )
        batch_op.drop_column("account_label")
        batch_op.drop_column("account_binding_id")
