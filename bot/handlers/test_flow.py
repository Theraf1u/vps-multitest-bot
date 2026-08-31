from __future__ import annotations

import asyncio
import collections
import datetime as dt
import logging
import os
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import keyboards as kb
from bot.config import settings
from bot.database import crud
from bot.services import archive
from bot.services import multitest as mt
from bot.services import notify
from bot.services import openrouter as orsvc
from bot.services import report as report_svc
from bot.services import security
from bot.services import ssh as sshsvc
from bot.services import test_info
from bot.states import TestFlow

log = logging.getLogger(__name__)
router = Router(name="test_flow")

# Non-serializable per-user run state (SSH connection, cancel event, background task).
# Keyed by telegram user id. Deliberately kept out of FSM storage / DB.
_pending: dict[int, dict] = {}

# FIFO of user_ids waiting for a free concurrency slot (MAX_CONCURRENT_TESTS reached). A
# waiting entry keeps its "message" (to edit with position updates) and "queue_state"
# (FSMContext, to resume the flow) inside _pending[user_id] — see _enqueue/_advance_queue.
_queue: "collections.deque[int]" = collections.deque()

ALL_FUNCS = [t.id for t in mt.TEST_CATALOG]

SUB_BAR_WIDTH = 10

# Default admin-configurable cap on successful reports per user per UTC day (Setting
# "daily_report_limit", 0 = unlimited) — see crud.count_runs_today.
DEFAULT_DAILY_LIMIT = 5


async def _daily_limit(user_id: int) -> int:
    """0 means unlimited. The bot admin is never capped (they're the primary tester); everyone
    else gets their per-user override (admin panel -> user card) if set, else the global
    default."""
    if user_id == settings.admin_id:
        return 0
    override = await crud.get_user_daily_limit(user_id)
    if override is not None:
        return override
    return int(await crud.get_setting("daily_report_limit", str(DEFAULT_DAILY_LIMIT)))


def _max_per_user(user_id: int) -> int:
    """The bot admin is never capped on concurrent tests either — see _daily_limit."""
    return 10**9 if user_id == settings.admin_id else settings.max_tests_per_user


async def confirm_start_text(user_id: int, host: str, port: int, login: str) -> str:
    """Shared by every screen that shows the "ready to run" confirmation (got_password,
    back_to_confirm here, and history's "🔄 Повторить") — keeps the daily-quota line in sync
    everywhere instead of drifting across three copy-pasted message strings."""
    lines = [
        f"Сервер: {host}:{port}",
        f"Логин: {login}",
        "",
        "⚠️ Multitest может устанавливать недостающие зависимости (curl/wget/iperf3/sysbench/jq) "
        "и выполнять сторонние benchmark-компоненты на сервере.",
    ]
    limit = await _daily_limit(user_id)
    if limit > 0:
        used = await crud.count_runs_today(user_id)
        lines.append(f"\n📅 Отчётов сегодня: {used}/{limit}")
    return "\n".join(lines)


def _sub_bar(pct: int) -> str:
    filled = int(SUB_BAR_WIDTH * pct / 100)
    return "█" * filled + "░" * (SUB_BAR_WIDTH - filled)


def _progress_text(
    host: str,
    subset: list[mt.TestDef],
    completed: int,
    running_idx: int | None,
    running_elapsed: float | None,
    total_elapsed: float = 0.0,
    skipped: set[int] | None = None,
) -> str:
    total = len(subset)
    filled = int(15 * completed / total) if total else 0
    bar = "█" * filled + "░" * (15 - filled)
    lines = ["🧪 Проверка сервера", f"Server: {host}", f"{bar} {completed}/{total}", ""]

    current_label = None
    remaining_secs = 0.0
    for i, t in enumerate(subset):
        label, estimate = t.label, t.estimated_secs
        if i < completed:
            if skipped and i in skipped:
                lines.append(f"⏭ {label} {_sub_bar(100)} Пропущен")
            else:
                lines.append(f"✅ {label} {_sub_bar(100)} 100%")
        elif running_idx is not None and i == running_idx:
            pct = min(99, int((running_elapsed or 0) / estimate * 100)) if estimate else 0
            lines.append(f"⏳ {label} {_sub_bar(pct)} {pct}%")
            current_label = label
            remaining_secs += max(0.0, estimate - (running_elapsed or 0))
        else:
            lines.append(f"▫️ {label}")
            remaining_secs += estimate

    lines.append("")
    elapsed_min, elapsed_sec = divmod(int(total_elapsed), 60)
    lines.append(f"⏱ Прошло: {elapsed_min:02d}:{elapsed_sec:02d}")
    lines.append(f"Текущий тест: {current_label or '—'}")
    if current_label:
        eta = dt.datetime.now() + dt.timedelta(seconds=remaining_secs)
        eta_min = max(1, round(remaining_secs / 60))
        lines.append(f"Ожидаемое окончание: ~{eta_min} мин (до {eta.strftime('%H:%M')})")
    return "\n".join(lines)


def _final_result_text(host: str, subset: list[mt.TestDef], result: mt.MultitestResult, skipped: set[int]) -> str:
    """Итоговый список по каждому тесту (✅/⏭/⚠️/▫️) — заменяет собой голое "X/Y успешно" в
    финальных сообщениях, чтобы по одному сообщению в чате/админ-топике сразу было видно, какие
    именно тесты прошли, какие пропущены, а какие упали, а не только агрегированную цифру."""
    total = len(subset)
    lines = ["🧪 Проверка сервера — итог", f"Server: {host}", f"{'█' * 15} {result.ok_count}/{total}", ""]
    for i, t in enumerate(subset):
        parsed = result.tests[i] if i < len(result.tests) else None
        if i in skipped:
            lines.append(f"⏭ {t.label} — Пропущен")
        elif parsed is not None and parsed.ok:
            lines.append(f"✅ {t.label}")
        elif parsed is not None and parsed.raw_log.strip():
            lines.append(f"⚠️ {t.label} — ошибка")
        else:
            lines.append(f"▫️ {t.label} — не выполнен")
    return "\n".join(lines)


