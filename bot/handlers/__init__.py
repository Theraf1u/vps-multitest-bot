from aiogram import Router

from bot.handlers import admin, broadcast, history, rating, start, test_flow


def build_root_router() -> Router:
    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(admin.router)
    root.include_router(broadcast.router)
    root.include_router(history.router)
    root.include_router(test_flow.router)
    root.include_router(rating.router)
    return root
