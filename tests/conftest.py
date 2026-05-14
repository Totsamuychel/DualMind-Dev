"""Shared pytest fixtures and factory helpers."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.protocol import (
    Task, PatchReport, ReviewResult, TestResults, TaskStatus,
)
from tools.companion_client import ExecResult


# ── Minimal agent configs ─────────────────────────────────────────────────────

JUNIOR_CONFIG = {
    "model_endpoint": "http://localhost:11434",
    "model_name":     "test-model",
    "sandbox_dir":    "/sandbox",
    "companion_url":  "http://localhost:8765",
    "host":           "localhost",
    "ssh_user":       "test",
    "ssh_key":        "~/.ssh/test_rsa",
}

LEAD_CONFIG = {
    "model_endpoint":  "http://localhost:11434",
    "model_name":      "test-model",
    "companion_url":   "http://localhost:8765",
    "host":            "localhost",
    "ssh_user":        "test",
    "ssh_key":         "~/.ssh/test_rsa",
    "sandbox_dir":     "/sandbox",
}

ORCH_CONFIG = {
    "lead_agent":  LEAD_CONFIG,
    "junior_agent": JUNIOR_CONFIG,
    "task_queue": {
        "max_retries": 2,
        "approval_timeout_minutes": 1,  # short for tests
    },
}


# ── Companion mock ────────────────────────────────────────────────────────────

def ok_exec(stdout: str = "", stderr: str = "") -> ExecResult:
    return ExecResult(stdout=stdout, stderr=stderr, exit_code=0)

def fail_exec(stderr: str = "error") -> ExecResult:
    return ExecResult(stdout="", stderr=stderr, exit_code=1)


@pytest.fixture
def mock_companion():
    c = MagicMock()
    c.execute                = AsyncMock(return_value=ok_exec())
    c.dm_update_agents_status = AsyncMock(return_value={"ok": True})
    c.dm_save_task           = AsyncMock(return_value={"ok": True})
    c.dm_check_approval      = AsyncMock(return_value={"status": "pending", "reason": ""})
    c.dm_get_tasks           = AsyncMock(return_value={"todo": [], "in_progress": [], "done": []})
    c.dm_add_goal            = AsyncMock(return_value={"ok": True})
    c.dm_approve             = AsyncMock(return_value={"ok": True})
    c.dm_reject              = AsyncMock(return_value={"ok": True})
    c.dm_get_agents_status   = AsyncMock(return_value={"lead": "idle", "junior": "idle"})
    c.dm_poll_notify         = AsyncMock(return_value=[])
    return c


# ── Protocol factory helpers ──────────────────────────────────────────────────

def make_task(
    task_id: str = "t1",
    title: str = "Test task",
    status: TaskStatus = TaskStatus.TODO,
    attempt: int = 0,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description="A test task description.",
        files_in_scope=["test.py"],
        constraints=[],
        acceptance_criteria=["tests pass"],
        branch=f"feature/{task_id}",
        status=status,
        attempt=attempt,
    )


def make_patch_report(task: Task, tests_passed: bool = True) -> PatchReport:
    return PatchReport(
        task_id=task.id,
        branch=task.branch,
        diff="--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-old\n+new\n",
        files_changed=["test.py"],
        test_results=TestResults(
            passed=tests_passed,
            summary="1 passed" if tests_passed else "1 failed",
        ),
        lint_passed=True,
        typecheck_passed=True,
    )


def make_review(task: Task, approved: bool = True, feedback: str = "") -> ReviewResult:
    return ReviewResult(
        task_id=task.id,
        approved=approved,
        feedback=feedback or ("LGTM" if approved else "Needs work"),
    )
