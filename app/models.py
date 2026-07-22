from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_TASK_STATUSES = (
    TaskStatus.PENDING.value,
    TaskStatus.UPLOADING.value,
    TaskStatus.SUBMITTED.value,
    TaskStatus.RUNNING.value,
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    runninghub_config: Mapped[Optional["RunningHubConfig"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    workflow_configs: Mapped[list["WorkflowConfig"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["GenerationTask"]] = relationship(back_populates="user")


class RunningHubConfig(Base):
    __tablename__ = "runninghub_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    ai_app_id: Mapped[str] = mapped_column(String(100), nullable=False)
    instance_type: Mapped[str] = mapped_column(String(20), nullable=False, default="plus")
    default_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, default="人物自然地说话，表情自然，动作自然，镜头保持稳定。"
    )
    max_concurrent_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="runninghub_config")


class WorkflowConfig(Base):
    """Per-user settings for one named workflow.

    RunningHub account credentials remain in ``RunningHubConfig``.  This model
    deliberately owns the workflow-specific App ID and defaults, so adding a
    workflow does not require adding columns to the account configuration.
    """

    __tablename__ = "workflow_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workflow_key: Mapped[str] = mapped_column(String(100), nullable=False)
    ai_app_id: Mapped[str] = mapped_column(String(100), nullable=False)
    instance_type: Mapped[str] = mapped_column(String(20), nullable=False, default="plus")
    default_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    settings_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="workflow_configs")

    __table_args__ = (
        UniqueConstraint("user_id", "workflow_key", name="uq_workflow_configs_user_workflow"),
    )


class GenerationTask(Base):
    __tablename__ = "generation_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    runninghub_task_id: Mapped[Optional[str]] = mapped_column(
        String(100), unique=True, nullable=True, index=True
    )
    workflow_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="digital_human", index=True
    )
    # Canonical workflow input.  Legacy dedicated columns below are kept so
    # existing local tasks and the current digital-human pages stay compatible.
    input_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_path: Mapped[str] = mapped_column(String(500), nullable=False)
    audio_path: Mapped[str] = mapped_column(String(500), nullable=False)
    image_original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    audio_original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    audio_duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=TaskStatus.PENDING.value, index=True
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    runninghub_usage: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    output_metadata: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    runninghub_submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="tasks")
