from __future__ import annotations

import asyncio
import logging
import os
import time

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import settings
from bot.database.db import init_db
from bot.handlers import build_root_router
from bot.middlewares.throttling import ThrottlingMiddleware
from bot.services import button_icons, notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot.main")


async def _cleanup_old_reports() -> None:
    """Deletes generated PDFs older than REPORT_TTL_HOURS. Runs forever in the background."""
    ttl_seconds = settings.report_ttl_hours * 3600
    while True:
        try:
            if os.path.isdir(settings.reports_dir):
                now = time.time()
                for name in os.listdir(settings.reports_dir):
                    path = os.path.join(settings.reports_dir, name)
                    try:
                        if now - os.path.getmtime(path) > ttl_seconds:
                            os.remove(path)
                    except OSError:
                        pass
        except Exception:
            log.exception("report cleanup pass failed")
        await asyncio.sleep(1800)


def _build_bot() -> Bot:
    session = None
    if settings.telegram_proxy_url:
        session = AiohttpSession(proxy=settings.telegram_proxy_url)
        log.info("Using proxy for Telegram API (sing-box)")
    kwargs = {}
    if settings.telegram_api_base:
        from aiogram.client.telegram import TelegramAPIServer

        kwargs["api"] = TelegramAPIServer.from_base(settings.telegram_api_base)
    return Bot(
        token=settings.bot_token,
        session=session,
        **kwargs,
    )


async def main() -> None:
    await init_db()
    await button_icons.load_all()

    bot = _build_bot()
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_root_router())

    # No rate limiting existed anywhere before this — a single user could hammer /start or
    # inline buttons with no cost. One shared instance for both observers so switching between
    # messages and callback taps doesn't reset the cooldown.
    throttling = ThrottlingMiddleware(rate_limit=0.6, admin_id=settings.admin_id)
    dp.message.outer_middleware(throttling)
    dp.callback_query.outer_middleware(throttling)

    asyncio.create_task(_cleanup_old_reports())

    await bot.delete_webhook(drop_pending_updates=True)
    await notify.notify_text(bot, "system", "🖥 Бот запущен и начал polling.")
    log.info("Starting polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
