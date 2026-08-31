from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import delete, func, select, update

from bot.database.db import async_session
from bot.database.models import (
    KnownFingerprint,
    OpenRouterUsage,
    RateLimit,
    Setting,
    TestRating,
    TestRun,
    User,
    utcnow,
)


async def upsert_user(user_id: int, username: str | None) -> bool:
    """Returns True if this is the user's first-ever /start (a fresh registration)."""
    async with async_session() as s:
        obj = await s.get(User, user_id)
        is_new = obj is None
        if obj is None:
            s.add(User(id=user_id, username=username))
        else:
            obj.username = username
            obj.last_seen = utcnow()
        await s.commit()
        return is_new


async def get_setting(key: str, default: str | None = None) -> str | None:
    async with async_session() as s:
        obj = await s.get(Setting, key)
        return obj.value if obj else default


async def set_setting(key: str, value: str | None) -> None:
    async with async_session() as s:
        obj = await s.get(Setting, key)
        if obj is None:
            s.add(Setting(key=key, value=value))
        else:
            obj.value = value
        await s.commit()


DEFAULT_MAINTENANCE_MESSAGE = (
    "🛠 Сейчас ведутся технические работы, совсем скоро всё заработает.\n"
    "Мы пришлём сообщение, как только можно будет запустить проверку."
)

MAINTENANCE_MODE_KEY = "maintenance_mode"
MAINTENANCE_MESSAGE_KEY = "maintenance_message"
MAINTENANCE_WAITERS_KEY = "maintenance_waiters"


async def is_maintenance_mode() -> bool:
    return (await get_setting(MAINTENANCE_MODE_KEY, "0")) == "1"


async def set_maintenance_mode(enabled: bool) -> None:
    await set_setting(MAINTENANCE_MODE_KEY, "1" if enabled else "0")


async def get_maintenance_message() -> str:
    return await get_setting(MAINTENANCE_MESSAGE_KEY, DEFAULT_MAINTENANCE_MESSAGE) or DEFAULT_MAINTENANCE_MESSAGE


async def set_maintenance_message(text: str) -> None:
    await set_setting(MAINTENANCE_MESSAGE_KEY, text)


async def add_maintenance_waiter(user_id: int) -> None:
    """Remembers a user who tried to start a test while maintenance mode was on, so they can be
    pinged once it's switched off. Stored in Settings (not memory) so it survives the very bot
    restart maintenance mode exists to smooth over."""
    raw = await get_setting(MAINTENANCE_WAITERS_KEY, "[]")
    try:
        waiters: list[int] = json.loads(raw or "[]")
    except (ValueError, TypeError):
        waiters = []
    if user_id not in waiters:
        waiters.append(user_id)
        await set_setting(MAINTENANCE_WAITERS_KEY, json.dumps(waiters))


async def pop_maintenance_waiters() -> list[int]:
    """Returns and clears the list of users waiting on maintenance mode to end."""
    raw = await get_setting(MAINTENANCE_WAITERS_KEY, "[]")
    try:
        waiters: list[int] = json.loads(raw or "[]")
    except (ValueError, TypeError):
        waiters = []
    await set_setting(MAINTENANCE_WAITERS_KEY, "[]")
    return waiters


def _user_daily_limit_key(user_id: int) -> str:
    return f"daily_limit_user_{user_id}"


async def get_user_daily_limit(user_id: int) -> int | None:
    """Per-user override of the daily report limit (Setting key per user id). None means no
    override — caller should fall back to the global "daily_report_limit" default."""
    raw = await get_setting(_user_daily_limit_key(user_id))
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def set_user_daily_limit(user_id: int, value: int | None) -> None:
    """`value=None` clears the override (falls back to the global default again)."""
    await set_setting(_user_daily_limit_key(user_id), None if value is None else str(value))


async def try_acquire_slot(user_id: int, max_per_user: int, max_global: int) -> bool:
    """Atomically checks per-user and global concurrency limits; registers the slot if free."""
    async with async_session() as s:
        mine = await s.scalar(select(func.count()).select_from(RateLimit).where(RateLimit.user_id == user_id))
        if mine and mine >= max_per_user:
            return False
        total = await s.scalar(select(func.count()).select_from(RateLimit))
        if total and total >= max_global:
            return False
        s.add(RateLimit(user_id=user_id))
        await s.commit()
        return True


async def release_slot(user_id: int) -> None:
    async with async_session() as s:
        await s.execute(delete(RateLimit).where(RateLimit.user_id == user_id))
        await s.commit()


