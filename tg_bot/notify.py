"""Notification queues for DualMind Telegram bot — T3 / T4.

Two separate queues:

1. Review queue (T3) — push_review_ready() enqueues a ReviewNotification;
   process_queue() delivers it with Approve / Reject inline keyboard buttons.

2. Events queue (T4) — push_event() enqueues a NotifyEvent; process_events()
   delivers it as a plain text message (no keyboard).
   Events: task_started, task_approved, task_rejected, system_idle.
   Companion-originated events are polled separately by bot.py and also
   converted into NotifyEvent objects before being pushed here.

Both queues are import-safe: no aiogram imports at module level.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup

logger = logging.getLogger("dualmind.tgbot")

# ── Notification payload ──────────────────────────────────────────────────────


@dataclass
class ReviewNotification:
    """Everything the bot needs to compose a review-ready message."""

    task_id: str
    task_title: str
    branch: str
    diff_lines: int
    tests_passed: bool
    lint_passed: bool
    files_changed: list[str] = field(default_factory=list)


# ── Module-level queue (same asyncio event loop as the bot) ──────────────────

_queue: asyncio.Queue[ReviewNotification] = asyncio.Queue()


def push_review_ready(notification: ReviewNotification) -> None:
    """Enqueue a review notification.  Safe to call from any coroutine in the
    same event loop (orchestrator, agent callbacks, etc.).

    put_nowait never blocks; the queue is unbounded so it won't raise Full.
    """
    _queue.put_nowait(notification)
    logger.debug("notify: queued review for task %s", notification.task_id)


# ── Message formatter ─────────────────────────────────────────────────────────


def _esc(value: object) -> str:
    return html.escape(str(value))


def format_review_message(n: ReviewNotification) -> str:
    """Return the HTML body of the review-ready Telegram message."""
    tests = "PASS" if n.tests_passed else "FAIL"
    lint  = "PASS" if n.lint_passed  else "FAIL"

    files = n.files_changed[:4]
    files_str = ", ".join(_esc(f) for f in files)
    if len(n.files_changed) > 4:
        files_str += f" (+{len(n.files_changed) - 4} more)"

    return (
        "<b>Task ready for review</b>\n\n"
        f"<code>{_esc(n.task_id)}</code> {_esc(n.task_title)}\n\n"
        f"Branch: <code>{_esc(n.branch)}</code>\n"
        f"Files:  <code>{files_str or '—'}</code>\n"
        f"Tests:  <b>{tests}</b>    Lint: <b>{lint}</b>\n"
        f"Diff:   {n.diff_lines} line(s)"
    )


# ── Background queue processor ────────────────────────────────────────────────


async def process_queue(
    bot: "Bot",
    chat_ids: list[int],
    keyboard_fn: Callable[[str], "InlineKeyboardMarkup"],
) -> None:
    """Long-running coroutine: deliver review notifications as Telegram messages.

    Started by bot.py as an asyncio background task.  Runs until cancelled.

    bot        — aiogram Bot instance (already authenticated)
    chat_ids   — Telegram user IDs to notify (from config.telegram.allowed_users)
    keyboard_fn — callable(task_id) → InlineKeyboardMarkup (from keyboards.py)
    """
    logger.info("notify: review queue processor started (%d recipient(s))", len(chat_ids))
    while True:
        notification: ReviewNotification = await _queue.get()
        text = format_review_message(notification)
        kb   = keyboard_fn(notification.task_id)

        for chat_id in chat_ids:
            try:
                await bot.send_message(chat_id, text, reply_markup=kb)
                logger.info(
                    "notify: sent review for task %s to user %d",
                    notification.task_id, chat_id,
                )
            except Exception as exc:
                logger.warning(
                    "notify: failed to deliver review to %d: %s", chat_id, exc
                )

        _queue.task_done()


# ── Generic event notifications (T4) ─────────────────────────────────────────


@dataclass
class NotifyEvent:
    """A lifecycle event pushed by the orchestrator or companion server.

    event values:
        task_started    — Junior began executing a task
        task_approved   — human approved a patch
        task_rejected   — Lead or human rejected a patch
        system_idle     — system has been idle for duration_minutes
    """

    event: str
    task_id: str = ""
    title: str = ""
    reason: str = ""
    duration_minutes: int = 0


_events_queue: asyncio.Queue[NotifyEvent] = asyncio.Queue()


def push_event(event: NotifyEvent) -> None:
    """Enqueue a lifecycle event.  Safe to call from any coroutine in the same
    event loop.  put_nowait never blocks."""
    _events_queue.put_nowait(event)
    logger.debug("notify: queued event %s for task %s", event.event, event.task_id)


def format_event_message(e: NotifyEvent) -> str:
    """Return the HTML body for a lifecycle event Telegram message."""
    tid = f" <code>{_esc(e.task_id)}</code>" if e.task_id else ""
    title = f" {_esc(e.title)}" if e.title else ""

    if e.event == "task_started":
        return f"<b>Task started</b>{tid}{title}"
    if e.event == "task_approved":
        return f"<b>Task approved</b>{tid}{title}"
    if e.event == "task_rejected":
        lines = [f"<b>Task rejected</b>{tid}{title}"]
        if e.reason:
            lines.append(f"Reason: {_esc(e.reason)}")
        return "\n".join(lines)
    if e.event == "system_idle":
        return f"<b>DualMind idle</b> — no activity for {e.duration_minutes} min"
    if e.event == "junior_stuck":
        return (
            f"<b>Junior stuck</b>{tid}{title} "
            f"— running for {e.duration_minutes} min with no completion"
        )
    # Unknown / custom event
    return f"<b>{_esc(e.event)}</b>{tid}{title}".strip()


async def process_events(bot: "Bot", chat_ids: list[int]) -> None:
    """Long-running coroutine: deliver lifecycle event messages.

    Started by bot.py alongside process_queue.  Runs until cancelled.
    """
    logger.info("notify: event processor started (%d recipient(s))", len(chat_ids))
    while True:
        event: NotifyEvent = await _events_queue.get()
        text = format_event_message(event)

        for chat_id in chat_ids:
            try:
                await bot.send_message(chat_id, text)
                logger.info(
                    "notify: sent event %s for task %s to user %d",
                    event.event, event.task_id, chat_id,
                )
            except Exception as exc:
                logger.warning(
                    "notify: failed to deliver event to %d: %s", chat_id, exc
                )

        _events_queue.task_done()
