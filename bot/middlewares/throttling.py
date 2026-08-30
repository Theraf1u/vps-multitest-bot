from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject


class ThrottlingMiddleware(BaseMiddleware):
    """Simple per-user cooldown against /start and inline-button spam — the bot had no rate
    limiting at all outside actual test-execution concurrency, so a single user hammering
    /start or callback buttons could flood the Telegram API and DB with no cost to them.
    Shared across message and callback updates (same instance registered for both) so
    interleaving the two doesn't bypass the cooldown. The admin is exempt — this is meant to
    stop abuse from arbitrary users, not slow down the operator's own clicking."""

    def __init__(self, rate_limit: float = 0.6, admin_id: int | None = None):
        self.rate_limit = rate_limit
        self.admin_id = admin_id
        self._last_seen: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None and user.id != self.admin_id:
            now = time.monotonic()
            last = self._last_seen.get(user.id, 0.0)
            if now - last < self.rate_limit:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ Слишком быстро — подождите немного.")
                # Messages: drop silently — replying would just add more bot traffic to the flood.
                return None
            self._last_seen[user.id] = now
        return await handler(event, data)
