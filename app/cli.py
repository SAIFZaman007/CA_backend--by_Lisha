"""
Operational CLI for Coach Auto.

    python -m app.cli status                    # what is actually in the database
    python -m app.cli seed                      # catalog, the coach, demo data
    python -m app.cli reset                     # wipe the demo data
    python -m app.cli reset --reseed            # wipe it and write it again
    python -m app.cli reset --scope all --yes   # empty every table
    python -m app.cli create-coach              # create the coach, or reset her password
    python -m app.cli deactivate-user           # switch an account off by email
    python -m app.cli sync-exercises            # refresh the shipped exercise catalogue
    python -m app.cli verify-links              # HEAD-check every demonstration URL
    python -m app.cli healthcheck               # verify the database connection

--- On prompts ----------------------------------------------------------------

`seed`, `sync-exercises` and `reset` (in its default demo scope) run
immediately, with no prompt and no environment check. That is deliberate and
it is not laziness: every one of them is either idempotent or scoped to rows
this repository wrote itself, so a human typing a word first was friction
without protection. It also makes them usable from a deploy hook, which a
prompt does not.

`reset --scope all` is the exception, and it is a real one rather than a
reflex. That command empties tables holding real clients, real messages, real
check-in photos and real payment history. It is not idempotent, not scoped,
and not undoable from here. So it requires `--yes` — a flag rather than an
interactive prompt, so it stays scriptable when you genuinely mean it and
cannot happen by tab-completing your way into the wrong command.

Credentials come from settings — i.e. from the environment — everywhere except
`create-coach`, which is the one deliberate place to set a password by hand and
takes `--email` / `--password` for automation or prompts for them when omitted.
"""

import argparse
import asyncio
import getpass
import sys
from collections.abc import Awaitable, Callable

from sqlalchemy import func, select, text

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.user import User

EXIT_OK = 0
EXIT_FAILED = 1


# --- Presentation --------------------------------------------------------------

def _rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def _line(label: str, value: object) -> None:
    print(f"  {label:<28} {value}")


# --- Commands ------------------------------------------------------------------

async def cmd_seed(args: argparse.Namespace) -> int:
    """
    Write the catalog, the coach and the demo data.

    Idempotent by construction — every insert inside `run_seed` checks for
    itself first — so this fills in whatever is missing rather than duplicating
    what is there. To get the demo data back to exactly what the seed writes
    after a week of clicking around, reach for `reset --reseed` instead: this
    command backfills, it does not restore.
    """
    from app.services.seed import run_seed

    async with SessionLocal() as db:
        await run_seed(db)

    print("Seed complete. See the log above for what was created, backfilled, or skipped.")
    return EXIT_OK


async def cmd_reset(args: argparse.Namespace) -> int:
    """Remove seeded data, optionally writing it again straight after.

    The default scope is `demo`, which is the one that is safe to reach for
    without thinking. `all` is guarded by `--yes` for the reasons in the module
    docstring.
    """
    from app.services import reset as reset_service

    drop_media = not args.keep_media

    if args.scope == "all" and not args.yes:
        print(
            "Refusing to run.\n\n"
            "  `--scope all` empties every application table — real clients, real\n"
            "  messages, real check-in photos, real payment history. It cannot be\n"
            "  undone from here.\n\n"
            "  If that is what you want, say so:\n"
            "      python -m app.cli reset --scope all --yes\n\n"
            "  If you only wanted the seeded demo accounts back to a clean state:\n"
            "      python -m app.cli reset --reseed",
            file=sys.stderr,
        )
        return EXIT_FAILED

    async with SessionLocal() as db:
        if args.scope == "all":
            report = await reset_service.reset_all(db, drop_media=drop_media)
        else:
            report = await reset_service.reset_demo(db, drop_media=drop_media)

    _rule(f"Reset — scope: {report.scope}")
    if report.scope == "all":
        _line("Tables cleared", report.tables_cleared)
    else:
        _line("Demo accounts removed", report.accounts_deleted)
        _line("Enquiries removed", report.leads_deleted)
        _line("Bookings removed", report.bookings_deleted)
        _line("Testimonials removed", report.testimonials_deleted)

    if drop_media:
        _line("Files removed", f"{report.files_deleted} ({report.megabytes_freed} MB)")
    else:
        _line("Files removed", "skipped (--keep-media)")

    for warning in report.warnings:
        print(f"  ! {warning}", file=sys.stderr)

    if args.reseed:
        # A separate session on purpose. The reset has committed; the seed
        # should start from a clean identity map rather than inheriting one
        # full of objects whose rows no longer exist.
        from app.services.seed import run_seed

        _rule("Reseeding")
        async with SessionLocal() as db:
            await run_seed(db)
        print("  Done. See the log above for what was written.")

        if report.scope == "all" and not settings.COACH_PASSWORD:
            print(
                "\n  ! A full reset removed the coach account, and COACH_PASSWORD is not\n"
                "    set, so the seed could not recreate it with a real credential.\n"
                "    Run: python -m app.cli create-coach",
                file=sys.stderr,
            )

    return EXIT_OK


