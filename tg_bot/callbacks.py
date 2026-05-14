"""Inline-keyboard callback handlers — T3.

Handles the Approve / Reject buttons attached to review-ready messages.
After the user taps a button the message is edited in-place:
  - keyboard is removed (reply_markup=None)
  - a status line is appended showing who took the action

callback_data format (set in keyboards.py):
    "approve:<task_id>"
    "reject:<task_id>"
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from tools.companion_client import CompanionClient

logger = logging.getLogger("dualmind.tgbot")

router = Router(name="callbacks")


def _esc(value: object) -> str:
    return html.escape(str(value))


def _actor(cq: CallbackQuery) -> str:
    """Return a display name for the user who pressed the button."""
    user = cq.from_user
    if user.username:
        return f"@{_esc(user.username)}"
    return _esc(user.first_name or str(user.id))


# ── Approve ───────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(cq: CallbackQuery, companion: CompanionClient) -> None:
    task_id = cq.data.split(":", 1)[1]
    try:
        await companion.dm_approve(task_id)
    except Exception as exc:
        logger.warning("TGBot cb_approve error for %s: %s", task_id, exc)
        await cq.answer(f"Failed to approve: {exc}", show_alert=True)
        return

    # Edit the original message: remove keyboard, append status line.
    original = cq.message.html_text or ""
    try:
        await cq.message.edit_text(
            original + f"\n\n<b>Approved by {_actor(cq)}</b>",
            reply_markup=None,
        )
    except Exception:
        pass  # message may be too old to edit — not critical

    await cq.answer("Approved!")
    logger.info("TGBot: task %s approved by user %d", task_id, cq.from_user.id)


# ── Reject ────────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(cq: CallbackQuery, companion: CompanionClient) -> None:
    """Reject via button press.

    Records "Rejected via Telegram" as the reason.  For a detailed rejection
    reason, use the /reject <id> <reason> text command instead.
    """
    task_id = cq.data.split(":", 1)[1]
    try:
        await companion.dm_reject(task_id, reason="Rejected via Telegram button")
    except Exception as exc:
        logger.warning("TGBot cb_reject error for %s: %s", task_id, exc)
        await cq.answer(f"Failed to reject: {exc}", show_alert=True)
        return

    original = cq.message.html_text or ""
    try:
        await cq.message.edit_text(
            original + f"\n\n<b>Rejected by {_actor(cq)}</b>",
            reply_markup=None,
        )
    except Exception:
        pass

    await cq.answer("Rejected!")
    logger.info("TGBot: task %s rejected by user %d", task_id, cq.from_user.id)
