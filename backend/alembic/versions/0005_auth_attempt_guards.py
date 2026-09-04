"""add login and verification attempt guards

Revision ID: 0005_auth_attempt_guards
Revises: 0004_ab_test_guards
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0005_auth_attempt_guards"
down_revision = "0004_ab_test_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        user_columns: set[str] = set()
        code_columns: set[str] = set()
    else:
        inspector = sa.inspect(op.get_bind())
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        code_columns = {column["name"] for column in inspector.get_columns("email_verification_codes")}
    if "failed_login_attempts" not in user_columns:
        op.add_column("users", sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"))
    if "locked_until" not in user_columns:
        op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    if "attempts" not in code_columns:
        op.add_column("email_verification_codes", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("email_verification_codes", "attempts")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_attempts")
