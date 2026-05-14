"""Tests for core/orchestrator.py — retry logic, approval wait, task persistence."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.orchestrator import Orchestrator
from core.protocol import Task, TaskStatus
from tests.conftest import (
    ORCH_CONFIG, mock_companion,
    make_task, make_patch_report, make_review,
)


@pytest.fixture
def orch(mock_companion):
    o = Orchestrator(ORCH_CONFIG)
    o.companion = mock_companion
    # Prevent actual LLM / SSH calls.
    o.lead = MagicMock()
    o.lead.decompose_next_goal = AsyncMock(return_value=[])
    o.lead.review_patch        = AsyncMock()
    o.lead.record_task_outcome = AsyncMock()
    o.junior = MagicMock()
    o.junior.execute_task      = AsyncMock()
    return o


# ── _save_task ────────────────────────────────────────────────────────────────

async def test_save_task_calls_companion(orch, mock_companion):
    task = make_task("t1")
    await orch._save_task(task)
    mock_companion.dm_save_task.assert_awaited_once()
    payload = mock_companion.dm_save_task.call_args[0][0]
    assert payload["id"] == "t1"
    assert payload["status"] == "todo"


async def test_save_task_silently_ignores_companion_error(orch, mock_companion):
    mock_companion.dm_save_task.side_effect = Exception("companion down")
    task = make_task()
    # Should NOT raise.
    await orch._save_task(task)


# ── _recover_tasks ────────────────────────────────────────────────────────────

async def test_recover_loads_in_progress_tasks(orch, mock_companion):
    task = make_task("r1", status=TaskStatus.IN_PROGRESS)
    data = task.model_dump(mode="json")
    mock_companion.dm_get_tasks.return_value = {
        "todo": [], "in_progress": [data], "done": []
    }
    await orch._recover_tasks()
    assert len(orch.task_queue) == 1
    assert orch.task_queue[0].id == "r1"


async def test_recover_loads_todo_tasks(orch, mock_companion):
    # _recover_tasks loads both todo and in_progress for crash recovery.
    task = make_task("t1", status=TaskStatus.TODO).model_dump(mode="json")
    mock_companion.dm_get_tasks.return_value = {
        "todo": [task], "in_progress": [], "done": []
    }
    await orch._recover_tasks()
    assert len(orch.task_queue) == 1
    assert orch.task_queue[0].id == "t1"


async def test_recover_ignores_done(orch, mock_companion):
    done = make_task("d1", status=TaskStatus.DONE).model_dump(mode="json")
    mock_companion.dm_get_tasks.return_value = {
        "todo": [], "in_progress": [], "done": [done]
    }
    await orch._recover_tasks()
    assert orch.task_queue == []


async def test_recover_skips_malformed_task(orch, mock_companion):
    mock_companion.dm_get_tasks.return_value = {
        "todo": [], "in_progress": [{"bad": "data"}], "done": []
    }
    # Should not raise, just skip.
    await orch._recover_tasks()
    assert orch.task_queue == []


async def test_recover_handles_companion_failure(orch, mock_companion):
    mock_companion.dm_get_tasks.side_effect = Exception("unreachable")
    # Should not raise.
    await orch._recover_tasks()
    assert orch.task_queue == []


# ── _wait_for_human_approval ──────────────────────────────────────────────────

async def test_wait_returns_approved(orch, mock_companion):
    mock_companion.dm_check_approval.return_value = {"status": "approved", "reason": ""}
    outcome, reason = await orch._wait_for_human_approval("t1")
    assert outcome == "approved"
    assert reason == ""


async def test_wait_returns_rejected_with_reason(orch, mock_companion):
    mock_companion.dm_check_approval.return_value = {
        "status": "rejected", "reason": "Tests still failing"
    }
    outcome, reason = await orch._wait_for_human_approval("t1")
    assert outcome == "rejected"
    assert "Tests still failing" in reason


async def test_wait_times_out(orch, mock_companion):
    # approval_timeout_minutes = 1 (set in ORCH_CONFIG), poll every 5s.
    # Override timeout to 0 to make it instant in tests.
    orch.approval_timeout = 0
    mock_companion.dm_check_approval.return_value = {"status": "pending", "reason": ""}
    outcome, reason = await orch._wait_for_human_approval("t1")
    assert outcome == "timeout"


async def test_wait_polls_until_resolved(orch, mock_companion):
    # First two calls return pending, third returns approved.
    mock_companion.dm_check_approval.side_effect = [
        {"status": "pending", "reason": ""},
        {"status": "pending", "reason": ""},
        {"status": "approved", "reason": ""},
    ]
    outcome, _ = await orch._wait_for_human_approval("t1")
    assert outcome == "approved"
    assert mock_companion.dm_check_approval.await_count == 3


# ── _execute_task_with_retries — happy path ───────────────────────────────────

async def test_happy_path_lead_approves_human_approves(orch, mock_companion):
    task = make_task("t1")
    orch.junior.execute_task.return_value = make_patch_report(task)
    orch.lead.review_patch.return_value = make_review(task, approved=True)
    mock_companion.dm_check_approval.return_value = {"status": "approved", "reason": ""}

    await orch._execute_task_with_retries(task)

    assert task.status == TaskStatus.DONE
    orch.lead.record_task_outcome.assert_awaited_once()
    call_args = orch.lead.record_task_outcome.call_args
    assert call_args[0][2] == "approved"


async def test_lead_approves_human_rejects(orch, mock_companion):
    task = make_task("t1")
    orch.junior.execute_task.return_value = make_patch_report(task)
    orch.lead.review_patch.return_value = make_review(task, approved=True)
    mock_companion.dm_check_approval.return_value = {
        "status": "rejected", "reason": "Not what I asked for"
    }

    await orch._execute_task_with_retries(task)

    assert task.status == TaskStatus.REJECTED
    call_args = orch.lead.record_task_outcome.call_args
    assert call_args[0][2] == "rejected"


# ── _execute_task_with_retries — retry logic ──────────────────────────────────

async def test_retry_injects_feedback_into_second_attempt(orch, mock_companion):
    task = make_task("t1")
    captured_feedbacks: list[str] = []

    async def mock_execute(t, *, feedback=""):
        captured_feedbacks.append(feedback)
        return make_patch_report(t)

    orch.junior.execute_task.side_effect = mock_execute
    orch.lead.review_patch.side_effect = [
        make_review(task, approved=False, feedback="Fix the null check"),
        make_review(task, approved=True),
    ]
    mock_companion.dm_check_approval.return_value = {"status": "approved", "reason": ""}

    await orch._execute_task_with_retries(task)

    assert len(captured_feedbacks) == 2
    assert captured_feedbacks[0] == ""                    # first attempt: no feedback
    assert "Fix the null check" in captured_feedbacks[1] # retry: Lead's feedback


async def test_retry_increments_attempt_counter(orch, mock_companion):
    task = make_task("t1")
    orch.junior.execute_task.return_value = make_patch_report(task)
    orch.lead.review_patch.side_effect = [
        make_review(task, approved=False, feedback="bad"),
        make_review(task, approved=True),
    ]
    mock_companion.dm_check_approval.return_value = {"status": "approved", "reason": ""}

    await orch._execute_task_with_retries(task)
    assert task.attempt == 1


async def test_max_retries_exhausted_marks_rejected(orch, mock_companion):
    # max_retries = 2 → allowed attempts: 0, 1, 2 → rejected after 3rd Lead rejection
    task = make_task("t1")
    orch.junior.execute_task.return_value = make_patch_report(task)
    orch.lead.review_patch.return_value = make_review(task, approved=False, feedback="still wrong")

    await orch._execute_task_with_retries(task)

    assert task.status == TaskStatus.REJECTED
    assert task.attempt == orch.max_retries + 1
    call_args = orch.lead.record_task_outcome.call_args
    assert call_args[0][2] == "rejected"


async def test_max_retries_junior_called_correct_times(orch, mock_companion):
    task = make_task("t1")
    orch.junior.execute_task.return_value = make_patch_report(task)
    orch.lead.review_patch.return_value = make_review(task, approved=False)

    await orch._execute_task_with_retries(task)

    # max_retries=2 → 3 total attempts (0, 1, 2)
    assert orch.junior.execute_task.await_count == orch.max_retries + 1


async def test_task_persisted_on_each_status_change(orch, mock_companion):
    task = make_task("t1")
    orch.junior.execute_task.return_value = make_patch_report(task)
    orch.lead.review_patch.return_value = make_review(task, approved=True)
    mock_companion.dm_check_approval.return_value = {"status": "approved", "reason": ""}

    await orch._execute_task_with_retries(task)

    # Should be saved at least twice: IN_PROGRESS and DONE
    assert mock_companion.dm_save_task.await_count >= 2


# ── _try_push_event ───────────────────────────────────────────────────────────

def test_push_event_silently_fails_without_tg_bot(orch):
    task = make_task("t1")
    # Should not raise even if tg_bot is unavailable.
    with patch("builtins.__import__", side_effect=ImportError("no tg_bot")):
        orch._try_push_event("task_started", task)
