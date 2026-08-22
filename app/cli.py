"""Small operational CLI.

    python -m app.cli seed              # catalog, the coach, and (outside
                                         # production) a few demo clients
    python -m app.cli create-coach      # create the coach, or reset an
                                         # existing one's password / role
    python -m app.cli deactivate-user   # switch an account off by email
    python -m app.cli healthcheck       # verify the database connection

`seed` takes its credentials from settings — i.e. from the environment — and
never prompts for a password. It used to: the prompts here fed into a
`run_seed()` that had already been rewritten to ignore whatever they typed,
so a password entered at this prompt was silently discarded. That mismatch is
exactly what caused a previous incident where a freshly typed password did
not work and a leftover from months earlier did. Prompting for input that is
then thrown away is worse than not prompting at all — better to have one
source of truth (`.env`) and one deliberate command (`create-coach`) for
setting a real credential by hand.
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
    from app.services.seed import run_seed

    if settings.is_production:
        print(f"This will seed against a PRODUCTION database ({settings.DATABASE_URL.host}).")
        print("Real customer data lives here. The catalogue and coach account are always")
        print("safe to (re-)seed; demo client accounts are skipped in production by default.")
        if input("Type 'seed' to continue: ").strip() != "seed":
            sys.exit("Cancelled.")

    async with SessionLocal() as db:
        await run_seed(db)

    print("Seed complete. See the log above for what was created or skipped.")


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
                    full_name="Coach Auto",
                    display_name="Coach Auto",
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


async def _healthcheck() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    print("Database reachable.")


COMMANDS = {
    "seed": _seed,
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