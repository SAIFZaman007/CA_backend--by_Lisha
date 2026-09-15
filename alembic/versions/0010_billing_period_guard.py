"""
Repair invalid `programs.billing_period` values and stop them recurring.

Revision ID: 0010_billing_period_guard
Revises: 0009_billing_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_billing_period_guard"
down_revision: str | None = "0009_billing_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ALLOWED = ("month", "year", "week", "day", "once")
CONSTRAINT_NAME = "ck_programs_billing_period"


def _constraint_exists(name: str, table: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE c.conname = :name AND t.relname = :table
                """
            ),
            {"name": name, "table": table},
        ).scalar()
    )


def upgrade() -> None:
    allowed = ", ".join(f"'{value}'" for value in ALLOWED)

    repaired = op.get_bind().execute(
        sa.text(
            f"""
            UPDATE programs
               SET billing_period = 'month'
             WHERE billing_period IS NULL
                OR billing_period NOT IN ({allowed})
            """
        )
    )
    print(f"0010: normalised {repaired.rowcount} program row(s) to billing_period='month'")

    if not _constraint_exists(CONSTRAINT_NAME, "programs"):
        op.create_check_constraint(
            CONSTRAINT_NAME,
            "programs",
            f"billing_period IN ({allowed})",
        )


def downgrade() -> None:
    if _constraint_exists(CONSTRAINT_NAME, "programs"):
        op.drop_constraint(CONSTRAINT_NAME, "programs", type_="check")