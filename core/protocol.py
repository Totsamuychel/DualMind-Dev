"""Message protocol schemas for inter-agent communication."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    REJECTED = "rejected"


class AgentRole(str, Enum):
    LEAD = "lead"
    JUNIOR = "junior"


# ─── Lead → Junior ──────────────────────────────────────────────────────────

class Task(BaseModel):
    """A scoped task assigned by the Lead agent to the Junior agent."""
    id: str
    title: str
    description: str
    files_in_scope: list[str] = Field(description="Relative file paths the junior may touch")
    constraints: list[str] = Field(default_factory=list, description="Hard rules, e.g. 'no new dependencies'")
    acceptance_criteria: list[str] = Field(description="Checklist for done")
    branch: str = Field(description="Git branch to work on")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: TaskStatus = TaskStatus.TODO
    attempt: int = 0  # incremented each time Lead rejects and orchestrator retries


# ─── Junior → Lead ──────────────────────────────────────────────────────────

class ProgressReport(BaseModel):
    """Mid-task status update from Junior to Lead."""
    task_id: str
    message: str
    files_modified_so_far: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class TestResults(BaseModel):
    passed: bool
    summary: str
    details: Optional[str] = None


class PatchReport(BaseModel):
    """Final report from Junior: diff, test results, risks."""
    task_id: str
    branch: str
    diff: str = Field(description="git diff output")
    files_changed: list[str]
    test_results: TestResults
    lint_passed: bool
    typecheck_passed: bool
    risks: list[str] = Field(default_factory=list, description="Potential side effects or concerns")
    notes: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ─── Lead → Junior (review) ─────────────────────────────────────────────────

class ReviewResult(BaseModel):
    """Lead agent's review decision on a PatchReport."""
    task_id: str
    approved: bool
    feedback: str
    requested_changes: list[str] = Field(default_factory=list)
    reviewer: AgentRole = AgentRole.LEAD
    timestamp: datetime = Field(default_factory=datetime.utcnow)