async def cmd_status(args: argparse.Namespace) -> int:
    """
    What is actually in the database right now.

    Exists because the honest answer to "did that work?" after a reset or a
    seed is a row count, and reading one should not mean opening a database
    client. Also the fastest way to tell a database that has never been
    migrated from one that has been emptied — both look broken from the app,
    and they need very different fixes.
    """
    from app.models.catalog import Exercise, Program
    from app.models.engagement import ConsultationBooking, Lead, Message
    from app.models.tracking import ProgressPhoto
    from app.services.reset import DEMO_CLIENT_EMAILS

    async with SessionLocal() as db:
        async def count(model, *where) -> int:
            stmt = select(func.count()).select_from(model)
            for clause in where:
                stmt = stmt.where(clause)
            return (await db.execute(stmt)).scalar_one()

        version = (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()

        staff = await count(User, User.role.in_((UserRole.COACH, UserRole.ADMIN)))
        clients = await count(User, User.role == UserRole.CLIENT)
        demo = await count(User, User.email.in_(DEMO_CLIENT_EMAILS))

        _rule("Coach Auto — database status")
        _line("Environment", settings.ENVIRONMENT)
        _line("Host", settings.db_host)
        _line("Schema revision", version or "NONE — run `alembic upgrade head`")
        print()
        _line("Staff accounts", staff)
        _line("Client accounts", f"{clients} ({demo} seeded demo)")
        _line("Programmes", await count(Program))
        _line("Exercises", await count(Exercise))
        _line("Messages", await count(Message))
        _line("Check-in photos", await count(ProgressPhoto))
        _line("Enquiries", await count(Lead))
        _line("Bookings", await count(ConsultationBooking))

    return EXIT_OK


async def cmd_create_coach(args: argparse.Namespace) -> int:
    """
    Create the coach-and-admin account, or fix one that already exists.

    This is the deliberate way to set or rotate the coach's password. `seed`
    never touches a password on an account that already exists — on purpose,
    so a routine reseed can never silently undo a credential rotated since —
    so this command is how you actually change it.
    """
    email = (args.email or input(f"Coach email [{settings.COACH_EMAIL}]: ").strip()
             or settings.COACH_EMAIL).lower()
    password = args.password or getpass.getpass(
        "New password (min 10 chars, upper+lower+number): "
    )
    if len(password) < 10:
        print("Password too short. Use at least 10 characters.", file=sys.stderr)
        return EXIT_FAILED

    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

        if user:
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

    return EXIT_OK


async def cmd_deactivate_user(args: argparse.Namespace) -> int:
    """
    Switch an account off by email.

    The safe default over deleting — it preserves history but the credential
    stops working immediately. This exists for exactly the situation of a
    stray or stale account that needs to stop working right now, from the same
    container shell used for everything else here.
    """
    email = (args.email or input("Email to deactivate: ")).strip().lower()
    if not email:
        print("No email given.", file=sys.stderr)
        return EXIT_FAILED

    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            print(f"No account found for {email}.", file=sys.stderr)
            return EXIT_FAILED
        if not user.is_active:
            print(f"{email} is already inactive.")
            return EXIT_OK

        if not args.yes:
            confirm = input(f"Deactivate {email} (role={user.role.value})? [y/N] ").strip().lower()
            if confirm != "y":
                print("Cancelled.")
                return EXIT_OK

        user.is_active = False
        await db.commit()
        print(f"Deactivated {email}.")

    return EXIT_OK


async def cmd_sync_exercises(args: argparse.Namespace) -> int:
    """Import or refresh the shipped exercise catalogue.

    Additive and idempotent, so it is safe to run on every deploy — it inserts
    what is missing and backfills blanks, and never overwrites a demonstration
    link or a cue the coach has edited by hand.
    """
    from app.data.exercise_library import catalog_size
    from app.services.exercise_import import sync_catalog

    async with SessionLocal() as db:
        report = await sync_catalog(db)
        await db.commit()

    print(
        f"Catalogue synced ({catalog_size()} movements shipped): "
        f"{report.created} created, {report.backfilled} backfilled, "
        f"{report.unchanged} already current."
    )
    return EXIT_OK


async def cmd_verify_links(args: argparse.Namespace) -> int:
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
    return EXIT_OK


async def cmd_healthcheck(args: argparse.Namespace) -> int:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    print(f"Database reachable ({settings.db_host}).")
    return EXIT_OK


# --- Wiring --------------------------------------------------------------------

Handler = Callable[[argparse.Namespace], Awaitable[int]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.cli",
        description="Coach Auto operations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def add(name: str, handler: Handler, help_text: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name, help=help_text, description=help_text)
        child.set_defaults(handler=handler)
        return child

    add("status", cmd_status, "Show what is currently in the database.")
    add("seed", cmd_seed, "Write the catalog, the coach and the demo data. Idempotent.")

    reset = add(
        "reset",
        cmd_reset,
        "Remove seeded data. Defaults to the demo scope; --scope all empties everything.",
    )
    reset.add_argument(
        "--scope",
        choices=("demo", "all"),
        default="demo",
        help=(
            "demo (default): the seeded demo accounts, enquiries, bookings and "
            "testimonials only — the coach, the tiers and the exercise library "
            "are left alone. all: every application table."
        ),
    )
    reset.add_argument(
        "--reseed",
        action="store_true",
        help="Run the seed immediately afterwards, in one command.",
    )
    reset.add_argument(
        "--keep-media",
        action="store_true",
        help=(
            "Leave uploaded files on the volume. Off by default — a row deleted "
            "without its file is a leak, not a reset."
        ),
    )
    reset.add_argument(
        "--yes",
        action="store_true",
        help="Required for --scope all. Confirms you mean to delete real data.",
    )

    coach = add("create-coach", cmd_create_coach, "Create the coach account, or reset its password.")
    coach.add_argument("--email", help="Defaults to COACH_EMAIL, or prompts.")
    coach.add_argument(
        "--password",
        help=(
            "Prompts when omitted, which is the better habit — a password passed "
            "as an argument lands in your shell history and the process list."
        ),
    )

    deactivate = add("deactivate-user", cmd_deactivate_user, "Switch an account off by email.")
    deactivate.add_argument("--email", help="Prompts when omitted.")
    deactivate.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    add("sync-exercises", cmd_sync_exercises, "Import or refresh the shipped exercise catalogue.")
    add("verify-links", cmd_verify_links, "HEAD-check every demonstration URL.")
    add("healthcheck", cmd_healthcheck, "Verify the database connection.")

    return parser


async def _run(args: argparse.Namespace) -> int:
    try:
        return await args.handler(args)
    finally:
        await engine.dispose()


def main() -> None:
    args = build_parser().parse_args()
    try:
        code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        code = EXIT_FAILED
    sys.exit(code)


if __name__ == "__main__":
    main()