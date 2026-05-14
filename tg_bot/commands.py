"""Command handlers for DualMind Telegram bot — T2.

Every handler receives `companion: CompanionClient` via aiogram's dependency
injection (stored in the dispatcher as dp["companion"]).  The /log handler
additionally receives `log_file: str` (dp["log_file"]).

Adding a new command:
    1. Write an async def decorated with @router.message(Command("name"))
    2. That's it — the router is included in the dispatcher by bot.py.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from tools.companion_client import CompanionClient

logger = logging.getLogger("dualmind.tgbot")

router = Router(name="commands")

# ── Helpers ───────────────────────────────────────────────────────────────────

_MSG_LIMIT = 4000        # Telegram limit is 4096; leave a small buffer
_MAX_LOG_LINES = 50      # hard cap on /log N to keep messages readable
_MAX_TASKS_SHOWN = 8     # max tasks displayed per status bucket

_LEVEL_KEYWORD: dict[str, str] = {
    "error":   "ERROR",
    "err":     "ERROR",
    "warning": "WARNING",
    "warn":    "WARNING",
    "info":    "INFO",
    "debug":   "DEBUG",
}


def _esc(value: object) -> str:
    """HTML-escape a value for safe insertion into Telegram HTML messages."""
    return html.escape(str(value))


def _trim(text: str, limit: int = _MSG_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def _args(message: Message) -> list[str]:
    """Return space-split tokens of the message, skipping the /command part."""
    return (message.text or "").split()[1:]


# ── /start  /help ─────────────────────────────────────────────────────────────

_HELP = (
    "<b>DualMind Bot</b>\n\n"
    "Commands:\n"
    "/goal <i>&lt;text&gt;</i> — queue a new goal for the Lead agent\n"
    "/status — current Lead / Junior agent status\n"
    "/tasks — list tasks grouped by status\n"
    "/approve <i>&lt;id&gt;</i> — approve a patch\n"
    "/reject <i>&lt;id&gt;</i> <i>[reason]</i> — reject a patch\n"
    "/log <i>[N]</i> <i>[level]</i> — last N log lines; level = error|warning|info\n"
    "/help — show this message"
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(_HELP)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(_HELP)


# ── /goal ─────────────────────────────────────────────────────────────────────

@router.message(Command("goal"))
async def cmd_goal(message: Message, companion: CompanionClient) -> None:
    """POST /goal — queue a new goal for the Lead agent."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Usage: /goal <description of what to build>")
        return

    goal = parts[1].strip()
    try:
        await companion.dm_add_goal(goal)
        await message.answer(
            f"Goal queued:\n<code>{_esc(goal[:300])}</code>"
        )
        logger.info("TGBot: goal queued via Telegram: %s", goal[:80])
    except Exception as exc:
        logger.warning("TGBot /goal error: %s", exc)
        await message.answer(f"Failed to queue goal: <code>{_esc(exc)}</code>")


# ── /status ───────────────────────────────────────────────────────────────────

@router.message(Command("status"))
async def cmd_status(message: Message, companion: CompanionClient) -> None:
    """GET /agents/status — show current Lead / Junior state."""
    try:
        data = await companion.dm_get_agents_status()
    except Exception as exc:
        logger.warning("TGBot /status error: %s", exc)
        await message.answer(f"Failed to fetch status: <code>{_esc(exc)}</code>")
        return

    lead   = _esc(data.get("lead",   "unknown"))
    junior = _esc(data.get("junior", "unknown"))
    task_id = _esc(data.get("task_id") or "—")

    lines = [
        "<b>Agent Status</b>",
        "",
        f"Lead:    <code>{lead}</code>",
        f"Junior:  <code>{junior}</code>",
        f"Task:    <code>{task_id}</code>",
    ]
    if data.get("iteration"):
        lines.append(f"Iteration: {data['iteration']}")
    if data.get("files"):
        files_str = ", ".join(_esc(f) for f in data["files"][:4])
        extra = f" (+{len(data['files']) - 4} more)" if len(data["files"]) > 4 else ""
        lines.append(f"Files:   <code>{files_str}{extra}</code>")

    await message.answer("\n".join(lines))


# ── /tasks ────────────────────────────────────────────────────────────────────

_BUCKET_LABEL: dict[str, str] = {
    "todo":        "TODO",
    "in_progress": "IN PROGRESS",
    "done":        "DONE",
}


@router.message(Command("tasks"))
async def cmd_tasks(message: Message, companion: CompanionClient) -> None:
    """GET /tasks — list tasks grouped by todo / in_progress / done."""
    try:
        data = await companion.dm_get_tasks()
    except Exception as exc:
        logger.warning("TGBot /tasks error: %s", exc)
        await message.answer(f"Failed to fetch tasks: <code>{_esc(exc)}</code>")
        return

    sections: list[str] = ["<b>Tasks</b>"]
    grand_total = 0

    for key in ("todo", "in_progress", "done"):
        bucket: list[dict] = data.get(key) or []
        grand_total += len(bucket)
        label = _BUCKET_LABEL.get(key, key.upper())
        sections.append(f"\n<b>{label} ({len(bucket)})</b>")

        for task in bucket[:_MAX_TASKS_SHOWN]:
            tid   = _esc(task.get("id",    "?"))
            title = _esc(task.get("title", "?")[:60])
            sections.append(f"  · <code>{tid}</code> {title}")

        overflow = len(bucket) - _MAX_TASKS_SHOWN
        if overflow > 0:
            sections.append(f"  ... and {overflow} more")

    if grand_total == 0:
        sections.append("\nNo tasks in queue.")

    await message.answer(_trim("\n".join(sections)))


