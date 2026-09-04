"""add A/B test workflow tables

Revision ID: 0003_ab_tests
Revises: 0002_multi_wb_connections
"""

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_ab_tests"
down_revision = "0002_multi_wb_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Development may have started the app with AUTO_CREATE_TABLES=true before
    # Alembic was introduced. In that case create_all already created both A/B
    # tables, while alembic_version still points to 0002. Reuse that schema and
    # let the following migrations add only their missing columns/constraints.
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        if inspector.has_table("ab_tests") or inspector.has_table("ab_test_variants"):
            return

    # Create the PostgreSQL enum explicitly. ``create_type=False`` on the
    # column type prevents op.create_table from emitting a second CREATE TYPE
    # in offline SQL and during normal online migrations.
    status_enum = postgresql.ENUM(
        "DRAFT", "RUNNING", "FINISHED", "FAILED", "STOPPED", name="ab_test_status", create_type=False
    )
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "ab_tests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.Integer(), sa.ForeignKey("wb_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("nm_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", status_enum, nullable=False, server_default="DRAFT"),
        sa.Column("wb_campaign_id", sa.BigInteger(), nullable=True),
        sa.Column("skip_current_photo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("keep_winner_as_main", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("delete_test_media", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("views_per_variant", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("cpm_rub", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("budget_rub", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("placement", sa.String(length=24), nullable=False, server_default="search"),
        sa.Column("current_variant_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("winner_variant_order", sa.Integer(), nullable=True),
        sa.Column("last_total_views", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_total_clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_total_spend_rub", sa.Float(), nullable=False, server_default="0"),
        sa.Column("original_media", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("original_main_backup_path", sa.String(length=512), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ab_tests_user_id", "ab_tests", ["user_id"])
    op.create_index("ix_ab_tests_connection_id", "ab_tests", ["connection_id"])
    op.create_index("ix_ab_tests_nm_id", "ab_tests", ["nm_id"])
    op.create_index("ix_ab_tests_status", "ab_tests", ["status"])
    op.create_index("ix_ab_tests_wb_campaign_id", "ab_tests", ["wb_campaign_id"])

    op.create_table(
        "ab_test_variants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("test_id", sa.Integer(), sa.ForeignKey("ab_tests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False, server_default="upload"),
        sa.Column("file_path", sa.String(length=512), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("wb_url", sa.String(length=2048), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("views", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("spend_rub", sa.Float(), nullable=False, server_default="0"),
        sa.Column("is_winner", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("test_id", "position", name="uq_ab_test_variant_position"),
    )
    op.create_index("ix_ab_test_variants_test_id", "ab_test_variants", ["test_id"])


def downgrade() -> None:
    op.drop_table("ab_test_variants")
    op.drop_table("ab_tests")
    sa.Enum(name="ab_test_status").drop(op.get_bind(), checkfirst=True)
