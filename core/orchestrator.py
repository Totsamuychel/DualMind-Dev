"""Orchestrator — coordinates Lead and Junior agents."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from agents.lead_agent import LeadAgent
from agents.junior_agent import JuniorAgent
from core.protocol import Task, PatchReport, ReviewResult, TaskStatus
from tools.companion_client import CompanionClient
from tools.rag_store import RAGStore

logger = logging.getLogger("dualmind.orchestrator")


class Orchestrator:
    def __init__(self, config: dict, rag: Optional[RAGStore] = None):
        self.config = config
        self.lead = LeadAgent(config["lead_agent"], rag=rag)
        self.junior = JuniorAgent(config["junior_agent"], rag=rag)
        self.task_queue: list[Task] = []
        self.companion = CompanionClient(
            config["lead_agent"].get("companion_url", "http://127.0.0.1:8765")
        )
        tq = config.get("task_queue", {})
        self.max_retries: int = int(tq.get("max_retries", 2))
        self.approval_timeout: int = int(tq.get("approval_timeout_minutes", 60))

    # ── Persistence helpers ───────────────────────────────────────────────────

    async def _save_task(self, task: Task) -> None:
        """Persist task state to the companion file queue (fire-and-forget on error)."""
        try:
            await self.companion.dm_save_task(task.model_dump(mode="json"))
        except Exception as exc:
            logger.warning("Could not persist task %s: %s", task.id, exc)

    async def _recover_tasks(self) -> None:
        """Load todo and in_progress tasks from disk at startup (crash recovery)."""
        try:
            data = await self.companion.dm_get_tasks()
        except Exception as exc:
            logger.warning("Task recovery skipped (companion unreachable): %s", exc)
            return

        for status_key in ("todo", "in_progress"):
            for task_dict in data.get(status_key, []):
                try:
                    task = Task.model_validate(task_dict)
                    self.task_queue.append(task)
                    logger.info(
                        "Recovered %s task: [%s] %s (attempt %d)",
                        status_key, task.id, task.title, task.attempt,
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not parse recovered task %s: %s",
                        task_dict.get("id", "?"), exc,
                    )

    # ── Human approval wait ───────────────────────────────────────────────────

    async def _wait_for_human_approval(self, task_id: str) -> tuple[str, str]:
        """Poll for .approved / .rejected sentinel files.

        Returns (outcome, reason) where outcome is one of:
          'approved'  — human approved the patch
          'rejected'  — human rejected with optional reason
          'timeout'   — approval_timeout_minutes elapsed with no response
        """
        timeout_secs = self.approval_timeout * 60
        start = time.monotonic()
        poll_secs = 5.0

        logger.info(
            "Waiting for human approval of task %s (timeout %d min)",
            task_id, self.approval_timeout,
        )
        while True:
            try:
                result = await self.companion.dm_check_approval(task_id)
                status = result.get("status", "pending")
                if status in ("approved", "rejected"):
                    return status, result.get("reason", "")
            except Exception as exc:
                logger.debug("Approval poll error for %s: %s", task_id, exc)

            elapsed = time.monotonic() - start
            if elapsed >= timeout_secs:
                logger.warning(
                    "Approval timeout for task %s after %d min",
                    task_id, self.approval_timeout,
                )
                return "timeout", f"No human response within {self.approval_timeout} minutes"

            await asyncio.sleep(min(poll_secs, timeout_secs - elapsed))

    # ── Notification helpers ──────────────────────────────────────────────────

    def _try_push_event(self, event: str, task: Task, reason: str = "") -> None:
        """Push a NotifyEvent; silently skip if tg_bot is not available."""
        try:
            from tg_bot.notify import push_event, NotifyEvent
            push_event(NotifyEvent(
                event=event,
                task_id=task.id,
                title=task.title,
                reason=reason,
            ))
        except Exception:
            pass

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info("Orchestrator starting — recovering persisted state...")
        await self._recover_tasks()
        logger.info(
            "Orchestrator ready. %d task(s) in queue.", len(self.task_queue)
        )

        while True:
            # Lead decomposes the next queued goal into tasks.
            new_tasks = await self.lead.decompose_next_goal()
            for task in new_tasks:
                await self._save_task(task)
            self.task_queue.extend(new_tasks)

            for task in self.task_queue:
                if task.status not in (TaskStatus.TODO, TaskStatus.IN_PROGRESS):
                    continue

                await self._execute_task_with_retries(task)

            await asyncio.sleep(5)

    async def _execute_task_with_retries(self, task: Task) -> None:
        """Run the full execute → review → approval cycle for one task.

        Retries up to self.max_retries times when Lead rejects, injecting
        the rejection feedback into each subsequent Junior prompt.
        """
        logger.info("Assigning task [%s] '%s' to Junior", task.id, task.title)
        task.status = TaskStatus.IN_PROGRESS
        await self._save_task(task)

        self._try_push_event("task_started", task)
        try:
            from tg_bot.monitor import record_task_started
            record_task_started(task.id, task.title)
        except Exception:
            pass

        prev_feedback = ""

        while True:
            # ── Junior executes ──────────────────────────────────────────────
            patch_report: PatchReport = await self.junior.execute_task(
                task, feedback=prev_feedback
            )

            # ── Lead reviews ─────────────────────────────────────────────────
            review: ReviewResult = await self.lead.review_patch(task, patch_report)

            try:
                from tg_bot.monitor import record_task_finished
                record_task_finished()
            except Exception:
                pass

            if review.approved:
                # ── Notify human and wait for their decision ─────────────────
                logger.info("✅ Task [%s] passed Lead review — awaiting human.", task.id)
                task.status = TaskStatus.DONE
                await self._notify_human(task, patch_report)

                outcome, reason = await self._wait_for_human_approval(task.id)

                if outcome == "approved":
                    logger.info("✅ Task [%s] approved by human.", task.id)
                    await self.lead.record_task_outcome(task, patch_report, "approved")
                    self._try_push_event("task_approved", task)
                else:
                    # Human rejected or timed out — treat as rejection.
                    logger.warning(
                        "❌ Task [%s] %s by human: %s", task.id, outcome, reason
                    )
                    task.status = TaskStatus.REJECTED
                    await self.lead.record_task_outcome(task, patch_report, "rejected")
                    self._try_push_event("task_rejected", task, reason)

                await self._save_task(task)
                break

            else:
                # ── Lead rejected — maybe retry ──────────────────────────────
                task.attempt += 1
                prev_feedback = review.feedback or ""
                logger.warning(
                    "❌ Task [%s] rejected by Lead (attempt %d/%d): %s",
                    task.id, task.attempt, self.max_retries + 1, review.feedback,
                )

                if task.attempt > self.max_retries:
                    task.status = TaskStatus.REJECTED
                    await self.lead.record_task_outcome(task, patch_report, "rejected")
                    await self._save_task(task)
                    exhausted_reason = (
                        f"Exhausted {self.max_retries} retries. "
                        f"Last feedback: {review.feedback}"
                    )
                    self._try_push_event("task_rejected", task, exhausted_reason)
                    break

                # Save updated attempt count, reset monitor, loop back.
                await self._save_task(task)
                try:
                    from tg_bot.monitor import record_task_started
                    record_task_started(task.id, task.title)
                except Exception:
                    pass
                logger.info(
                    "Retrying task [%s] (attempt %d of %d)",
                    task.id, task.attempt + 1, self.max_retries + 1,
                )

    async def _notify_human(self, task: Task, patch: PatchReport) -> None:
        """Print console approval prompt and push Telegram review notification."""
        print("\n" + "=" * 60)
        print("🔔 HUMAN REVIEW REQUIRED")
        print(f"Task  : {task.title}")
        print(f"Branch: {patch.branch}")
        print(f"Files : {', '.join(patch.files_changed)}")
        print(f"Tests : {'PASS' if patch.test_results.passed else 'FAIL'}")
        print(f"Risks : {patch.risks or 'None'}")
        print("Run: git diff " + patch.branch)
        print("Then approve via Telegram or: POST /approve/" + task.id)
        print("=" * 60 + "\n")

        try:
            from tg_bot.notify import push_review_ready, ReviewNotification
            diff_lines = len(patch.diff.splitlines()) if patch.diff else 0
            push_review_ready(ReviewNotification(
                task_id=task.id,
                task_title=task.title,
                branch=patch.branch,
                diff_lines=diff_lines,
                tests_passed=patch.test_results.passed,
                lint_passed=getattr(patch, "lint_passed", True),
                files_changed=list(patch.files_changed),
            ))
        except Exception as exc:
            logger.debug("TG notify skipped: %s", exc)
