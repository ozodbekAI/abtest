"""Add atomic store/card locking, stage metrics, audit events and funding provenance.

Revision ID: 0015_ab_test_safety_invariants
Revises: 0014_wb_connection_token_fingerprint
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_ab_test_safety_invariants"
down_revision = "0014_wb_connection_token_fingerprint"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ab_tests", sa.Column("store_fingerprint", sa.String(length=64), nullable=False, server_default=""))
    op.add_column("ab_tests", sa.Column("lifecycle_lock", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("ab_tests", sa.Column("stage_views", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ab_tests", sa.Column("stage_clicks", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ab_tests", sa.Column("stage_spend_rub", sa.Float(), nullable=False, server_default="0"))
    op.add_column("ab_tests", sa.Column("funding_source", sa.String(length=16), nullable=False, server_default="auto"))
    op.create_index("ix_ab_tests_store_fingerprint", "ab_tests", ["store_fingerprint"])
    op.create_index("ix_ab_tests_lifecycle_lock", "ab_tests", ["lifecycle_lock"])
    # Legacy rows all receive the empty default before the application can
    # backfill a seller fingerprint.  Multiple historical tests for one card
    # must not make the migration itself fail on the partial unique index.
    # Keep at most the newest unfinished row locked; finished rows are safe to
    # unlock and will be assigned their seller fingerprint by migration 0017.
    op.execute("UPDATE ab_tests SET lifecycle_lock = false WHERE store_fingerprint = ''")
    op.execute("""
        WITH ranked AS (
            SELECT id, status,
                   row_number() OVER (PARTITION BY nm_id ORDER BY id DESC) AS rn
            FROM ab_tests
            WHERE store_fingerprint = ''
        )
        UPDATE ab_tests AS target
        SET lifecycle_lock = true
        FROM ranked
        WHERE target.id = ranked.id
          AND ranked.rn = 1
          AND ranked.status IN ('DRAFT', 'RUNNING', 'FAILED')
    """)
    op.create_index("uq_ab_test_store_card_open", "ab_tests", ["store_fingerprint", "nm_id"], unique=True, postgresql_where=sa.text("lifecycle_lock = true"))
    op.create_table(
        "ab_test_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("test_id", sa.Integer(), sa.ForeignKey("ab_tests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("after_state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ab_test_audit_events_test_id", "ab_test_audit_events", ["test_id"])
    op.create_index("ix_ab_test_audit_events_user_id", "ab_test_audit_events", ["user_id"])
    op.create_index("ix_ab_test_audit_events_action", "ab_test_audit_events", ["action"])


def downgrade():
    op.drop_index("ix_ab_test_audit_events_action", table_name="ab_test_audit_events")
    op.drop_index("ix_ab_test_audit_events_user_id", table_name="ab_test_audit_events")
    op.drop_index("ix_ab_test_audit_events_test_id", table_name="ab_test_audit_events")
    op.drop_table("ab_test_audit_events")
    op.drop_index("uq_ab_test_store_card_open", table_name="ab_tests")
    op.drop_index("ix_ab_tests_lifecycle_lock", table_name="ab_tests")
    op.drop_index("ix_ab_tests_store_fingerprint", table_name="ab_tests")
    for name in ("funding_source", "stage_spend_rub", "stage_clicks", "stage_views", "lifecycle_lock", "store_fingerprint"):
        op.drop_column("ab_tests", name)
