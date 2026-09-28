"""Add durable safety-incident notification outbox.

Revision ID: 0018_incident_notification_outbox
Revises: 0017_wb_seller_identity
"""
from alembic import op
import sqlalchemy as sa


revision = "0018_incident_notification_outbox"
down_revision = "0017_wb_seller_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ab_test_incident_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("test_id", sa.Integer(), sa.ForeignKey("ab_tests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("incident_id", sa.String(48), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("test_id", "incident_id", name="uq_ab_test_incident_notification"),
    )
    op.create_index("ix_ab_test_incident_notifications_test_id", "ab_test_incident_notifications", ["test_id"])
    op.create_index("ix_ab_test_incident_notifications_user_id", "ab_test_incident_notifications", ["user_id"])
    op.create_index("ix_ab_test_incident_notifications_incident_id", "ab_test_incident_notifications", ["incident_id"])
    op.create_index("ix_ab_test_incident_notifications_status", "ab_test_incident_notifications", ["status"])
    op.create_index("ix_ab_test_incident_notifications_next_attempt_at", "ab_test_incident_notifications", ["next_attempt_at"])


def downgrade():
    op.drop_index("ix_ab_test_incident_notifications_next_attempt_at", table_name="ab_test_incident_notifications")
    op.drop_index("ix_ab_test_incident_notifications_status", table_name="ab_test_incident_notifications")
    op.drop_index("ix_ab_test_incident_notifications_incident_id", table_name="ab_test_incident_notifications")
    op.drop_index("ix_ab_test_incident_notifications_user_id", table_name="ab_test_incident_notifications")
    op.drop_index("ix_ab_test_incident_notifications_test_id", table_name="ab_test_incident_notifications")
    op.drop_table("ab_test_incident_notifications")
