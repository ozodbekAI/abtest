"""Persist remotely verified WB seller identity and write access.

Revision ID: 0017_wb_seller_identity
Revises: 0016_final_safety_and_audit_hardening
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_wb_seller_identity"
down_revision = "0016_final_safety_and_audit_hardening"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("wb_connections", sa.Column("seller_id", sa.String(128), nullable=True))
    op.add_column("wb_connections", sa.Column("write_access", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_wb_connections_seller_id", "wb_connections", ["seller_id"])
    # Historical ping-only readiness is not proof of write permission. Existing
    # credentials must be verified against WB before a new financial operation.
    op.execute("UPDATE wb_connections SET ready_for_ab_tests = false")


def downgrade():
    op.drop_index("ix_wb_connections_seller_id", table_name="wb_connections")
    op.drop_column("wb_connections", "write_access")
    op.drop_column("wb_connections", "seller_id")
