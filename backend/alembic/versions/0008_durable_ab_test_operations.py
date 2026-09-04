"""add durable A/B operation journal and incident state

Revision ID: 0008_durable_ab_test_operations
Revises: 0007_ab_test_media_state
"""

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0008_durable_ab_test_operations"
down_revision = "0007_ab_test_media_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if context.is_offline_mode():
        existing_columns: set[str] = set()
        has_operations = False
    else:
        inspector = sa.inspect(bind)
        existing_columns = {column["name"] for column in inspector.get_columns("ab_tests")}
        has_operations = inspector.has_table("ab_test_operations")

    for name, column in (
        ("operation_state", sa.Column("operation_state", sa.String(length=40), nullable=False, server_default="ready")),
        ("campaign_state", sa.Column("campaign_state", sa.String(length=32), nullable=False, server_default="not_created")),
        ("media_status", sa.Column("media_status", sa.String(length=32), nullable=False, server_default="original")),
        ("stats_quality", sa.Column("stats_quality", sa.String(length=32), nullable=False, server_default="not_started")),
        ("incident_id", sa.Column("incident_id", sa.String(length=48), nullable=True)),
        ("unallocated_views", sa.Column("unallocated_views", sa.Integer(), nullable=False, server_default="0")),
        ("unallocated_clicks", sa.Column("unallocated_clicks", sa.Integer(), nullable=False, server_default="0")),
        ("unallocated_spend_rub", sa.Column("unallocated_spend_rub", sa.Float(), nullable=False, server_default="0")),
    ):
        if name not in existing_columns:
            op.add_column("ab_tests", column)

    if has_operations:
        return

    operation_status = postgresql.ENUM(
        "PREPARED",
        "IN_PROGRESS",
        "SUCCEEDED",
        "FAILED",
        "RECONCILIATION_REQUIRED",
        name="ab_test_operation_status",
        create_type=False,
    )
    operation_status.create(bind, checkfirst=True)
    op.create_table(
        "ab_test_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("test_id", sa.Integer(), sa.ForeignKey("ab_tests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.Integer(), sa.ForeignKey("wb_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("nm_id", sa.BigInteger(), nullable=False),
        sa.Column("operation_key", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", operation_status, nullable=False, server_default="PREPARED"),
        sa.Column("external_id", sa.BigInteger(), nullable=True),
        sa.Column("request_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("response_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("operation_key", name="uq_ab_test_operations_operation_key"),
    )
    op.create_index("ix_ab_test_operations_test_id", "ab_test_operations", ["test_id"])
    op.create_index("ix_ab_test_operations_user_id", "ab_test_operations", ["user_id"])
    op.create_index("ix_ab_test_operations_connection_id", "ab_test_operations", ["connection_id"])
    op.create_index("ix_ab_test_operations_nm_id", "ab_test_operations", ["nm_id"])
    op.create_index("ix_ab_test_operations_status", "ab_test_operations", ["status"])
    op.create_index("ix_ab_test_operations_external_id", "ab_test_operations", ["external_id"])
    op.create_index("ix_ab_tests_incident_id", "ab_tests", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_ab_tests_incident_id", table_name="ab_tests")
    op.drop_index("ix_ab_test_operations_external_id", table_name="ab_test_operations")
    op.drop_index("ix_ab_test_operations_status", table_name="ab_test_operations")
    op.drop_index("ix_ab_test_operations_nm_id", table_name="ab_test_operations")
    op.drop_index("ix_ab_test_operations_connection_id", table_name="ab_test_operations")
    op.drop_index("ix_ab_test_operations_user_id", table_name="ab_test_operations")
    op.drop_index("ix_ab_test_operations_test_id", table_name="ab_test_operations")
    op.drop_table("ab_test_operations")
    sa.Enum(name="ab_test_operation_status").drop(op.get_bind(), checkfirst=True)
    for name in (
        "unallocated_spend_rub",
        "unallocated_clicks",
        "unallocated_views",
        "incident_id",
        "stats_quality",
        "media_status",
        "campaign_state",
        "operation_state",
    ):
        op.drop_column("ab_tests", name)
