"""Background monitor for DualMind Telegram bot — T5.

Fires alerts when:
  - junior_stuck  : same task has been running for > stuck_minutes
  - system_idle   : no task has been running for > idle_minutes

Architecture
------------
MonitorState is a module-level object.  The orchestrator calls
record_task_started() / record_task_finished() at each transition.
run_monitor() reads the state periodically and pushes NotifyEvent objects
into the events queue (tg_bot.notify.push_event).

Each alert fires at most once per condition:
  - stuck alert resets when the task_id changes (new task starts)
  - idle  alert resets when any task starts

Import-safe: no aiogram imports at module level.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("dualmind.tgbot")


# ── Shared state ──────────────────────────────────────────────────────────────


@dataclass
class MonitorState:
    """Mutable state updated by the orchestrator at each task transition."""

    task_id: str = ""
    task_title: str = ""
    is_running: bool = False
    last_transition: float = field(default_factory=time.monotonic)


_state: MonitorState = MonitorState()


def get_state() -> MonitorState:
    """Return the shared MonitorState singleton.  Safe to import anywhere."""
    return _state


def record_task_started(task_id: str, task_title: str) -> None:
    """Call from orchestrator when Junior is assigned a task."""
    _state.task_id = task_id
    _state.task_title = task_title
    _state.is_running = True
    _state.last_transition = time.monotonic()
    logger.debug("monitor: task_started %s", task_id)


def record_task_finished() -> None:
    """Call from orchestrator when a task completes (any outcome)."""
    _state.is_running = False
    _state.last_transition = time.monotonic()
    logger.debug("monitor: task_finished %s", _state.task_id)


# ── Background monitor coroutine ──────────────────────────────────────────────


async def run_monitor(
    *,
    stuck_minutes: int = 30,
    idle_minutes: int = 30,
    poll_interval: float = 60.0,
) -> None:
    """Long-running coroutine: check for stuck/idle conditions.

    Pushes NotifyEvent objects into the events queue; bot delivers them.
    Runs until cancelled.

    stuck_minutes — alert if Junior is on the same task longer than this
    idle_minutes  — alert if no task has been running longer than this
    poll_interval — check interval in seconds (default 60)
    """
    from tg_bot.notify import push_event, NotifyEvent

    logger.info(
        "TGBot: monitor started (stuck=%dm idle=%dm poll=%.0fs)",
        stuck_minutes, idle_minutes, poll_interval,
    )

    _stuck_alerted_for: str = ""  # task_id for which we last sent a stuck alert
    _idle_alerted: bool = False   # True after we've sent the current idle alert

    while True:
        await asyncio.sleep(poll_interval)

        elapsed_min = (time.monotonic() - _state.last_transition) / 60.0

        if _state.is_running:
            _idle_alerted = False  # reset idle tracking whenever a task runs

            if elapsed_min >= stuck_minutes and _stuck_alerted_for != _state.task_id:
                push_event(NotifyEvent(
                    event="junior_stuck",
                    task_id=_state.task_id,
                    title=_state.task_title,
                    duration_minutes=int(elapsed_min),
                ))
                _stuck_alerted_for = _state.task_id
                logger.warning(
                    "monitor: Junior stuck on task %s (%.0f min)",
                    _state.task_id, elapsed_min,
                )
        else:
            _stuck_alerted_for = ""  # reset stuck tracking when no task is running

            if elapsed_min >= idle_minutes and not _idle_alerted:
                push_event(NotifyEvent(
                    event="system_idle",
                    duration_minutes=int(elapsed_min),
                ))
                _idle_alerted = True
                logger.info("monitor: system idle for %.0f min", elapsed_min)
