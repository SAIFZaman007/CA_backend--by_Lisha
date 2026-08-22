"""programme artwork and uploaded tutorial videos

Two additions:

1. `programs.image_key` / `programs.image_external_url` — the hero shot on each
   level's public page, editable from the coach dashboard.
2. `video_tutorials.file_key` / `file_size_bytes`, and `video_url` becomes
   NULLable — a tutorial is now either a hosting link or an uploaded file.

Revision ID: 0004_media_uploads
Revises: 0003_billing_auth
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_media_uploads"
down_revision: str | None = "0003_billing_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Programme artwork ----------------------------------------------------
    op.add_column("programs", sa.Column("image_key", sa.String(length=300), nullable=True))
    op.add_column(
        "programs", sa.Column("image_external_url", sa.String(length=500), nullable=True)
    )

    # --- Uploaded tutorials ---------------------------------------------------
    op.add_column("video_tutorials", sa.Column("file_key", sa.String(length=300), nullable=True))
    op.add_column("video_tutorials", sa.Column("file_size_bytes", sa.Integer(), nullable=True))
    op.alter_column(
        "video_tutorials", "video_url", existing_type=sa.String(length=500), nullable=True
    )


def downgrade() -> None:
    # A row that only ever had an uploaded file has no link to fall back on, so
    # give it a placeholder rather than failing the NOT NULL that follows.
    op.execute(
        "UPDATE video_tutorials SET video_url = 'about:blank' WHERE video_url IS NULL"
    )
    op.alter_column(
        "video_tutorials", "video_url", existing_type=sa.String(length=500), nullable=False
    )
    op.drop_column("video_tutorials", "file_size_bytes")
    op.drop_column("video_tutorials", "file_key")

    op.drop_column("programs", "image_external_url")
    op.drop_column("programs", "image_key")