from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.services import button_icons as icons
from bot.services.multitest import CATEGORIES, TEST_CATALOG, category_numbering


def _icon_text(slot: str, emoji: str, text: str) -> str:
    """A custom_emoji icon renders as its own glyph before the button text — keeping the plain
    unicode emoji in `text` too would show both side by side. Drop it once a premium icon is
    actually assigned to this slot; fall back to the plain emoji prefix otherwise."""
    return text if icons.get(slot) else f"{emoji} {text}"


def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=_icon_text("main_test_new", "🚀", "Проверить сервер"), callback_data="test:new", style="primary",
            icon_custom_emoji_id=icons.get("main_test_new"),
        )],
        [InlineKeyboardButton(
            text=_icon_text("main_history", "📜", "История"), callback_data="hist:list:0",
            icon_custom_emoji_id=icons.get("main_history"),
        )],
        [InlineKeyboardButton(
            text=_icon_text("main_info", "📋", "Описание тестов"), callback_data="info:tests",
            icon_custom_emoji_id=icons.get("main_info"),
        )],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(
            text=_icon_text("main_admin", "⚙️", "Админ-панель"), callback_data="admin:menu",
            icon_custom_emoji_id=icons.get("main_admin"),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_flow() -> InlineKeyboardMarkup:
    """Used only on the host step and the fingerprint-confirm screen — the two points in the
    flow with no previous step to return to, so "back" really does mean "abandon"."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="test:cancel_flow")]]
    )


def queue_wait() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚫 Покинуть очередь", callback_data="test:queue_leave", style="danger")]]
    )


def port_step() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Стандартный 22", callback_data="test:port_default", style="primary")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="test:back:host")],
        ]
    )


def login_step() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 root", callback_data="test:login_root", style="primary")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="test:back:port")],
        ]
    )


def password_step() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="test:back:login")]]
    )


def confirm_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=_icon_text("confirm_all", "🚀", f"Все тесты ({len(TEST_CATALOG)})"),
                callback_data="test:confirm", style="success",
                icon_custom_emoji_id=icons.get("confirm_all"),
            )],
            [InlineKeyboardButton(
                text=_icon_text("confirm_pick", "🎯", "Выбрать тесты"), callback_data="test:pick", style="primary",
                icon_custom_emoji_id=icons.get("confirm_pick"),
            )],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="test:back:password")],
        ]
    )


def confirm_all_warning() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Всё равно запустить всё", callback_data="test:confirm_all_go", style="success")],
            [InlineKeyboardButton(text="🎯 Выбрать тесты", callback_data="test:pick", style="primary")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="test:confirm_all_back")],
        ]
    )


def test_picker(selected: set[str]) -> InlineKeyboardMarkup:
    by_id = {t.id: t.label for t in TEST_CATALOG}
    numbering = category_numbering()
    rows: list[list[InlineKeyboardButton]] = []
    for cat_key, title, ids in CATEGORIES:
        cat_selected = all(tid in selected for tid in ids)
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if cat_selected else '▫️'} — {title} —",
            callback_data=f"ts:cat:{cat_key}",
            style="success" if cat_selected else None,
        )])
        buttons = [
            InlineKeyboardButton(
                text=f"{numbering[tid]}. {'✅' if tid in selected else '▫️'} {by_id[tid]}",
                callback_data=f"ts:t:{tid}",
                style="success" if tid in selected else None,
            )
            for tid in ids
        ]
        for i in range(0, len(buttons), 2):
            rows.append(buttons[i : i + 2])
    rows.append(
        [
            InlineKeyboardButton(text="✅ Все", callback_data="ts:all", style="success"),
            InlineKeyboardButton(text="🚫 Ничего", callback_data="ts:none", style="danger"),
        ]
    )
    n = len(selected)
    rows.append([InlineKeyboardButton(
        text=_icon_text("picker_go", "▶️", f"Начать ({n})"), callback_data="ts:go", style="primary",
        icon_custom_emoji_id=icons.get("picker_go"),
    )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="test:back:confirm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_fingerprint() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить сервер", callback_data="test:confirm_fp", style="success")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="test:cancel_flow")],
        ]
    )


def reconnect_offer() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать ещё раз", callback_data="test:reconnect", style="primary")],
            [InlineKeyboardButton(text="🛑 Отменить", callback_data="test:reconnect_cancel", style="danger")],
        ]
    )


def ai_offer(run_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(
            text=_icon_text("history_ai", "🤖", "Проанализировать"), callback_data=f"ai:go:{run_id}", style="primary",
            icon_custom_emoji_id=icons.get("history_ai"),
        )]]
    )


def rating_request(run_id: int) -> InlineKeyboardMarkup:
    styles = {1: "danger", 2: "primary", 3: "primary", 4: "primary", 5: "success"}
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=str(n), callback_data=f"rate:{run_id}:{n}", style=styles[n])
            for n in range(1, 6)
        ]]
    )


def rating_thanks(rating_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💬 Оставить комментарий", callback_data=f"rate:comment:{rating_id}")]]
    )


def stop_test() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=_icon_text("test_skip", "⏭", "Скипнуть тест"), callback_data="test:skip", style="primary",
                icon_custom_emoji_id=icons.get("test_skip"),
            )],
            [InlineKeyboardButton(
                text=_icon_text("test_finish_early", "📊", "Отчёт по готовым (прервать)"), callback_data="test:finish_early",
                style="success", icon_custom_emoji_id=icons.get("test_finish_early"),
            )],
            [InlineKeyboardButton(
                text=_icon_text("stop_test", "❌", "Остановить тест"), callback_data="test:stop", style="danger",
                icon_custom_emoji_id=icons.get("stop_test"),
            )],
        ]
    )


def back_to_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")]])


def back_to_openrouter() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:openrouter")]])


def back_to_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")]])


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="🤖 OpenRouter", callback_data="admin:openrouter")],
            [InlineKeyboardButton(text="🧪 Multitest", callback_data="admin:multitest")],
            [InlineKeyboardButton(text="🖥 Состояние бота", callback_data="admin:status")],
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users:0")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="📡 Чаты и топики", callback_data="admin:notify")],
            [InlineKeyboardButton(text="💎 Премиум-эмодзи на кнопках", callback_data="admin:icons")],
            [InlineKeyboardButton(text="📅 Лимит отчётов/сутки", callback_data="admin:daily_limit")],
            [InlineKeyboardButton(text="🛠 Технические работы", callback_data="admin:maintenance")],
        ]
    )


def admin_maintenance(enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "🔴 Выключить техработы" if enabled else "🟢 Включить техработы"
    toggle_style = "danger" if enabled else "success"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data="admin:maintenance_toggle", style=toggle_style)],
            [InlineKeyboardButton(text="✏️ Изменить сообщение", callback_data="admin:maintenance_msg")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
        ]
    )


def admin_icons_menu() -> InlineKeyboardMarkup:
    rows = []
    for slot, label in icons.SLOTS.items():
        mark = "💎" if icons.get(slot) else "▫️"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"admin:icon_set:{slot}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_openrouter_menu(ai_enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "🔴 Выключить AI" if ai_enabled else "🟢 Включить AI"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Основная модель", callback_data="admin:or_model")],
            [InlineKeyboardButton(text="🔁 Фолбэк модель", callback_data="admin:or_fallback")],
            [InlineKeyboardButton(text="🔑 Сменить API key", callback_data="admin:or_key")],
            [InlineKeyboardButton(text="⚙️ Лимит токенов/отчёт", callback_data="admin:or_maxtokens")],
            [InlineKeyboardButton(text="🔌 Проверить API", callback_data="admin:or_check")],
            [InlineKeyboardButton(text="💰 Расходы", callback_data="admin:or_spend")],
            [InlineKeyboardButton(
                text=toggle_text, callback_data="admin:or_toggle", style="danger" if ai_enabled else "success"
            )],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
        ]
    )


def admin_spend_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💱 Курс USD→RUB", callback_data="admin:or_rate")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:openrouter")],
        ]
    )


def admin_notify_menu(categories: dict[str, str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ Chat ID", callback_data="admin:notify_set:chat")],
        [InlineKeyboardButton(text="🧵 Создать все топики автоматически", callback_data="admin:notify_autocreate", style="success")],
    ]
    for key, title in categories.items():
        rows.append([InlineKeyboardButton(text=f"✏️ Топик: {title}", callback_data=f"admin:notify_set:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


BROADCAST_CATEGORIES: dict[str, str] = {
    "all": "👥 Все пользователи",
    "active": "🟢 Активные (за N дней)",
    "inactive": "⚪ Неактивные (N+ дней)",
    "limit_hit": "📅 Упёрлись в дневной лимит сегодня",
    "never_ran": "🆕 Зарегистрированы, но ни разу не тестировали",
}

BROADCAST_DAYS_OPTIONS = (1, 3, 7, 14, 30)


def broadcast_categories() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=title, callback_data=f"bcast:cat:{key}")] for key, title in BROADCAST_CATEGORIES.items()]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_days(category: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{d} дн.", callback_data=f"bcast:days:{category}:{d}") for d in BROADCAST_DAYS_OPTIONS]]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_confirm(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧪 Тестовая отправка (себе)", callback_data="bcast:test", style="primary")],
            [InlineKeyboardButton(text=f"🚀 Отправить всем ({count})", callback_data="bcast:go", style="success")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="bcast:cancel", style="danger")],
        ]
    )


def admin_multitest_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Проверить обновление", callback_data="admin:mt_check")],
            [InlineKeyboardButton(text="⬆️ Обновить pinned script", callback_data="admin:mt_update", style="success")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
        ]
    )


def history_list(runs: list, page: int, has_more: bool) -> InlineKeyboardMarkup:
    rows = []
    for run in runs:
        icon = {"success": "✅", "error": "❌", "cancelled": "🛑", "running": "⏳"}.get(run.status, "▫️")
        label = f"{icon} {run.started_at:%Y-%m-%d %H:%M} — {run.host}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"hist:item:{run.id}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"hist:list:{page - 1}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"hist:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def history_item(run_id: int, status: str, has_pdf: bool, ai_available: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if has_pdf:
        rows.append([InlineKeyboardButton(
            text=_icon_text("history_pdf", "📄", "Прислать PDF"), callback_data=f"hist:pdf:{run_id}", style="primary",
            icon_custom_emoji_id=icons.get("history_pdf"),
        )])
    if ai_available:
        rows.append([InlineKeyboardButton(
            text=_icon_text("history_ai", "🤖", "Проанализировать"), callback_data=f"hist:ai:{run_id}", style="primary",
            icon_custom_emoji_id=icons.get("history_ai"),
        )])
    if status == "running":
        rows.append([InlineKeyboardButton(text="🛑 Остановить тест", callback_data=f"hist:cancel:{run_id}", style="danger")])
    else:
        rows.append([InlineKeyboardButton(
            text=_icon_text("history_retry", "🔄", "Повторить"), callback_data=f"hist:retry:{run_id}", style="success",
            icon_custom_emoji_id=icons.get("history_retry"),
        )])
    rows.append([InlineKeyboardButton(text="⬅️ К истории", callback_data="hist:list:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_history_item(run_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К тесту", callback_data=f"hist:item:{run_id}")]])


def admin_users_list(user_ids: list[int], page: int, has_more: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=str(uid), callback_data=f"admin:user:{uid}:0")] for uid in user_ids]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:users:{page - 1}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:users:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_card(user_id: int, runs: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📅 Лимит отчётов/сутки", callback_data=f"admin:user_limit:{user_id}")],
    ]
    for run in runs:
        icon = {"success": "✅", "error": "❌", "cancelled": "🛑", "running": "⏳"}.get(run.status, "▫️")
        label = f"{icon} {run.started_at:%Y-%m-%d %H:%M} — {run.host}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"admin:report:{run.id}")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить пользователя", callback_data=f"admin:deluser:{user_id}", style="danger")])
    rows.append([InlineKeyboardButton(text="⬅️ К пользователям", callback_data="admin:users:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_deluser_confirm(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить всё", callback_data=f"admin:deluser_go:{user_id}", style="danger")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:user:{user_id}:0")],
        ]
    )


def admin_report_actions(user_id: int, run_id: int, has_pdf: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_pdf:
        rows.append([InlineKeyboardButton(text="📄 Прислать PDF", callback_data=f"admin:report_pdf:{run_id}", style="primary")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:user:{user_id}:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
