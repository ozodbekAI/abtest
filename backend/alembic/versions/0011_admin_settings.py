"""add administrator role and global application settings

Revision ID: 0011_admin_settings
Revises: 0010_ab_test_order_metrics
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0011_admin_settings"
down_revision = "0010_ab_test_order_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if context.is_offline_mode():
        user_columns: set[str] = set()
        has_settings = False
    else:
        inspector = sa.inspect(bind)
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        has_settings = inspector.has_table("app_settings")

    if "is_admin" not in user_columns:
        op.add_column(
            "users",
            sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index("ix_users_is_admin", "users", ["is_admin"])

    if not has_settings:
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(length=64), primary_key=True),
            sa.Column("value", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    op.execute(
        sa.text(
            "INSERT INTO app_settings (key, value) VALUES ('registration_enabled', '{\"value\": true}') "
            "ON CONFLICT (key) DO NOTHING"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not context.is_offline_mode() and sa.inspect(bind).has_table("app_settings"):
        op.drop_table("app_settings")
    if context.is_offline_mode() or "is_admin" in {column["name"] for column in sa.inspect(bind).get_columns("users")}:
        op.drop_index("ix_users_is_admin", table_name="users")
        op.drop_column("users", "is_admin")
