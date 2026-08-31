from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, Message

from bot.database import crud

log = logging.getLogger(__name__)

# Categories the admin panel lets you point at a forum topic. One shared chat id,
# each category optionally pinned to its own topic (message_thread_id) inside it.
CATEGORIES: dict[str, str] = {
    "reports": "📄 Отчёты (PDF)",
    "registrations": "👤 Регистрации пользователей",
    "errors": "⚠️ Ошибки тестов",
    "test_errors": "📉 Тест завершился с ошибкой",
    "test_starts": "🚀 Старты тестов",
    "user_activity": "📋 Действия пользователей",
    "ai_usage": "🤖 AI-анализы",
    "rate_limits": "⏳ Лимиты и отказы",
    "system": "🖥 Системные события",
    "ratings": "⭐ Оценки работы бота",
    "broadcasts": "📢 Рассылки",
}

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


async def notify_send(bot: Bot, category: str, text: str, **kwargs) -> Message | None:
    """Like notify_text but returns the sent Message so the caller can later edit it in place
    (see notify_edit) — used to mirror a live-updating progress screen into the admin topic."""
    target = await _target(category)
    if not target:
        return None
    chat_id, thread_id = target
    try:
        return await bot.send_message(chat_id, text, message_thread_id=thread_id, **kwargs)
    except TelegramAPIError as e:
        log.warning("notify_send(%s) failed: %s", category, e)
        return None


async def notify_edit(bot: Bot, chat_id: int, message_id: int, text: str, **kwargs) -> None:
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, **kwargs)
    except TelegramAPIError as e:
        if "message is not modified" not in str(e):
            log.debug("notify_edit failed: %s", e)


async def notify_maintenance_ended(bot: Bot) -> int:
    """Called right after maintenance mode is switched off — pops everyone who tried to start
    a test while it was on and sends each of them two messages in sequence: a short "techworks
    are over" notice, then the exact same screen a fresh /start would show (test list + main
    menu button), so they don't have to type /start themselves to get going again. Returns how
    many people were notified."""
    from bot.config import settings
    from bot import keyboards as kb
    from bot.services import test_info

    waiters = await crud.pop_maintenance_waiters()
    sent = 0
    for uid in waiters:
        try:
            await bot.send_message(uid, "✅ Технические работы завершены — можно запускать проверку.")
            await bot.send_message(
                uid,
                test_info.build_menu_intro_text(),
                reply_markup=kb.main_menu(uid == settings.admin_id),
                parse_mode="Markdown",
            )
            sent += 1
        except TelegramAPIError as e:
            log.warning("notify_maintenance_ended: failed to message user %s: %s", uid, e)
    return sent


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
