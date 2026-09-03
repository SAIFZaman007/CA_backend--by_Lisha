"""Repair tutorial rows whose video_url holds an expired stream token.

Revision ID: 0006_repair_tutorial_stream_urls
Revises: 0005_gallery_attachments_catalog

`tutorials.attach_stream_url` used to assign a signed playback URL onto the
`VideoTutorial` instance it had just loaded from the request session, and the
session commits at the end of every successful request. So what was meant as a
response decoration was in fact an UPDATE: a short-lived, single-viewer media
token was written into the `video_url` column of every uploaded tutorial the
moment anyone listed the library.

Two things then broke, both quietly. The minting was guarded by
`not tutorial.video_url`, so once the column was populated no fresh token was
ever issued again — every client got the same frozen signature, and playback
started returning 401 as soon as it expired. And the coach dashboard reads the
same column, so it began treating the stale token as if the coach had pasted a
hosting link.

This clears the damage. Only rows that have a `file_key` (an uploaded file,
which should never carry a `video_url` at all) and whose `video_url` looks like
one of these generated stream paths are touched — a genuine hosting link the
coach pasted has neither property and is left exactly as it is. `provider` goes
back to DIRECT on the same rows, since `detect_provider` may have classified
the token path when the row was last written.

The code no longer writes to this column, so this runs once and stays clean.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_repair_tutorial_stream_urls"
down_revision: str | None = "0005_gallery_attachments_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Matches both shapes the old code could produce: the root-relative path it
# always emitted, and an absolute one had PUBLIC_API_URL been in play.
_STREAM_URL_PREDICATE = """
    file_key IS NOT NULL
    AND video_url IS NOT NULL
    AND video_url LIKE '%/api/v1/tutorials/%/stream%'
"""


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE video_tutorials
           SET video_url = NULL,
               provider = 'DIRECT'::video_provider
         WHERE {_STREAM_URL_PREDICATE}
        """
    )


def downgrade() -> None:
    """Deliberately a no-op.

    The previous contents of these columns were expired credentials. There is
    nothing to restore, and restoring it would reintroduce the fault.
    """