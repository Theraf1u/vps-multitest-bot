from __future__ import annotations

import os
import shutil

import httpx
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import keyboards as kb
from bot.config import settings
from bot.database import crud
from bot.services import archive
from bot.services import button_icons as icons
from bot.services import multitest as mt
from bot.services import notify
from bot.services import openrouter as orsvc
from bot.services import security
from bot.services.crypto import decrypt, encrypt
from bot.handlers import test_flow
from bot.states import AdminIcons, AdminLimits, AdminNotify, AdminOpenRouter

router = Router(name="admin")
router.message.filter(F.from_user.id == settings.admin_id)
router.callback_query.filter(F.from_user.id == settings.admin_id)


async def _current_or_key() -> str:
    stored = await crud.get_setting("openrouter_api_key")
    if stored:
        return decrypt(stored) or ""
    return settings.openrouter_api_key


@router.callback_query(F.data == "admin:menu")
async def admin_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_text("⚙️ Админ-панель", reply_markup=kb.admin_menu())
    await cb.answer()


@router.callback_query(F.data == "admin:stats")
async def admin_stats(cb: CallbackQuery) -> None:
    s = await crud.stats_summary()
    text = (
        "📊 Статистика\n\n"
        f"Всего пользователей: {s['total_users']}\n"
        f"Тестов сегодня: {s['tests_today']}\n"
        f"Всего тестов: {s['total_tests']}\n"
        f"Успешных: {s['success']}\n"
        f"Ошибок/отмен: {s['errors']}\n"
        f"Активных сейчас: {s['active']}"
    )
    await cb.message.edit_text(text, reply_markup=kb.back_to_admin())
    await cb.answer()


DEFAULT_USD_RUB_RATE = "95"


@router.callback_query(F.data == "admin:openrouter")
async def admin_openrouter(cb: CallbackQuery) -> None:
    model = await crud.get_setting("openrouter_model", settings.openrouter_model)
    fallback = await crud.get_setting("openrouter_fallback_model")
    key = await _current_or_key()
    ai_enabled = (await crud.get_setting("ai_enabled", "1")) == "1"
    max_tokens = await crud.get_setting("ai_max_tokens", str(orsvc.DEFAULT_MAX_TOKENS))
    spend = await crud.openrouter_spend_summary()
    rate = float(await crud.get_setting("usd_rub_rate", DEFAULT_USD_RUB_RATE))
    text = (
        "🤖 OpenRouter\n\n"
        f"Основная модель: `{model}`\n"
        f"Фолбэк модель: `{fallback or 'не задана'}`\n"
        f"API key: `{security.mask_key(key) if key else 'не задан'}`\n"
        f"AI-анализ: {'включён' if ai_enabled else 'выключен'}\n"
        f"Лимит токенов/отчёт: {max_tokens}\n\n"
        f"Расходы сегодня: ${spend['today_cost']:.4f} (≈{spend['today_cost'] * rate:.2f} ₽), {spend['today_calls']} запрос(ов)\n"
        f"Расходы всего: ${spend['total_cost']:.4f} (≈{spend['total_cost'] * rate:.2f} ₽), {spend['total_calls']} запрос(ов)"
    )
    await cb.message.edit_text(text, reply_markup=kb.admin_openrouter_menu(ai_enabled), parse_mode="Markdown")
    await cb.answer()


@router.callback_query(F.data == "admin:or_model")
async def admin_or_model(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminOpenRouter.waiting_model)
    await cb.message.edit_text("Введите новый идентификатор основной модели OpenRouter (например `openai/gpt-4o-mini`):", parse_mode="Markdown")
    await cb.answer()


@router.message(AdminOpenRouter.waiting_model)
async def admin_or_model_set(message: Message, state: FSMContext) -> None:
    model = (message.text or "").strip()
    if not model:
        await message.reply("Пусто, попробуйте снова.")
        return
    await crud.set_setting("openrouter_model", model)
    await state.clear()
    await message.answer(f"✅ Основная модель обновлена: `{model}`", reply_markup=kb.back_to_openrouter(), parse_mode="Markdown")


