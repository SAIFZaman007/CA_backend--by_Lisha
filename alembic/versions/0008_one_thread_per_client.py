"""
Collapse duplicate message threads and enforce one per client.

Revision ID: 0008_one_thread_per_client
Revises: 0007_tutorial_poster_frames

A client has exactly one conversation with their coach. The application has
always assumed that; the schema never said so, and nothing stopped a second
`message_threads` row appearing for the same client — a race between the portal
opening a thread and the inbox opening one, or an older code path that looked
the thread up differently on each side.

The damage was quiet and confusing. The coach inbox listed the same client
twice, once with real history and once reading "No messages yet". The portal's
lookup took an unordered `.first()`, so a client could be shown one row while
the coach replied into the other — two people writing to what they each
believed was the same conversation. And the coach-side lookup used
`scalar_one_or_none()`, which raises `MultipleResultsFound` outright, so the
thread failed to open at all once a third row appeared.

This merges every duplicate into one survivor per client, re-points its
messages, refreshes `last_message_at`, and then adds the unique constraint that
makes the whole class of problem impossible. Attachments need no attention:
they hang off `messages`, and the messages keep their ids.

Survivor choice is deliberate: an unarchived thread wins over an archived one,
and the oldest wins the tie. That matches the ordering `thread_for_client` uses
at runtime, so the migration and the application agree on which row is the
real one even before the constraint lands.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_one_thread_per_client"
down_revision: str | None = "0007_tutorial_poster_frames"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SURVIVORS = """
    SELECT
        id,
        client_id,
        first_value(id) OVER (
            PARTITION BY client_id
            ORDER BY is_archived, created_at, id
        ) AS keep_id
      FROM message_threads
"""


def upgrade() -> None:

    op.execute(
        f"""
        UPDATE messages AS m
           SET thread_id = s.keep_id
          FROM ({_SURVIVORS}) AS s
         WHERE m.thread_id = s.id
           AND s.id <> s.keep_id
        """
    )

    op.execute(
        f"""
        DELETE FROM message_threads AS t
         USING ({_SURVIVORS}) AS s
         WHERE t.id = s.id
           AND s.id <> s.keep_id
        """
    )

    op.execute(
        """
        UPDATE message_threads AS t
           SET last_message_at = latest.at
          FROM (
                SELECT thread_id, MAX(created_at) AS at
                  FROM messages
                 GROUP BY thread_id
               ) AS latest
         WHERE latest.thread_id = t.id
           AND (t.last_message_at IS NULL OR t.last_message_at < latest.at)
        """
    )

    op.create_unique_constraint(
        "uq_message_threads_client_id", "message_threads", ["client_id"]
    )


def downgrade() -> None:

    op.drop_constraint(
        "uq_message_threads_client_id", "message_threads", type_="unique"
    )