"""
Training intake and automatic training blocks.

1. `client_profiles` gains the five answers the automatic plan builder needs
   that the profile did not already hold: where the client trains, how long
   they have trained, what equipment they can reach, how long a session can
   run, and when the intake was completed. Height, weight, goal and days per
   week were already there and are reused rather than duplicated.

   Text columns rather than native enums, deliberately. Intake questions grow —
   "outdoors", "studio", a new piece of kit — and `ALTER TYPE ... ADD VALUE`
   on a live database is a migration and an outage risk, where adding a value
   to a `StrEnum` in `app.models.enums` is a line of code. The API validates
   every value at the boundary (`schemas.training.IntakeIn`).

2. `workout_plans.source` — "manual" (the coach's, and the default for every
   existing row, which is exactly right: everything written so far was written
   by a human) or "auto" (built by `app.services.workout_planner`). This is
   what lets an automatic plan be rebuilt freely while a coach's plan is never
   touched.

Both steps are additive and re-runnable: each column is created only if it is
absent, so a half-applied run is safe to retry. There is no data backfill —
existing clients simply have no intake yet, and the portal asks them for it.

Revision ID: 0012_training_intake_auto_plans
Revises: 0011_cloud_media_auto_meals
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_training_intake_auto_plans"
down_revision: str | None = "0011_cloud_media_auto_meals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INTAKE_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    ("training_location", sa.Column("training_location", sa.String(length=16), nullable=True)),
    ("training_experience", sa.Column("training_experience", sa.String(length=16), nullable=True)),
    (
        "available_equipment",
        sa.Column(
            "available_equipment",
            postgresql.ARRAY(sa.String(length=32)),
            nullable=False,
            server_default=sa.text("'{}'::character varying[]"),
        ),
    ),
    (
        "session_minutes",
        sa.Column("session_minutes", sa.Integer(), nullable=False, server_default="45"),
    ),
    (
        "intake_completed_at",
        sa.Column("intake_completed_at", sa.DateTime(timezone=True), nullable=True),
    ),
)


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
    for name, column in INTAKE_COLUMNS:
        if not _column_exists("client_profiles", name):
            op.add_column("client_profiles", column)

    if not _column_exists("workout_plans", "source"):
        op.add_column(
            "workout_plans",
            sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
        )


def downgrade() -> None:
    if _column_exists("workout_plans", "source"):
        op.drop_column("workout_plans", "source")

    for name, _ in reversed(INTAKE_COLUMNS):
        if _column_exists("client_profiles", name):
            op.drop_column("client_profiles", name)