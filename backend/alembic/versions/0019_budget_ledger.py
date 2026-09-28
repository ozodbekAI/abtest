"""Add an immutable funding/provider-spend ledger.

Revision ID: 0019_budget_ledger
Revises: 0018_incident_notification_outbox
"""
from alembic import op
import sqlalchemy as sa


revision = "0019_budget_ledger"
down_revision = "0018_incident_notification_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ab_test_budget_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("test_id", sa.Integer(), sa.ForeignKey("ab_tests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("operation_id", sa.Integer(), sa.ForeignKey("ab_test_operations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("amount_rub", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("provider_balance_rub", sa.Numeric(18, 2), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="provider"),
        sa.Column("provider_reference", sa.String(128), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("test_id", "operation_id", "kind", name="uq_ab_test_budget_entry"),
    )
    op.create_index("ix_ab_test_budget_entries_test_id", "ab_test_budget_entries", ["test_id"])
    op.create_index("ix_ab_test_budget_entries_operation_id", "ab_test_budget_entries", ["operation_id"])
    op.create_index("ix_ab_test_budget_entries_kind", "ab_test_budget_entries", ["kind"])
    op.create_index("ix_ab_test_budget_entries_created_at", "ab_test_budget_entries", ["created_at"])


def downgrade():
    op.drop_index("ix_ab_test_budget_entries_created_at", table_name="ab_test_budget_entries")
    op.drop_index("ix_ab_test_budget_entries_kind", table_name="ab_test_budget_entries")
    op.drop_index("ix_ab_test_budget_entries_operation_id", table_name="ab_test_budget_entries")
    op.drop_index("ix_ab_test_budget_entries_test_id", table_name="ab_test_budget_entries")
    op.drop_table("ab_test_budget_entries")