async def active_tests_count() -> int:
    async with async_session() as s:
        return int(await s.scalar(select(func.count()).select_from(RateLimit)) or 0)


async def count_runs_today(user_id: int) -> int:
    """Successful reports this user has produced since 00:00 UTC today — backs the
    admin-configurable daily per-user report limit (Setting "daily_report_limit")."""
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with async_session() as s:
        result = await s.execute(
            select(func.count()).select_from(TestRun).where(
                TestRun.user_id == user_id,
                TestRun.status == "success",
                TestRun.started_at >= start,
            )
        )
        return result.scalar_one()


async def create_test_run(user_id: int, host: str, port: int) -> int:
    async with async_session() as s:
        run = TestRun(user_id=user_id, host=host, port=port)
        s.add(run)
        await s.commit()
        return run.id


async def finish_test_run(run_id: int, status: str, tests_ok: int, tests_failed: int, error: str | None = None) -> None:
    async with async_session() as s:
        await s.execute(
            update(TestRun)
            .where(TestRun.id == run_id)
            .values(
                status=status,
                tests_ok=tests_ok,
                tests_failed=tests_failed,
                error=error,
                finished_at=utcnow(),
            )
        )
        await s.commit()


async def get_known_fingerprint(user_id: int, host: str, port: int) -> str | None:
    async with async_session() as s:
        obj = await s.scalar(
            select(KnownFingerprint).where(
                KnownFingerprint.user_id == user_id,
                KnownFingerprint.host == host,
                KnownFingerprint.port == port,
            )
        )
        return obj.fingerprint if obj else None


async def remember_fingerprint(user_id: int, host: str, port: int, fingerprint: str) -> None:
    async with async_session() as s:
        obj = await s.scalar(
            select(KnownFingerprint).where(
                KnownFingerprint.user_id == user_id,
                KnownFingerprint.host == host,
                KnownFingerprint.port == port,
            )
        )
        if obj is None:
            s.add(KnownFingerprint(user_id=user_id, host=host, port=port, fingerprint=fingerprint))
        else:
            obj.fingerprint = fingerprint
            obj.updated_at = utcnow()
        await s.commit()


async def list_recent_users(limit: int = 10) -> list[User]:
    async with async_session() as s:
        rows = await s.scalars(select(User).order_by(User.last_seen.desc()).limit(limit))
        return list(rows)


async def list_users_page(offset: int = 0, limit: int = 10) -> list[User]:
    async with async_session() as s:
        rows = await s.scalars(
            select(User).order_by(User.last_seen.desc()).offset(offset).limit(limit)
        )
        return list(rows)


async def count_users() -> int:
    async with async_session() as s:
        return int(await s.scalar(select(func.count()).select_from(User)) or 0)


async def get_user(user_id: int) -> User | None:
    async with async_session() as s:
        return await s.get(User, user_id)


async def list_user_runs(user_id: int, limit: int = 10) -> list[TestRun]:
    async with async_session() as s:
        rows = await s.scalars(
            select(TestRun)
            .where(TestRun.user_id == user_id)
            .order_by(TestRun.started_at.desc())
            .limit(limit)
        )
        return list(rows)


async def get_test_run(run_id: int) -> TestRun | None:
    async with async_session() as s:
        return await s.get(TestRun, run_id)