async def _safe_edit(message: Message, text: str, **kwargs) -> None:
    """Never lets a Telegram-side hiccup take down the caller — the actual multitest run keeps
    going on the target VPS via setsid nohup regardless of whether we can currently paint a
    progress bar. Retries a transient failure (flood control, a Bad-Gateway-style blip) a few
    times with backoff before giving up, since giving up on a *terminal* status message (run
    finished/failed) means it never gets corrected — there's no "next poll" to retry it for."""
    delay = 3.0
    for attempt in range(4):
        try:
            await message.edit_text(text, **kwargs)
            return
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                log.warning("edit_text failed: %s", e)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramAPIError as e:
            if attempt == 3:
                log.warning("edit_text gave up after %d attempts: %s", attempt + 1, e)
                return
            log.warning("edit_text failed (Telegram-side, attempt %d/4): %s", attempt + 1, e)
            await asyncio.sleep(delay)
            delay *= 2


async def _mirror_admin(entry: dict, bot, text: str) -> None:
    """Keeps the admin "test_starts" topic showing the exact same live progress screen the
    user sees, prefixed with who's running it — see entry["admin_msg_id"] set up in
    _proceed_after_fingerprint."""
    msg_id = entry.get("admin_msg_id")
    if not msg_id:
        return
    header = entry.get("admin_header", "")
    await notify.notify_edit(bot, entry["admin_chat_id"], msg_id, header + text)


async def _log_activity(bot, user_id: int, who: str, text: str) -> None:
    """Полный лог действий пользователей (вход в раздел, старт/скип/отмена/прерывание теста,
    получение PDF и т.д.) в отдельный настраиваемый топик "user_activity" — только для обычных
    пользователей, действия самого админа сюда не пишутся."""
    if user_id == settings.admin_id:
        return
    await notify.notify_text(bot, "user_activity", f"{text} — user {who} (id {user_id})")


async def cancel_running(user_id: int) -> bool:
    """Used by the history "🛑 Остановить" button: signals the same cancel_event the live
    progress message's stop button would. Returns False if this user has nothing running."""
    entry = _pending.get(user_id)
    if entry and entry.get("cancel_event"):
        entry["cancel_event"].set()
        return True
    return False


async def cleanup_abandoned(user_id: int, bot=None) -> None:
    """Called from /start etc. to reclaim state/rate-limit slots (and queue position) left
    behind by a flow the user walked away from before a background test task was actually
    launched, or from a connection-lost run the user never came back to retry. Never touches a
    run that's actively polling — that cleans up itself when it finishes or is cancelled."""
    entry = _pending.get(user_id)
    if entry and (not entry.get("task") or entry.get("awaiting_reconnect")):
        if user_id in _queue:
            _queue.remove(user_id)
        mt_run = entry.get("mt_run")
        run_id = entry.get("run_id")
        _cleanup_pending(user_id)
        if mt_run is not None:
            asyncio.create_task(mt_run.abandon())  # best-effort remote cleanup, don't block on it
        if run_id is not None:
            await crud.finish_test_run(run_id, "error", 0, 0, error="Соединение потеряно, пользователь не вернулся")
        if bot is not None:
            await _release_and_advance(user_id, bot)
        else:
            await crud.release_slot(user_id)


def _cleanup_pending(user_id: int) -> None:
    entry = _pending.pop(user_id, None)
    if entry:
        creds = entry.get("creds")
        if creds:
            creds.wipe()
        conn = entry.get("conn")
        if conn:
            try:
                conn.close()
            except Exception:
                pass


async def _release_and_advance(user_id: int, bot) -> None:
    """Frees this user's concurrency slot, then lets the next queued user (if any) take it."""
    await crud.release_slot(user_id)
    await _advance_queue(bot)


async def _advance_queue(bot) -> None:
    while _queue:
        next_user_id = _queue[0]
        entry = _pending.get(next_user_id)
        if not entry:
            _queue.popleft()  # their session vanished in the meantime — nothing to resume
            continue
        if not await crud.try_acquire_slot(next_user_id, _max_per_user(next_user_id), settings.max_concurrent_tests):
            break
        _queue.popleft()
        state = entry.pop("queue_state", None)
        message = entry.pop("queue_message", None)
        if state is None or message is None:
            await crud.release_slot(next_user_id)  # don't strand the slot we just took
            continue
        await _safe_edit(message, "▶️ Ваша очередь подошла, начинаю проверку...", reply_markup=None)
        await _start_now(message, state, next_user_id)
    await _notify_queue_positions()


async def _notify_queue_positions() -> None:
    for i, uid in enumerate(_queue, start=1):
        entry = _pending.get(uid)
        message = entry.get("queue_message") if entry else None
        if message:
            await _safe_edit(
                message,
                f"🕓 Все {settings.max_concurrent_tests} слота заняты. Вы в очереди — позиция {i}.\n"
                "Тест запустится автоматически, как только освободится слот.",
                reply_markup=kb.queue_wait(),
            )


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data == "test:new")
async def test_new(cb: CallbackQuery, state: FSMContext) -> None:
    if await crud.is_maintenance_mode():
        await crud.add_maintenance_waiter(cb.from_user.id)
        await cb.answer(await crud.get_maintenance_message(), show_alert=True)
        return

    who = f"@{cb.from_user.username}" if cb.from_user.username else str(cb.from_user.id)
    await _log_activity(cb.bot, cb.from_user.id, who, "🚪 Вход в раздел «Проверить сервер»")

    await state.clear()
    await state.set_state(TestFlow.waiting_host)
    await cb.message.edit_text(
        "Введите IP-адрес или hostname сервера:",
        reply_markup=kb.cancel_flow(),
    )
    await cb.answer()


