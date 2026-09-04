"""add distributed WB API rate-limit reservations

Revision ID: 0006_wb_rate_limits
Revises: 0005_auth_attempt_guards
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0006_wb_rate_limits"
down_revision = "0005_auth_attempt_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table("wb_api_rate_limits"):
        return
    op.create_table(
        "wb_api_rate_limits",
        sa.Column("scope", sa.String(length=128), primary_key=True),
        sa.Column("next_allowed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("wb_api_rate_limits")
