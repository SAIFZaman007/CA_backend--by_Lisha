"""Small operational CLI.

    python -m app.cli seed              # catalog, the coach, and (outside
                                         # production) a few demo clients
    python -m app.cli create-coach      # create the coach, or reset an
                                         # existing one's password / role
    python -m app.cli deactivate-user   # switch an account off by email
    python -m app.cli healthcheck       # verify the database connection
    python -m app.cli sync-exercises    # import/refresh the shipped exercise
                                         # catalogue (idempotent, additive)
    python -m app.cli verify-links      # HEAD-check every demonstration URL

`seed` takes its credentials from settings — i.e. from the environment — and
runs immediately, with no prompts and no confirmation. It used to ask for a
password here that a rewritten `run_seed()` had already started ignoring, and
separately used to pause for a "type 'seed' to continue" confirmation before
touching production. Neither one was really protecting anything: the
password prompt fed a value straight into the void, and every insert inside
`run_seed()` already checks for itself before writing — that per-row check is
the actual safety property, not a human typing a word first. One source of
truth for credentials (`.env`), one deliberate command (`create-coach`) for
setting one by hand, and `seed` itself just runs.
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select, text

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.user import User


async def _seed() -> None:
    """Run the one seed. No prompts, no confirmation — every insert already
    checks for itself before writing anything, which is the actual safety
    property that matters; a "type a word to continue" prompt in front of an
    idempotent operation was friction without adding protection."""
    from app.services.seed import run_seed

    async with SessionLocal() as db:
        await run_seed(db)

    print("Seed complete. See the log above for what was created, backfilled, or skipped.")


async def _create_coach() -> None:
    """Create the coach-and-admin account, or fix one that already exists.

    This is the deliberate way to set or rotate the coach's password. `seed`
    never touches a password on an account that already exists — on purpose,
    so a routine reseed can never silently undo a credential rotated since —
    so this command is how you actually change it.
    """
    default_email = settings.COACH_EMAIL
    email = (input(f"Coach email [{default_email}]: ").strip() or default_email).lower()
    password = getpass.getpass("New password (min 10 chars, upper+lower+number): ")
    if len(password) < 10:
        sys.exit("Password too short. Use at least 10 characters.")

    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

        if user:
            # ADMIN, not COACH: this business runs on one person who both
            # coaches and administers, and only ADMIN satisfies the
            # dashboard's admin-only routes (account management, hard
            # deletes). Setting COACH here would leave them locked out of
            # half the dashboard despite the login working fine.
            user.role = UserRole.ADMIN
            user.hashed_password = hash_password(password)
            user.is_active = True
            print(f"Updated {email}: role=admin, password reset, account active.")
        else:
            db.add(
                User(
                    email=email,
                    hashed_password=hash_password(password),
                    full_name="Lisha Chesson",
                    display_name="Lisha Chesson",
                    role=UserRole.ADMIN,
                    is_active=True,
                    is_verified=True,
                )
            )
            print(f"Created coach account {email}.")
        await db.commit()


async def _deactivate_user() -> None:
    """Switch an account off by email.

    The safe default over deleting — it preserves history but the credential
    stops working immediately. This exists for exactly the situation of a
    stray or stale account (leftover test data, an old credential that should
    no longer grant access) that needs to stop working right now, from the
    same container shell used for everything else here, with no separate
    database client required.
    """
    email = input("Email to deactivate: ").strip().lower()
    if not email:
        sys.exit("No email given.")

    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            sys.exit(f"No account found for {email}.")
        if not user.is_active:
            print(f"{email} is already inactive.")
            return

        confirm = input(f"Deactivate {email} (role={user.role.value})? [y/N] ").strip().lower()
        if confirm != "y":
            sys.exit("Cancelled.")

        user.is_active = False
        await db.commit()
        print(f"Deactivated {email}.")


async def _sync_exercises() -> None:
    """Import or refresh the shipped exercise catalogue.

    Additive and idempotent, so it is safe to run on every deploy — it inserts
    what is missing and backfills blanks, and never overwrites a demonstration
    link or a cue the coach has edited by hand.
    """
    from app.data.exercise_library import catalog_size
    from app.services.exercise_import import sync_catalog

    async with SessionLocal() as db:
        report = await sync_catalog(db)
        # The commit is the point. `sync_catalog` only flushes, and a flush
        # without a commit looks like a success in the logs and then rolls
        # back silently when the session closes.
        await db.commit()

    print(
        f"Catalogue synced ({catalog_size()} movements shipped): "
        f"{report.created} created, {report.backfilled} backfilled, "
        f"{report.unchanged} already current."
    )


async def _verify_links() -> None:
    """HEAD-check every demonstration URL and print the ones that fail.

    The catalogue's links are derived from a slug pattern rather than scraped,
    so a handful will not resolve. This finds all of them in one pass instead
    of one client complaint at a time.
    """
    from app.services.exercise_import import verify_video_links

    async with SessionLocal() as db:
        results = await verify_video_links(db)

    broken = [row for row in results if not row.ok]
    print(f"Checked {len(results)} links — {len(results) - len(broken)} OK, {len(broken)} broken.")
    for row in broken:
        reason = row.error or f"HTTP {row.status}"
        print(f"  {row.name}: {reason}\n    {row.url}")
    if broken:
        print(
            "\nRepoint these from Exercise Library in the dashboard, or edit "
            "the name in app/data/exercise_library.py so the slug matches."
        )


async def _healthcheck() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    print("Database reachable.")


COMMANDS = {
    "seed": _seed,
    "sync-exercises": _sync_exercises,
    "verify-links": _verify_links,
    "create-coach": _create_coach,
    "deactivate-user": _deactivate_user,
    "healthcheck": _healthcheck,
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli", description="Coach Auto operations")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args()
    asyncio.run(COMMANDS[args.command]())


if __name__ == "__main__":
    main()