# ── /approve ──────────────────────────────────────────────────────────────────

@router.message(Command("approve"))
async def cmd_approve(message: Message, companion: CompanionClient) -> None:
    """POST /approve/<id> — approve a patch so it can be merged."""
    args = _args(message)
    if not args:
        await message.answer("Usage: /approve <task_id>")
        return

    task_id = args[0]
    try:
        await companion.dm_approve(task_id)
        await message.answer(f"Approved: <code>{_esc(task_id)}</code>")
        logger.info("TGBot: task %s approved via Telegram", task_id)
    except Exception as exc:
        logger.warning("TGBot /approve error: %s", exc)
        await message.answer(f"Failed to approve <code>{_esc(task_id)}</code>: <code>{_esc(exc)}</code>")


# ── /reject ───────────────────────────────────────────────────────────────────

@router.message(Command("reject"))
async def cmd_reject(message: Message, companion: CompanionClient) -> None:
    """POST /reject/<id> — reject a patch with an optional reason."""
    parts = (message.text or "").split(maxsplit=2)
    # parts[0] = "/reject", parts[1] = task_id, parts[2] = reason (optional)
    if len(parts) < 2:
        await message.answer("Usage: /reject <task_id> [reason]")
        return

    task_id = parts[1]
    reason  = parts[2].strip() if len(parts) > 2 else ""

    try:
        await companion.dm_reject(task_id, reason=reason)
        lines = [f"Rejected: <code>{_esc(task_id)}</code>"]
        if reason:
            lines.append(f"Reason: {_esc(reason)}")
        await message.answer("\n".join(lines))
        logger.info("TGBot: task %s rejected via Telegram: %s", task_id, reason or "(no reason)")
    except Exception as exc:
        logger.warning("TGBot /reject error: %s", exc)
        await message.answer(f"Failed to reject <code>{_esc(task_id)}</code>: <code>{_esc(exc)}</code>")


# ── /log ──────────────────────────────────────────────────────────────────────

@router.message(Command("log"))
async def cmd_log(message: Message, log_file: str = "") -> None:
    """Show the tail of dualmind.log.

    Usage: /log [N] [level]
      N     — number of lines (default 20, max 50)
      level — filter: error | warning | info | debug

    Examples:
      /log 30         → last 30 lines
      /log error      → last ERROR lines (up to 50)
      /log 10 warning → last 10 WARNING lines
    """
    args = _args(message)
    n = 20
    level_kw: str = ""

    for arg in args:
        arg_lower = arg.lower()
        if arg_lower in _LEVEL_KEYWORD:
            level_kw = _LEVEL_KEYWORD[arg_lower]
        else:
            try:
                n = max(1, min(int(arg), _MAX_LOG_LINES))
            except ValueError:
                await message.answer(
                    f"Usage: /log [N] [level]\n"
                    f"  N     — lines to show (max {_MAX_LOG_LINES})\n"
                    f"  level — error | warning | info | debug"
                )
                return

    if not log_file:
        await message.answer(
            "No log file configured.\n"
            "Set <code>logging.file</code> in config.yaml to enable this command."
        )
        return

    path = Path(log_file)
    if not path.exists():
        await message.answer(f"Log file not found: <code>{_esc(log_file)}</code>")
        return

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        await message.answer(f"Cannot read log: <code>{_esc(exc)}</code>")
        return

    lines = text.splitlines()

    if level_kw:
        # Filter to lines containing the requested level keyword.
        lines = [l for l in lines if level_kw in l]
        if not lines:
            await message.answer(
                f"No <code>{level_kw}</code> lines in the log."
            )
            return

    tail_lines = lines[-n:]
    if not tail_lines:
        await message.answer("Log file is empty.")
        return

    if level_kw:
        # Format each line individually so ERROR/WARNING can be highlighted.
        parts: list[str] = []
        for line in tail_lines:
            escaped = _esc(line[:300])   # cap per-line to stay sane
            if "ERROR" in line:
                parts.append(f"<b>{escaped}</b>")
            elif "WARNING" in line:
                parts.append(f"<i>{escaped}</i>")
            else:
                parts.append(f"<code>{escaped}</code>")
        body = _trim("\n".join(parts), _MSG_LIMIT)
        await message.answer(body)
    else:
        # Plain tail — use a <pre> block for monospace output.
        tail = "\n".join(tail_lines).strip()
        pre_budget = _MSG_LIMIT - len("<pre></pre>")
        body = _trim(_esc(tail), pre_budget)
        await message.answer(f"<pre>{body}</pre>")