@router.callback_query(F.data == "test:cancel_flow")
async def cancel_flow(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(password=None)
    await state.clear()
    if cb.from_user.id in _queue:
        _queue.remove(cb.from_user.id)
    _cleanup_pending(cb.from_user.id)
    await _release_and_advance(cb.from_user.id, cb.bot)
    is_admin = cb.from_user.id == settings.admin_id
    await cb.message.edit_text(
        test_info.build_menu_intro_text(),
        reply_markup=kb.main_menu(is_admin),
        parse_mode="Markdown",
    )
    await cb.answer()


@router.callback_query(F.data == "test:queue_leave")
async def queue_leave(cb: CallbackQuery, state: FSMContext) -> None:
    user_id = cb.from_user.id
    was_queued = user_id in _queue
    if was_queued:
        _queue.remove(user_id)
    _cleanup_pending(user_id)
    await state.clear()
    is_admin = user_id == settings.admin_id
    await cb.message.edit_text(
        "🚫 Вы покинули очередь.\n\n👋 Нажмите кнопку ниже, чтобы начать заново.",
        reply_markup=kb.main_menu(is_admin),
    )
    await cb.answer()
    if was_queued:
        await _notify_queue_positions()  # positions shift for everyone still behind


@router.callback_query(F.data == "test:back:host", TestFlow.waiting_port)
async def back_to_host(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TestFlow.waiting_host)
    await cb.message.edit_text("Введите IP-адрес или hostname сервера:", reply_markup=kb.cancel_flow())
    await cb.answer()


@router.callback_query(F.data == "test:back:port", TestFlow.waiting_login)
async def back_to_port(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TestFlow.waiting_port)
    await cb.message.edit_text("Введите SSH-порт:", reply_markup=kb.port_step())
    await cb.answer()


@router.callback_query(F.data == "test:back:login", TestFlow.waiting_password)
async def back_to_login(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TestFlow.waiting_login)
    await cb.message.edit_text("Введите SSH login:", reply_markup=kb.login_step())
    await cb.answer()


@router.callback_query(F.data == "test:back:password", TestFlow.confirm_start)
async def back_to_password(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TestFlow.waiting_password)
    await cb.message.edit_text(
        "Введите SSH password.\n"
        "⚠️ Сообщение с паролем будет удалено сразу после получения.",
        reply_markup=kb.password_step(),
    )
    await cb.answer()


@router.callback_query(F.data == "test:back:confirm", TestFlow.picking_tests)
async def back_to_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    host, port, login = data.get("host"), data.get("port"), data.get("login")
    await state.set_state(TestFlow.confirm_start)
    await cb.message.edit_text(
        await confirm_start_text(cb.from_user.id, host, port, login),
        reply_markup=kb.confirm_start(),
    )
    await cb.answer()


@router.message(TestFlow.waiting_host)
async def got_host(message: Message, state: FSMContext) -> None:
    host = (message.text or "").strip()
    try:
        security.validate_target(host)
    except security.SSRFError as e:
        await message.reply(f"❌ {e}\nВведите другой адрес:", reply_markup=kb.cancel_flow())
        return
    await state.update_data(host=host)
    await state.set_state(TestFlow.waiting_port)
    await message.answer("Введите SSH-порт:", reply_markup=kb.port_step())


@router.callback_query(F.data == "test:port_default", TestFlow.waiting_port)
async def port_default(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(port=22)
    await state.set_state(TestFlow.waiting_login)
    await cb.message.edit_text("Введите SSH login:", reply_markup=kb.login_step())
    await cb.answer()


@router.message(TestFlow.waiting_port)
async def got_port(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw in ("", "-"):
        port = 22
    else:
        try:
            port = security.validate_port(raw)
        except ValueError as e:
            await message.reply(f"❌ {e}\nПопробуйте снова:", reply_markup=kb.port_step())
            return
    await state.update_data(port=port)
    await state.set_state(TestFlow.waiting_login)
    await message.answer("Введите SSH login:", reply_markup=kb.login_step())


@router.callback_query(F.data == "test:login_root", TestFlow.waiting_login)
async def login_root(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(login="root")
    await state.set_state(TestFlow.waiting_password)
    await cb.message.edit_text(
        "Введите SSH password.\n"
        "⚠️ Сообщение с паролем будет удалено сразу после получения.",
        reply_markup=kb.password_step(),
    )
    await cb.answer()


@router.message(TestFlow.waiting_login)
async def got_login(message: Message, state: FSMContext) -> None:
    login = (message.text or "").strip()
    if not login:
        await message.reply("❌ Логин не может быть пустым.", reply_markup=kb.login_step())
        return
    await state.update_data(login=login)
    await state.set_state(TestFlow.waiting_password)
    await message.answer(
        "Введите SSH password.\n"
        "⚠️ Сообщение с паролем будет удалено сразу после получения.",
        reply_markup=kb.password_step(),
    )


@router.message(TestFlow.waiting_password)
async def got_password(message: Message, state: FSMContext) -> None:
    password = message.text or ""
    data = await state.get_data()
    host, port, login = data["host"], data["port"], data["login"]

    try:
        await message.delete()
    except TelegramBadRequest:
        pass  # bot may lack delete rights in this chat; password is never logged either way

    old = _pending.get(message.from_user.id)
    if old and old.get("creds"):
        old["creds"].wipe()  # re-entering this step via "⬅️ Назад" must not leak the old password
    _pending[message.from_user.id] = {
        "creds": sshsvc.Credentials(host=host, port=port, username=login, password=password)
    }
    await state.set_state(TestFlow.confirm_start)
    await message.answer(
        await confirm_start_text(message.from_user.id, host, port, login),
        reply_markup=kb.confirm_start(),
    )


@router.callback_query(F.data == "test:pick", TestFlow.confirm_start)
async def pick_tests(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(selected=[])
    await state.set_state(TestFlow.picking_tests)
    await cb.message.edit_text(
        "Выберите тесты, которые нужно запустить:",
        reply_markup=kb.test_picker(set()),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ts:t:"), TestFlow.picking_tests)
async def toggle_test(cb: CallbackQuery, state: FSMContext) -> None:
    func = cb.data.split(":", 2)[2]
    data = await state.get_data()
    selected = set(data.get("selected", []))
    if func in selected:
        selected.discard(func)
    else:
        selected.add(func)
    await state.update_data(selected=list(selected))
    await cb.message.edit_reply_markup(reply_markup=kb.test_picker(selected))
    await cb.answer()


@router.callback_query(F.data.startswith("ts:cat:"), TestFlow.picking_tests)
async def toggle_category(cb: CallbackQuery, state: FSMContext) -> None:
    cat_key = cb.data.split(":", 2)[2]
    cat_ids = next((ids for key, _title, ids in mt.CATEGORIES if key == cat_key), None)
    if not cat_ids:
        await cb.answer()
        return
    data = await state.get_data()
    selected = set(data.get("selected", []))
    if set(cat_ids).issubset(selected):
        selected -= set(cat_ids)  # whole group already selected -> tap deselects it
    else:
        selected |= set(cat_ids)  # otherwise tap fills in whatever's missing
    await state.update_data(selected=list(selected))
    await cb.message.edit_reply_markup(reply_markup=kb.test_picker(selected))
    await cb.answer()


@router.callback_query(F.data == "ts:all", TestFlow.picking_tests)
async def select_all_tests(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(selected=list(ALL_FUNCS))
    await cb.message.edit_reply_markup(reply_markup=kb.test_picker(set(ALL_FUNCS)))
    await cb.answer()


@router.callback_query(F.data == "ts:none", TestFlow.picking_tests)
async def select_no_tests(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(selected=[])
    await cb.message.edit_reply_markup(reply_markup=kb.test_picker(set()))
    await cb.answer()


@router.callback_query(F.data == "ts:go", TestFlow.picking_tests)
async def go_with_selection(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("selected", [])
    if not selected:
        await cb.answer("Выберите хотя бы один тест.", show_alert=True)
        return
    await _begin_connect(cb, state, selected_funcs=selected)


@router.callback_query(F.data == "test:confirm", TestFlow.confirm_start)
async def confirm_start(cb: CallbackQuery, state: FSMContext) -> None:
    await _begin_connect(cb, state, selected_funcs=None)


async def _begin_connect(cb: CallbackQuery, state: FSMContext, selected_funcs: list[str] | None) -> None:
    user_id = cb.from_user.id
    entry = _pending.get(user_id)
    if not entry or not entry.get("creds"):
        await cb.answer("Сессия истекла, начните заново.", show_alert=True)
        await state.clear()
        return

    if user_id in _queue:
        await cb.answer("Вы уже в очереди — дождитесь своей очереди.", show_alert=True)
        return

    limit = await _daily_limit(user_id)
    if limit > 0:
        used_today = await crud.count_runs_today(user_id)
        if used_today >= limit:
            await cb.answer(
                f"📅 Дневной лимит отчётов исчерпан: {used_today}/{limit}. Попробуйте завтра.",
                show_alert=True,
            )
            return

    entry["selected_funcs"] = selected_funcs

    if not await crud.try_acquire_slot(user_id, _max_per_user(user_id), settings.max_concurrent_tests):
        await _enqueue(cb, state, user_id)
        return

    await cb.answer()
    await _start_now(cb.message, state, user_id)


async def _enqueue(cb: CallbackQuery, state: FSMContext, user_id: int) -> None:
    """All MAX_CONCURRENT_TESTS slots are taken — park this request instead of rejecting it
    outright. _advance_queue picks it up automatically as soon as a slot frees up."""
    entry = _pending[user_id]
    entry["queue_state"] = state
    entry["queue_message"] = cb.message
    _queue.append(user_id)
    creds: sshsvc.Credentials = entry["creds"]
    who = f"@{cb.from_user.username}" if cb.from_user.username else str(user_id)
    await notify.notify_text(
        cb.bot, "rate_limits",
        f"🕓 В очередь (позиция {len(_queue)}): user {who}, сервер {creds.host}:{creds.port}",
    )
    await cb.message.edit_text(
        f"🕓 Все {settings.max_concurrent_tests} слота заняты. Вы в очереди — позиция {len(_queue)}.\n"
        "Тест запустится автоматически, как только освободится слот.",
        reply_markup=kb.queue_wait(),
    )
    await cb.answer()


async def _start_now(message: Message, state: FSMContext, user_id: int) -> None:
    """Runs the actual TCP/SSH/fingerprint sequence once a concurrency slot is held — called
    right after try_acquire_slot succeeds, whether that's immediate (_begin_connect) or later
    when this user reaches the front of the queue (_advance_queue)."""
    entry = _pending[user_id]
    creds: sshsvc.Credentials = entry["creds"]
    who = f"@{message.chat.username}" if message.chat.username else str(user_id)
    await notify.notify_text(message.bot, "test_starts", f"🚀 Старт теста: user {who}, сервер {creds.host}:{creds.port}")
    await _log_activity(message.bot, user_id, who, f"🚀 Тест запущен: {creds.host}:{creds.port}")
    await _safe_edit(message, f"🔎 Проверяю TCP-доступность {creds.host}:{creds.port}...", reply_markup=None)

    try:
        await sshsvc.check_tcp_reachable(creds.host, creds.port)
    except sshsvc.SSHConnectError as e:
        await _fail(message, state, user_id, f"❌ SSH недоступен\n{security.redact(str(e))}")
        return

    await _safe_edit(message, "🔐 Авторизуюсь по SSH...")
    try:
        conn = await sshsvc.connect(creds, timeout=settings.ssh_connect_timeout)
    except sshsvc.SSHAuthError:
        await _fail(message, state, user_id, "❌ Ошибка авторизации\nПроверьте логин и пароль.")
        return
    except sshsvc.SSHConnectError as e:
        await _fail(message, state, user_id, f"❌ SSH недоступен\n{security.redact(str(e))}")
        return

    # Login/password are never written to disk or forwarded anywhere — they live only in
    # `entry["creds"]` for the duration of this run and are wiped in _cleanup_pending. See
    # README "Безопасность".
    fingerprint = sshsvc.fingerprint_of(conn)
    known = await crud.get_known_fingerprint(user_id, creds.host, creds.port)
    entry["conn"] = conn
    entry["fingerprint"] = fingerprint

    if known == fingerprint:
        await _proceed_after_fingerprint(message, state, user_id)
        return

    changed_note = "\n⚠️ Отпечаток отличается от ранее сохранённого — проверьте сервер внимательно!" if known else ""
    await state.set_state(TestFlow.confirm_fingerprint)
    await _safe_edit(
        message,
        f"Отпечаток ключа сервера:\n`{fingerprint}`{changed_note}\n\nПодтвердите, что это ваш сервер.",
        reply_markup=kb.confirm_fingerprint(),
    )


@router.callback_query(F.data == "test:confirm_fp", TestFlow.confirm_fingerprint)
async def confirm_fingerprint(cb: CallbackQuery, state: FSMContext) -> None:
    user_id = cb.from_user.id
    entry = _pending.get(user_id)
    if not entry or not entry.get("conn"):
        await cb.answer("Сессия истекла.", show_alert=True)
        await state.clear()
        return
    creds: sshsvc.Credentials = entry["creds"]
    await crud.remember_fingerprint(user_id, creds.host, creds.port, entry["fingerprint"])
    await cb.answer()
    await _proceed_after_fingerprint(cb.message, state, user_id)


async def _proceed_after_fingerprint(message: Message, state: FSMContext, user_id: int) -> None:
    entry = _pending[user_id]
    conn = entry["conn"]
    creds: sshsvc.Credentials = entry["creds"]
    subset = mt.catalog_subset(entry.get("selected_funcs"))

    await _safe_edit(message, "🔎 Проверяю права доступа...")
    if not await sshsvc.check_root(conn):
        await _fail(message, state, user_id, "❌ Недостаточно прав\nДля полного Multitest нужны root-права (или passwordless sudo).")
        return

    await _safe_edit(message, "ℹ️ Собираю информацию о системе...")
    facts = await sshsvc.gather_system_facts(conn)
    entry["facts"] = facts

    run_id = await crud.create_test_run(user_id, creds.host, creds.port)
    entry["run_id"] = run_id
    entry["started_at"] = time.time()

    cancel_event = asyncio.Event()
    entry["cancel_event"] = cancel_event
    finish_early_event = asyncio.Event()
    entry["finish_early_event"] = finish_early_event

    await state.set_state(TestFlow.running)
    text = _progress_text(creds.host, subset, 0, None, None)
    await _safe_edit(message, text, reply_markup=kb.stop_test())
    entry["mt_run"] = mt.MultitestRun(conn, subset, creds)

    who = f"@{message.chat.username}" if message.chat.username else str(user_id)
    entry["admin_header"] = f"👤 user {who} (id {user_id})\n\n"
    admin_msg = await notify.notify_send(message.bot, "test_starts", entry["admin_header"] + text)
    if admin_msg:
        entry["admin_chat_id"] = admin_msg.chat.id
        entry["admin_msg_id"] = admin_msg.message_id

    task = asyncio.create_task(_run_and_report(message, state, user_id))
    entry["task"] = task


async def _fail(message: Message, state: FSMContext, user_id: int, text: str) -> None:
    entry = _pending.get(user_id)
    _cleanup_pending(user_id)
    await _release_and_advance(user_id, message.bot)
    await state.clear()
    await _safe_edit(message, text)
    if entry:
        await _mirror_admin(entry, message.bot, text)


async def _run_and_report(
    message: Message, state: FSMContext, user_id: int, mt_run: mt.MultitestRun | None = None
) -> None:
    """`mt_run=None` starts a fresh run; passing an existing instance (from a previous
    MultitestConnectionLost) resumes polling it instead — see manual_reconnect below. Either
    way the remote test process itself is untouched: it survives our SSH connection dropping
    via `setsid nohup`, so a resume never re-runs already-completed tests."""
    entry = _pending[user_id]
    creds: sshsvc.Credentials = entry["creds"]
    facts = entry["facts"]
    run_id = entry["run_id"]
    cancel_event: asyncio.Event = entry["cancel_event"]
    finish_early_event: asyncio.Event = entry["finish_early_event"]
    subset = mt.catalog_subset(entry.get("selected_funcs"))
    run: mt.MultitestRun = mt_run if mt_run is not None else entry["mt_run"]

    last_edit = 0.0

    async def on_progress(completed: int, running_idx: int | None, running_elapsed: float | None) -> None:
        nonlocal last_edit
        now = time.monotonic()
        if now - last_edit < 2.5:
            return
        last_edit = now
        total_elapsed = time.time() - entry["started_at"]
        progress_text = _progress_text(
            creds.host, subset, completed, running_idx, running_elapsed, total_elapsed,
            skipped=run.skipped_indices,
        )
        await _safe_edit(message, progress_text, reply_markup=kb.stop_test())
        await _mirror_admin(entry, message.bot, progress_text)

    async def on_status(text: str) -> None:
        await _safe_edit(message, text, reply_markup=None)
        await _mirror_admin(entry, message.bot, text)

    try:
        if mt_run is None:
            result = await run.run(
                on_progress=on_progress, cancel_event=cancel_event, on_status=on_status,
                finish_early_event=finish_early_event,
            )
        else:
            result = await run.resume(
                on_progress=on_progress, cancel_event=cancel_event, on_status=on_status,
                finish_early_event=finish_early_event,
            )
    except mt.MultitestConnectionLost as e:
        entry["mt_run"] = e.run
        entry["awaiting_reconnect"] = True
        who = f"@{message.chat.username}" if message.chat.username else str(user_id)
        await notify.notify_text(
            message.bot, "errors",
            f"⚠️ Связь потеряна (авто-переподключение не помогло): user {who}, сервер {creds.host}:{creds.port}",
        )
        lost_text = (
            "⚠️ Связь с сервером потеряна, автоматически переподключиться не удалось.\n"
            "Тест не сброшен — уже пройденные проверки сохранены на сервере, можно продолжить позже."
        )
        await _safe_edit(message, lost_text, reply_markup=kb.reconnect_offer())
        await _mirror_admin(entry, message.bot, lost_text)

        # Even after ~30 минут автоматических попыток переподключения не оставляем пользователя
        # ни с чем — собираем промежуточный отчёт из того, что уже успело сохраниться на нашем
        # диске во время опроса (см. MultitestRun._cache_test_locally). Статус прогона в БД не
        # трогаем — итог решится позже: либо успешное переподключение, либо отмена пользователем.
        cached = e.run.load_cached_partial()
        if any(t.raw_log.strip() for t in cached):
            interim_result = mt.MultitestResult(tests=cached, cancelled=False, partial=True)
            interim_path = os.path.join(settings.reports_dir, f"server-report-{user_id}-{run_id}-interim.pdf")
            try:
                os.makedirs(settings.reports_dir, exist_ok=True)
                report_svc.render_pdf(f"{creds.host}:{creds.port}", facts, interim_result, None, interim_path)
                from aiogram.types import FSInputFile

                await message.bot.send_document(
                    chat_id=message.chat.id,
                    document=FSInputFile(interim_path, filename="server-report-interim.pdf"),
                    caption=(
                        f"📄 Промежуточный отчёт по {interim_result.ok_count}/{len(subset)} уже "
                        "пройденным тестам — на случай, если переподключиться не получится."
                    ),
                )
            except Exception:
                log.exception("interim partial report build/send failed")
            finally:
                try:
                    os.remove(interim_path)
                except OSError:
                    pass
        return
    except Exception as e:
        log.exception("multitest run failed")
        err_text = security.redact(str(e))

        # A crash mid-run (Telegram-side outage, a bug, whatever) shouldn't throw away tests
        # that already finished — the remote data survives independently of what killed us
        # here, so try to salvage it into a partial report before giving up outright, the same
        # way "📊 Отчёт по готовым" does for a user-requested early stop.
        salvaged: list[mt.ParsedTest] = []
        try:
            salvaged = await run._download_and_parse()
        except Exception:
            log.exception("salvage download after crash also failed — falling back to local cache")
            salvaged = run.load_cached_partial()

        if not any(t.raw_log.strip() for t in salvaged):
            run._cleanup_local_cache()
            await crud.finish_test_run(run_id, "error", 0, 0, error=err_text)
            who = f"@{message.chat.username}" if message.chat.username else str(user_id)
            await notify.notify_text(
                message.bot, "test_errors",
                f"📉 Тест завершился с ошибкой: user {who}, сервер {creds.host}:{creds.port}\n{err_text}",
            )
            await _log_activity(message.bot, user_id, who, f"❌ Тест завершился с ошибкой: {creds.host}:{creds.port}")
            await _fail(message, state, user_id, f"❌ Ошибка при выполнении тестов\n{err_text}")
            return

        result = mt.MultitestResult(tests=salvaged, cancelled=False, partial=True)
        run._cleanup_local_cache()
        await notify.notify_text(
            message.bot, "errors",
            f"⚠️ Тест {creds.host}:{creds.port} (user {user_id}) прервался ({err_text}), но "
            f"{result.ok_count}/{len(subset)} тестов успели пройти — собираю отчёт по ним.",
        )
        # falls through to the normal report-building path below with the salvaged result

    if result.cancelled:
        await crud.finish_test_run(run_id, "cancelled", 0, 0)
        await _fail(message, state, user_id, "🛑 Тест остановлен пользователем. Временные файлы на сервере удалены.")
        return

    server_label = f"{creds.host}:{creds.port}"
    done_note = "⏭ Отчёт по пройденным тестам\n\n" if result.partial else ""
    detail_text = _final_result_text(creds.host, subset, result, run.skipped_indices)
    forming_text = f"{done_note}{detail_text}\n\n📄 Формирую отчёт..."
    await _safe_edit(message, forming_text)
    await _mirror_admin(entry, message.bot, forming_text)

    os.makedirs(settings.reports_dir, exist_ok=True)
    date_str = time.strftime("%Y-%m-%d")
    pdf_path = os.path.join(settings.reports_dir, f"server-report-{user_id}-{run_id}-{date_str}.pdf")
    try:
        report_svc.render_pdf(server_label, facts, result, None, pdf_path)
        pdf_ok = True
    except Exception:
        log.exception("PDF render failed")
        pdf_ok = False

    await crud.finish_test_run(run_id, "success", result.ok_count, result.failed_count)

    raw_text = archive.build_raw_text(server_label, facts, result)
    archive.save_report(user_id, run_id, creds.host, pdf_path if pdf_ok else None, raw_text, None)

    who = f"@{message.chat.username}" if message.chat.username else str(user_id)
    pass_note = "тест пройден частично" if result.partial else "тест пройден"
    await _log_activity(
        message.bot, user_id, who, f"✅ {pass_note} ({result.ok_count}/{len(subset)}): {server_label}"
    )

    sending_text = f"{done_note}{detail_text}\n\n📤 Отправляю PDF..."
    await _safe_edit(message, sending_text)
    await _mirror_admin(entry, message.bot, sending_text)

    from aiogram import Bot

    bot: Bot = message.bot
    if pdf_ok:
        from aiogram.types import FSInputFile

        report_name = archive.report_filename(creds.host, date_str)
        await bot.send_document(
            chat_id=message.chat.id,
            document=FSInputFile(pdf_path, filename=report_name),
            caption=f"📄 {report_name}",
        )
        await notify.notify_document(
            bot, "reports", pdf_path, report_name,
            caption=f"📄 {server_label} — user {who} — {result.ok_count}/{len(subset)} OK",
        )
        await _log_activity(bot, user_id, who, f"📄 PDF-отчёт получен: {server_label}")
    else:
        await bot.send_message(message.chat.id, "⚠️ Не удалось сформировать PDF. Отправляю текстовый итог отдельным сообщением.")
        text_summary = "\n".join(f"{'✅' if t.ok else '⚠️'} {t.label}" for t in result.tests)
        await bot.send_message(message.chat.id, text_summary)

    ai_enabled = (await crud.get_setting("ai_enabled", "1")) == "1"
    from bot.services.crypto import decrypt

    stored_key = await crud.get_setting("openrouter_api_key")
    api_key = decrypt(stored_key) if stored_key else settings.openrouter_api_key
    if ai_enabled and api_key:
        payload = orsvc.build_compact_payload(facts, result)
        archive.save_ai_payload(user_id, run_id, payload)
        _ai_pending[run_id] = {"user_id": user_id, "payload": payload, "server_label": server_label}
        await bot.send_message(
            message.chat.id,
            "🤖 Хотите получить AI-анализ этого отчёта?",
            reply_markup=kb.ai_offer(run_id),
        )

    final_text = f"{done_note}{detail_text}"
    await _safe_edit(message, final_text, reply_markup=None)
    await _mirror_admin(entry, bot, final_text)

    _cleanup_pending(user_id)
    await _release_and_advance(user_id, bot)
    await state.clear()


@router.callback_query(F.data == "test:reconnect")
async def manual_reconnect(cb: CallbackQuery, state: FSMContext) -> None:
    user_id = cb.from_user.id
    entry = _pending.get(user_id)
    mt_run = entry.get("mt_run") if entry else None
    if not entry or mt_run is None:
        await cb.answer("Сессия истекла — начните заново.", show_alert=True)
        return
    entry.pop("awaiting_reconnect", None)
    await cb.answer()
    reconnect_text = "🔄 Пробую переподключиться..."
    await _safe_edit(cb.message, reconnect_text, reply_markup=None)
    await _mirror_admin(entry, cb.bot, reconnect_text)
    task = asyncio.create_task(_run_and_report(cb.message, state, user_id, mt_run=mt_run))
    entry["task"] = task


@router.callback_query(F.data == "test:reconnect_cancel")
async def reconnect_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    user_id = cb.from_user.id
    entry = _pending.get(user_id)
    mt_run = entry.get("mt_run") if entry else None
    run_id = entry.get("run_id") if entry else None
    _cleanup_pending(user_id)
    if mt_run is not None:
        asyncio.create_task(mt_run.abandon())  # best-effort remote cleanup, don't block the UI on it
    if run_id is not None:
        await crud.finish_test_run(run_id, "cancelled", 0, 0, error="Соединение потеряно, пользователь отменил")
    await _release_and_advance(user_id, cb.bot)
    await state.clear()
    is_admin = user_id == settings.admin_id
    cancel_text = "🛑 Тест отменён. Данные на сервере (по возможности) удалены."
    await cb.message.edit_text(cancel_text, reply_markup=kb.main_menu(is_admin))
    if entry:
        await _mirror_admin(entry, cb.bot, cancel_text)
    await cb.answer()


# Keyed by TestRun.id — holds just enough to run the AI analysis later, on demand, after the
# background task above has already finished (report already sent, slot already released).
_ai_pending: dict[int, dict] = {}

# Max AI analyses spent per test run: one from the offer shown right after the test, one more
# from history — enforced in run_ai_analysis's callers, not here, since it's a DB-backed count
# (crud.count_ai_analyses) rather than in-memory state.
AI_ANALYSES_LIMIT = 2


async def run_ai_analysis(bot, user_id: int, run_id: int, payload: dict) -> orsvc.AnalyzeResult | None:
    """Shared by the immediate post-test offer (ai_analyze below) and the history "🤖
    Проанализировать" button (bot/handlers/history.py) — same OpenRouter call, usage
    bookkeeping, and archive write either way."""
    model = await crud.get_setting("openrouter_model", settings.openrouter_model)
    fallback_model = await crud.get_setting("openrouter_fallback_model") or None
    max_tokens = int(await crud.get_setting("ai_max_tokens", str(orsvc.DEFAULT_MAX_TOKENS)))
    from bot.services.crypto import decrypt

    stored_key = await crud.get_setting("openrouter_api_key")
    api_key = decrypt(stored_key) if stored_key else settings.openrouter_api_key

    result = await orsvc.analyze(
        api_key,
        model,
        payload,
        proxy=settings.openrouter_proxy_url or None,
        fallback_model=fallback_model,
        max_tokens=max_tokens,
    )

    if result is None:
        await crud.record_openrouter_usage(user_id, run_id, model, 0.0, 0, 0, ok=False)
        await notify.notify_text(bot, "ai_usage", f"🤖 AI-анализ провален: run {run_id}, user {user_id}, модель {model}")
        return None

    await crud.record_openrouter_usage(
        user_id, run_id, result.model, result.cost_usd, result.tokens_in, result.tokens_out, ok=True
    )
    archive.attach_ai_text(user_id, run_id, result.text)
    await notify.notify_text(
        bot, "ai_usage",
        f"🤖 AI-анализ: run {run_id}, user {user_id}, модель {result.model}\n"
        f"Токены: {result.tokens_in}→{result.tokens_out}, стоимость: ${result.cost_usd:.4f}",
    )
    return result


@router.callback_query(F.data.startswith("ai:go:"))
async def ai_analyze(cb: CallbackQuery) -> None:
    run_id = int(cb.data.split(":")[2])
    entry = _ai_pending.get(run_id)
    if not entry or entry["user_id"] != cb.from_user.id:
        await cb.answer("Эта кнопка уже неактуальна.", show_alert=True)
        return

    if await crud.count_ai_analyses(run_id) >= AI_ANALYSES_LIMIT:
        await cb.answer(f"Лимит AI-анализов ({AI_ANALYSES_LIMIT}) для этого теста уже исчерпан.", show_alert=True)
        _ai_pending.pop(run_id, None)
        return

    await cb.answer()
    model = await crud.get_setting("openrouter_model", settings.openrouter_model)
    await _safe_edit(cb.message, f"🤖 Анализирую отчёт через {model}... ⏳")

    result = await run_ai_analysis(cb.bot, cb.from_user.id, run_id, entry["payload"])
    if result is None:
        await _safe_edit(cb.message, "❌ Не удалось получить AI-анализ (OpenRouter не ответил). Отчёт остаётся без AI-раздела.")
        _ai_pending.pop(run_id, None)
        return

    await _safe_edit(cb.message, f"🤖 Анализ ({entry['server_label']})\n\n{result.text}")
    _ai_pending.pop(run_id, None)


@router.callback_query(F.data == "test:stop", TestFlow.running)
async def stop_test(cb: CallbackQuery, state: FSMContext) -> None:
    entry = _pending.get(cb.from_user.id)
    if entry and entry.get("cancel_event"):
        entry["cancel_event"].set()
        await cb.answer("Останавливаю тест...")
        stopping_text = "🛑 Останавливаю тест и удаляю временные файлы на сервере..."
        await _safe_edit(cb.message, stopping_text)
        await _mirror_admin(entry, cb.bot, stopping_text)
        who = f"@{cb.from_user.username}" if cb.from_user.username else str(cb.from_user.id)
        creds: sshsvc.Credentials = entry["creds"]
        await _log_activity(cb.bot, cb.from_user.id, who, f"🛑 Тест остановлен пользователем: {creds.host}:{creds.port}")
    else:
        await cb.answer()


@router.callback_query(F.data == "test:skip", TestFlow.running)
async def skip_test(cb: CallbackQuery, state: FSMContext) -> None:
    entry = _pending.get(cb.from_user.id)
    mt_run: mt.MultitestRun | None = entry.get("mt_run") if entry else None
    if not mt_run:
        await cb.answer()
        return
    skipped = await mt_run.skip_current()
    if skipped:
        await cb.answer("⏭ Пропускаю текущий тест...")
        who = f"@{cb.from_user.username}" if cb.from_user.username else str(cb.from_user.id)
        creds: sshsvc.Credentials = entry["creds"]
        current_label = mt_run.subset[mt_run._last_running_idx].label if mt_run._last_running_idx is not None else "?"
        await _log_activity(
            cb.bot, cb.from_user.id, who, f"⏭ Тест «{current_label}» скипнут: {creds.host}:{creds.port}"
        )
    else:
        await cb.answer("Сейчас нечего пропускать — тест уже завершается сам.", show_alert=True)


@router.callback_query(F.data == "test:finish_early", TestFlow.running)
async def finish_early(cb: CallbackQuery, state: FSMContext) -> None:
    entry = _pending.get(cb.from_user.id)
    if entry and entry.get("finish_early_event"):
        entry["finish_early_event"].set()
        await cb.answer("📊 Собираю отчёт по уже пройденным тестам...")
        stopping_text = "📊 Останавливаю оставшиеся тесты и формирую отчёт по пройденным..."
        await _safe_edit(cb.message, stopping_text, reply_markup=None)
        await _mirror_admin(entry, cb.bot, stopping_text)
        who = f"@{cb.from_user.username}" if cb.from_user.username else str(cb.from_user.id)
        creds: sshsvc.Credentials = entry["creds"]
        await _log_activity(cb.bot, cb.from_user.id, who, f"📊 Тест прерван (отчёт по готовым): {creds.host}:{creds.port}")
    else:
        await cb.answer()
