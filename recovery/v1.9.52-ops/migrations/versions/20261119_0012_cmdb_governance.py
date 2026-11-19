"""Add operational governance fields to CMDB assets."""
from alembic import op
import sqlalchemy as sa


revision = "20261119_0012"
down_revision = "20261119_0011"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {row["name"] for row in sa.inspect(bind).get_columns("cmdb_assets")}
    additions = (
        ("asset_class", sa.String(20), "硬件", False),
        ("company", sa.String(100), "", False),
        ("operational_status", sa.String(30), "运行", False),
        ("environment", sa.String(30), "生产", False),
        ("business_service", sa.String(200), "", False),
        ("source_of_truth", sa.String(50), "手工维护", False),
        ("last_verified_date", sa.Date(), None, True),
    )
    for name, kind, default, nullable in additions:
        if name in columns:
            continue
        kwargs = {"nullable": nullable}
        if default is not None:
            kwargs["server_default"] = default
        op.add_column("cmdb_assets", sa.Column(name, kind, **kwargs))
        op.create_index(f"ix_cmdb_assets_{name}", "cmdb_assets", [name])


def downgrade():
    raise RuntimeError("为保留 CMDB 治理字段，不自动删除新增列")
