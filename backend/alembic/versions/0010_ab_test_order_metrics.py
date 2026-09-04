"""store order totals returned by WB campaign statistics

Revision ID: 0010_ab_test_order_metrics
Revises: 0009_unified_ab_test_bid_type
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0010_ab_test_order_metrics"
down_revision = "0009_unified_ab_test_bid_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        existing_ab_tests: set[str] = set()
        existing_variants: set[str] = set()
    else:
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        existing_ab_tests = {column["name"] for column in inspector.get_columns("ab_tests")}
        existing_variants = {column["name"] for column in inspector.get_columns("ab_test_variants")}

    if "last_total_orders" not in existing_ab_tests:
        op.add_column(
            "ab_tests",
            sa.Column("last_total_orders", sa.Integer(), nullable=False, server_default="0"),
        )
    if "orders" not in existing_variants:
        op.add_column(
            "ab_test_variants",
            sa.Column("orders", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    if context.is_offline_mode():
        op.drop_column("ab_test_variants", "orders")
        op.drop_column("ab_tests", "last_total_orders")
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "orders" in {column["name"] for column in inspector.get_columns("ab_test_variants")}:
        op.drop_column("ab_test_variants", "orders")
    if "last_total_orders" in {column["name"] for column in inspector.get_columns("ab_tests")}:
        op.drop_column("ab_tests", "last_total_orders")
