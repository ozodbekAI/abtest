from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum as SQLEnum, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint, func, Index
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
    # Audit rows must survive test deletion; they are historical evidence, not
    # child state. The archival migration keeps the old rows readable even if
    # the parent object is later removed.
    test_id: Mapped[int | None] = mapped_column(ForeignKey("ab_tests.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, index=True, default=0, server_default="0")
    connection_id: Mapped[int] = mapped_column(Integer, index=True, default=0, server_default="0")
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


class ABTestAuditEvent(Base):
    """Append-only audit trail for safety-critical A/B-test transitions."""

    __tablename__ = "ab_test_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Audit rows must survive test deletion; they are historical evidence, not
    # child state. The archival migration keeps the old rows readable even if
    # the parent object is later removed.
    test_id: Mapped[int | None] = mapped_column(ForeignKey("ab_tests.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, index=True, default=0, server_default="0")
    connection_id: Mapped[int] = mapped_column(Integer, index=True, default=0, server_default="0")
    action: Mapped[str] = mapped_column(String(64), index=True)
    before_state: Mapped[dict] = mapped_column(JSON, default=dict)
    after_state: Mapped[dict] = mapped_column(JSON, default=dict)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    test = relationship("ABTest", back_populates="audit_events")


class ABTestAuditEventArchive(Base):
    """Historical copy of audit events retained after a test is deleted."""

    __tablename__ = "ab_test_audit_event_archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_event_id: Mapped[int] = mapped_column(Integer, index=True)
    test_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", index=True)
    connection_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True)
    action: Mapped[str] = mapped_column(String(64))
    before_state: Mapped[dict] = mapped_column(JSON, default=dict)
    after_state: Mapped[dict] = mapped_column(JSON, default=dict)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ABTestIncidentNotification(Base):
    """Durable outbox row for safety incidents.

    Provider/network failures must not disappear in a log line.  The row is
    created by the scheduler from the durable incident state and retried until
    SMTP accepts the notification.
    """

    __tablename__ = "ab_test_incident_notifications"
    __table_args__ = (UniqueConstraint("test_id", "incident_id", name="uq_ab_test_incident_notification"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_id: Mapped[int | None] = mapped_column(ForeignKey("ab_tests.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    recipient: Mapped[str] = mapped_column(String(320))
    incident_id: Mapped[str] = mapped_column(String(48), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ABTestBudgetEntry(Base):
    """Immutable distinction between funding, provider balance and spend."""

    __tablename__ = "ab_test_budget_entries"
    __table_args__ = (UniqueConstraint("test_id", "operation_id", "kind", name="uq_ab_test_budget_entry"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_id: Mapped[int | None] = mapped_column(ForeignKey("ab_tests.id", ondelete="SET NULL"), nullable=True, index=True)
    operation_id: Mapped[int | None] = mapped_column(ForeignKey("ab_test_operations.id", ondelete="SET NULL"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    amount_rub: Mapped[float] = mapped_column(Numeric(18, 2), default=0, server_default="0")
    provider_balance_rub: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="provider", server_default="provider")
    provider_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ABTest(Base):
    __tablename__ = "ab_tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("wb_connections.id", ondelete="CASCADE"), index=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, index=True)
    store_fingerprint: Mapped[str] = mapped_column(String(64), default="", server_default="", index=True)
    lifecycle_lock: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", index=True)
    title: Mapped[str] = mapped_column(String(512))
    status: Mapped[ABTestStatus] = mapped_column(SQLEnum(ABTestStatus, name="ab_test_status"), default=ABTestStatus.DRAFT, index=True)

    wb_campaign_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    skip_current_photo: Mapped[bool] = mapped_column(Boolean, default=True)
    keep_winner_as_main: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
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
    last_total_spend_rub: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    # Last campaign totals that have been durably settled into a specific
    # photo stage. Live fullstats snapshots are never attributed directly to
    # a photo; they remain provisional until the campaign is paused and the
    # totals become stable.
    settled_total_views: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    settled_total_clicks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    settled_total_orders: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    settled_total_spend_rub: Mapped[float] = mapped_column(Numeric(18, 2), default=0, server_default="0")
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
    unallocated_spend_rub: Mapped[float] = mapped_column(Numeric(18, 2), default=0, server_default="0")
    stage_views: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stage_clicks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stage_spend_rub: Mapped[float] = mapped_column(Numeric(18, 2), default=0, server_default="0")
    funding_source: Mapped[str] = mapped_column(String(16), default="auto", server_default="auto")

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Timeout is measured from the beginning of the currently served stage,
    # not from the lifetime of the whole experiment.
    stage_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    audit_events: Mapped[list[ABTestAuditEvent]] = relationship(
        back_populates="test", cascade="all, delete-orphan", order_by="ABTestAuditEvent.id"
    )

Index(
    "uq_ab_test_store_card_open",
    ABTest.store_fingerprint,
    ABTest.nm_id,
    unique=True,
    postgresql_where=ABTest.lifecycle_lock.is_(True),
    sqlite_where=ABTest.lifecycle_lock.is_(True),
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
    spend_rub: Mapped[float] = mapped_column(Numeric(18, 2), default=0, server_default="0")
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    test: Mapped[ABTest] = relationship(back_populates="variants")

    @property
    def ctr(self) -> float:
        return round((self.clicks / self.views) * 100, 4) if self.views else 0.0
