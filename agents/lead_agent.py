"""Lead Agent — planner, decomposer, reviewer."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from core.protocol import Task, PatchReport, ReviewResult, TaskStatus

logger = logging.getLogger("dualmind.lead")


class LeadAgent:
    """Runs on the high-end GPU machine (e.g. RTX 3090).
    Responsible for: goal decomposition, task creation, code review.
    """

    def __init__(self, config: dict):
        self.endpoint = config["model_endpoint"]
        self.model = config["model_name"]
        self.goals: list[str] = []  # Populated by human or loaded from file

    async def _chat(self, prompt: str, system: str = "") -> str:
        """Send a prompt to the local Ollama endpoint."""
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.endpoint}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                },
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"]

    async def decompose_next_goal(self) -> list[Task]:
        """Break the next high-level goal into scoped tasks for Junior."""
        if not self.goals:
            logger.info("No goals queued. Waiting...")
            return []

        goal = self.goals.pop(0)
        logger.info(f"Decomposing goal: {goal}")

        system = (
            "You are a senior software architect. "
            "Break the given goal into small, scoped coding tasks for a junior developer. "
            "Each task should touch at most 3 files and have clear acceptance criteria. "
            "Respond with a JSON array of tasks."
        )
        # TODO: parse LLM response into Task objects
        raw = await self._chat(goal, system=system)
        logger.debug(f"Lead decomposition raw: {raw[:200]}...")

        # Placeholder: return single stub task
        stub = Task(
            id=str(uuid.uuid4())[:8],
            title=f"Implement: {goal[:60]}",
            description=goal,
            files_in_scope=[],
            acceptance_criteria=["All tests pass", "Linter clean"],
            branch=f"feature/task-{str(uuid.uuid4())[:6]}",
        )
        return [stub]

    async def review_patch(self, task: Task, patch: PatchReport) -> ReviewResult:
        """Review a patch report and decide approve/reject."""
        if not patch.test_results.passed or not patch.lint_passed:
            return ReviewResult(
                task_id=task.id,
                approved=False,
                feedback="Tests or linter failed. Fix before resubmitting.",
                requested_changes=[patch.test_results.summary],
            )

        system = (
            "You are a senior code reviewer. "
            "Evaluate the diff and decide if it meets the acceptance criteria. "
            "Reply with JSON: {approved: bool, feedback: str, requested_changes: list[str]}"
        )
        prompt = (
            f"Task: {task.title}\n"
            f"Criteria: {task.acceptance_criteria}\n"
            f"Diff:\n{patch.diff[:3000]}\n"
            f"Risks: {patch.risks}"
        )
        raw = await self._chat(prompt, system=system)
        logger.debug(f"Lead review raw: {raw[:200]}...")

        # TODO: parse LLM response properly
        return ReviewResult(
            task_id=task.id,
            approved=True,
            feedback="Looks good. Ready for human merge.",
        )
