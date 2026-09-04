from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum as SQLEnum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ABTestStatus(str, enum.Enum):
    DRAFT = "draft"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"


class ABTestOperationStatus(str, enum.Enum):
    PREPARED = "prepared"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ABTestOperation(Base):
    """Durable journal for effects that can happen outside our database.

    A row is written before the first WB mutation. This is intentionally
    separate from the test status: an experiment may be marked failed while
    its campaign result is still unknown and must not be repeated blindly.
    """

    __tablename__ = "ab_test_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("ab_tests.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("wb_connections.id", ondelete="CASCADE"), index=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, index=True)
    operation_key: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[ABTestOperationStatus] = mapped_column(
        SQLEnum(ABTestOperationStatus, name="ab_test_operation_status"),
        default=ABTestOperationStatus.PREPARED,
        index=True,
    )
    external_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    request_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    response_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    test = relationship("ABTest", back_populates="operations")


class ABTest(Base):
    __tablename__ = "ab_tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("wb_connections.id", ondelete="CASCADE"), index=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(512))
    status: Mapped[ABTestStatus] = mapped_column(SQLEnum(ABTestStatus, name="ab_test_status"), default=ABTestStatus.DRAFT, index=True)

    wb_campaign_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    skip_current_photo: Mapped[bool] = mapped_column(Boolean, default=True)
    keep_winner_as_main: Mapped[bool] = mapped_column(Boolean, default=True)
    delete_test_media: Mapped[bool] = mapped_column(Boolean, default=True)
    views_per_variant: Mapped[int] = mapped_column(Integer, default=1000)
    cpm_rub: Mapped[int] = mapped_column(Integer, default=300)
    budget_rub: Mapped[int] = mapped_column(Integer, default=1000)
    # New experiments use WB's unified bid model. Legacy rows are marked as
    # manual by the migration so their already-created campaigns keep using
    # their original contract when they are resumed.
    bid_type: Mapped[str] = mapped_column(String(16), default="unified", server_default="unified")
    placement: Mapped[str] = mapped_column(String(24), default="combined", server_default="combined")

    current_variant_order: Mapped[int] = mapped_column(Integer, default=0)
    winner_variant_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winner_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_total_views: Mapped[int] = mapped_column(Integer, default=0)
    last_total_clicks: Mapped[int] = mapped_column(Integer, default=0)
    last_total_orders: Mapped[int] = mapped_column(Integer, default=0)
    last_total_spend_rub: Mapped[float] = mapped_column(Float, default=0)
    original_media: Mapped[list] = mapped_column(JSON, default=list)
    # Persist the exact slot-level backup/shadow map used by the swap engine.
    # URL order alone is not enough: WB can keep a CDN URL while replacing the
    # bytes behind it, so every touched slot must be recoverable independently.
    media_state: Mapped[dict] = mapped_column(JSON, default=dict)
    original_main_backup_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # These fields make an incident understandable without interpreting one
    # generic ``failed`` flag. They are updated independently because stopping
    # a campaign and restoring media are separate safety obligations.
    operation_state: Mapped[str] = mapped_column(String(40), default="ready", server_default="ready")
    campaign_state: Mapped[str] = mapped_column(String(32), default="not_created", server_default="not_created")
    media_status: Mapped[str] = mapped_column(String(32), default="original", server_default="original")
    stats_quality: Mapped[str] = mapped_column(String(32), default="not_started", server_default="not_started")
    incident_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    unallocated_views: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    unallocated_clicks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    unallocated_spend_rub: Mapped[float] = mapped_column(Float, default=0, server_default="0")

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", backref="ab_tests")
    connection = relationship("WBConnection", backref="ab_tests")
    variants: Mapped[list["ABTestVariant"]] = relationship(
        back_populates="test", cascade="all, delete-orphan", order_by="ABTestVariant.position"
    )
    operations: Mapped[list[ABTestOperation]] = relationship(
        back_populates="test", cascade="all, delete-orphan", order_by="ABTestOperation.id"
    )


class ABTestVariant(Base):
    __tablename__ = "ab_test_variants"
    __table_args__ = (UniqueConstraint("test_id", "position", name="uq_ab_test_variant_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("ab_tests.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    source_type: Mapped[str] = mapped_column(String(24), default="upload")
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    wb_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    views: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    spend_rub: Mapped[float] = mapped_column(Float, default=0)
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    test: Mapped[ABTest] = relationship(back_populates="variants")

    @property
    def ctr(self) -> float:
        return round((self.clicks / self.views) * 100, 4) if self.views else 0.0