@router.callback_query(F.data == "admin:or_fallback")
async def admin_or_fallback(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminOpenRouter.waiting_fallback)
    await cb.message.edit_text(
        "Введите идентификатор фолбэк-модели (используется, если основная не ответила). "
        "Отправьте «-», чтобы убрать фолбэк.",
    )
    await cb.answer()


@router.message(AdminOpenRouter.waiting_fallback)
async def admin_or_fallback_set(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    value = None if raw == "-" else raw
    if not raw:
        await message.reply("Пусто, попробуйте снова.")
        return
    await crud.set_setting("openrouter_fallback_model", value)
    await state.clear()
    await message.answer(
        f"✅ Фолбэк модель: `{value or 'убрана'}`", reply_markup=kb.back_to_openrouter(), parse_mode="Markdown"
    )


@router.callback_query(F.data == "admin:or_maxtokens")
async def admin_or_maxtokens(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminOpenRouter.waiting_max_tokens)
    await cb.message.edit_text(
        "Введите лимит токенов ответа модели на один отчёт (влияет на длину и примерную "
        f"стоимость AI-анализа). Сейчас: {await crud.get_setting('ai_max_tokens', str(orsvc.DEFAULT_MAX_TOKENS))}"
    )
    await cb.answer()


@router.message(AdminOpenRouter.waiting_max_tokens)
async def admin_or_maxtokens_set(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.reply("❌ Нужно положительное целое число. Попробуйте снова.")
        return
    await crud.set_setting("ai_max_tokens", str(value))
    await state.clear()
    await message.answer(f"✅ Лимит токенов: {value}", reply_markup=kb.back_to_openrouter())


@router.callback_query(F.data == "admin:or_spend")
async def admin_or_spend(cb: CallbackQuery) -> None:
    spend = await crud.openrouter_spend_summary()
    rate = float(await crud.get_setting("usd_rub_rate", DEFAULT_USD_RUB_RATE))
    text = (
        "💰 Расходы OpenRouter\n\n"
        f"Сегодня: ${spend['today_cost']:.4f} ≈ {spend['today_cost'] * rate:.2f} ₽ "
        f"({spend['today_calls']} запрос(ов))\n"
        f"Всего: ${spend['total_cost']:.4f} ≈ {spend['total_cost'] * rate:.2f} ₽ "
        f"({spend['total_calls']} запрос(ов))\n"
        f"Неудачных запросов: {spend['failed_calls']}\n\n"
        f"Курс: 1 USD = {rate:.2f} ₽"
    )
    await cb.message.edit_text(text, reply_markup=kb.admin_spend_menu())
    await cb.answer()


@router.callback_query(F.data == "admin:or_rate")
async def admin_or_rate(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminOpenRouter.waiting_rate)
    await cb.message.edit_text("Введите курс USD → RUB (например 95.5):")
    await cb.answer()


@router.message(AdminOpenRouter.waiting_rate)
async def admin_or_rate_set(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.reply("❌ Нужно положительное число. Попробуйте снова.")
        return
    await crud.set_setting("usd_rub_rate", str(value))
    await state.clear()
    await message.answer(f"✅ Курс обновлён: 1 USD = {value:.2f} ₽", reply_markup=kb.admin_spend_menu())


@router.callback_query(F.data == "admin:or_key")
async def admin_or_key(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminOpenRouter.waiting_key)
    await cb.message.edit_text("Введите новый OpenRouter API key. Сообщение будет удалено сразу после сохранения.")
    await cb.answer()


@router.message(AdminOpenRouter.waiting_key)
async def admin_or_key_set(message: Message, state: FSMContext) -> None:
    key = (message.text or "").strip()
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    if not key:
        await message.answer("Пусто, попробуйте снова.")
        return
    await crud.set_setting("openrouter_api_key", encrypt(key))
    await state.clear()
    await message.answer(f"✅ Ключ обновлён: `{security.mask_key(key)}`", reply_markup=kb.back_to_openrouter(), parse_mode="Markdown")


@router.callback_query(F.data == "admin:or_check")
async def admin_or_check(cb: CallbackQuery) -> None:
    await cb.answer("Проверяю...")
    key = await _current_or_key()
    model = await crud.get_setting("openrouter_model", settings.openrouter_model)
    ok, msg = await orsvc.check_api_key(key, model, proxy=settings.openrouter_proxy_url or None)
    await cb.message.edit_text(
        f"{'✅' if ok else '❌'} {msg}",
        reply_markup=kb.back_to_openrouter(),
    )


@router.callback_query(F.data == "admin:or_toggle")
async def admin_or_toggle(cb: CallbackQuery) -> None:
    ai_enabled = (await crud.get_setting("ai_enabled", "1")) == "1"
    await crud.set_setting("ai_enabled", "0" if ai_enabled else "1")
    await admin_openrouter(cb)


@router.callback_query(F.data == "admin:multitest")
async def admin_multitest(cb: CallbackQuery) -> None:
    version = mt.pinned_version()
    sha = mt.pinned_sha256()
    text = (
        "🧪 Multitest\n\n"
        f"Версия (pinned): `{version}`\n"
        f"SHA256: `{sha[:16]}…`"
    )
    await cb.message.edit_text(text, reply_markup=kb.admin_multitest_menu(), parse_mode="Markdown")
    await cb.answer()


@router.callback_query(F.data == "admin:mt_check")
async def admin_mt_check(cb: CallbackQuery) -> None:
    await cb.answer("Проверяю GitHub...")
    try:
        info = await mt.check_github_update()
    except httpx.HTTPError as e:
        await cb.message.edit_text(f"❌ Не удалось обратиться к GitHub: {e}", reply_markup=kb.admin_multitest_menu())
        return
    if info["has_update"]:
        text = (
            "🔄 Доступно обновление!\n\n"
            f"Текущий SHA256: `{info['local_sha256'][:16]}…`\n"
            f"На GitHub SHA256: `{info['remote_sha256'][:16]}…`\n\n"
            "Нажмите «Обновить pinned script», чтобы подтянуть новую версию."
        )
    else:
        text = "✅ Pinned-версия совпадает с GitHub master, обновление не требуется."
    await cb.message.edit_text(text, reply_markup=kb.admin_multitest_menu(), parse_mode="Markdown")


@router.callback_query(F.data == "admin:mt_update")
async def admin_mt_update(cb: CallbackQuery) -> None:
    await cb.answer("Обновляю...")
    try:
        new_sha = await mt.update_pinned_script()
    except (mt.MultitestError, httpx.HTTPError) as e:
        await cb.message.edit_text(f"❌ Обновление не удалось: {e}", reply_markup=kb.admin_multitest_menu())
        return
    await cb.message.edit_text(
        f"✅ Pinned-скрипт обновлён.\nНовый SHA256: `{new_sha[:16]}…`\n\n"
        "⚠️ Проверьте, что порядок/названия 11 тестов в новой версии не изменились "
        "(bot/services/multitest.py::TEST_CATALOG), иначе прогресс-бар и отчёт будут врать.",
        reply_markup=kb.admin_multitest_menu(),
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "admin:status")
async def admin_status(cb: CallbackQuery) -> None:
    await cb.answer("Проверяю...")
    lines = ["🖥 Состояние бота\n"]
    lines.append("✅ Telegram: бот отвечает (вы читаете это сообщение)")

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://openrouter.ai/api/v1/models")
        lines.append("✅ OpenRouter: доступен" if r.status_code < 500 else f"⚠️ OpenRouter: статус {r.status_code}")
    except httpx.HTTPError:
        lines.append("❌ OpenRouter: недоступен")

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://raw.githubusercontent.com")
        lines.append("✅ GitHub: доступен")
    except httpx.HTTPError:
        lines.append("❌ GitHub: недоступен")

    try:
        usage = shutil.disk_usage(settings.reports_dir if os.path.isdir(settings.reports_dir) else "/")
        free_gb = usage.free / (1024**3)
        lines.append(f"💾 Свободно места: {free_gb:.1f} GB")
    except OSError:
        lines.append("💾 Свободно места: неизвестно")

    active = await crud.active_tests_count()
    lines.append(f"🧪 Активных SSH-тестов: {active}")

    await cb.message.edit_text("\n".join(lines), reply_markup=kb.back_to_admin())


@router.callback_query(F.data == "admin:notify")
async def admin_notify(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    cfg = await notify.get_config()
    lines = [
        "📡 Чаты и топики\n",
        "Сюда бот шлёт PDF-отчёты, уведомления о регистрации новых пользователей и ошибках "
        "тестов — один чат (форум-группа), у каждой категории свой топик.\n",
        f"Chat ID: {cfg['chat_id'] or 'не задан'}",
    ]
    for key, title in notify.CATEGORIES.items():
        lines.append(f"{title}: топик {cfg.get(key) or 'не задан'}")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb.admin_notify_menu(notify.CATEGORIES))
    await cb.answer()


@router.callback_query(F.data == "admin:notify_autocreate")
async def admin_notify_autocreate(cb: CallbackQuery) -> None:
    await cb.answer("Создаю топики...")
    results = await notify.auto_create_topics(cb.bot)
    if "_error" in results:
        await cb.message.edit_text(f"❌ {results['_error']}", reply_markup=kb.back_to_admin())
        return

    lines = ["🧵 Готово:\n"]
    for key, outcome in results.items():
        lines.append(f"{notify.CATEGORIES.get(key, key)}: {outcome}")

    cfg = await notify.get_config()
    lines.append(f"\nChat ID: {cfg['chat_id'] or 'не задан'}")
    for k, title in notify.CATEGORIES.items():
        lines.append(f"{title}: топик {cfg.get(k) or 'не задан'}")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb.admin_notify_menu(notify.CATEGORIES))


@router.callback_query(F.data.startswith("admin:notify_set:"))
async def admin_notify_set(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.split(":", 2)[2]
    await state.set_state(AdminNotify.waiting_value)
    await state.update_data(notify_key=key)
    label = "Chat ID" if key == "chat" else f"ID топика «{notify.CATEGORIES.get(key, key)}»"
    await cb.message.edit_text(
        f"Отправьте {label} (число). Чтобы очистить значение — отправьте «-».\n\n"
        "Chat ID и id топиков можно узнать, переслав любое сообщение из нужного топика боту "
        "@userinfobot или через клиент Telegram Desktop (Copy Message Link содержит оба id)."
    )
    await cb.answer()


@router.message(AdminNotify.waiting_value)
async def admin_notify_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = data.get("notify_key")
    raw = (message.text or "").strip()

    if raw == "-":
        value = None
    else:
        try:
            int(raw)
        except ValueError:
            await message.reply("❌ Нужно число (или «-» чтобы очистить). Попробуйте снова.")
            return
        value = raw

    if key == "chat":
        await notify.set_chat_id(value)
    else:
        await notify.set_topic(key, value)

    await state.clear()
    await message.answer("✅ Сохранено.")
    cfg = await notify.get_config()
    lines = [f"Chat ID: {cfg['chat_id'] or 'не задан'}"]
    for k, title in notify.CATEGORIES.items():
        lines.append(f"{title}: топик {cfg.get(k) or 'не задан'}")
    await message.answer("\n".join(lines), reply_markup=kb.admin_notify_menu(notify.CATEGORIES))


@router.callback_query(F.data == "admin:icons")
async def admin_icons(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    n_assigned = len(icons.all_assigned())
    await cb.message.edit_text(
        "💎 Премиум-эмодзи на кнопках\n\n"
        f"Назначено: {n_assigned}/{len(icons.SLOTS)}.\n"
        "Нажмите на кнопку, затем пришлите сообщение с нужным премиум-эмодзи — заберу его id "
        "и подставлю на эту кнопку. Чтобы снять эмодзи — отправьте «-».\n\n"
        "⚠️ Работает только если у владельца бота (аккаунта в BotFather) есть Telegram Premium, "
        "либо у бота куплены доп. юзернеймы на Fragment — иначе Telegram проигнорирует иконку.",
        reply_markup=kb.admin_icons_menu(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:icon_set:"))
async def admin_icon_set(cb: CallbackQuery, state: FSMContext) -> None:
    slot = cb.data.split(":", 2)[2]
    if slot not in icons.SLOTS:
        await cb.answer("Неизвестная кнопка.", show_alert=True)
        return
    await state.set_state(AdminIcons.waiting_emoji)
    await state.update_data(icon_slot=slot)
    current = " (сейчас назначен)" if icons.get(slot) else ""
    await cb.message.edit_text(
        f"Кнопка: {icons.SLOTS[slot]}{current}\n\n"
        "Пришлите сообщение с премиум-эмодзи (просто отправьте его как текст), или «-» чтобы очистить."
    )
    await cb.answer()


@router.message(AdminIcons.waiting_emoji)
async def admin_icon_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    slot = data.get("icon_slot")
    raw = (message.text or "").strip()

    if raw == "-":
        await icons.set_icon(slot, None)
        await state.clear()
        await message.answer("✅ Эмодзи снят с кнопки.", reply_markup=kb.admin_icons_menu())
        return

    custom_emoji_id = None
    for entity in message.entities or []:
        if entity.type == "custom_emoji":
            custom_emoji_id = entity.custom_emoji_id
            break

    if not custom_emoji_id:
        await message.reply(
            "❌ В сообщении нет премиум-эмодзи (обычный юникод-эмодзи не подходит — нужен именно "
            "премиум/кастомный). Пришлите его ещё раз, или «-» чтобы очистить."
        )
        return

    await icons.set_icon(slot, custom_emoji_id)
    await state.clear()
    await message.answer(
        f"✅ Готово: {icons.SLOTS.get(slot, slot)} → id `{custom_emoji_id}`.",
        reply_markup=kb.admin_icons_menu(),
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "admin:daily_limit")
async def admin_daily_limit(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminLimits.waiting_daily_limit)
    current = await crud.get_setting("daily_report_limit", str(test_flow.DEFAULT_DAILY_LIMIT))
    await cb.message.edit_text(
        f"📅 Лимит отчётов в сутки на пользователя: {current}\n\n"
        "Отправьте новое число (0 — без лимита).",
    )
    await cb.answer()


@router.message(AdminLimits.waiting_daily_limit)
async def admin_daily_limit_set(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        value = int(raw)
        if value < 0:
            raise ValueError
    except ValueError:
        await message.reply("❌ Нужно целое число ≥ 0. Попробуйте снова.")
        return
    await crud.set_setting("daily_report_limit", str(value))
    await state.clear()
    text = f"✅ Лимит отчётов в сутки: {value}" if value > 0 else "✅ Лимит отчётов в сутки снят."
    await message.answer(text, reply_markup=kb.back_to_admin())


USERS_PAGE_SIZE = 10
RUNS_PER_CARD = 8


@router.callback_query(F.data.startswith("admin:users:"))
async def admin_users(cb: CallbackQuery) -> None:
    page = int(cb.data.split(":")[2])
    total = await crud.count_users()
    users = await crud.list_users_page(offset=page * USERS_PAGE_SIZE, limit=USERS_PAGE_SIZE)
    has_more = total > (page + 1) * USERS_PAGE_SIZE

    if not users and page == 0:
        await cb.message.edit_text("👥 Пользователи\n\nПока никого.", reply_markup=kb.back_to_admin())
        await cb.answer()
        return

    text = f"👥 Пользователи (всего {total})\n\nВыберите пользователя:"
    await cb.message.edit_text(text, reply_markup=kb.admin_users_list([u.id for u in users], page, has_more))
    await cb.answer()


@router.callback_query(F.data.startswith("admin:user:"))
async def admin_user_card(cb: CallbackQuery) -> None:
    _, _, user_id_s, _page = cb.data.split(":")
    user_id = int(user_id_s)
    user = await crud.get_user(user_id)
    if not user:
        await cb.answer("Пользователь не найден.", show_alert=True)
        return
    runs = await crud.list_user_runs(user_id, limit=RUNS_PER_CARD)
    uname = f"@{user.username}" if user.username else str(user.id)
    lines = [
        f"👤 {uname} (id {user.id})",
        f"Первый визит: {user.first_seen:%Y-%m-%d %H:%M}",
        f"Последний визит: {user.last_seen:%Y-%m-%d %H:%M}",
        "",
        "Последние проверки:" if runs else "Проверок ещё не было.",
    ]
    await cb.message.edit_text("\n".join(lines), reply_markup=kb.admin_user_card(user_id, runs))
    await cb.answer()


@router.callback_query(F.data.startswith("admin:deluser:"))
async def admin_deluser_confirm(cb: CallbackQuery) -> None:
    user_id = int(cb.data.split(":")[2])
    user = await crud.get_user(user_id)
    if not user:
        await cb.answer("Пользователь не найден.", show_alert=True)
        return
    uname = f"@{user.username}" if user.username else str(user.id)
    await cb.message.edit_text(
        f"⚠️ Удалить пользователя {uname} безвозвратно?\n\n"
        "Будут стёрты: все его проверки и статистика в БД, отпечатки серверов, "
        "данные серверов (IP/логин/пароль) и все PDF/отчёты в архиве. Отменить нельзя.",
        reply_markup=kb.admin_deluser_confirm(user_id),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:deluser_go:"))
async def admin_deluser_go(cb: CallbackQuery) -> None:
    user_id = int(cb.data.split(":")[2])
    await crud.delete_user(user_id)
    archive.delete_user_dir(user_id)
    await cb.message.edit_text(f"🗑 Пользователь {user_id} и все его данные удалены.", reply_markup=kb.back_to_admin())
    await cb.answer("Удалено.")


@router.callback_query(F.data.startswith("admin:report:"))
async def admin_report(cb: CallbackQuery) -> None:
    run_id = int(cb.data.split(":")[2])
    run = await crud.get_test_run(run_id)
    if not run:
        await cb.answer("Не найдено.", show_alert=True)
        return
    base = archive.find_report_base(run.user_id, run_id)
    has_pdf = bool(base and archive.report_pdf_path(run.user_id, base))
    lines = [
        f"Сервер: {run.host}:{run.port}",
        f"Начато: {run.started_at:%Y-%m-%d %H:%M}",
        f"Статус: {run.status}",
    ]
    if run.status == "success":
        lines.append(f"Успешно: {run.tests_ok}, ошибок: {run.tests_failed}")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb.admin_report_actions(run.user_id, run_id, has_pdf))
    await cb.answer()


@router.callback_query(F.data.startswith("admin:report_pdf:"))
async def admin_report_pdf(cb: CallbackQuery) -> None:
    from aiogram.types import FSInputFile

    run_id = int(cb.data.split(":")[2])
    run = await crud.get_test_run(run_id)
    if not run:
        await cb.answer("Не найдено.", show_alert=True)
        return
    base = archive.find_report_base(run.user_id, run_id)
    pdf_path = archive.report_pdf_path(run.user_id, base) if base else None
    if not pdf_path:
        await cb.answer("PDF не найден.", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer_document(
        FSInputFile(pdf_path, filename=archive.report_filename(run.host, run.started_at.strftime("%Y-%m-%d")))
    )
