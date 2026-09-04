"""persist exact A/B media slot state for safe swaps and rollback

Revision ID: 0007_ab_test_media_state
Revises: 0006_wb_rate_limits
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0007_ab_test_media_state"
down_revision = "0006_wb_rate_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not context.is_offline_mode():
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("ab_tests")}
        if "media_state" in columns:
            return
    op.add_column(
        "ab_tests",
        sa.Column("media_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("ab_tests", "media_state")
