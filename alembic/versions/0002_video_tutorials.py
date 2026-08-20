"""video tutorial library

Adds the `video_tutorials` table behind the client portal's Video Tutorials
section and the dashboard's tutorial manager.

`training_level` and `equipment` already exist from 0001, so those columns reuse
the types with create_type=False rather than trying to define them twice.

Revision ID: 0002_video_tutorials
Revises: 0001_initial
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_video_tutorials"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TUTORIAL_CATEGORIES = (
    "GETTING_STARTED",
    "FORM_TECHNIQUE",
    "WARM_UP",
    "MOBILITY",
    "CARDIO",
    "NUTRITION",
    "EQUIPMENT",
    "RECOVERY",
)
VIDEO_PROVIDERS = ("YOUTUBE", "VIMEO", "DIRECT")
EQUIPMENT = (
    "BARBELL",
    "DUMBBELL",
    "MACHINE",
    "CABLE",
    "BODYWEIGHT",
    "KETTLEBELL",
    "BAND",
    "OTHER",
)
TRAINING_LEVELS = ("LEVEL_1", "LEVEL_2", "LEVEL_3")


def upgrade() -> None:
    bind = op.get_bind()

    postgresql.ENUM(*TUTORIAL_CATEGORIES, name="tutorial_category").create(
        bind, checkfirst=True
    )
    postgresql.ENUM(*VIDEO_PROVIDERS, name="video_provider").create(bind, checkfirst=True)

    op.create_table(
        "video_tutorials",
        sa.Column("slug", sa.String(length=180), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("summary", sa.String(length=300), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "category",
            postgresql.ENUM(*TUTORIAL_CATEGORIES, name="tutorial_category", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "provider",
            postgresql.ENUM(*VIDEO_PROVIDERS, name="video_provider", create_type=False),
            nullable=False,
        ),
        sa.Column("video_url", sa.String(length=500), nullable=False),
        sa.Column("thumbnail_url", sa.String(length=500), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("target_muscle", sa.String(length=80), nullable=True),
        sa.Column(
            "equipment",
            postgresql.ENUM(*EQUIPMENT, name="equipment", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "min_level",
            postgresql.ENUM(*TRAINING_LEVELS, name="training_level", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.String(length=40)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("exercise_id", sa.UUID(), nullable=True),
        sa.Column("is_published", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_featured", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("view_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["exercise_id"],
            ["exercises.id"],
            name=op.f("fk_video_tutorials_exercise_id_exercises"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_video_tutorials_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_video_tutorials")),
    )

    op.create_index(op.f("ix_video_tutorials_slug"), "video_tutorials", ["slug"], unique=True)
    op.create_index(op.f("ix_video_tutorials_category"), "video_tutorials", ["category"])
    op.create_index(
        op.f("ix_video_tutorials_target_muscle"), "video_tutorials", ["target_muscle"]
    )
    op.create_index(op.f("ix_video_tutorials_is_published"), "video_tutorials", ["is_published"])
    op.create_index(op.f("ix_video_tutorials_exercise_id"), "video_tutorials", ["exercise_id"])

    # The library screen always sorts featured → manual order → newest.
    op.create_index(
        "ix_video_tutorials_published_order",
        "video_tutorials",
        ["is_published", "is_featured", "sort_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_video_tutorials_published_order", table_name="video_tutorials")
    op.drop_index(op.f("ix_video_tutorials_exercise_id"), table_name="video_tutorials")
    op.drop_index(op.f("ix_video_tutorials_is_published"), table_name="video_tutorials")
    op.drop_index(op.f("ix_video_tutorials_target_muscle"), table_name="video_tutorials")
    op.drop_index(op.f("ix_video_tutorials_category"), table_name="video_tutorials")
    op.drop_index(op.f("ix_video_tutorials_slug"), table_name="video_tutorials")
    op.drop_table("video_tutorials")

    bind = op.get_bind()
    postgresql.ENUM(name="video_provider").drop(bind, checkfirst=True)
    postgresql.ENUM(name="tutorial_category").drop(bind, checkfirst=True)