"""Stage-local timeout and explicit winner safety.

Revision ID: 0013_stage_timeout_and_winner_safety
Revises: 0012_admin_audit_logs
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_stage_timeout_and_winner_safety"
down_revision = "0012_admin_audit_logs"
branch_labels = None
depends_on = None

def upgrade():
    # Preserve published revision identifiers while allowing names longer
    # than Alembic's default 32 characters on PostgreSQL.
    op.alter_column("alembic_version", "version_num", existing_type=sa.String(32), type_=sa.String(128), existing_nullable=False)
    op.add_column("ab_tests", sa.Column("stage_started_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE ab_tests SET stage_started_at = started_at WHERE stage_started_at IS NULL")
    op.alter_column("ab_tests", "keep_winner_as_main", server_default=sa.false())
    op.execute("UPDATE ab_tests SET keep_winner_as_main = FALSE")

def downgrade():
    op.drop_column("ab_tests", "stage_started_at")
    op.alter_column("ab_tests", "keep_winner_as_main", server_default=sa.true())
