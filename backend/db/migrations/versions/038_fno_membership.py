"""Active F&O membership; preserve retired instrument identities for held books."""
from alembic import op
revision = "038_fno_membership"
down_revision = "037_spread_estimated_flag"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("ALTER TABLE fo_underlying_catalog ADD COLUMN IF NOT EXISTS fno_active boolean")
    op.execute("ALTER TABLE fo_underlying_catalog ADD COLUMN IF NOT EXISTS fno_snapshot_at timestamptz")

def downgrade():
    op.execute("ALTER TABLE fo_underlying_catalog DROP COLUMN IF EXISTS fno_snapshot_at")
    op.execute("ALTER TABLE fo_underlying_catalog DROP COLUMN IF EXISTS fno_active")
