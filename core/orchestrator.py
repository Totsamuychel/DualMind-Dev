"""Orchestrator — coordinates Lead and Junior agents."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agents.lead_agent import LeadAgent
from agents.junior_agent import JuniorAgent
from core.protocol import Task, PatchReport, ReviewResult, TaskStatus

logger = logging.getLogger("dualmind.orchestrator")


class Orchestrator:
    def __init__(self, config: dict):
        self.config = config
        self.lead = LeadAgent(config["lead_agent"])
        self.junior = JuniorAgent(config["junior_agent"])
        self.task_queue: list[Task] = []

    async def run(self):
        logger.info("Orchestrator started. Waiting for tasks...")
        # Main loop: lead decomposes → junior executes → lead reviews
        while True:
            # 1. Lead agent produces tasks
            new_tasks = await self.lead.decompose_next_goal()
            self.task_queue.extend(new_tasks)

            # 2. Junior picks up tasks one by one
            for task in self.task_queue:
                if task.status != TaskStatus.TODO:
                    continue

                logger.info(f"Assigning task [{task.id}] '{task.title}' to Junior")
                task.status = TaskStatus.IN_PROGRESS

                patch_report: PatchReport = await self.junior.execute_task(task)

                # 3. Lead reviews the patch
                review: ReviewResult = await self.lead.review_patch(task, patch_report)

                if review.approved:
                    logger.info(f"✅ Task [{task.id}] approved. Ready for human commit.")
                    task.status = TaskStatus.DONE
                    # NOTE: Human must manually git merge/commit — no auto-push.
                    await self._notify_human(task, patch_report)
                else:
                    logger.warning(f"❌ Task [{task.id}] rejected: {review.feedback}")
                    task.status = TaskStatus.REJECTED

            await asyncio.sleep(5)

    async def _notify_human(self, task: Task, patch: PatchReport):
        """Print approval prompt for human review."""
        print("\n" + "="*60)
        print(f"🔔 HUMAN REVIEW REQUIRED")
        print(f"Task : {task.title}")
        print(f"Branch: {patch.branch}")
        print(f"Files : {', '.join(patch.files_changed)}")
        print(f"Tests : {'PASS' if patch.test_results.passed else 'FAIL'}")
        print(f"Risks : {patch.risks or 'None'}")
        print("Run: git diff " + patch.branch)
        print("Then: git merge " + patch.branch + " (if satisfied)")
        print("="*60 + "\n")
