"""Finalize stage attribution, audit retention and runtime safety fields.

Revision ID: 0016_final_safety_and_audit_hardening
Revises: 0015_ab_test_safety_invariants
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_final_safety_and_audit_hardening"
down_revision = "0015_ab_test_safety_invariants"
branch_labels = None
depends_on = None


def upgrade():
    for name, col in (
        ("settled_total_views", sa.Column("settled_total_views", sa.Integer(), nullable=False, server_default="0")),
        ("settled_total_clicks", sa.Column("settled_total_clicks", sa.Integer(), nullable=False, server_default="0")),
        ("settled_total_orders", sa.Column("settled_total_orders", sa.Integer(), nullable=False, server_default="0")),
        ("settled_total_spend_rub", sa.Column("settled_total_spend_rub", sa.Float(), nullable=False, server_default="0")),
    ):
        op.add_column("ab_tests", col)

    # Preserve audit history outside the lifecycle of the mutable test row.
    op.add_column("ab_test_audit_events", sa.Column("nm_id", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("ab_test_audit_events", sa.Column("connection_id", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_ab_test_audit_events_nm_id", "ab_test_audit_events", ["nm_id"])
    op.create_index("ix_ab_test_audit_events_connection_id", "ab_test_audit_events", ["connection_id"])
    op.create_table(
        "ab_test_audit_event_archive",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("original_event_id", sa.Integer(), nullable=False, index=True),
        sa.Column("test_id", sa.Integer(), nullable=True, index=True),
        sa.Column("user_id", sa.Integer(), nullable=True, index=True),
        sa.Column("nm_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("connection_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("after_state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade():
    op.drop_table("ab_test_audit_event_archive")
    op.drop_index("ix_ab_test_audit_events_connection_id", table_name="ab_test_audit_events")
    op.drop_index("ix_ab_test_audit_events_nm_id", table_name="ab_test_audit_events")
    op.drop_column("ab_test_audit_events", "connection_id")
    op.drop_column("ab_test_audit_events", "nm_id")
    for name in ("settled_total_spend_rub", "settled_total_orders", "settled_total_clicks", "settled_total_views"):
        op.drop_column("ab_tests", name)
