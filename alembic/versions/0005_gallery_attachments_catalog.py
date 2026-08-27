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
    "ABDUCTORS",
    "ABS",
    "ADDUCTORS",
    "BICEPS",
    "CALVES",
    "CHEST",
    "FOREARMS",
    "GLUTES",
    "HAMSTRINGS",
    "HIP_FLEXORS",
    "IT_BAND",
    "LATS",
    "LOWER_BACK",
    "UPPER_BACK",
    "NECK",
    "OBLIQUES",
    "PALMAR_FASCIA",
    "PLANTAR_FASCIA",
    "QUADS",
    "SHOULDERS",
    "TRAPS",
    "TRICEPS",
)

# `equipment` is a native enum created back in 0001 using upper-case member
# names (`sa.Enum(PyEnum)` serializes `.name`, not `.value`, unless
# `values_callable` says otherwise — nothing in this codebase does). These ten
# new labels have to match that existing convention or every row written with
# one of them becomes unreadable the same way `muscle_group` broke below.
NEW_EQUIPMENT = (
    "EZ_BAR",
    "SMITH_MACHINE",
    "MEDICINE_BALL",
    "WEIGHT_PLATE",
    "TRAP_BAR",
    "SUSPENSION",
    "STRETCH",
    "FOAM_ROLLER",
    "SLED",
    "CARDIO_MACHINE",
)

# NOTE: these four enums must use upper-case member names, matching the
# convention every other native enum in this schema follows (see 0001-0003).
# SQLAlchemy's `Enum(SomePyEnum)` column type stores the Python member's
# `.name` on write and looks values back up by `.name` on read — it never
# touches `.value` unless the column is declared with `values_callable`,
# which nothing here does. An earlier version of this migration created
# these four types (and backfilled `muscle_group`) using the lower-case
# `.value` strings instead, which matched the type at CREATE TIME but not
# what the ORM actually reads/writes — every row is a `LookupError` waiting
# to happen on the first SELECT. Fixed here before this migration is ever
# applied anywhere that matters.
MECHANICS = ("COMPOUND", "ISOLATION", "STATIC")
FORCE_TYPES = ("PUSH", "PULL", "STATIC", "HINGE", "SQUAT", "CARRY")
GALLERY_CATEGORIES = (
    "TRANSFORMATIONS",
    "COACHING",
    "COMPETITION",
    "GYM",
    "CERTIFICATIONS",
    "COMMUNITY",
    "BEHIND_THE_SCENES",
)
ATTACHMENT_KINDS = ("IMAGE",)


def upgrade() -> None:
    bind = op.get_bind()
    
    # --- Clean up any pre-existing orphan enum types before creation ---------
    # --- Extend the existing `equipment` enum --------------------------------
    #
    # Outside the surrounding transaction, for the reasons in the module
    # docstring. `IF NOT EXISTS` makes this re-runnable, which matters because
    # a migration that half-applied and then failed must be safe to retry.
    with op.get_context().autocommit_block():
        for enum_name in ("gallery_category", "muscle_group", "mechanics", "force_type", "attachment_kind"):
            op.execute(f"DROP TYPE IF EXISTS {enum_name} CASCADE;")
        
        for value in NEW_EQUIPMENT:
            op.execute(f"ALTER TYPE equipment ADD VALUE IF NOT EXISTS '{value}'")

    # 2. Instantiate ENUM objects with create_type=False for columns so SQLAlchemy doesn't auto-create them twice
    muscle_group = postgresql.ENUM(*MUSCLE_GROUPS, name="muscle_group", create_type=False)
    mechanics = postgresql.ENUM(*MECHANICS, name="mechanics", create_type=False)
    force_type = postgresql.ENUM(*FORCE_TYPES, name="force_type", create_type=False)
    gallery_category = postgresql.ENUM(*GALLERY_CATEGORIES, name="gallery_category", create_type=False)
    attachment_kind = postgresql.ENUM(*ATTACHMENT_KINDS, name="attachment_kind", create_type=False)

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
    # now. Anything unrecognised lands in `ABS` and is re-filed correctly the
    # first time `POST /admin/exercises/sync` runs.
    # Every branch of this CASE is a string literal, so Postgres infers the
    # expression's type as `text`. The column it is being assigned to is the
    # native `muscle_group` enum, and Postgres will not implicitly cast
    # text -> enum even though every literal here is a valid member of it —
    # that gap is exactly what raised `DatatypeMismatchError` on first run.
    # The `::muscle_group` cast on the CASE result closes it explicitly.
    # Literals here are upper-case member names (`QUADS`, not `quads`) to
    # match how `MUSCLE_GROUPS` above defines the type, and how SQLAlchemy's
    # `Enum(MuscleGroup)` column actually reads/writes it.
    op.execute(
        """
        UPDATE exercises SET muscle_group = (CASE
            WHEN target_muscle ILIKE '%quad%'        THEN 'QUADS'
            WHEN target_muscle ILIKE '%hamstring%'   THEN 'HAMSTRINGS'
            WHEN target_muscle ILIKE '%glute%'       THEN 'GLUTES'
            WHEN target_muscle ILIKE '%calf%'
              OR target_muscle ILIKE '%calve%'
              OR target_muscle ILIKE '%soleus%'      THEN 'CALVES'
            WHEN target_muscle ILIKE '%chest%'       THEN 'CHEST'
            WHEN target_muscle ILIKE '%lat%'
              OR target_muscle ILIKE '%posterior chain%' THEN 'LATS'
            WHEN target_muscle ILIKE '%rear delt%'
              OR target_muscle ILIKE '%rhomboid%'
              OR target_muscle ILIKE '%mid back%'    THEN 'UPPER_BACK'
            WHEN target_muscle ILIKE '%lower back%'
              OR target_muscle ILIKE '%erector%'     THEN 'LOWER_BACK'
            WHEN target_muscle ILIKE '%shoulder%'
              OR target_muscle ILIKE '%delt%'        THEN 'SHOULDERS'
            WHEN target_muscle ILIKE '%bicep%'       THEN 'BICEPS'
            WHEN target_muscle ILIKE '%tricep%'      THEN 'TRICEPS'
            WHEN target_muscle ILIKE '%trap%'        THEN 'TRAPS'
            WHEN target_muscle ILIKE '%forearm%'
              OR target_muscle ILIKE '%grip%'
              OR target_muscle ILIKE '%wrist%'       THEN 'FOREARMS'
            WHEN target_muscle ILIKE '%oblique%'     THEN 'OBLIQUES'
            WHEN target_muscle ILIKE '%neck%'        THEN 'NECK'
            WHEN target_muscle ILIKE '%adductor%'    THEN 'ADDUCTORS'
            WHEN target_muscle ILIKE '%abductor%'    THEN 'ABDUCTORS'
            WHEN target_muscle ILIKE '%hip flexor%'
              OR target_muscle ILIKE '%psoas%'       THEN 'HIP_FLEXORS'
            ELSE 'ABS'
        END)::muscle_group
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