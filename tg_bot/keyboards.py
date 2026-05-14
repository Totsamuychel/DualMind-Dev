"""Inline keyboard builders for DualMind Telegram bot."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def review_keyboard(task_id: str) -> InlineKeyboardMarkup:
    """Two-button keyboard attached to a review-ready notification message.

    callback_data format:  "approve:<task_id>"  |  "reject:<task_id>"
    Handlers are in tg_bot/callbacks.py.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Approve",
                callback_data=f"approve:{task_id}",
            ),
            InlineKeyboardButton(
                text="Reject",
                callback_data=f"reject:{task_id}",
            ),
        ]]
    )
