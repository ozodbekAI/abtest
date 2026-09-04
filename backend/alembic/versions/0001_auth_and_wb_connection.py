"""create auth and wb connection tables

Revision ID: 0001_auth_and_wb_connection
Revises:
"""
from alembic import op
import sqlalchemy as sa


revision = "0001_auth_and_wb_connection"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("first_name", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("last_name", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_is_verified", "users", ["is_verified"])
    op.create_index("ix_users_is_active", "users", ["is_active"])

    op.create_table(
        "email_verification_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_email_verification_codes_user_id", "email_verification_codes", ["user_id"])
    op.create_index("ix_email_verification_codes_purpose", "email_verification_codes", ["purpose"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)
    op.create_index("ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"])

    op.create_table(
        "wb_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_encrypted", sa.Text(), nullable=False),
        sa.Column("token_last4", sa.String(length=4), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("content_access", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("promotion_access", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("analytics_access", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("statistics_access", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ready_for_ab_tests", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ping_results", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index("ix_wb_connections_user_id", "wb_connections", ["user_id"], unique=True)
    op.create_index("ix_wb_connections_status", "wb_connections", ["status"])


def downgrade() -> None:
    op.drop_table("wb_connections")
    op.drop_table("refresh_tokens")
    op.drop_table("email_verification_codes")
    op.drop_table("users")

