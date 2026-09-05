"""
Add a poster-frame key to video tutorials.

Revision ID: 0007_tutorial_poster_frames
Revises: 0006_repair_tutorial_stream_urls

An uploaded tutorial had nowhere to keep a still of its own. `thumbnail_url`
holds an address someone else owns — YouTube's hqdefault.jpg, a Vimeo still —
which is fine for a hosted clip and useless for a file on our volume, so every
uploaded video rendered as an identical grey play icon and the library gave a
client no way to tell one clip from another at a glance.

`thumbnail_key` is the local counterpart: a storage key for a poster frame
captured in the browser when the coach uploads the video. Kept as a key and not
a URL on purpose. The address for a private file is signed and short-lived, and
migration 0006 exists because a generated URL was once written into a column
that outlived it — the same mistake is not worth making twice in the same table.

Nullable and additive. Hosted tutorials never set it and keep using
`thumbnail_url`; existing uploaded tutorials read NULL and simply carry on
showing the placeholder until the coach edits them and a poster is captured.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_tutorial_poster_frames"
down_revision: str | None = "0006_repair_tutorial_stream_urls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "video_tutorials",
        sa.Column("thumbnail_key", sa.String(length=300), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("video_tutorials", "thumbnail_key")