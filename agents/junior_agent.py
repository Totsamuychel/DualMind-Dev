"""Junior Agent — scoped executor on the secondary GPU machine."""

from __future__ import annotations

import logging

import httpx

from core.protocol import Task, PatchReport, ProgressReport, TestResults
from core.ssh_bridge import SSHBridge
from tools.test_runner import run_checks
import tools.git_tools as git_tools  # all functions are now async + take CompanionClient

logger = logging.getLogger("dualmind.junior")


class JuniorAgent:
    """Runs on the secondary machine (e.g. RTX 3060) via SSH.
    Responsible for: executing scoped tasks, running tests, returning patch reports.
    """

    def __init__(self, config: dict):
        self.endpoint = config["model_endpoint"]
        self.model = config["model_name"]
        self.sandbox_dir = config["sandbox_dir"]
        self.ssh = SSHBridge(
            host=config["host"],
            user=config["ssh_user"],
            key_path=config["ssh_key"],
        )

    async def _chat(self, prompt: str, system: str = "") -> str:
        """Send a prompt to the Junior's local Ollama endpoint via SSH tunnel."""
        async with httpx.AsyncClient(timeout=180) as client:
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

    async def execute_task(self, task: Task) -> PatchReport:
        """Execute a scoped task and return a patch report."""
        logger.info(f"Junior executing task [{task.id}]: {task.title}")

        system = (
            "You are a junior software developer. "
            "Implement exactly what is asked. Touch only the files in scope. "
            "Follow all constraints. Write clean, tested code."
        )
        prompt = (
            f"Task: {task.title}\n"
            f"Description: {task.description}\n"
            f"Files you may edit: {task.files_in_scope}\n"
            f"Constraints: {task.constraints}\n"
            f"Acceptance criteria: {task.acceptance_criteria}\n"
            "Implement the changes and explain what you did."
        )

        # TODO(step-5): wire LLM output to actual file edits via tool calls
        raw = await self._chat(prompt, system=system)
        logger.debug(f"Junior response: {raw[:200]}...")

        # TODO(step-3): replace with async companion-based run_checks
        checks = run_checks(self.sandbox_dir)
        # TODO(step-5): replace with await git_tools.get_diff(self.companion, ...)
        diff = ""

        return PatchReport(
            task_id=task.id,
            branch=task.branch,
            diff=diff,
            files_changed=task.files_in_scope,
            test_results=TestResults(
                passed=checks["tests_passed"],
                summary=checks["summary"],
            ),
            lint_passed=checks["lint_passed"],
            typecheck_passed=checks["typecheck_passed"],
            risks=[],
            notes=raw[:500],
        )
