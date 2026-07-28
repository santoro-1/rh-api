"""Add persistent batch generation and staged upload records."""

from alembic import op
import sqlalchemy as sa


revision = "0004_batch_generation"
down_revision = "0003_workflow_adapters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generation_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("workflow_type", sa.String(length=100), nullable=False),
        sa.Column("audio_mode", sa.String(length=30), nullable=False),
        sa.Column("request_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("total_items", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "request_key", name="uq_batches_user_request_key"
        ),
    )
    op.create_index(
        "ix_generation_batches_user_id",
        "generation_batches",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_batches_workflow_type",
        "generation_batches",
        ["workflow_type"],
        unique=False,
    )

    op.create_table(
        "generation_batch_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("row_key", sa.String(length=100), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("audio_status", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["generation_batches.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id", "row_number", name="uq_batch_items_row_number"
        ),
        sa.UniqueConstraint("batch_id", "row_key", name="uq_batch_items_row_key"),
    )
    op.create_index(
        "ix_generation_batch_items_batch_id",
        "generation_batch_items",
        ["batch_id"],
        unique=False,
    )

    op.create_table(
        "staged_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("relative_path", sa.String(length=500), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_staged_assets_user_id", "staged_assets", ["user_id"], unique=False
    )

    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("batch_item_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_generation_tasks_batch_item",
            "generation_batch_items",
            ["batch_item_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_generation_tasks_batch_item", ["batch_item_id"]
        )
    op.create_index(
        "ix_generation_tasks_batch_item_id",
        "generation_tasks",
        ["batch_item_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_generation_tasks_batch_item_id", table_name="generation_tasks"
    )
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.drop_constraint("uq_generation_tasks_batch_item", type_="unique")
        batch_op.drop_constraint("fk_generation_tasks_batch_item", type_="foreignkey")
        batch_op.drop_column("batch_item_id")
    op.drop_index("ix_staged_assets_user_id", table_name="staged_assets")
    op.drop_table("staged_assets")
    op.drop_index(
        "ix_generation_batch_items_batch_id",
        table_name="generation_batch_items",
    )
    op.drop_table("generation_batch_items")
    op.drop_index(
        "ix_generation_batches_workflow_type",
        table_name="generation_batches",
    )
    op.drop_index(
        "ix_generation_batches_user_id", table_name="generation_batches"
    )
    op.drop_table("generation_batches")
