from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import keyboards as kb
from bot.database import crud
from bot.services import notify
from bot.states import RatingFlow

router = Router(name="rating")


async def _safe_answer(cb: CallbackQuery, **kwargs) -> None:
    """cb.answer() must always run — it's what tells Telegram the tap was received, and
    skipping it (e.g. because an earlier step in the handler raised) is exactly what makes the
    client show the button as "outdated" on the next tap. A callback that's already genuinely
    expired can still fail here; that one case is unrecoverable and fine to swallow."""
    try:
        await cb.answer(**kwargs)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.regexp(r"^rate:\d+:[1-5]$"))
async def rate_test(cb: CallbackQuery) -> None:
    _, run_id_s, score_s = cb.data.split(":")
    run_id, score = int(run_id_s), int(score_s)

    try:
        rating_id = await crud.save_rating(run_id, cb.from_user.id, score)

        run = await crud.get_test_run(run_id)
        server = f"{run.host}:{run.port}" if run else f"run {run_id}"
        who = f"@{cb.from_user.username}" if cb.from_user.username else str(cb.from_user.id)
        stars = "⭐" * score + "▫️" * (5 - score)
        await notify.notify_text(
            cb.bot, "ratings",
            f"{stars} ({score}/5) — {server} — user {who} (id {cb.from_user.id})",
        )

        await cb.message.edit_text(f"Спасибо за оценку! {'⭐' * score}", reply_markup=kb.rating_thanks(rating_id))
    except TelegramBadRequest:
        # A duplicate/retried tap on the same button (flaky network, Telegram redelivery) lands
        # here as "message is not modified" once the first tap already applied this exact edit —
        # harmless, but must not skip cb.answer() below or the button reads as outdated forever.
        pass
    finally:
        await _safe_answer(cb)


@router.callback_query(F.data.startswith("rate:comment:"))
async def rate_comment_start(cb: CallbackQuery, state: FSMContext) -> None:
    rating_id = int(cb.data.split(":")[2])
    await state.update_data(rating_id=rating_id)
    await state.set_state(RatingFlow.waiting_comment)
    try:
        await cb.message.edit_text("💬 Напишите комментарий одним сообщением:")
    except TelegramBadRequest:
        pass
    finally:
        await _safe_answer(cb)


@router.message(RatingFlow.waiting_comment)
async def rate_comment_got(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rating_id = data.get("rating_id")
    comment = (message.text or message.caption or "").strip()
    await state.clear()

    if not rating_id or not comment:
        await message.answer("Не получилось сохранить комментарий — попробуйте ещё раз позже.")
        return

    await crud.save_rating_comment(rating_id, comment)
    who = f"@{message.chat.username}" if message.chat.username else str(message.from_user.id)
    await notify.notify_text(message.bot, "ratings", f"💬 Комментарий (user {who}, оценка #{rating_id}): {comment}")
    await message.answer("Спасибо, комментарий сохранён! 🙏")
