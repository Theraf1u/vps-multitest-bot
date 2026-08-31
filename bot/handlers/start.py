from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import keyboards as kb
from bot.config import settings
from bot.database import crud
from bot.handlers.test_flow import cleanup_abandoned
from bot.services import notify, test_info

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await cleanup_abandoned(message.from_user.id, message.bot)
    is_new = await crud.upsert_user(message.from_user.id, message.from_user.username)
    is_admin = message.from_user.id == settings.admin_id

    if is_new:
        uname = f"@{message.from_user.username}" if message.from_user.username else "без username"
        await notify.notify_text(
            message.bot, "registrations", f"👤 Новый пользователь: {uname} (id {message.from_user.id})"
        )

    if await crud.is_maintenance_mode():
        # Только сообщение о техработах — ни списка тестов, ни кнопки. Как только техработы
        # закончатся, этому же пользователю придёт отдельное уведомление, а следом — обычный
        # экран /start (см. crud.add_maintenance_waiter / notify_maintenance_waiters_off).
        await crud.add_maintenance_waiter(message.from_user.id)
        await message.answer(await crud.get_maintenance_message())
        return

    await message.answer(
        test_info.build_menu_intro_text(),
        reply_markup=kb.main_menu(is_admin),
        parse_mode="Markdown",
    )
