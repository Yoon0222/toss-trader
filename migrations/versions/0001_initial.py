"""Initial Phase 1 schema, frozen at this revision."""
from alembic import op
import sqlalchemy as sa
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    # Frozen table definitions are loaded from a version-specific snapshot.
    from migrations.schema_v1 import metadata
    metadata.create_all(op.get_bind())
def downgrade():
    from migrations.schema_v1 import metadata
    metadata.drop_all(op.get_bind())
