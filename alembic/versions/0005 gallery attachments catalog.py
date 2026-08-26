"""gallery, message attachments and the classified exercise catalogue

Four additions, all backwards compatible. Nothing is dropped or renamed, so a
rollback to the previous application version keeps working against this schema.

1. `exercises` gains `muscle_group`, `mechanics`, `force_type`, `source_url`
   and `popularity`. `target_muscle` is untouched — it stays as the free-text
   human label while `muscle_group` becomes the enum the coach filters by.
2. `gallery_images` — the public "Hall of the Coach".
3. `message_attachments` — images on coach/client messages. `messages.body`
   picks up a server default so an image-only message can store an empty string
   rather than being forced to invent a caption.
4. `workout_day_exercises.video_url` — a per-prescription demonstration that
   overrides the library's.

**On the `equipment` enum.** Ten new values are added to a native PostgreSQL
enum. `ALTER TYPE ... ADD VALUE` could not run inside a transaction at all
before PostgreSQL 12, and even on 12+ a value added in a transaction cannot be
*used* by a later statement in that same transaction. Alembic wraps migrations
in a transaction by default, so both halves of that are a problem here. The
`autocommit_block()` below drops out of the transaction for exactly the ALTER
statements and rejoins for everything else.

Revision ID: 0005_gallery_attachments_catalog
Revises: 0004_media_uploads
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_gallery_attachments_catalog"
down_revision: str | None = "0004_media_uploads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MUSCLE_GROUPS = (
    "abductors",
    "abs",
    "adductors",
    "biceps",
    "calves",
    "chest",
    "forearms",
    "glutes",
    "hamstrings",
    "hip_flexors",
    "it_band",
    "lats",
    "lower_back",
    "upper_back",
    "neck",
    "obliques",
    "palmar_fascia",
    "plantar_fascia",
    "quads",
    "shoulders",
    "traps",
    "triceps",
)

NEW_EQUIPMENT = (
    "ez_bar",
    "smith_machine",
    "medicine_ball",
    "weight_plate",
    "trap_bar",
    "suspension",
    "stretch",
    "foam_roller",
    "sled",
    "cardio_machine",
)

MECHANICS = ("compound", "isolation", "static")
FORCE_TYPES = ("push", "pull", "static", "hinge", "squat", "carry")
GALLERY_CATEGORIES = (
    "transformations",
    "coaching",
    "competition",
    "gym",
    "certifications",
    "community",
    "behind_the_scenes",
)
ATTACHMENT_KINDS = ("image",)


def upgrade() -> None:
    bind = op.get_bind()

    # --- Extend the existing `equipment` enum --------------------------------
    #
    # Outside the surrounding transaction, for the reasons in the module
    # docstring. `IF NOT EXISTS` makes this re-runnable, which matters because
    # a migration that half-applied and then failed must be safe to retry.
    with op.get_context().autocommit_block():
        for value in NEW_EQUIPMENT:
            op.execute(f"ALTER TYPE equipment ADD VALUE IF NOT EXISTS '{value}'")

    # --- New enum types ------------------------------------------------------
    muscle_group = postgresql.ENUM(*MUSCLE_GROUPS, name="muscle_group")
    mechanics = postgresql.ENUM(*MECHANICS, name="mechanics")
    force_type = postgresql.ENUM(*FORCE_TYPES, name="force_type")
    gallery_category = postgresql.ENUM(*GALLERY_CATEGORIES, name="gallery_category")
    attachment_kind = postgresql.ENUM(*ATTACHMENT_KINDS, name="attachment_kind")

    for enum_type in (muscle_group, mechanics, force_type, gallery_category, attachment_kind):
        enum_type.create(bind, checkfirst=True)

    # --- exercises -----------------------------------------------------------
    #
    # Added nullable, backfilled, then made NOT NULL. Adding a NOT NULL column
    # with a default to a populated table rewrites it under an ACCESS EXCLUSIVE
    # lock; three cheap statements beat one that blocks every read of the
    # exercise library while it runs.
    op.add_column(
        "exercises", sa.Column("muscle_group", muscle_group, nullable=True)
    )
    op.add_column("exercises", sa.Column("mechanics", mechanics, nullable=True))
    op.add_column("exercises", sa.Column("force_type", force_type, nullable=True))
    op.add_column("exercises", sa.Column("source_url", sa.String(length=500), nullable=True))
    op.add_column(
        "exercises",
        sa.Column("popularity", sa.Integer(), server_default="0", nullable=False),
    )

    # Best-effort classification of whatever is already in the library, by
    # matching the free-text label that has been carrying this meaning until
    # now. Anything unrecognised lands in `abs` and is re-filed correctly the
    # first time `POST /admin/exercises/sync` runs.
    op.execute(
        """
        UPDATE exercises SET muscle_group = CASE
            WHEN target_muscle ILIKE '%quad%'        THEN 'quads'
            WHEN target_muscle ILIKE '%hamstring%'   THEN 'hamstrings'
            WHEN target_muscle ILIKE '%glute%'       THEN 'glutes'
            WHEN target_muscle ILIKE '%calf%'
              OR target_muscle ILIKE '%calve%'
              OR target_muscle ILIKE '%soleus%'      THEN 'calves'
            WHEN target_muscle ILIKE '%chest%'       THEN 'chest'
            WHEN target_muscle ILIKE '%lat%'
              OR target_muscle ILIKE '%posterior chain%' THEN 'lats'
            WHEN target_muscle ILIKE '%rear delt%'
              OR target_muscle ILIKE '%rhomboid%'
              OR target_muscle ILIKE '%mid back%'    THEN 'upper_back'
            WHEN target_muscle ILIKE '%lower back%'
              OR target_muscle ILIKE '%erector%'     THEN 'lower_back'
            WHEN target_muscle ILIKE '%shoulder%'
              OR target_muscle ILIKE '%delt%'        THEN 'shoulders'
            WHEN target_muscle ILIKE '%bicep%'       THEN 'biceps'
            WHEN target_muscle ILIKE '%tricep%'      THEN 'triceps'
            WHEN target_muscle ILIKE '%trap%'        THEN 'traps'
            WHEN target_muscle ILIKE '%forearm%'
              OR target_muscle ILIKE '%grip%'
              OR target_muscle ILIKE '%wrist%'       THEN 'forearms'
            WHEN target_muscle ILIKE '%oblique%'     THEN 'obliques'
            WHEN target_muscle ILIKE '%neck%'        THEN 'neck'
            WHEN target_muscle ILIKE '%adductor%'    THEN 'adductors'
            WHEN target_muscle ILIKE '%abductor%'    THEN 'abductors'
            WHEN target_muscle ILIKE '%hip flexor%'
              OR target_muscle ILIKE '%psoas%'       THEN 'hip_flexors'
            ELSE 'abs'
        END
        WHERE muscle_group IS NULL
        """
    )
    op.alter_column("exercises", "muscle_group", nullable=False)

    op.create_index(
        "ix_exercises_muscle_group", "exercises", ["muscle_group"], unique=False
    )
    op.create_index(
        "ix_exercises_group_equipment",
        "exercises",
        ["muscle_group", "equipment"],
        unique=False,
    )

    # --- workout_day_exercises ----------------------------------------------
    op.add_column(
        "workout_day_exercises", sa.Column("video_url", sa.String(length=500), nullable=True)
    )

    # --- gallery_images ------------------------------------------------------
    op.create_table(
        "gallery_images",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=180), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("alt_text", sa.String(length=300), nullable=False),
        sa.Column("category", gallery_category, nullable=False),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.String(length=40)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("image_key", sa.String(length=300), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("taken_on", sa.Date(), nullable=True),
        sa.Column("credit", sa.String(length=160), nullable=True),
        sa.Column("is_published", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_featured", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gallery_images_slug", "gallery_images", ["slug"], unique=True)
    op.create_index("ix_gallery_images_category", "gallery_images", ["category"])
    op.create_index("ix_gallery_images_is_published", "gallery_images", ["is_published"])
    op.create_index(
        "ix_gallery_published_order",
        "gallery_images",
        ["is_published", "category", "sort_order"],
    )

    # --- message_attachments -------------------------------------------------
    op.create_table(
        "message_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Nullable on purpose: bytes go up before the message that references
        # them exists. See the model docstring.
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("uploaded_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", attachment_kind, nullable=False),
        sa.Column("file_key", sa.String(length=300), nullable=False),
        sa.Column("content_type", sa.String(length=80), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("original_name", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_message_attachments_message_id", "message_attachments", ["message_id"])
    op.create_index(
        "ix_message_attachments_uploaded_by_id", "message_attachments", ["uploaded_by_id"]
    )
    # The orphan sweep queries exactly this shape: my unbound uploads, older
    # than a cutoff. A partial index keeps it to the handful of rows that are
    # actually unbound rather than the whole attachment history.
    op.create_index(
        "ix_message_attachments_orphans",
        "message_attachments",
        ["uploaded_by_id", "created_at"],
        postgresql_where=sa.text("message_id IS NULL"),
    )

    # An image-only message stores an empty body. The default lets an INSERT
    # omit the column entirely rather than every caller remembering to pass "".
    op.alter_column("messages", "body", server_default="")


def downgrade() -> None:
    op.alter_column("messages", "body", server_default=None)

    op.drop_index("ix_message_attachments_orphans", table_name="message_attachments")
    op.drop_index("ix_message_attachments_uploaded_by_id", table_name="message_attachments")
    op.drop_index("ix_message_attachments_message_id", table_name="message_attachments")
    op.drop_table("message_attachments")

    op.drop_index("ix_gallery_published_order", table_name="gallery_images")
    op.drop_index("ix_gallery_images_is_published", table_name="gallery_images")
    op.drop_index("ix_gallery_images_category", table_name="gallery_images")
    op.drop_index("ix_gallery_images_slug", table_name="gallery_images")
    op.drop_table("gallery_images")

    op.drop_column("workout_day_exercises", "video_url")

    op.drop_index("ix_exercises_group_equipment", table_name="exercises")
    op.drop_index("ix_exercises_muscle_group", table_name="exercises")
    op.drop_column("exercises", "popularity")
    op.drop_column("exercises", "source_url")
    op.drop_column("exercises", "force_type")
    op.drop_column("exercises", "mechanics")
    op.drop_column("exercises", "muscle_group")

    bind = op.get_bind()
    for name in ("attachment_kind", "gallery_category", "force_type", "mechanics", "muscle_group"):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)

    # The ten added `equipment` values are deliberately left in place.
    # PostgreSQL has no `ALTER TYPE ... DROP VALUE`, so removing them means
    # rebuilding the type and rewriting both columns that use it — a far more
    # dangerous operation than leaving unused labels behind, which cost
    # nothing and break nothing.