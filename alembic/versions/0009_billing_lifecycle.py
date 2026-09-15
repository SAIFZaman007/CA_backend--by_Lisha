"""
Billing lifecycle columns, and a self-healing repair of drifted schema.

Revision ID: 0009_billing_lifecycle
Revises: 0008_one_thread_per_client

A note on the revision id
-------------------------
It is `0009_billing_lifecycle`, not the longer name this file describes, and
that is not a style choice. Alembic's `alembic_version.version_num` column is
`VARCHAR(32)`. The id is written into it at the end of every migration, so an
id of 33 characters or more runs the whole migration successfully and then
fails on the bookkeeping UPDATE with:

    asyncpg.exceptions.StringDataRightTruncationError:
    value too long for type character varying(32)

Because this project runs the whole upgrade in one transaction, that failure
rolls back *every* migration in the run, not just this one — so a too-long id
in the newest revision silently prevents four older ones from applying. The two
longest ids already in this tree (`0005_gallery_attachments_catalog` and
`0006_repair_tutorial_stream_urls`) are exactly 32 characters, which is how
close this has always been. Keep new ids under 32; the descriptive name belongs
in the docstring, where it costs nothing.

Part one: the repair
--------------------
`GET /api/v1/tutorials` was returning 500 on every call, client side and coach
side, with:

    asyncpg.exceptions.UndefinedColumnError:
    column video_tutorials.thumbnail_key does not exist
    HINT: Perhaps you meant to reference "video_tutorials.thumbnail_url".

The column is declared on the model and migration 0007 adds it, so the code was
correct and the database was not. That gap is the signature of a partial
migration run: `alembic_version` had been stamped at or past 0008 while 0007's
DDL never executed. `alembic upgrade head` then does nothing at all — it sees
the version table already ahead and no-ops, which is why re-running it never
fixed anything and why the error looked permanent.

The usual recovery is `alembic stamp 0006_...` followed by `alembic upgrade
head`, but that replays 0008 as well, and 0008 creates a unique constraint that
already exists — so the recovery fails on a DuplicateObject error and leaves
the database in a third state. Doing it by hand also means every environment
needs the same manual surgery performed correctly, which is not something to
rely on at deploy time.

So this migration reconciles the schema by inspecting it. Every statement below
checks first and is safe to run against a database that is already correct, one
that is missing everything, and anything in between. It converges rather than
assuming a starting point, which is the only property that makes it safe to
ship to an environment whose exact drift you cannot see.

The lesson worth keeping: **a migration that repairs drift must be idempotent.**
Additive DDL guarded by a catalogue lookup is cheap; a failed deploy at 2am is
not.

Part two: the billing lifecycle
-------------------------------
The new columns support post-purchase subscription management — scheduled
downgrades, cancellation with a reason, dunning state and a cached payment
method for display. All are nullable or defaulted, so the deploy is additive
and needs no backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_billing_lifecycle"
down_revision: str | None = "0008_one_thread_per_client"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Helpers ------------------------------------------------------------------
#
# Inspection rather than assumption. `op.get_bind()` gives the live connection;
# SQLAlchemy's inspector reads the real catalogue, so these answer "what does
# this database actually have", not "what should it have by now".


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return column in {col["name"] for col in _inspector().get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if _has_column(table, column.name):
        return
    op.add_column(table, column)


def _drop_column_if_present(table: str, column: str) -> None:
    if _has_column(table, column):
        op.drop_column(table, column)


def upgrade() -> None:
    # --- Repair: columns earlier migrations were meant to have created --------
    #
    # `thumbnail_key` is the one that was actually missing in production. The
    # others are checked in the same pass because they came from the same run
    # of migrations and there is no reason to find out the hard way, one 500 at
    # a time, which of them also failed to land.

    _add_column_if_missing(
        "video_tutorials",
        sa.Column("thumbnail_key", sa.String(length=300), nullable=True),
    )

    # --- Subscription lifecycle ----------------------------------------------

    # A downgrade must not take effect the moment it is requested — the client
    # has paid through the end of the period and keeps what they paid for. The
    # pending tier is parked here and applied by the webhook when the period
    # rolls over. Nullable: most subscriptions have nothing scheduled.
    _add_column_if_missing(
        "subscriptions",
        sa.Column("scheduled_program_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    _add_column_if_missing(
        "subscriptions",
        sa.Column("scheduled_price_cents", sa.Integer(), nullable=True),
    )
    _add_column_if_missing(
        "subscriptions",
        sa.Column("scheduled_change_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        "subscriptions",
        sa.Column("stripe_schedule_id", sa.String(length=80), nullable=True),
    )

    # Why they left. Free-text reason plus a coded category, because a coach
    # reading "too expensive" across nine cancellations learns something a
    # status column can never tell them.
    _add_column_if_missing(
        "subscriptions",
        sa.Column("cancellation_reason", sa.String(length=60), nullable=True),
    )
    _add_column_if_missing(
        "subscriptions",
        sa.Column("cancellation_comment", sa.Text(), nullable=True),
    )

    # Dunning. When a card fails, Stripe retries on its own schedule; these
    # record where in that process the subscription currently is so the portal
    # can say "your card was declined, update it before the 14th" instead of
    # silently locking the client out.
    _add_column_if_missing(
        "subscriptions",
        sa.Column("payment_failed_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        "subscriptions",
        sa.Column("payment_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        "subscriptions",
        sa.Column("last_payment_error", sa.String(length=300), nullable=True),
    )

    # Cached card display. Never the number — brand and last four only, which
    # is all a person needs to recognise which card is on file, and all that is
    # safe to keep.
    _add_column_if_missing(
        "subscriptions",
        sa.Column("payment_method_brand", sa.String(length=40), nullable=True),
    )
    _add_column_if_missing(
        "subscriptions",
        sa.Column("payment_method_last4", sa.String(length=4), nullable=True),
    )
    _add_column_if_missing(
        "subscriptions",
        sa.Column("payment_method_exp", sa.String(length=7), nullable=True),
    )

    # --- Invoices -------------------------------------------------------------

    # Stripe's hosted invoice page and its PDF. Stored rather than fetched on
    # demand so the billing history renders from one local query instead of N
    # round trips to Stripe, and still reads correctly if Stripe is briefly
    # unreachable.
    _add_column_if_missing(
        "payments",
        sa.Column("invoice_pdf_url", sa.String(length=500), nullable=True),
    )
    _add_column_if_missing(
        "payments",
        sa.Column("invoice_number", sa.String(length=60), nullable=True),
    )
    _add_column_if_missing(
        "payments",
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        "payments",
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        "payments",
        sa.Column("failure_reason", sa.String(length=300), nullable=True),
    )
    _add_column_if_missing(
        "payments",
        sa.Column("attempt_count", sa.Integer(), nullable=True),
    )

    # Billing history is always read as "this client's, newest first", so index
    # precisely that rather than relying on the single-column index on
    # client_id and a sort afterwards.
    existing = {ix["name"] for ix in _inspector().get_indexes("payments")}
    if "ix_payments_client_created" not in existing:
        op.create_index(
            "ix_payments_client_created",
            "payments",
            ["client_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    existing = {ix["name"] for ix in _inspector().get_indexes("payments")}
    if "ix_payments_client_created" in existing:
        op.drop_index("ix_payments_client_created", table_name="payments")

    for column in (
        "attempt_count",
        "failure_reason",
        "period_end",
        "period_start",
        "invoice_number",
        "invoice_pdf_url",
    ):
        _drop_column_if_present("payments", column)

    for column in (
        "payment_method_exp",
        "payment_method_last4",
        "payment_method_brand",
        "last_payment_error",
        "payment_retry_at",
        "payment_failed_at",
        "cancellation_comment",
        "cancellation_reason",
        "stripe_schedule_id",
        "scheduled_change_at",
        "scheduled_price_cents",
        "scheduled_program_id",
    ):
        _drop_column_if_present("subscriptions", column)

    # `thumbnail_key` is deliberately not dropped here. It belongs to migration
    # 0007; this one only restored it. Dropping it on the way down would delete
    # a column this revision does not own and undo 0007 behind its back.