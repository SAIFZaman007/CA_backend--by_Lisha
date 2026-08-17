"""Small operational CLI.

    python -m app.cli seed              # programmes, exercises, coach, demo client
    python -m app.cli create-coach      # promote or create a coach account
    python -m app.cli healthcheck       # verify the database connection
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select, text

from app.core.database import SessionLocal, engine
from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.user import User


async def _seed() -> None:
    from app.services.seed import run_seed

    password = getpass.getpass("Coach password (min 10 chars): ")
    if len(password) < 10:
        sys.exit("Password too short. Use at least 10 characters.")
    demo = getpass.getpass("Demo client password (blank to reuse the coach password): ") or password

    async with SessionLocal() as db:
        await run_seed(db, password, demo)
    print("Seed complete.")


async def _create_coach() -> None:
    email = input("Coach email: ").strip().lower()
    password = getpass.getpass("Password (min 10 chars): ")
    if len(password) < 10:
        sys.exit("Password too short. Use at least 10 characters.")

    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user:
            user.role = UserRole.COACH
            user.hashed_password = hash_password(password)
            print(f"Updated {email} to a coach account.")
        else:
            db.add(
                User(
                    email=email,
                    hashed_password=hash_password(password),
                    full_name="Coach Auto",
                    display_name="Coach Auto",
                    role=UserRole.COACH,
                    is_active=True,
                    is_verified=True,
                )
            )
            print(f"Created coach account {email}.")
        await db.commit()


async def _healthcheck() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    print("Database reachable.")


COMMANDS = {"seed": _seed, "create-coach": _create_coach, "healthcheck": _healthcheck}


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli", description="Coach Auto operations")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args()
    asyncio.run(COMMANDS[args.command]())


if __name__ == "__main__":
    main()
