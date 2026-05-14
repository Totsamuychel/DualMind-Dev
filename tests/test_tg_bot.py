"""Tests for tg_bot modules — notify formatters, keyboards, monitor state,
AllowedUsersMiddleware, command handlers, and callback handlers."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

import tg_bot.monitor as monitor_module
from tg_bot.notify import (
    ReviewNotification,
    NotifyEvent,
    format_review_message,
    format_event_message,
)
from tg_bot.monitor import get_state, record_task_started, record_task_finished
from tg_bot.bot import AllowedUsersMiddleware


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_monitor():
    monitor_module._state = monitor_module.MonitorState()
    yield
    monitor_module._state = monitor_module.MonitorState()


def make_message(text: str = "/cmd") -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.answer = AsyncMock()
    return msg


def make_cq(
    data: str,
    username: str = "tester",
    first_name: str = "Test",
    user_id: int = 42,
) -> MagicMock:
    cq = MagicMock()
    cq.data = data
    cq.from_user = MagicMock(id=user_id, username=username, first_name=first_name)
    cq.message = MagicMock()
    cq.message.html_text = "<b>Task ready for review</b>"
    cq.message.edit_text = AsyncMock()
    cq.answer = AsyncMock()
    return cq


def make_companion(**overrides) -> MagicMock:
    c = MagicMock()
    c.dm_add_goal            = AsyncMock()
    c.dm_get_agents_status   = AsyncMock(return_value={"lead": "idle", "junior": "idle"})
    c.dm_get_tasks           = AsyncMock(return_value={"todo": [], "in_progress": [], "done": []})
    c.dm_approve             = AsyncMock()
    c.dm_reject              = AsyncMock()
    for attr, val in overrides.items():
        setattr(c, attr, val)
    return c


# ── format_review_message ─────────────────────────────────────────────────────


class TestFormatReviewMessage:
    def _make(self, **kw) -> ReviewNotification:
        return ReviewNotification(
            task_id=kw.get("task_id", "t1"),
            task_title=kw.get("task_title", "Add feature"),
            branch=kw.get("branch", "feature/t1"),
            diff_lines=kw.get("diff_lines", 12),
            tests_passed=kw.get("tests_passed", True),
            lint_passed=kw.get("lint_passed", True),
            files_changed=kw.get("files_changed", ["src/main.py"]),
        )

    def test_contains_task_id(self):
        assert "t1" in format_review_message(self._make())

    def test_contains_branch(self):
        assert "feature/t1" in format_review_message(self._make())

    def test_tests_pass_shows_pass(self):
        assert "PASS" in format_review_message(self._make(tests_passed=True))

    def test_tests_fail_shows_fail(self):
        assert "FAIL" in format_review_message(self._make(tests_passed=False))

    def test_diff_lines_shown(self):
        assert "42" in format_review_message(self._make(diff_lines=42))

    def test_html_escapes_title(self):
        msg = format_review_message(self._make(task_title="Fix <XSS> & co"))
        assert "&lt;XSS&gt;" in msg
        assert "<XSS>" not in msg

    def test_files_truncated_at_four(self):
        files = [f"file{i}.py" for i in range(6)]
        msg = format_review_message(self._make(files_changed=files))
        assert "+2 more" in msg

    def test_no_files_shows_dash(self):
        assert "—" in format_review_message(self._make(files_changed=[]))


# ── format_event_message ──────────────────────────────────────────────────────


class TestFormatEventMessage:
    def test_task_started(self):
        e = NotifyEvent(event="task_started", task_id="t1", title="My task")
        msg = format_event_message(e)
        assert "Task started" in msg
        assert "t1" in msg

    def test_task_approved(self):
        msg = format_event_message(NotifyEvent(event="task_approved", task_id="t2"))
        assert "Task approved" in msg
        assert "t2" in msg

    def test_task_rejected_with_reason(self):
        e = NotifyEvent(event="task_rejected", task_id="t3", reason="Tests failing")
        msg = format_event_message(e)
        assert "Task rejected" in msg
        assert "Tests failing" in msg

    def test_task_rejected_no_reason_omits_reason_line(self):
        e = NotifyEvent(event="task_rejected", task_id="t3")
        msg = format_event_message(e)
        assert "Reason" not in msg

    def test_system_idle_shows_duration(self):
        e = NotifyEvent(event="system_idle", duration_minutes=45)
        msg = format_event_message(e)
        assert "idle" in msg.lower()
        assert "45" in msg

    def test_junior_stuck_shows_duration(self):
        e = NotifyEvent(event="junior_stuck", task_id="t4", title="Big task", duration_minutes=35)
        msg = format_event_message(e)
        assert "stuck" in msg.lower()
        assert "35" in msg

    def test_html_escapes_event_title(self):
        e = NotifyEvent(event="task_started", task_id="t1", title="Fix <XSS>")
        msg = format_event_message(e)
        assert "&lt;XSS&gt;" in msg
        assert "<XSS>" not in msg

    def test_unknown_event_falls_back_to_event_name(self):
        e = NotifyEvent(event="deploy_done", task_id="t5")
        msg = format_event_message(e)
        assert "deploy_done" in msg


# ── review_keyboard ───────────────────────────────────────────────────────────


class TestReviewKeyboard:
    def test_returns_inline_markup(self):
        from tg_bot.keyboards import review_keyboard
        from aiogram.types import InlineKeyboardMarkup
        assert isinstance(review_keyboard("t1"), InlineKeyboardMarkup)

    def test_single_row_two_buttons(self):
        from tg_bot.keyboards import review_keyboard
        kb = review_keyboard("t1")
        assert len(kb.inline_keyboard) == 1
        assert len(kb.inline_keyboard[0]) == 2

    def test_approve_button_callback_data(self):
        from tg_bot.keyboards import review_keyboard
        kb = review_keyboard("abc-123")
        assert kb.inline_keyboard[0][0].callback_data == "approve:abc-123"

    def test_reject_button_callback_data(self):
        from tg_bot.keyboards import review_keyboard
        kb = review_keyboard("abc-123")
        assert kb.inline_keyboard[0][1].callback_data == "reject:abc-123"

    def test_button_labels(self):
        from tg_bot.keyboards import review_keyboard
        kb = review_keyboard("t1")
        texts = {btn.text for btn in kb.inline_keyboard[0]}
        assert "Approve" in texts
        assert "Reject" in texts


# ── MonitorState ──────────────────────────────────────────────────────────────


class TestMonitorState:
    def test_initial_not_running(self):
        assert get_state().is_running is False
        assert get_state().task_id == ""

    def test_record_started_sets_fields(self):
        record_task_started("t1", "Do something")
        state = get_state()
        assert state.is_running is True
        assert state.task_id == "t1"
        assert state.task_title == "Do something"

    def test_record_finished_clears_running(self):
        record_task_started("t1", "x")
        record_task_finished()
        assert get_state().is_running is False

    def test_record_finished_preserves_task_id(self):
        record_task_started("t2", "Other task")
        record_task_finished()
        assert get_state().task_id == "t2"

    def test_transition_time_updated_on_start(self):
        import time
        before = time.monotonic()
        record_task_started("t1", "x")
        after = time.monotonic()
        assert before <= get_state().last_transition <= after

    def test_transition_time_updated_on_finish(self):
        import time
        record_task_started("t1", "x")
        before = time.monotonic()
        record_task_finished()
        after = time.monotonic()
        assert before <= get_state().last_transition <= after

    def test_second_start_replaces_first(self):
        record_task_started("t1", "first")
        record_task_started("t2", "second")
        assert get_state().task_id == "t2"
        assert get_state().task_title == "second"


# ── AllowedUsersMiddleware ────────────────────────────────────────────────────


class TestAllowedUsersMiddleware:
    async def test_allowed_user_passes_through(self):
        mw = AllowedUsersMiddleware(frozenset({123}))
        handler = AsyncMock(return_value="ok")
        data = {"event_from_user": MagicMock(id=123, username="user")}
        result = await mw(handler, MagicMock(), data)
        handler.assert_awaited_once()
        assert result == "ok"

    async def test_blocked_user_not_passed(self):
        mw = AllowedUsersMiddleware(frozenset({123}))
        handler = AsyncMock()
        data = {"event_from_user": MagicMock(id=999, username="stranger")}
        result = await mw(handler, MagicMock(), data)
        handler.assert_not_awaited()
        assert result is None

    async def test_no_from_user_blocked(self):
        mw = AllowedUsersMiddleware(frozenset({123}))
        handler = AsyncMock()
        result = await mw(handler, MagicMock(), {})
        handler.assert_not_awaited()
        assert result is None

    async def test_empty_allow_list_blocks_all(self):
        mw = AllowedUsersMiddleware(frozenset())
        handler = AsyncMock()
        data = {"event_from_user": MagicMock(id=1, username="anyone")}
        await mw(handler, MagicMock(), data)
        handler.assert_not_awaited()


# ── cmd_goal ──────────────────────────────────────────────────────────────────


class TestCmdGoal:
    async def test_no_args_sends_usage(self):
        from tg_bot.commands import cmd_goal
        msg = make_message("/goal")
        await cmd_goal(msg, companion=make_companion())
        assert "Usage" in msg.answer.call_args[0][0]

    async def test_queues_goal_text(self):
        from tg_bot.commands import cmd_goal
        msg = make_message("/goal build a REST API")
        c = make_companion()
        await cmd_goal(msg, companion=c)
        c.dm_add_goal.assert_awaited_once_with("build a REST API")

    async def test_success_reply_contains_goal(self):
        from tg_bot.commands import cmd_goal
        msg = make_message("/goal refactor auth")
        c = make_companion()
        await cmd_goal(msg, companion=c)
        assert "refactor auth" in msg.answer.call_args[0][0]

    async def test_companion_error_replies_failure(self):
        from tg_bot.commands import cmd_goal
        msg = make_message("/goal something")
        c = make_companion(dm_add_goal=AsyncMock(side_effect=Exception("refused")))
        await cmd_goal(msg, companion=c)
        assert "Failed" in msg.answer.call_args[0][0]


# ── cmd_status ────────────────────────────────────────────────────────────────


class TestCmdStatus:
    async def test_shows_lead_junior_task(self):
        from tg_bot.commands import cmd_status
        msg = make_message("/status")
        c = make_companion(dm_get_agents_status=AsyncMock(return_value={
            "lead": "idle", "junior": "working", "task_id": "t1"
        }))
        await cmd_status(msg, companion=c)
        reply = msg.answer.call_args[0][0]
        assert "idle" in reply
        assert "working" in reply
        assert "t1" in reply

    async def test_missing_task_id_shows_dash(self):
        from tg_bot.commands import cmd_status
        msg = make_message("/status")
        c = make_companion(dm_get_agents_status=AsyncMock(return_value={
            "lead": "idle", "junior": "idle"
        }))
        await cmd_status(msg, companion=c)
        assert "—" in msg.answer.call_args[0][0]

    async def test_companion_error_replies_failure(self):
        from tg_bot.commands import cmd_status
        msg = make_message("/status")
        c = make_companion(dm_get_agents_status=AsyncMock(side_effect=Exception("timeout")))
        await cmd_status(msg, companion=c)
        assert "Failed" in msg.answer.call_args[0][0]


# ── cmd_tasks ─────────────────────────────────────────────────────────────────


class TestCmdTasks:
    async def test_empty_queue_message(self):
        from tg_bot.commands import cmd_tasks
        msg = make_message("/tasks")
        await cmd_tasks(msg, companion=make_companion())
        assert "No tasks" in msg.answer.call_args[0][0]

    async def test_shows_task_id_and_title(self):
        from tg_bot.commands import cmd_tasks
        msg = make_message("/tasks")
        c = make_companion(dm_get_tasks=AsyncMock(return_value={
            "todo": [{"id": "t1", "title": "Fix bug"}],
            "in_progress": [],
            "done": [],
        }))
        await cmd_tasks(msg, companion=c)
        reply = msg.answer.call_args[0][0]
        assert "t1" in reply
        assert "Fix bug" in reply

    async def test_companion_error_replies_failure(self):
        from tg_bot.commands import cmd_tasks
        msg = make_message("/tasks")
        c = make_companion(dm_get_tasks=AsyncMock(side_effect=Exception("net error")))
        await cmd_tasks(msg, companion=c)
        assert "Failed" in msg.answer.call_args[0][0]


# ── cmd_approve ───────────────────────────────────────────────────────────────


class TestCmdApprove:
    async def test_no_args_sends_usage(self):
        from tg_bot.commands import cmd_approve
        msg = make_message("/approve")
        await cmd_approve(msg, companion=make_companion())
        assert "Usage" in msg.answer.call_args[0][0]

    async def test_approves_task(self):
        from tg_bot.commands import cmd_approve
        msg = make_message("/approve t42")
        c = make_companion()
        await cmd_approve(msg, companion=c)
        c.dm_approve.assert_awaited_once_with("t42")

    async def test_reply_contains_task_id(self):
        from tg_bot.commands import cmd_approve
        msg = make_message("/approve t42")
        await cmd_approve(msg, companion=make_companion())
        assert "t42" in msg.answer.call_args[0][0]

    async def test_companion_error_replies_failure(self):
        from tg_bot.commands import cmd_approve
        msg = make_message("/approve t1")
        c = make_companion(dm_approve=AsyncMock(side_effect=Exception("gone")))
        await cmd_approve(msg, companion=c)
        assert "Failed" in msg.answer.call_args[0][0]


# ── cmd_reject ────────────────────────────────────────────────────────────────


class TestCmdReject:
    async def test_no_args_sends_usage(self):
        from tg_bot.commands import cmd_reject
        msg = make_message("/reject")
        await cmd_reject(msg, companion=make_companion())
        assert "Usage" in msg.answer.call_args[0][0]

    async def test_rejects_without_reason(self):
        from tg_bot.commands import cmd_reject
        msg = make_message("/reject t5")
        c = make_companion()
        await cmd_reject(msg, companion=c)
        c.dm_reject.assert_awaited_once_with("t5", reason="")

    async def test_rejects_with_reason(self):
        from tg_bot.commands import cmd_reject
        msg = make_message("/reject t5 Tests still fail")
        c = make_companion()
        await cmd_reject(msg, companion=c)
        c.dm_reject.assert_awaited_once_with("t5", reason="Tests still fail")

    async def test_reason_in_reply(self):
        from tg_bot.commands import cmd_reject
        msg = make_message("/reject t5 Wrong approach")
        await cmd_reject(msg, companion=make_companion())
        assert "Wrong approach" in msg.answer.call_args[0][0]


# ── cmd_log ───────────────────────────────────────────────────────────────────


class TestCmdLog:
    async def test_no_log_file_configured(self):
        from tg_bot.commands import cmd_log
        msg = make_message("/log")
        await cmd_log(msg, log_file="")
        assert "No log file" in msg.answer.call_args[0][0]

    async def test_file_not_found(self, tmp_path):
        from tg_bot.commands import cmd_log
        msg = make_message("/log")
        await cmd_log(msg, log_file=str(tmp_path / "missing.log"))
        assert "not found" in msg.answer.call_args[0][0].lower()

    async def test_shows_tail_in_pre_block(self, tmp_path):
        from tg_bot.commands import cmd_log
        log = tmp_path / "dualmind.log"
        log.write_text("\n".join(f"INFO line {i}" for i in range(30)))
        msg = make_message("/log 5")
        await cmd_log(msg, log_file=str(log))
        reply = msg.answer.call_args[0][0]
        assert "<pre>" in reply
        assert "line 29" in reply

    async def test_level_filter_keeps_only_matching_level(self, tmp_path):
        from tg_bot.commands import cmd_log
        log = tmp_path / "dualmind.log"
        log.write_text("INFO hello\nERROR bad thing happened\nINFO world\n")
        msg = make_message("/log error")
        await cmd_log(msg, log_file=str(log))
        reply = msg.answer.call_args[0][0]
        assert "bad thing happened" in reply
        assert "hello" not in reply

    async def test_level_filter_no_matches_says_so(self, tmp_path):
        from tg_bot.commands import cmd_log
        log = tmp_path / "dualmind.log"
        log.write_text("INFO line1\nINFO line2\n")
        msg = make_message("/log error")
        await cmd_log(msg, log_file=str(log))
        assert "No" in msg.answer.call_args[0][0]

    async def test_empty_log_says_so(self, tmp_path):
        from tg_bot.commands import cmd_log
        log = tmp_path / "dualmind.log"
        log.write_text("")
        msg = make_message("/log")
        await cmd_log(msg, log_file=str(log))
        assert "empty" in msg.answer.call_args[0][0].lower()

    async def test_invalid_arg_sends_usage(self):
        from tg_bot.commands import cmd_log
        msg = make_message("/log notanumber")
        await cmd_log(msg, log_file="/irrelevant/path")
        assert "Usage" in msg.answer.call_args[0][0]


# ── cb_approve ────────────────────────────────────────────────────────────────


class TestCbApprove:
    async def test_calls_companion_approve(self):
        from tg_bot.callbacks import cb_approve
        cq = make_cq("approve:task-99")
        c = make_companion()
        await cb_approve(cq, companion=c)
        c.dm_approve.assert_awaited_once_with("task-99")

    async def test_answers_approved(self):
        from tg_bot.callbacks import cb_approve
        cq = make_cq("approve:task-99")
        await cb_approve(cq, companion=make_companion())
        cq.answer.assert_awaited_once_with("Approved!")

    async def test_edits_message_with_actor(self):
        from tg_bot.callbacks import cb_approve
        cq = make_cq("approve:t1", username="alice")
        await cb_approve(cq, companion=make_companion())
        cq.message.edit_text.assert_awaited_once()
        edited = cq.message.edit_text.call_args[0][0]
        assert "Approved by" in edited
        assert "@alice" in edited

    async def test_companion_error_shows_alert(self):
        from tg_bot.callbacks import cb_approve
        cq = make_cq("approve:t1")
        c = make_companion(dm_approve=AsyncMock(side_effect=Exception("net error")))
        await cb_approve(cq, companion=c)
        assert cq.answer.call_args.kwargs.get("show_alert") is True

    async def test_edit_failure_does_not_raise(self):
        from tg_bot.callbacks import cb_approve
        cq = make_cq("approve:t1")
        cq.message.edit_text = AsyncMock(side_effect=Exception("too old"))
        await cb_approve(cq, companion=make_companion())
        # Should not raise — edit errors are swallowed.
        cq.answer.assert_awaited_once_with("Approved!")


# ── cb_reject ─────────────────────────────────────────────────────────────────


class TestCbReject:
    async def test_calls_companion_reject_with_reason(self):
        from tg_bot.callbacks import cb_reject
        cq = make_cq("reject:task-7")
        c = make_companion()
        await cb_reject(cq, companion=c)
        c.dm_reject.assert_awaited_once_with("task-7", reason="Rejected via Telegram button")

    async def test_answers_rejected(self):
        from tg_bot.callbacks import cb_reject
        cq = make_cq("reject:t1")
        await cb_reject(cq, companion=make_companion())
        cq.answer.assert_awaited_once_with("Rejected!")

    async def test_edits_message_with_actor(self):
        from tg_bot.callbacks import cb_reject
        cq = make_cq("reject:t1", username="bob")
        await cb_reject(cq, companion=make_companion())
        cq.message.edit_text.assert_awaited_once()
        edited = cq.message.edit_text.call_args[0][0]
        assert "Rejected by" in edited
        assert "@bob" in edited

    async def test_companion_error_shows_alert(self):
        from tg_bot.callbacks import cb_reject
        cq = make_cq("reject:t1")
        c = make_companion(dm_reject=AsyncMock(side_effect=Exception("server down")))
        await cb_reject(cq, companion=c)
        assert cq.answer.call_args.kwargs.get("show_alert") is True
