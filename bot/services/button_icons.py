from __future__ import annotations

from bot.database import crud

# Assignable button "slots" — key -> label shown in the admin picker (matches the button's own
# text, purely for the admin UI, not used for matching). Extend this dict to make more buttons
# assignable; keyboards.py reads back via get(slot) wherever it wants to opt a button in.
SLOTS: dict[str, str] = {
    "main_test_new": "🚀 Проверить сервер",
    "main_history": "📜 История",
    "main_info": "📋 Описание тестов",
    "main_admin": "⚙️ Админ-панель",
    "confirm_all": "🚀 Все тесты",
    "confirm_pick": "🎯 Выбрать тесты",
    "picker_go": "▶️ Начать",
    "stop_test": "❌ Остановить тест",
    "test_skip": "⏭ Скипнуть тест",
    "test_finish_early": "📊 Отчёт по готовым",
    "history_pdf": "📄 Прислать PDF",
    "history_ai": "🤖 Проанализировать",
    "history_retry": "🔄 Повторить",
}

_KEY_PREFIX = "btn_icon_"

# In-memory write-through cache: keyboards.py's builders are plain sync functions called from
# many places, so hitting the DB per-button per-render isn't practical. Populated once at
# startup (see bot/main.py) and kept in sync on every admin write below.
_cache: dict[str, str] = {}


async def load_all() -> None:
    global _cache
    loaded: dict[str, str] = {}
    for slot in SLOTS:
        val = await crud.get_setting(_KEY_PREFIX + slot)
        if val:
            loaded[slot] = val
    _cache = loaded


def get(slot: str) -> str | None:
    return _cache.get(slot)


def all_assigned() -> dict[str, str]:
    return dict(_cache)


async def set_icon(slot: str, custom_emoji_id: str | None) -> None:
    if slot not in SLOTS:
        raise ValueError(f"unknown button slot: {slot}")
    await crud.set_setting(_KEY_PREFIX + slot, custom_emoji_id)
    if custom_emoji_id:
        _cache[slot] = custom_emoji_id
    else:
        _cache.pop(slot, None)
