"""Add CMDB lifecycle, multi-IP, multi-serial and entitlement tables."""
from alembic import op
import sqlalchemy as sa

revision = "20261119_0011"
down_revision = "20260925_0010"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "cmdb_assets" not in tables:
        op.create_table(
            "cmdb_assets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("asset_type", sa.String(50), nullable=False, server_default="网络设备"),
            sa.Column("brand", sa.String(100), nullable=False, server_default=""),
            sa.Column("model", sa.String(200), nullable=False, server_default=""),
            sa.Column("asset_tag", sa.String(100), nullable=False, server_default=""),
            sa.Column("linked_device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="SET NULL"), nullable=True),
            sa.Column("linked_server_id", sa.Integer(), sa.ForeignKey("server_assets.id", ondelete="SET NULL"), nullable=True),
            sa.Column("lifecycle_status", sa.String(30), nullable=False, server_default="在用"),
            sa.Column("criticality", sa.String(20), nullable=False, server_default="一般"),
            sa.Column("purchase_date", sa.Date(), nullable=True),
            sa.Column("initial_expiry_date", sa.Date(), nullable=True),
            sa.Column("vendor", sa.String(200), nullable=False, server_default=""),
            sa.Column("purchase_order", sa.String(100), nullable=False, server_default=""),
            sa.Column("purchase_cost", sa.Float(), nullable=True),
            sa.Column("currency", sa.String(10), nullable=False, server_default="CNY"),
            sa.Column("department", sa.String(100), nullable=False, server_default=""),
            sa.Column("owner", sa.String(100), nullable=False, server_default=""),
            sa.Column("location", sa.String(300), nullable=False, server_default=""),
            sa.Column("notes", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        for column in ("name", "asset_type", "asset_tag", "linked_device_id", "linked_server_id", "lifecycle_status", "criticality", "purchase_date", "initial_expiry_date", "department", "owner"):
            op.create_index(f"ix_cmdb_assets_{column}", "cmdb_assets", [column])
    if "cmdb_ip_addresses" not in tables:
        op.create_table(
            "cmdb_ip_addresses",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("cmdb_assets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("address", sa.String(45), nullable=False),
            sa.Column("purpose", sa.String(100), nullable=False, server_default="管理"),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.UniqueConstraint("asset_id", "address", name="uq_cmdb_asset_ip"),
        )
        op.create_index("ix_cmdb_ip_addresses_asset_id", "cmdb_ip_addresses", ["asset_id"])
        op.create_index("ix_cmdb_ip_addresses_address", "cmdb_ip_addresses", ["address"])
    if "cmdb_serial_numbers" not in tables:
        op.create_table(
            "cmdb_serial_numbers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("cmdb_assets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("serial_number", sa.String(200), nullable=False),
            sa.Column("kind", sa.String(50), nullable=False, server_default="主机序列号"),
            sa.Column("label", sa.String(100), nullable=False, server_default=""),
            sa.UniqueConstraint("asset_id", "serial_number", name="uq_cmdb_asset_serial"),
        )
        op.create_index("ix_cmdb_serial_numbers_asset_id", "cmdb_serial_numbers", ["asset_id"])
        op.create_index("ix_cmdb_serial_numbers_serial_number", "cmdb_serial_numbers", ["serial_number"])
    if "cmdb_entitlements" not in tables:
        op.create_table(
            "cmdb_entitlements",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("cmdb_assets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("entitlement_type", sa.String(50), nullable=False, server_default="硬件维保"),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("vendor", sa.String(200), nullable=False, server_default=""),
            sa.Column("contract_number", sa.String(100), nullable=False, server_default=""),
            sa.Column("start_date", sa.Date(), nullable=True),
            sa.Column("end_date", sa.Date(), nullable=True),
            sa.Column("reminder_days", sa.Integer(), nullable=False, server_default="90"),
            sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        )
        op.create_index("ix_cmdb_entitlements_asset_id", "cmdb_entitlements", ["asset_id"])
        op.create_index("ix_cmdb_entitlements_entitlement_type", "cmdb_entitlements", ["entitlement_type"])
        op.create_index("ix_cmdb_entitlements_end_date", "cmdb_entitlements", ["end_date"])
    if "cmdb_lifecycle_events" not in tables:
        op.create_table(
            "cmdb_lifecycle_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("cmdb_assets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("stage", sa.String(30), nullable=False),
            sa.Column("event_date", sa.Date(), nullable=False),
            sa.Column("operator", sa.String(100), nullable=False, server_default=""),
            sa.Column("notes", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_cmdb_lifecycle_events_asset_id", "cmdb_lifecycle_events", ["asset_id"])
        op.create_index("ix_cmdb_lifecycle_events_stage", "cmdb_lifecycle_events", ["stage"])
        op.create_index("ix_cmdb_lifecycle_events_event_date", "cmdb_lifecycle_events", ["event_date"])


def downgrade():
    raise RuntimeError("为保留 CMDB 资产、维保授权和生命周期历史，不自动删除新增表")
