"""
Broader equipment taxonomy and automatic meal plans.

1. `equipment` gains EXERCISE_BALL, BOSU_BALL, LANDMINE, PLYO_BOX, YOGA and
   PILATES, matching the equipment filter the coach knows from the reference
   sites. Upper-case member *names*, like every native enum in this schema —
   SQLAlchemy stores `.name`, not `.value` (see 0005 for the incident).
   `ALTER TYPE ... ADD VALUE` runs outside the migration transaction and with
   `IF NOT EXISTS`, so a half-applied run is safe to retry.

2. `meal_plans.source` — "manual" (coach-written, the default for every
   existing row) or "auto" (built by `app.services.meal_planner`).

No media columns change: Cloudinary keys (`cld:<type>:<delivery>:<public_id>`)
fit the existing VARCHAR(300)/(400) key columns.

Revision ID: 0011_cloud_media_auto_meals
Revises: 0010_billing_period_guard
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_cloud_media_auto_meals"
down_revision: str | None = "0010_billing_period_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_EQUIPMENT = ("EXERCISE_BALL", "BOSU_BALL", "LANDMINE", "PLYO_BOX", "YOGA", "PILATES")


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in NEW_EQUIPMENT:
            op.execute(f"ALTER TYPE equipment ADD VALUE IF NOT EXISTS '{value}'")

    if not _column_exists("meal_plans", "source"):
        op.add_column(
            "meal_plans",
            sa.Column(
                "source",
                sa.String(length=16),
                nullable=False,
                server_default="manual",
            ),
        )


def downgrade() -> None:
    if _column_exists("meal_plans", "source"):
        op.drop_column("meal_plans", "source")