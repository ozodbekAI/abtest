"""store the WB bid model used by each A/B campaign

Revision ID: 0009_unified_ab_test_bid_type
Revises: 0008_durable_ab_test_operations
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0009_unified_ab_test_bid_type"
down_revision = "0008_durable_ab_test_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Schema inspection is unavailable while Alembic renders offline SQL.
    # The migration is ordered after the table creation, so emitting the
    # add-column operation is safe in that mode; on a live database retain
    # the idempotent inspection for existing installations.
    if context.is_offline_mode():
        columns = set()
    else:
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        columns = {column["name"] for column in inspector.get_columns("ab_tests")}

    if "bid_type" not in columns:
        op.add_column(
            "ab_tests",
            sa.Column("bid_type", sa.String(length=16), nullable=False, server_default="unified"),
        )

    # Before this column existed, this application created every campaign as
    # manual and stored its placement as search/recommendations. Preserve
    # those campaigns so a retry never sends a unified bid to a manual WB
    # campaign. New rows use unified + combined.
    op.execute(
        sa.text(
            "UPDATE ab_tests "
            "SET bid_type = 'manual' "
            "WHERE placement IN ('search', 'recommendations') "
            "AND bid_type = 'unified'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE ab_tests "
            "SET placement = 'combined' "
            "WHERE bid_type = 'unified'"
        )
    )

    op.alter_column("ab_tests", "bid_type", server_default="unified")
    op.alter_column("ab_tests", "placement", server_default="combined")


def downgrade() -> None:
    if context.is_offline_mode():
        columns = {"bid_type"}
    else:
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        columns = {column["name"] for column in inspector.get_columns("ab_tests")}
    if "bid_type" in columns:
        op.drop_column("ab_tests", "bid_type")