async def delete_user(user_id: int) -> None:
    """Wipes every DB row tied to this user (test runs, fingerprints, rate-limit slot,
    OpenRouter usage). Does not touch the filesystem archive — see archive.delete_user_dir."""
    async with async_session() as s:
        for model in (TestRun, KnownFingerprint, RateLimit, OpenRouterUsage):
            await s.execute(delete(model).where(model.user_id == user_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def count_ai_analyses(run_id: int) -> int:
    """Successful OpenRouter analyses already spent on this test run — used to cap the AI
    button at 2 uses per run (one right after the test, one from history)."""
    async with async_session() as s:
        result = await s.execute(
            select(func.count()).select_from(OpenRouterUsage).where(
                OpenRouterUsage.run_id == run_id, OpenRouterUsage.ok.is_(True)
            )
        )
        return result.scalar_one()


async def record_openrouter_usage(
    user_id: int, run_id: int | None, model: str, cost_usd: float, tokens_in: int, tokens_out: int, ok: bool
) -> None:
    async with async_session() as s:
        s.add(
            OpenRouterUsage(
                user_id=user_id,
                run_id=run_id,
                model=model,
                cost_usd=cost_usd,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                ok=ok,
            )
        )
        await s.commit()


async def openrouter_spend_summary() -> dict:
    async with async_session() as s:
        total_cost = await s.scalar(select(func.sum(OpenRouterUsage.cost_usd)))
        total_calls = await s.scalar(select(func.count()).select_from(OpenRouterUsage))
        today_start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        today_cost = await s.scalar(
            select(func.sum(OpenRouterUsage.cost_usd)).where(OpenRouterUsage.created_at >= today_start)
        )
        today_calls = await s.scalar(
            select(func.count()).select_from(OpenRouterUsage).where(OpenRouterUsage.created_at >= today_start)
        )
        failed_calls = await s.scalar(
            select(func.count()).select_from(OpenRouterUsage).where(OpenRouterUsage.ok.is_(False))
        )
        return {
            "total_cost": total_cost or 0.0,
            "total_calls": total_calls or 0,
            "today_cost": today_cost or 0.0,
            "today_calls": today_calls or 0,
            "failed_calls": failed_calls or 0,
        }


async def stats_summary() -> dict:
    async with async_session() as s:
        total_users = await s.scalar(select(func.count()).select_from(User))
        total_tests = await s.scalar(select(func.count()).select_from(TestRun))
        today_start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        tests_today = await s.scalar(
            select(func.count()).select_from(TestRun).where(TestRun.started_at >= today_start)
        )
        success = await s.scalar(select(func.count()).select_from(TestRun).where(TestRun.status == "success"))
        errors = await s.scalar(
            select(func.count()).select_from(TestRun).where(TestRun.status.in_(("error", "cancelled")))
        )
        active = await s.scalar(select(func.count()).select_from(RateLimit))
        return {
            "total_users": total_users or 0,
            "total_tests": total_tests or 0,
            "tests_today": tests_today or 0,
            "success": success or 0,
            "errors": errors or 0,
            "active": active or 0,
        }


async def broadcast_all_user_ids() -> list[int]:
    async with async_session() as s:
        result = await s.execute(select(User.id))
        return [r[0] for r in result.all()]


async def broadcast_active_user_ids(days: int) -> list[int]:
    """Used the bot (opened /start, or ran a test) within the last `days` days."""
    since = utcnow() - dt.timedelta(days=days)
    async with async_session() as s:
        seen = await s.execute(select(User.id).where(User.last_seen >= since))
        ran = await s.execute(select(TestRun.user_id).where(TestRun.started_at >= since).distinct())
        return list({r[0] for r in seen.all()} | {r[0] for r in ran.all()})


async def broadcast_inactive_user_ids(days: int) -> list[int]:
    active_ids = set(await broadcast_active_user_ids(days))
    all_ids = set(await broadcast_all_user_ids())
    return list(all_ids - active_ids)


async def broadcast_never_ran_user_ids() -> list[int]:
    async with async_session() as s:
        result = await s.execute(
            select(User.id).outerjoin(TestRun, TestRun.user_id == User.id).where(TestRun.id.is_(None))
        )
        return [r[0] for r in result.all()]


async def broadcast_limit_hit_user_ids() -> list[int]:
    """Users who've already used up today's daily report-limit quota (unlimited users never
    qualify). Mirrors the limit logic in test_flow._daily_limit, minus the admin-never-capped rule
    since the admin isn't a broadcast target anyway."""
    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with async_session() as s:
        result = await s.execute(
            select(TestRun.user_id, func.count())
            .where(TestRun.status == "success", TestRun.started_at >= today_start)
            .group_by(TestRun.user_id)
        )
        counts = dict(result.all())

    default_limit = int(await get_setting("daily_report_limit", "5") or "5")
    ids = []
    for user_id, used in counts.items():
        override = await get_user_daily_limit(user_id)
        limit = override if override is not None else default_limit
        if limit and used >= limit:
            ids.append(user_id)
    return ids


async def save_rating(run_id: int, user_id: int, rating: int) -> int:
    async with async_session() as s:
        obj = TestRating(run_id=run_id, user_id=user_id, rating=rating)
        s.add(obj)
        await s.commit()
        return obj.id


async def save_rating_comment(rating_id: int, comment: str) -> None:
    async with async_session() as s:
        obj = await s.get(TestRating, rating_id)
        if obj:
            obj.comment = comment
            await s.commit()
