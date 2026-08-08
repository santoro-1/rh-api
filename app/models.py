from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
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


class AudioTaskStatus(str, Enum):
    PENDING = "PENDING"
    CLONING = "CLONING"
    SYNTHESIZING = "SYNTHESIZING"
    REMOTE_PENDING = "REMOTE_PENDING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    ALIGNING = "ALIGNING"
    SEGMENTING = "SEGMENTING"
    HANDOFF = "HANDOFF"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class VoiceAssetStatus(str, Enum):
    TEMPORARY = "TEMPORARY"
    UPLOADED = "UPLOADED"
    CLONED = "CLONED"
    READY = "READY"
    ACTIVE = "ACTIVE"
    HIDDEN = "HIDDEN"
    FAILED = "FAILED"


class VoiceCreationStatus(str, Enum):
    PENDING = "PENDING"
    CLONING = "CLONING"
    SYNTHESIZING = "SYNTHESIZING"
    PREVIEW_READY = "PREVIEW_READY"
    SAVE_PENDING = "SAVE_PENDING"
    SAVING = "SAVING"
    SAVED = "SAVED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class LongAudioProjectStatus(str, Enum):
    PENDING_ANALYSIS = "PENDING_ANALYSIS"
    ANALYZING = "ANALYZING"
    REVIEW = "REVIEW"
    PENDING_CUT = "PENDING_CUT"
    CUTTING = "CUTTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), index=True)
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
    tasks: Mapped[list["GenerationTask"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    batches: Mapped[list["GenerationBatch"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    staged_assets: Mapped[list["StagedAsset"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    minimax_config: Mapped[Optional["MiniMaxConfig"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    ark_config: Mapped[Optional["ArkConfig"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    content_analysis_caches: Mapped[list["ContentAnalysisCache"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    visual_analysis_caches: Mapped[list["VisualAnalysisCache"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    minimax_voices: Mapped[list["MiniMaxVoiceAsset"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    audio_tasks: Mapped[list["AudioGenerationTask"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    voice_creation_tasks: Mapped[list["VoiceCreationTask"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    long_audio_projects: Mapped[list["LongAudioProject"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    runninghub_pool_memberships: Mapped[list["RunningHubPoolMembership"]] = relationship(
        back_populates="admin_user", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("username"),)


class RunningHubConfig(Base):
    __tablename__ = "runninghub_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    credential_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
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


class MiniMaxConfig(Base):
    """Encrypted account-level MiniMax connection and pacing settings."""

    __tablename__ = "minimax_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The binding stays stable when an API key is rotated for the same
    # MiniMax account. Credential fingerprints are retained only for audit.
    account_binding_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        default=lambda: str(uuid.uuid4()),
    )
    account_label: Mapped[str] = mapped_column(
        String(100), nullable=False, default="MiniMax 账号"
    )
    credential_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    base_url: Mapped[str] = mapped_column(
        String(500), nullable=False, default="https://api.minimaxi.com"
    )
    requests_per_minute: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20
    )
    last_t2a_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="minimax_config")
    voices: Mapped[list["MiniMaxVoiceAsset"]] = relationship(
        back_populates="config", cascade="all, delete-orphan"
    )
    audio_tasks: Mapped[list["AudioGenerationTask"]] = relationship(
        back_populates="config"
    )
    voice_creation_tasks: Mapped[list["VoiceCreationTask"]] = relationship(
        back_populates="config"
    )


class ArkConfig(Base):
    """Encrypted user-level Volcengine Ark connection settings."""

    __tablename__ = "ark_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    base_url: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        default="https://ark.cn-beijing.volces.com/api/v3",
    )
    model: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="ark_config")


class ContentAnalysisCache(Base):
    """Validated whole-script analysis, isolated by user and contract inputs."""

    __tablename__ = "content_analysis_caches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    script_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    script_length: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    overall_status: Mapped[str] = mapped_column(String(20), nullable=False)
    music_analysis_status: Mapped[str] = mapped_column(String(20), nullable=False)
    subtitle_analysis_status: Mapped[str] = mapped_column(String(20), nullable=False)
    music_intent_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subtitle_units_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    music_error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    music_error_summary: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    subtitle_error_code: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    subtitle_error_summary: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    provider_request_id: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )
    provider_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cacheable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="content_analysis_caches")

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "script_sha256",
            "schema_version",
            "prompt_version",
            "model",
            name="uq_content_analysis_cache_key",
        ),
    )


class VisualAnalysisCache(Base):
    """Validated semantic-visual decisions keyed by every material input."""

    __tablename__ = "visual_analysis_caches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    script_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    script_length: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_set_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    provider_request_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    provider_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="visual_analysis_caches")

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "script_sha256",
            "catalog_version",
            "candidate_set_sha256",
            "schema_version",
            "prompt_version",
            "model",
            name="uq_visual_analysis_cache_key",
        ),
    )


class MiniMaxVoiceAsset(Base):
    """One provider system voice or a reusable custom MiniMax voice."""

    __tablename__ = "minimax_voice_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    config_id: Mapped[int] = mapped_column(
        ForeignKey("minimax_configs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    voice_id: Mapped[str] = mapped_column(String(256), nullable=False)
    account_binding_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    credential_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=VoiceAssetStatus.TEMPORARY.value
    )
    method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="legacy"
    )
    category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_saved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    source_relative_path: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    source_original_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    remote_file_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    preview_relative_path: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="minimax_voices")
    config: Mapped[MiniMaxConfig] = relationship(back_populates="voices")
    creation_task: Mapped[Optional["VoiceCreationTask"]] = relationship(
        back_populates="voice_asset", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("config_id", "voice_id", name="uq_minimax_voice_config_id"),
    )


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


class RunningHubExecutionAccount(Base):
    """One real RunningHub credential used as an independent capacity pool member."""

    __tablename__ = "runninghub_execution_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    credential_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    digital_human_ai_app_id: Mapped[str] = mapped_column(String(100), nullable=False)
    max_concurrent_tasks: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    health_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="UNKNOWN"
    )
    health_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    health_error_code: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    pool_memberships: Mapped[list["RunningHubPoolMembership"]] = relationship(
        back_populates="execution_account", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["GenerationTask"]] = relationship(
        back_populates="execution_account"
    )
    attempts: Mapped[list["GenerationTaskAttempt"]] = relationship(
        back_populates="execution_account"
    )

    __table_args__ = (
        CheckConstraint(
            "max_concurrent_tasks >= 1 AND max_concurrent_tasks <= 5",
            name="ck_runninghub_execution_account_concurrency",
        ),
    )


class RunningHubPoolMembership(Base):
    """Grant one administrator access to one execution account."""

    __tablename__ = "runninghub_pool_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_account_id: Mapped[int] = mapped_column(
        ForeignKey("runninghub_execution_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    admin_user: Mapped[User] = relationship(
        back_populates="runninghub_pool_memberships"
    )
    execution_account: Mapped[RunningHubExecutionAccount] = relationship(
        back_populates="pool_memberships"
    )

    __table_args__ = (
        UniqueConstraint(
            "admin_user_id",
            "execution_account_id",
            name="uq_runninghub_pool_admin_account",
        ),
    )


class GenerationTask(Base):
    __tablename__ = "generation_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    batch_item_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("generation_batch_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    segment_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("generation_segments.id", ondelete="SET NULL"),
        nullable=True,
    )
    runninghub_task_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, index=True
    )
    execution_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("runninghub_execution_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
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
    runninghub_failed_reason: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    runninghub_attempt_history: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    runninghub_auto_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    runninghub_auto_retry_after: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
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
    batch_item: Mapped[Optional["GenerationBatchItem"]] = relationship(
        back_populates="generation_task"
    )
    segment: Mapped[Optional["GenerationSegment"]] = relationship(
        back_populates="generation_task"
    )
    execution_account: Mapped[Optional[RunningHubExecutionAccount]] = relationship(
        back_populates="tasks"
    )
    runninghub_attempts: Mapped[list["GenerationTaskAttempt"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="GenerationTaskAttempt.attempt_number",
    )

    __table_args__ = (
        Index(
            "ix_generation_tasks_batch_item_id",
            "batch_item_id",
            unique=True,
        ),
        Index(
            "ix_generation_tasks_segment_id",
            "segment_id",
            unique=True,
        ),
        UniqueConstraint("runninghub_task_id"),
        UniqueConstraint(
            "batch_item_id",
            name="uq_generation_tasks_batch_item",
        ),
    )


class GenerationTaskAttempt(Base):
    """Immutable account binding and outcome for one RunningHub submit attempt."""

    __tablename__ = "generation_task_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    generation_task_id: Mapped[str] = mapped_column(
        ForeignKey("generation_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("runninghub_execution_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    remote_task_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="RESERVED", index=True
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failed_reason_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    task: Mapped[GenerationTask] = relationship(back_populates="runninghub_attempts")
    execution_account: Mapped[Optional[RunningHubExecutionAccount]] = relationship(
        back_populates="attempts"
    )

    __table_args__ = (
        UniqueConstraint(
            "generation_task_id",
            "attempt_number",
            name="uq_generation_task_attempt_number",
        ),
    )


BATCH_SOURCE_LEGACY_WEB = "legacy_web"
BATCH_SOURCE_NEW_WORKBENCH = "new_workbench"


class GenerationBatch(Base):
    """One user submission containing multiple independently queued video tasks."""

    __tablename__ = "generation_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    runninghub_execution_account_ids_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    source_channel: Mapped[str] = mapped_column(
        String(30), nullable=False, default=BATCH_SOURCE_LEGACY_WEB, index=True
    )
    correlation_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    audio_mode: Mapped[str] = mapped_column(String(30), nullable=False, default="upload")
    review_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    video_review_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    request_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="ACTIVE")
    total_items: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="batches")
    items: Mapped[list["GenerationBatchItem"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="GenerationBatchItem.row_number",
    )

    __table_args__ = (
        UniqueConstraint("user_id", "request_key", name="uq_batches_user_request_key"),
    )


class GenerationBatchItem(Base):
    """Durable row-level orchestration state before and after audio is ready."""

    __tablename__ = "generation_batch_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("generation_batches.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    row_key: Mapped[str] = mapped_column(String(100), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    audio_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="AUDIO_READY"
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="TASK_CREATED"
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    merged_video_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="NOT_APPLICABLE"
    )
    merged_video_path: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    merged_video_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    merged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    merged_reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    batch: Mapped[GenerationBatch] = relationship(back_populates="items")
    generation_task: Mapped[Optional[GenerationTask]] = relationship(
        back_populates="batch_item", uselist=False
    )
    audio_task: Mapped[Optional["AudioGenerationTask"]] = relationship(
        back_populates="batch_item",
        uselist=False,
        cascade="all, delete-orphan",
    )
    segments: Mapped[list["GenerationSegment"]] = relationship(
        back_populates="batch_item",
        cascade="all, delete-orphan",
        order_by="GenerationSegment.segment_index",
    )
    long_audio_project: Mapped[Optional["LongAudioProject"]] = relationship(
        back_populates="batch_item",
        uselist=False,
    )

    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="uq_batch_items_row_number"),
        UniqueConstraint("batch_id", "row_key", name="uq_batch_items_row_key"),
    )


class StagedAsset(Base):
    """Validated upload waiting to be referenced by a batch manifest."""

    __tablename__ = "staged_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="staged_assets")


class LongAudioProject(Base):
    """Temporary long-media analysis that becomes a segmented video batch."""

    __tablename__ = "long_audio_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    batch_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("generation_batches.id", ondelete="SET NULL"),
        unique=True,
        nullable=True,
    )
    batch_item_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("generation_batch_items.id", ondelete="CASCADE"),
        unique=True,
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    workflow_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="ltx_lip_sync", index=True
    )
    review_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    script_text: Mapped[str] = mapped_column(Text, nullable=False)
    audio_path: Mapped[str] = mapped_column(String(500), nullable=False)
    audio_original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    video_path: Mapped[str] = mapped_column(String(500), nullable=False)
    video_original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False)
    plan_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    alignment_provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="funasr_http"
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=LongAudioProjectStatus.PENDING_ANALYSIS.value,
        index=True,
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    remote_lease_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    remote_worker_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    remote_lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    remote_last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    remote_metrics_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="long_audio_projects")
    batch: Mapped[Optional["GenerationBatch"]] = relationship()
    batch_item: Mapped[Optional["GenerationBatchItem"]] = relationship(
        back_populates="long_audio_project"
    )


class GenerationSegment(Base):
    """One visible RunningHub child task cut from a full script/audio row."""

    __tablename__ = "generation_segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_item_id: Mapped[str] = mapped_column(
        ForeignKey("generation_batch_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    script_text: Mapped[str] = mapped_column(Text, nullable=False)
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    audio_path: Mapped[str] = mapped_column(String(500), nullable=False)
    video_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    alignment_method: Mapped[str] = mapped_column(
        String(30), nullable=False, default="punctuation_silence"
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PENDING", index=True
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    batch_item: Mapped[GenerationBatchItem] = relationship(
        back_populates="segments"
    )
    generation_task: Mapped[Optional[GenerationTask]] = relationship(
        back_populates="segment", uselist=False
    )

    __table_args__ = (
        UniqueConstraint(
            "batch_item_id",
            "segment_index",
            name="uq_generation_segments_item_index",
        ),
    )


class AudioGenerationTask(Base):
    """Persistent script-to-audio work that hands off to video task creation."""

    __tablename__ = "audio_generation_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    config_id: Mapped[int] = mapped_column(
        ForeignKey("minimax_configs.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    batch_item_id: Mapped[str] = mapped_column(
        ForeignKey("generation_batch_items.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    voice_a_id: Mapped[str] = mapped_column(
        ForeignKey("minimax_voice_assets.id", ondelete="RESTRICT"), nullable=False
    )
    voice_b_id: Mapped[str] = mapped_column(
        ForeignKey("minimax_voice_assets.id", ondelete="RESTRICT"), nullable=False
    )
    voice_asset_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("minimax_voice_assets.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    planned_generation_task_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False
    )
    account_binding_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    credential_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    # Workbench text-to-speech is intentionally independent of the future
    # digital-human picture.  These fields are bound only when Module 4A is
    # started; legacy/full-flow batches may still populate them immediately.
    primary_kind: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    primary_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    primary_original_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    primary_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    speech_script: Mapped[str] = mapped_column(Text, nullable=False)
    pronunciation_dict_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    video_parameters_json: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    weight_a: Mapped[int] = mapped_column(Integer, nullable=False)
    weight_b: Mapped[int] = mapped_column(Integer, nullable=False)
    speed: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    pitch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language_boost: Mapped[str] = mapped_column(
        String(50), nullable=False, default="auto"
    )
    output_format: Mapped[str] = mapped_column(
        String(10), nullable=False, default="mp3"
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=AudioTaskStatus.PENDING.value, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    output_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    subtitle_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    provider_task_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, unique=True, index=True
    )
    provider_file_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    provider_submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    generation_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    alignment_method: Mapped[Optional[str]] = mapped_column(
        String(30), nullable=True
    )
    cost_confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="audio_tasks")
    config: Mapped[MiniMaxConfig] = relationship(back_populates="audio_tasks")
    batch_item: Mapped[GenerationBatchItem] = relationship(back_populates="audio_task")
    voice_a: Mapped[MiniMaxVoiceAsset] = relationship(
        foreign_keys=[voice_a_id]
    )
    voice_b: Mapped[MiniMaxVoiceAsset] = relationship(
        foreign_keys=[voice_b_id]
    )
    voice_asset: Mapped[Optional[MiniMaxVoiceAsset]] = relationship(
        foreign_keys=[voice_asset_id]
    )
    attempts: Mapped[list["AudioGenerationAttempt"]] = relationship(
        back_populates="audio_task",
        cascade="all, delete-orphan",
        order_by="AudioGenerationAttempt.version",
    )


class AudioGenerationAttempt(Base):
    """One paid MiniMax output version retained for review and audit."""

    __tablename__ = "audio_generation_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    audio_task_id: Mapped[str] = mapped_column(
        ForeignKey("audio_generation_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_task_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    provider_file_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    output_path: Mapped[str] = mapped_column(String(500), nullable=False)
    subtitle_path: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="READY"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    audio_task: Mapped[AudioGenerationTask] = relationship(
        back_populates="attempts"
    )

    __table_args__ = (
        UniqueConstraint(
            "audio_task_id",
            "version",
            name="uq_audio_attempts_task_version",
        ),
    )


class VoiceCreationTask(Base):
    """Persistent clone or blend audition that can be saved as one voice."""

    __tablename__ = "voice_creation_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    config_id: Mapped[int] = mapped_column(
        ForeignKey("minimax_configs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    voice_asset_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("minimax_voice_assets.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    account_binding_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    credential_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    preview_text: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    weight_a: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    weight_b: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    noise_reduction: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    volume_normalization: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    source_a_relative_path: Mapped[str] = mapped_column(
        String(500), nullable=False
    )
    source_a_original_name: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    source_b_relative_path: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    source_b_original_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    source_a_file_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    source_b_file_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    final_file_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    temporary_voice_a_id: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    temporary_voice_b_id: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    final_voice_id: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    preview_relative_path: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=VoiceCreationStatus.PENDING.value,
        index=True,
    )
    cost_confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    save_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="voice_creation_tasks")
    config: Mapped[MiniMaxConfig] = relationship(
        back_populates="voice_creation_tasks"
    )
    voice_asset: Mapped[Optional[MiniMaxVoiceAsset]] = relationship(
        back_populates="creation_task"
    )
