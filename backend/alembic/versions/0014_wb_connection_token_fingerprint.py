"""Add token fingerprint to detect the same WB store across users/connections.

Revision ID: 0014_wb_connection_token_fingerprint
Revises: 0013_stage_timeout_and_winner_safety
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_wb_connection_token_fingerprint"
down_revision = "0013_stage_timeout_and_winner_safety"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "wb_connections",
        sa.Column("token_fingerprint", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_wb_connections_token_fingerprint",
        "wb_connections",
        ["token_fingerprint"],
    )
    # Eslatma: ustun qo'shilgach, mavjud ulanishlar uchun token_fingerprint
    # bo'sh ("") qoladi — ularni to'ldirish uchun bir martalik backfill
    # skripti kerak (masalan: har bir ulanish uchun tokenni deshifrlab,
    # SHA-256 hisoblab, UPDATE qilish). Yangi/qayta saqlanadigan tokenlar
    # uchun bu maydon avtomatik to'ladi (app/services/wb_token_service.py).


def downgrade():
    op.drop_index("ix_wb_connections_token_fingerprint", table_name="wb_connections")
    op.drop_column("wb_connections", "token_fingerprint")
