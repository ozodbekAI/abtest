"""add A/B outcome and running-card guard

Revision ID: 0004_ab_test_guards
Revises: 0003_ab_tests
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0004_ab_test_guards"
down_revision = "0003_ab_tests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not context.is_offline_mode():
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("ab_tests")}
        if "winner_decision" in columns:
            return
    op.add_column("ab_tests", sa.Column("winner_decision", sa.String(length=32), nullable=True))
    # Make the index creation safe for databases that already contain an
    # accidental duplicate running test. Keep the oldest run and mark the
    # others failed so no campaign can continue without a single owner.
    op.execute(
        sa.text(
            """
            WITH duplicates AS (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY connection_id, nm_id ORDER BY id
                ) AS row_number
                FROM ab_tests
                WHERE status = 'RUNNING'::ab_test_status
            )
            UPDATE ab_tests AS test
            SET status = 'FAILED'::ab_test_status,
                winner_decision = 'test_interrupted',
                last_error = 'Дубликат активного теста закрыт миграцией'
            FROM duplicates
            WHERE test.id = duplicates.id AND duplicates.row_number > 1
            """
        )
    )
    # One card must never be controlled by two running experiments in the same
    # WB connection. Drafts are intentionally allowed so a user can prepare a
    # replacement before starting it.
    op.create_index(
        "uq_ab_tests_running_connection_nm",
        "ab_tests",
        ["connection_id", "nm_id"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'::ab_test_status"),
    )


def downgrade() -> None:
    op.drop_index("uq_ab_tests_running_connection_nm", table_name="ab_tests")
    op.drop_column("ab_tests", "winner_decision")
