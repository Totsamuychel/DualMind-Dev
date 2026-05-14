"""DualMind Telegram Bot — T1: infrastructure skeleton.

Start/stop:
    The bot is launched by main.py as a background asyncio task alongside the
    orchestrator.  It is disabled silently when telegram.token is empty or
    telegram.allowed_users is not configured.

Security:
    AllowedUsersMiddleware drops every update from users whose Telegram ID is
    not listed in config.yaml → telegram.allowed_users.  Empty list = disabled.

Extending:
    Add new commands to `router` in this file (T2) or import from sub-modules.
    Inline-keyboard handlers go in the same router (T3).
    For push notifications from the orchestrator, call notify() directly (T4).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, TelegramObject

from tools.companion_client import CompanionClient

logger = logging.getLogger("dualmind.tgbot")

# ── Middleware ────────────────────────────────────────────────────────────────


class AllowedUsersMiddleware(BaseMiddleware):
    """Silently drop every update from users not in the allow-list.

    Applied to both message and callback_query update types so no handler
    ever fires for an unauthorized user.
    """

    def __init__(self, allowed_ids: frozenset[int]) -> None:
        self.allowed = allowed_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = data.get("event_from_user")
        if from_user is None or from_user.id not in self.allowed:
            if from_user:
                logger.debug(
                    "TGBot: dropped update from unauthorized user_id=%d (@%s)",
                    from_user.id,
                    from_user.username or "?",
                )
            return  # swallow the update
        return await handler(event, data)


# ── Bot commands visible in the Telegram UI ───────────────────────────────────

_BOT_COMMANDS = [
    BotCommand(command="goal",    description="Queue a new goal"),
    BotCommand(command="status",  description="Current agent status"),
    BotCommand(command="tasks",   description="List tasks by status"),
    BotCommand(command="approve", description="Approve a patch"),
    BotCommand(command="reject",  description="Reject a patch"),
    BotCommand(command="log",     description="Show recent log lines"),
    BotCommand(command="help",    description="Show help"),
]


# ── Runner ────────────────────────────────────────────────────────────────────

async def _poll_companion_events(
    bot: Bot,
    companion: CompanionClient,
    chat_ids: list[int],
    interval: float = 10.0,
) -> None:
    """Poll companion's /notify/events every `interval` seconds and forward
    any new lifecycle events to all allowed Telegram users as plain messages.

    Runs until cancelled.  Errors are logged and swallowed so one companion
    hiccup doesn't kill the poller.
    """
    from tg_bot.notify import format_event_message, NotifyEvent

    logger.info("TGBot: companion event poller started (interval=%.0fs)", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            raw_events = await companion.dm_poll_notify()
        except Exception as exc:
            logger.debug("TGBot: companion event poll failed: %s", exc)
            continue

        for raw in raw_events:
            event = NotifyEvent(
                event=raw.get("event", ""),
                task_id=raw.get("task_id", ""),
                title=raw.get("title", ""),
                reason=raw.get("reason", ""),
                duration_minutes=int(raw.get("duration_minutes", 0) or 0),
            )
            text = format_event_message(event)
            for chat_id in chat_ids:
                try:
                    await bot.send_message(chat_id, text)
                    logger.info(
                        "TGBot: companion event %s → user %d", event.event, chat_id
                    )
                except Exception as exc:
                    logger.warning(
                        "TGBot: companion event to %d failed: %s", chat_id, exc
                    )


async def run_polling(cfg: dict) -> None:
    """Start aiogram long-polling.  Returns immediately when the bot is not
    configured so callers never need to check whether Telegram is enabled.

    Disable the bot by leaving telegram.token empty in config.yaml.
    Disable access control by leaving telegram.allowed_users empty — in that
    case the bot refuses to start as a safety measure (it controls AI agents
    that can write code to your machines).
    """
    tg_cfg = cfg.get("telegram", {})
    token: str = str(tg_cfg.get("token", "")).strip()

    if not token:
        logger.info("TGBot: no token configured — Telegram bot disabled")
        return

    raw_ids: list = tg_cfg.get("allowed_users") or []
    if not raw_ids:
        logger.warning(
            "TGBot: telegram.allowed_users is empty — bot disabled for safety. "
            "Add your Telegram user ID to config.yaml to enable it."
        )
        return

    allowed_ids: frozenset[int] = frozenset(int(uid) for uid in raw_ids)
    companion_url: str = str(tg_cfg.get("companion_url", "http://localhost:8765"))

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Middleware: applied before any handler runs.
    dp.message.middleware(AllowedUsersMiddleware(allowed_ids))
    dp.callback_query.middleware(AllowedUsersMiddleware(allowed_ids))

    # Inject shared objects; available as typed parameters in every handler:
    #   async def my_handler(message: Message, companion: CompanionClient) -> None:
    dp["companion"] = CompanionClient(companion_url)
    dp["log_file"]  = cfg.get("logging", {}).get("file", "")

    from tg_bot.commands import router as commands_router
    from tg_bot.callbacks import router as callbacks_router
    dp.include_router(commands_router)
    dp.include_router(callbacks_router)

    try:
        await bot.set_my_commands(_BOT_COMMANDS)
    except Exception as exc:
        logger.warning("TGBot: could not set command menu: %s", exc)

    from tg_bot.notify import process_queue, process_events
    from tg_bot.keyboards import review_keyboard
    from tg_bot.monitor import run_monitor

    companion_obj: CompanionClient = dp["companion"]
    queue_task = asyncio.create_task(
        process_queue(bot, list(allowed_ids), review_keyboard),
        name="review-queue-processor",
    )
    events_task = asyncio.create_task(
        process_events(bot, list(allowed_ids)),
        name="event-processor",
    )
    poll_task = asyncio.create_task(
        _poll_companion_events(bot, companion_obj, list(allowed_ids)),
        name="companion-event-poller",
    )
    monitor_task = asyncio.create_task(
        run_monitor(
            stuck_minutes=int(tg_cfg.get("stuck_timeout_minutes", 30)),
            idle_minutes=int(tg_cfg.get("idle_timeout_minutes", 30)),
        ),
        name="monitor",
    )

    logger.info(
        "TGBot: polling started (allowed: %s)",
        ", ".join(str(uid) for uid in sorted(allowed_ids)),
    )
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
        )
    finally:
        for task in (queue_task, events_task, poll_task, monitor_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await bot.session.close()
        logger.info("TGBot: polling stopped")
