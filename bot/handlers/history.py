from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile

from bot import keyboards as kb
from bot.config import settings
from bot.database import crud
from bot.handlers import test_flow
from bot.services import archive
from bot.services import test_info
from bot.states import TestFlow

router = Router(name="history")

PAGE_SIZE = 8


@router.callback_query(F.data == "menu:main")
async def back_to_menu(cb: CallbackQuery) -> None:
    is_admin = cb.from_user.id == settings.admin_id
    await cb.message.edit_text(
        test_info.build_menu_intro_text(),
        reply_markup=kb.main_menu(is_admin),
        parse_mode="Markdown",
    )
    await cb.answer()


def _who(cb: CallbackQuery) -> str:
    return f"@{cb.from_user.username}" if cb.from_user.username else str(cb.from_user.id)


@router.callback_query(F.data == "info:tests")
async def show_test_info(cb: CallbackQuery) -> None:
    await test_flow._log_activity(cb.bot, cb.from_user.id, _who(cb), "📋 Посмотрел описание тестов")
    await cb.message.edit_text(
        test_info.build_test_info_text(),
        reply_markup=kb.back_to_main_menu(),
        parse_mode="Markdown",
    )
    await cb.answer()


@router.callback_query(F.data.startswith("hist:list:"))
async def history_list(cb: CallbackQuery) -> None:
    page = int(cb.data.split(":")[2])
    user_id = cb.from_user.id
    if page == 0:  # только первый заход в историю, не каждое пролистывание страниц
        await test_flow._log_activity(cb.bot, user_id, _who(cb), "📜 Открыл историю проверок")
    runs = await crud.list_user_runs(user_id, limit=PAGE_SIZE * (page + 1) + 1)
    page_runs = runs[page * PAGE_SIZE : page * PAGE_SIZE + PAGE_SIZE]
    has_more = len(runs) > page * PAGE_SIZE + PAGE_SIZE

    if not page_runs and page == 0:
        await cb.message.edit_text("📜 История пуста — вы ещё не запускали проверок.", reply_markup=kb.back_to_main_menu())
        await cb.answer()
        return

    await cb.message.edit_text("📜 История проверок:", reply_markup=kb.history_list(page_runs, page, has_more))
    await cb.answer()


@router.callback_query(F.data.startswith("hist:item:"))
async def history_item(cb: CallbackQuery) -> None:
    run_id = int(cb.data.split(":")[2])
    run = await crud.get_test_run(run_id)
    if not run or run.user_id != cb.from_user.id:
        await cb.answer("Не найдено.", show_alert=True)
        return

    await test_flow._log_activity(
        cb.bot, cb.from_user.id, _who(cb), f"📄 Открыл детали прогона #{run_id}: {run.host}:{run.port}"
    )

    base = archive.find_report_base(cb.from_user.id, run_id)
    has_pdf = bool(base and archive.report_pdf_path(cb.from_user.id, base))
    ai_available = run.status == "success" and await _ai_available(cb.from_user.id, run_id)

    lines = [
        f"Сервер: {run.host}:{run.port}",
        f"Начато: {run.started_at:%Y-%m-%d %H:%M}",
        f"Статус: {run.status}",
    ]
    if run.status == "success":
        lines.append(f"Успешно: {run.tests_ok}, ошибок: {run.tests_failed}")
    if run.error:
        lines.append(f"Ошибка: {run.error[:300]}")

    await cb.message.edit_text(
        "\n".join(lines), reply_markup=kb.history_item(run_id, run.status, has_pdf, ai_available)
    )
    await cb.answer()


async def _ai_available(user_id: int, run_id: int) -> bool:
    """Whether the "🤖 Проанализировать" button should be offered from history: AI must be
    on, a key configured, this run's compact payload must have been archived (it wasn't, on
    runs from before this feature or ones where AI was off/misconfigured at the time), and the
    per-run cap (test_flow.AI_ANALYSES_LIMIT) not yet spent."""
    ai_enabled = (await crud.get_setting("ai_enabled", "1")) == "1"
    if not ai_enabled:
        return False
    from bot.services.crypto import decrypt

    stored_key = await crud.get_setting("openrouter_api_key")
    api_key = decrypt(stored_key) if stored_key else settings.openrouter_api_key
    if not api_key:
        return False
    if archive.load_ai_payload(user_id, run_id) is None:
        return False
    return await crud.count_ai_analyses(run_id) < test_flow.AI_ANALYSES_LIMIT


