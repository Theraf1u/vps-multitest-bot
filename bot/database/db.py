from __future__ import annotations

import os

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings
from bot.database.models import Base

os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)

engine = create_async_engine(f"sqlite+aiosqlite:///{settings.db_path}", echo=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record) -> None:
    # Default SQLite locking (rollback journal) serializes ALL access — readers block on a
    # writer too — which falls over once more than a handful of test runs write to the DB
    # concurrently (RateLimit rows, TestRun status, progress polling). WAL lets readers and
    # the writer run concurrently; busy_timeout makes a genuine write/write collision wait and
    # retry for 5s instead of raising "database is locked" immediately.
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
