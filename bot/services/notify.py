from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile

from bot.database import crud

log = logging.getLogger(__name__)

# Categories the admin panel lets you point at a forum topic. One shared chat id,
# each category optionally pinned to its own topic (message_thread_id) inside it.
CATEGORIES: dict[str, str] = {
    "reports": "📄 Отчёты (PDF)",
    "registrations": "👤 Регистрации пользователей",
    "errors": "⚠️ Ошибки тестов",
    "test_starts": "🚀 Старты тестов",
    "ai_usage": "🤖 AI-анализы",
    "rate_limits": "⏳ Лимиты и отказы",
    "system": "🖥 Системные события",
}
# Deliberately no "credentials" category: this fork never forwards SSH host/login/password
# anywhere, including here — see README "Безопасность".

CHAT_KEY = "notify_chat_id"


def _topic_key(category: str) -> str:
    return f"notify_topic_{category}"


async def get_config() -> dict[str, str | None]:
    chat_id = await crud.get_setting(CHAT_KEY)
    cfg = {"chat_id": chat_id}
    for cat in CATEGORIES:
        cfg[cat] = await crud.get_setting(_topic_key(cat))
    return cfg


async def set_chat_id(value: str | None) -> None:
    await crud.set_setting(CHAT_KEY, value)


async def set_topic(category: str, value: str | None) -> None:
    await crud.set_setting(_topic_key(category), value)


async def _target(category: str) -> tuple[int, int | None] | None:
    chat_id = await crud.get_setting(CHAT_KEY)
    if not chat_id:
        return None
    topic_raw = await crud.get_setting(_topic_key(category))
    thread_id = int(topic_raw) if topic_raw else None
    try:
        return int(chat_id), thread_id
    except ValueError:
        return None


async def auto_create_topics(bot: Bot) -> dict[str, str]:
    """Creates a forum topic (via the Bot API, requires the bot to be an admin with "Manage
    Topics" in the target supergroup) for every category that doesn't have one configured yet,
    and saves each new thread id. Avoids the old failure mode where an unconfigured category
    silently fell back to the group's General topic. Returns {category: outcome} for the
    admin screen to report back."""
    chat_id = await crud.get_setting(CHAT_KEY)
    if not chat_id:
        return {"_error": "Chat ID не задан — сначала укажите его."}

    results: dict[str, str] = {}
    for cat, title in CATEGORIES.items():
        existing = await crud.get_setting(_topic_key(cat))
        if existing:
            results[cat] = f"уже есть (топик {existing})"
            continue
        try:
            topic = await bot.create_forum_topic(int(chat_id), title)
        except TelegramAPIError as e:
            results[cat] = f"ошибка: {e}"
            log.warning("auto_create_topics(%s) failed: %s", cat, e)
            continue
        await set_topic(cat, str(topic.message_thread_id))
        results[cat] = f"создан (топик {topic.message_thread_id})"
    return results


async def notify_text(bot: Bot, category: str, text: str) -> None:
    target = await _target(category)
    if not target:
        return
    chat_id, thread_id = target
    try:
        await bot.send_message(chat_id, text, message_thread_id=thread_id)
    except TelegramAPIError as e:
        log.warning("notify_text(%s) failed: %s", category, e)


async def notify_document(bot: Bot, category: str, file_path: str, filename: str, caption: str | None = None) -> None:
    target = await _target(category)
    if not target:
        return
    chat_id, thread_id = target
    try:
        await bot.send_document(
            chat_id,
            FSInputFile(file_path, filename=filename),
            caption=caption,
            message_thread_id=thread_id,
        )
    except TelegramAPIError as e:
        log.warning("notify_document(%s) failed: %s", category, e)