@router.callback_query(F.data.startswith("hist:cancel:"))
async def history_cancel(cb: CallbackQuery) -> None:
    run_id = int(cb.data.split(":")[2])
    run = await crud.get_test_run(run_id)
    if not run or run.user_id != cb.from_user.id:
        await cb.answer("Не найдено.", show_alert=True)
        return
    if run.status != "running" or not await test_flow.cancel_running(cb.from_user.id):
        await cb.answer("Этот тест уже не выполняется.", show_alert=True)
        return
    await test_flow._log_activity(
        cb.bot, cb.from_user.id, _who(cb), f"🛑 Остановил тест из истории: {run.host}:{run.port}"
    )
    await cb.answer("Останавливаю...")
    await cb.message.edit_text("🛑 Останавливаю тест и удаляю временные файлы на сервере...")


@router.callback_query(F.data.startswith("hist:retry:"))
async def history_retry(cb: CallbackQuery, state: FSMContext) -> None:
    """No credentials are ever persisted (see README "Безопасность"), so a retry can only
    prefill the host:port — login/password still have to be typed again."""
    run_id = int(cb.data.split(":")[2])
    run = await crud.get_test_run(run_id)
    if not run or run.user_id != cb.from_user.id:
        await cb.answer("Не найдено.", show_alert=True)
        return

    if await crud.is_maintenance_mode():
        await crud.add_maintenance_waiter(cb.from_user.id)
        await cb.answer(await crud.get_maintenance_message(), show_alert=True)
        return

    await test_flow._log_activity(
        cb.bot, cb.from_user.id, _who(cb), f"🔄 Повторил тест из истории: {run.host}:{run.port}"
    )

    await test_flow.cleanup_abandoned(cb.from_user.id, cb.bot)
    await state.clear()
    await state.update_data(host=run.host, port=run.port)
    await state.set_state(TestFlow.waiting_login)
    await cb.message.edit_text(
        f"Сервер: {run.host}:{run.port}\n\nВведите SSH login:",
        reply_markup=kb.login_step(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("hist:ai:"))
async def history_ai(cb: CallbackQuery) -> None:
    run_id = int(cb.data.split(":")[2])
    run = await crud.get_test_run(run_id)
    if not run or run.user_id != cb.from_user.id:
        await cb.answer("Не найдено.", show_alert=True)
        return

    if not await _ai_available(cb.from_user.id, run_id):
        await cb.answer("AI-анализ для этого теста сейчас недоступен.", show_alert=True)
        return

    payload = archive.load_ai_payload(cb.from_user.id, run_id)
    await cb.answer()
    model = await crud.get_setting("openrouter_model", settings.openrouter_model)
    await cb.message.edit_text(f"🤖 Анализирую отчёт через {model}... ⏳", reply_markup=None)
    await test_flow._log_activity(
        cb.bot, cb.from_user.id, _who(cb), f"🤖 Запросил AI-анализ из истории: {run.host}:{run.port}"
    )

    result = await test_flow.run_ai_analysis(cb.bot, cb.from_user.id, run_id, payload)
    if result is None:
        await cb.message.edit_text(
            "❌ Не удалось получить AI-анализ (OpenRouter не ответил).",
            reply_markup=kb.back_to_history_item(run_id),
        )
        return

    await cb.message.edit_text(
        f"🤖 Анализ ({run.host}:{run.port})\n\n{result.text}",
        reply_markup=kb.back_to_history_item(run_id),
    )


@router.callback_query(F.data.startswith("hist:pdf:"))
async def history_pdf(cb: CallbackQuery) -> None:
    run_id = int(cb.data.split(":")[2])
    run = await crud.get_test_run(run_id)
    if not run or run.user_id != cb.from_user.id:
        await cb.answer("Не найдено.", show_alert=True)
        return

    base = archive.find_report_base(cb.from_user.id, run_id)
    pdf_path = archive.report_pdf_path(cb.from_user.id, base) if base else None
    if not pdf_path:
        await cb.answer("PDF не найден.", show_alert=True)
        return

    await cb.answer()
    await cb.message.answer_document(
        FSInputFile(pdf_path, filename=archive.report_filename(run.host, run.started_at.strftime("%Y-%m-%d")))
    )
    await test_flow._log_activity(
        cb.bot, cb.from_user.id, _who(cb), f"📄 Запросил повторную отправку PDF из истории: {run.host}:{run.port}"
    )
