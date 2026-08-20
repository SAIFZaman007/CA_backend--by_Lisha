"""billing, entitlements and refresh-token rotation

Three changes that arrived together:

1. Stripe billing — `subscriptions`, `payments`, `webhook_events`.
2. `client_profiles.level` becomes NULLable. A level is now earned by paying
   for a tier, so a sign-up who has not paid has no level at all.
3. `refresh_sessions.replaced_by_id` — the rotation chain that lets a benign
   double-refresh be told apart from a replayed token.

Note on step 2: existing clients are left on the level they already have.
Wiping them would lock out people who are mid-programme, and their real
subscription state has to be reconciled deliberately, not by a migration.

Revision ID: 0003_billing_auth
Revises: 0002_video_tutorials
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_billing_auth"
down_revision: str | None = "0002_video_tutorials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUBSCRIPTION_STATUS = (
    "INCOMPLETE",
    "INCOMPLETE_EXPIRED",
    "TRIALING",
    "ACTIVE",
    "PAST_DUE",
    "CANCELED",
    "UNPAID",
    "PAUSED",
)
PAYMENT_STATUS = ("PENDING", "SUCCEEDED", "FAILED", "REFUNDED")


def upgrade() -> None:
    bind = op.get_bind()

    postgresql.ENUM(*SUBSCRIPTION_STATUS, name="subscription_status").create(
        bind, checkfirst=True
    )
    postgresql.ENUM(*PAYMENT_STATUS, name="payment_status").create(bind, checkfirst=True)

    # --- 1. Billing -----------------------------------------------------------
    op.create_table(
        "subscriptions",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("program_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*SUBSCRIPTION_STATUS, name="subscription_status", create_type=False),
            nullable=False,
        ),
        sa.Column("stripe_customer_id", sa.String(length=80), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=80), nullable=True),
        sa.Column("stripe_price_id", sa.String(length=80), nullable=True),
        sa.Column("price_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="usd", nullable=False),
        sa.Column("billing_period", sa.String(length=20), server_default="month", nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["users.id"],
            name=op.f("fk_subscriptions_client_id_users"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["program_id"], ["programs.id"],
            name=op.f("fk_subscriptions_program_id_programs"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
    )
    op.create_index(op.f("ix_subscriptions_client_id"), "subscriptions", ["client_id"])
    op.create_index(op.f("ix_subscriptions_program_id"), "subscriptions", ["program_id"])
    op.create_index(op.f("ix_subscriptions_status"), "subscriptions", ["status"])
    op.create_index(
        op.f("ix_subscriptions_stripe_customer_id"), "subscriptions", ["stripe_customer_id"]
    )
    op.create_index(
        op.f("ix_subscriptions_stripe_subscription_id"),
        "subscriptions",
        ["stripe_subscription_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_subscriptions_current_period_end"), "subscriptions", ["current_period_end"]
    )
    # The entitlement lookup on every gated request: one client, live statuses.
    op.create_index(
        "ix_subscriptions_client_status", "subscriptions", ["client_id", "status"]
    )

    op.create_table(
        "payments",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*PAYMENT_STATUS, name="payment_status", create_type=False),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="usd", nullable=False),
        sa.Column("stripe_invoice_id", sa.String(length=80), nullable=True),
        sa.Column("stripe_payment_intent_id", sa.String(length=80), nullable=True),
        sa.Column("receipt_url", sa.String(length=500), nullable=True),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["users.id"],
            name=op.f("fk_payments_client_id_users"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"],
            name=op.f("fk_payments_subscription_id_subscriptions"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
    )
    op.create_index(op.f("ix_payments_client_id"), "payments", ["client_id"])
    op.create_index(op.f("ix_payments_subscription_id"), "payments", ["subscription_id"])
    op.create_index(
        op.f("ix_payments_stripe_invoice_id"), "payments", ["stripe_invoice_id"], unique=True
    )
    op.create_index(
        op.f("ix_payments_stripe_payment_intent_id"), "payments", ["stripe_payment_intent_id"]
    )

    op.create_table(
        "webhook_events",
        sa.Column("stripe_event_id", sa.String(length=80), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
        # This constraint is the actual idempotency guard for Stripe's
        # at-least-once delivery, not the SELECT that precedes the insert.
        sa.UniqueConstraint("stripe_event_id", name="uq_webhook_events_stripe_event_id"),
    )
    op.create_index(op.f("ix_webhook_events_stripe_event_id"), "webhook_events", ["stripe_event_id"])
    op.create_index(op.f("ix_webhook_events_event_type"), "webhook_events", ["event_type"])

    # --- 2. A level is earned, not assigned -----------------------------------
    op.alter_column("client_profiles", "level", existing_type=sa.Enum(name="training_level"), nullable=True)

    # --- 3. Refresh-token rotation chain --------------------------------------
    op.add_column("refresh_sessions", sa.Column("replaced_by_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_refresh_sessions_replaced_by_id_refresh_sessions"),
        "refresh_sessions",
        "refresh_sessions",
        ["replaced_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_refresh_sessions_replaced_by_id_refresh_sessions"),
        "refresh_sessions",
        type_="foreignkey",
    )
    op.drop_column("refresh_sessions", "replaced_by_id")

    # Anything without a level would violate NOT NULL, so give it the base tier.
    op.execute("UPDATE client_profiles SET level = 'LEVEL_1' WHERE level IS NULL")
    op.alter_column(
        "client_profiles", "level", existing_type=sa.Enum(name="training_level"), nullable=False
    )

    op.drop_table("webhook_events")
    op.drop_table("payments")
    op.drop_table("subscriptions")

    bind = op.get_bind()
    postgresql.ENUM(name="payment_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="subscription_status").drop(bind, checkfirst=True)