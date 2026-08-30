"""CLI toggle for maintenance mode — run inside the container so a deploy can wrap the actual
container swap in a short "no new tests, please" window instead of silently killing whatever
a user just started (see the run-19 incident: a test began in the same second `docker rm -f`
fired, got orphaned, and its DB row stayed stuck on "running" forever).

Usage (from the host):
    docker exec vps-test-bot python -m bot.scripts.maintenance on
    docker exec vps-test-bot python -m bot.scripts.maintenance off   # also notifies waiters
    docker exec vps-test-bot python -m bot.scripts.maintenance status

Suggested deploy sequence: `on` -> wait a couple seconds for any handler mid-flight to finish
its current await -> rsync/build/recreate -> `off` once the new container is confirmed healthy.
"""

from __future__ import annotations

import asyncio
import sys

from aiogram.exceptions import TelegramBadRequest

from bot.database import crud
from bot.database.db import init_db


async def _notify_waiters(bot) -> int:
    waiters = await crud.pop_maintenance_waiters()
    sent = 0
    for uid in waiters:
        try:
            await bot.send_message(uid, "✅ Технические работы завершены — можно запускать проверку.")
            sent += 1
        except TelegramBadRequest:
            pass
    return sent


async def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("on", "off", "status"):
        print(__doc__)
        sys.exit(1)

    action = sys.argv[1]
    await init_db()

    if action == "status":
        enabled = await crud.is_maintenance_mode()
        print("on" if enabled else "off")
        return

    await crud.set_maintenance_mode(action == "on")

    if action == "off":
        from bot.main import _build_bot

        bot = _build_bot()
        try:
            sent = await _notify_waiters(bot)
        finally:
            await bot.session.close()
        print(f"maintenance: off (notified {sent} waiting user(s))")
    else:
        print("maintenance: on")


if __name__ == "__main__":
    asyncio.run(main())
