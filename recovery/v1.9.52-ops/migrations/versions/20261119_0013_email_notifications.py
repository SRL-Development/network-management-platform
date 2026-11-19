"""Add persistent acknowledgement state for modular email notifications."""
from alembic import op
import sqlalchemy as sa


revision = "20261119_0013"
down_revision = "20261119_0012"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "notification_alerts" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "notification_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("module", sa.String(50), nullable=False),
        sa.Column("event_key", sa.String(300), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_by", sa.String(100), nullable=True),
        sa.UniqueConstraint("module", "event_key", name="uq_notification_module_event"),
    )
    for name in ("module", "is_active", "last_seen_at", "last_sent_at", "acknowledged_at"):
        op.create_index(f"ix_notification_alerts_{name}", "notification_alerts", [name])


def downgrade():
    raise RuntimeError("为保留邮件提醒确认历史，不自动删除 notification_alerts")
