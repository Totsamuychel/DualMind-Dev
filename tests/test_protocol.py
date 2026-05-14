"""Tests for core/protocol.py — Pydantic model serialisation / deserialisation."""

from __future__ import annotations

from datetime import datetime

import pytest

from core.protocol import (
    AgentRole,
    PatchReport,
    ProgressReport,
    ReviewResult,
    Task,
    TaskStatus,
    TestResults,
)
from tests.conftest import make_task, make_patch_report, make_review


# ── Task ──────────────────────────────────────────────────────────────────────

class TestTask:
    def test_defaults(self):
        task = make_task()
        assert task.status == TaskStatus.TODO
        assert task.attempt == 0
        assert task.constraints == []
        assert isinstance(task.created_at, datetime)

    def test_round_trip_json(self):
        task = make_task("abc", attempt=1, status=TaskStatus.IN_PROGRESS)
        data = task.model_dump(mode="json")

        assert data["id"] == "abc"
        assert data["status"] == "in_progress"  # enum → string
        assert data["attempt"] == 1
        assert isinstance(data["created_at"], str)  # datetime → ISO string

        task2 = Task.model_validate(data)
        assert task2.id == task.id
        assert task2.status == TaskStatus.IN_PROGRESS
        assert task2.attempt == task.attempt
        assert task2.branch == task.branch

    def test_all_statuses_round_trip(self):
        for status in TaskStatus:
            task = make_task(status=status)
            data = task.model_dump(mode="json")
            task2 = Task.model_validate(data)
            assert task2.status == status

    def test_attempt_increments_survive_round_trip(self):
        task = make_task(attempt=3)
        data = task.model_dump(mode="json")
        task2 = Task.model_validate(data)
        assert task2.attempt == 3

    def test_files_in_scope_preserved(self):
        task = make_task()
        task.files_in_scope = ["src/a.py", "src/b.py"]
        data = task.model_dump(mode="json")
        task2 = Task.model_validate(data)
        assert task2.files_in_scope == ["src/a.py", "src/b.py"]

    def test_constraints_preserved(self):
        task = make_task()
        task.constraints = ["no new deps", "no breaking changes"]
        data = task.model_dump(mode="json")
        task2 = Task.model_validate(data)
        assert task2.constraints == ["no new deps", "no breaking changes"]


# ── TestResults ───────────────────────────────────────────────────────────────

class TestTestResults:
    def test_passed(self):
        r = TestResults(passed=True, summary="3 passed")
        assert r.passed is True
        assert r.details is None

    def test_failed_with_details(self):
        r = TestResults(passed=False, summary="1 failed", details="AssertionError at line 5")
        assert r.passed is False
        assert "AssertionError" in r.details


# ── PatchReport ───────────────────────────────────────────────────────────────

class TestPatchReport:
    def test_round_trip(self):
        task = make_task()
        report = make_patch_report(task)
        data = report.model_dump(mode="json")
        r2 = PatchReport.model_validate(data)
        assert r2.task_id == report.task_id
        assert r2.branch == report.branch
        assert r2.lint_passed == report.lint_passed
        assert r2.test_results.passed == report.test_results.passed

    def test_failed_tests(self):
        task = make_task()
        report = make_patch_report(task, tests_passed=False)
        assert report.test_results.passed is False

    def test_files_changed(self):
        task = make_task()
        report = make_patch_report(task)
        assert "test.py" in report.files_changed


# ── ReviewResult ──────────────────────────────────────────────────────────────

class TestReviewResult:
    def test_approved(self):
        task = make_task()
        review = make_review(task, approved=True)
        assert review.approved is True
        assert review.reviewer == AgentRole.LEAD

    def test_rejected_with_feedback(self):
        task = make_task()
        review = make_review(task, approved=False, feedback="Fix the tests.")
        assert review.approved is False
        assert review.feedback == "Fix the tests."

    def test_round_trip(self):
        task = make_task()
        review = make_review(task, approved=False, feedback="Bad code")
        data = review.model_dump(mode="json")
        r2 = ReviewResult.model_validate(data)
        assert r2.approved is False
        assert r2.feedback == "Bad code"
        assert r2.reviewer == AgentRole.LEAD

    def test_requested_changes_default_empty(self):
        task = make_task()
        review = make_review(task)
        assert review.requested_changes == []


# ── ProgressReport ────────────────────────────────────────────────────────────

class TestProgressReport:
    def test_basic(self):
        p = ProgressReport(
            task_id="t1",
            message="Halfway done",
            files_modified_so_far=["a.py"],
        )
        assert p.task_id == "t1"
        assert p.blockers == []

    def test_round_trip(self):
        p = ProgressReport(
            task_id="t1",
            message="Done reading files",
            files_modified_so_far=["src/main.py"],
            blockers=["missing dependency"],
        )
        data = p.model_dump(mode="json")
        p2 = ProgressReport.model_validate(data)
        assert p2.blockers == ["missing dependency"]
