"""allow multiple WB connections per user and name stores

Revision ID: 0002_multi_wb_connections
Revises: 0001_auth_and_wb_connection
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_multi_wb_connections"
down_revision = "0001_auth_and_wb_connection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wb_connections", sa.Column("store_name", sa.String(length=120), nullable=True))
    op.execute(
        sa.text(
            """
            WITH numbered AS (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id) AS number
                FROM wb_connections
            )
            UPDATE wb_connections AS connection
            SET store_name = 'Магазин ' || numbered.number
            FROM numbered
            WHERE connection.id = numbered.id
            """
        )
    )
    op.alter_column("wb_connections", "store_name", nullable=False, server_default="Магазин")
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE constraint_name text;
            BEGIN
                SELECT c.conname INTO constraint_name
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = 'wb_connections'
                  AND c.contype = 'u'
                  AND pg_get_constraintdef(c.oid) = 'UNIQUE (user_id)'
                LIMIT 1;
                IF constraint_name IS NOT NULL THEN
                    EXECUTE format('ALTER TABLE wb_connections DROP CONSTRAINT %I', constraint_name);
                END IF;
            END $$;
            """
        )
    )
    op.drop_index("ix_wb_connections_user_id", table_name="wb_connections")
    op.create_index("ix_wb_connections_user_id", "wb_connections", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_wb_connections_user_id", table_name="wb_connections")
    op.create_index("ix_wb_connections_user_id", "wb_connections", ["user_id"], unique=True)
    op.create_unique_constraint("wb_connections_user_id_key", "wb_connections", ["user_id"])
    op.drop_column("wb_connections", "store_name")
