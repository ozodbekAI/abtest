"""Use fixed-point ruble amounts for all A/B-test spend fields.

Revision ID: 0020_money_as_numeric
Revises: 0019_budget_ledger
"""
from alembic import op
import sqlalchemy as sa


revision = "0020_money_as_numeric"
down_revision = "0019_budget_ledger"
branch_labels = None
depends_on = None


def upgrade():
    for table, column in (
        ("ab_tests", "last_total_spend_rub"),
        ("ab_tests", "settled_total_spend_rub"),
        ("ab_tests", "unallocated_spend_rub"),
        ("ab_tests", "stage_spend_rub"),
        ("ab_test_variants", "spend_rub"),
    ):
        op.alter_column(
            table,
            column,
            type_=sa.Numeric(18, 2),
            existing_type=sa.Float(),
            postgresql_using=f"ROUND({column}::numeric, 2)",
        )


def downgrade():
    for table, column in (
        ("ab_test_variants", "spend_rub"),
        ("ab_tests", "stage_spend_rub"),
        ("ab_tests", "unallocated_spend_rub"),
        ("ab_tests", "settled_total_spend_rub"),
        ("ab_tests", "last_total_spend_rub"),
    ):
        op.alter_column(table, column, type_=sa.Float(), existing_type=sa.Numeric(18, 2))
