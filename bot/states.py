from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class TestFlow(StatesGroup):
    waiting_host = State()
    waiting_port = State()
    waiting_login = State()
    waiting_password = State()
    confirm_start = State()
    picking_tests = State()
    confirm_fingerprint = State()
    running = State()


class AdminOpenRouter(StatesGroup):
    waiting_model = State()
    waiting_fallback = State()
    waiting_key = State()
    waiting_max_tokens = State()
    waiting_rate = State()


class AdminNotify(StatesGroup):
    waiting_value = State()  # data["notify_key"] tells which setting is being edited


class AdminIcons(StatesGroup):
    waiting_emoji = State()  # data["icon_slot"] tells which button is being assigned


class AdminLimits(StatesGroup):
    waiting_daily_limit = State()
    waiting_user_daily_limit = State()  # data["target_user_id"] tells which user is being edited


class AdminMaintenance(StatesGroup):
    waiting_message = State()


class RatingFlow(StatesGroup):
    waiting_comment = State()  # data["rating_id"] tells which rating the comment is attached to


class Broadcast(StatesGroup):
    choosing_category = State()
    waiting_message = State()  # data["category"]/["days"] carry the chosen segment
    confirm = State()
