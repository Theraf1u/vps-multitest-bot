from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import keyboards as kb
from bot.config import settings
from bot.database import crud
from bot.services import notify
from bot.states import Broadcast

log = logging.getLogger(__name__)
router = Router(name="broadcast")
router.message.filter(F.from_user.id == settings.admin_id)
router.callback_query.filter(F.from_user.id == settings.admin_id)

# Delay between individual sends during a mass broadcast — keeps us comfortably under
# Telegram's ~30 msg/sec global bot rate limit while still finishing in reasonable time.
SEND_DELAY = 0.05
PROGRESS_EVERY = 25


async def _segment_user_ids(category: str, days: int | None) -> list[int]:
    if category == "all":
        return await crud.broadcast_all_user_ids()
    if category == "active":
        return await crud.broadcast_active_user_ids(days or 7)
    if category == "inactive":
        return await crud.broadcast_inactive_user_ids(days or 7)
    if category == "limit_hit":
        return await crud.broadcast_limit_hit_user_ids()
    if category == "never_ran":
        return await crud.broadcast_never_ran_user_ids()
    return []


def _category_label(category: str, days: int | None) -> str:
    title = kb.BROADCAST_CATEGORIES.get(category, category)
    if category in ("active", "inactive") and days:
        return f"{title.split('(')[0].strip()} ({days} дн.)"
    return title


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Broadcast.choosing_category)
    await cb.message.edit_text(
        "📢 Рассылка\n\nВыберите, кому отправить сообщение:", reply_markup=kb.broadcast_categories()
    )
    await cb.answer()


@router.callback_query(F.data.startswith("bcast:cat:"), Broadcast.choosing_category)
async def broadcast_pick_category(cb: CallbackQuery, state: FSMContext) -> None:
    category = cb.data.split(":", 2)[2]
    if category in ("active", "inactive"):
        await state.update_data(category=category)
        await cb.message.edit_text(
            f"{kb.BROADCAST_CATEGORIES[category]}\n\nЗа сколько дней считать активность?",
            reply_markup=kb.broadcast_days(category),
        )
        await cb.answer()
        return

    await state.update_data(category=category, days=None)
    await state.set_state(Broadcast.waiting_message)
    await cb.message.edit_text(
        f"Сегмент: {kb.BROADCAST_CATEGORIES[category]}\n\n"
        "Пришлите сообщение для рассылки прямо сюда — текстом, с фото, форматированием, "
        "премиум-эмодзи и т.д. Отправится один в один, как получите.",
    )
    await cb.answer()


@router.callback_query(F.data.startswith("bcast:days:"), Broadcast.choosing_category)
async def broadcast_pick_days(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, category, days_s = cb.data.split(":")
    days = int(days_s)
    await state.update_data(category=category, days=days)
    await state.set_state(Broadcast.waiting_message)
    await cb.message.edit_text(
        f"Сегмент: {_category_label(category, days)}\n\n"
        "Пришлите сообщение для рассылки прямо сюда — текстом, с фото, форматированием, "
        "премиум-эмодзи и т.д. Отправится один в один, как получите.",
    )
    await cb.answer()


@router.message(Broadcast.waiting_message)
async def broadcast_got_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    category, days = data["category"], data.get("days")

    await state.update_data(from_chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(Broadcast.confirm)

    recipients = await _segment_user_ids(category, days)
    await state.update_data(recipient_count=len(recipients))

    await message.answer(
        f"Сегмент: {_category_label(category, days)}\nПолучателей: {len(recipients)}\n\n"
        "Сообщение выше будет разослано именно в таком виде. Что дальше?",
        reply_markup=kb.broadcast_confirm(len(recipients)),
    )


@router.callback_query(F.data == "bcast:test", Broadcast.confirm)
async def broadcast_test(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        await cb.bot.copy_message(
            chat_id=settings.admin_id, from_chat_id=data["from_chat_id"], message_id=data["message_id"]
        )
        await cb.answer("Тестовое сообщение отправлено вам выше ⬆️", show_alert=True)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await cb.answer(f"Не удалось отправить тест: {e}", show_alert=True)


@router.callback_query(F.data == "bcast:cancel", Broadcast.confirm)
async def broadcast_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_text("❌ Рассылка отменена.", reply_markup=kb.back_to_admin())
    await cb.answer()


@router.callback_query(F.data == "bcast:go", Broadcast.confirm)
async def broadcast_go(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    category, days = data["category"], data.get("days")
    from_chat_id, message_id = data["from_chat_id"], data["message_id"]
    await state.clear()

    recipients = await _segment_user_ids(category, days)
    total = len(recipients)
    await cb.answer()
    progress_msg = await cb.message.edit_text(f"🚀 Рассылка запущена: 0/{total}", reply_markup=None)

    sent = failed = blocked = 0
    for i, user_id in enumerate(recipients, start=1):
        try:
            await cb.bot.copy_message(chat_id=user_id, from_chat_id=from_chat_id, message_id=message_id)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await cb.bot.copy_message(chat_id=user_id, from_chat_id=from_chat_id, message_id=message_id)
                sent += 1
            except (TelegramBadRequest, TelegramForbiddenError):
                failed += 1
        except TelegramBadRequest:
            failed += 1
        except Exception:
            log.exception("broadcast send failed for user %s", user_id)
            failed += 1

        if i % PROGRESS_EVERY == 0 or i == total:
            try:
                await progress_msg.edit_text(f"🚀 Рассылка идёт: {i}/{total}")
            except TelegramBadRequest:
                pass
        await asyncio.sleep(SEND_DELAY)

    summary = (
        f"✅ Рассылка завершена.\n\n"
        f"Сегмент: {_category_label(category, days)}\n"
        f"Доставлено: {sent}\n"
        f"Заблокировали бота: {blocked}\n"
        f"Ошибок: {failed}"
    )
    await progress_msg.edit_text(summary, reply_markup=kb.back_to_admin())

    who = f"@{cb.from_user.username}" if cb.from_user.username else str(cb.from_user.id)
    await notify.notify_text(
        cb.bot, "broadcasts",
        f"📢 Рассылка ({_category_label(category, days)}) от {who}: доставлено {sent}, "
        f"заблокировали {blocked}, ошибок {failed} (всего получателей {total})",
    )
