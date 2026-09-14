"""Async database engine, session factory and declarative base."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import settings

# Predictable constraint names keep Alembic autogenerate diffs clean.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


def _utcnow() -> datetime:
    """Timezone-aware now, evaluated in Python rather than by the database.

    See `TimestampMixin` below for why that distinction is load-bearing.
    """
    return datetime.now(UTC)


class TimestampMixin:
    """`created_at` / `updated_at` on every row.

    Both timestamps are generated **in Python**, not by a SQL expression, and
    that is the whole point of this docstring — the previous version used
    `onupdate=func.now()` and it was silently breaking every write endpoint in
    the application.

    Here is the mechanism. When `onupdate` is a SQL expression, SQLAlchemy
    cannot know the value the database computed, so after the UPDATE statement
    flushes it marks the attribute **expired**. The next time anything reads
    `obj.updated_at` the ORM has to go back to the database to fetch it. In a
    synchronous app that is an invisible extra SELECT. In an async app it is a
    crash: the ORM attempts I/O from a plain attribute access, outside any
    `await`, and SQLAlchemy raises

        MissingGreenlet: greenlet_spawn has not been called;
        can't call await_only() here.

    Every admin endpoint that mutates a row and then serialises it — which is
    all of them — read `updated_at` immediately after `await db.flush()`. So
    every PATCH, every reorder, every toggle returned 500. INSERTs were
    unaffected, because on PostgreSQL SQLAlchemy 2.x fetches insert defaults
    eagerly via RETURNING, which is exactly why "add an image" worked while
    "hide an image" did not.

    A Python-side `default`/`onupdate` is assigned to the instance before the
    statement is emitted, so nothing is ever expired and nothing ever needs a
    round trip. `server_default=func.now()` is deliberately kept so that rows
    written outside the ORM — a migration, a psql session, a bulk `INSERT` —
    still get correct timestamps. No schema change and no migration is
    required for this: `onupdate` never existed in the DDL, only in the ORM.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=_utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


engine = create_async_engine(
    settings.sqlalchemy_url,
    echo=settings.DB_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=1800,
)

SessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. Commits on success, rolls back on any exception."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise