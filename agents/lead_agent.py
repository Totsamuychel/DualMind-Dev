"""Lead Agent — planner, decomposer, reviewer."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Optional

import httpx

from core.protocol import Task, PatchReport, ReviewResult
from tools.companion_client import CompanionClient
from tools.rag_store import RAGStore, COLLECTION_TASK_HISTORY

logger = logging.getLogger("dualmind.lead")

_GOALS_FILE = Path("queue/goals.txt")

# ── JSON extraction ───────────────────────────────────────────────────────────

def _extract_json(text: str) -> str:
    """Strip markdown fences and return the outermost JSON array or object."""
    text = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    for start_ch, end_ch in [("[", "]"), ("{", "}")]:
        s = text.find(start_ch)
        e = text.rfind(end_ch)
        if s != -1 and e != -1 and e > s:
            return text[s : e + 1]
    return text


# ── Agent ─────────────────────────────────────────────────────────────────────

class LeadAgent:
    """Runs on the high-end GPU machine (RTX 3090).

    Responsible for: loading goals, web research, task decomposition,
    code review with real LLM parsing.
    """

    def __init__(self, config: dict, rag: Optional[RAGStore] = None):
        self.endpoint = config["model_endpoint"]
        self.model = config["model_name"]
        self.companion = CompanionClient(
            config.get("companion_url", "http://127.0.0.1:8765")
        )
        self.goals: list[str] = []
        self.rag = rag

    # ── LLM ──────────────────────────────────────────────────────────────────

    async def _chat(self, prompt: str, system: str = "") -> str:
        async with httpx.AsyncClient(timeout=600) as client:
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

    # ── Goal loading ──────────────────────────────────────────────────────────

    def load_goals_from_file(self) -> int:
        """Read goals from queue/goals.txt (written by companion /goal endpoint).

        Clears the file after reading so goals are not processed twice.
        Returns the number of new goals loaded.
        """
        if not _GOALS_FILE.exists():
            return 0
        try:
            text = _GOALS_FILE.read_text(encoding="utf-8")
            _GOALS_FILE.write_text("", encoding="utf-8")  # clear atomically
            new_goals = [l.strip() for l in text.splitlines() if l.strip()]
            self.goals.extend(new_goals)
            if new_goals:
                logger.info("Loaded %d goal(s) from %s", len(new_goals), _GOALS_FILE)
            return len(new_goals)
        except OSError as exc:
            logger.warning("Could not read goals file: %s", exc)
            return 0

    # ── Research ─────────────────────────────────────────────────────────────

    async def _research(self, goal: str) -> str:
        """Web-search the goal for context before decomposing.

        Returns a bullet list of snippets, or empty string on failure.
        """
        try:
            results = await self.companion.search(goal, num_results=3)
            if not results:
                return ""
            lines = [f"- {r.title}: {r.snippet}" for r in results if r.snippet]
            logger.info("Research found %d result(s) for goal", len(lines))
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("Research skipped (%s)", exc)
            return ""

    # ── Task history (RAG) ────────────────────────────────────────────────────

    async def _task_history_context(self, goal: str) -> str:
        """Search past completed tasks for ones similar to the current goal.

        Returns a compact bullet list injected into the decomposition prompt so
        the LLM avoids re-decomposing work that was already done, and can learn
        from how previous similar goals were broken down.

        Returns an empty string when RAG is unavailable or finds nothing above
        the similarity threshold.
        """
        if self.rag is None:
            return ""
        try:
            hits = await self.rag.search(
                self.companion,
                goal,
                COLLECTION_TASK_HISTORY,
                top_k=3,
                score_threshold=0.40,
            )
        except Exception as exc:
            logger.debug("Task history lookup failed: %s", exc)
            return ""

        if not hits:
            return ""

        lines: list[str] = []
        for hit in hits:
            p = hit.payload
            title = p.get("title", "?")
            outcome = p.get("outcome", "?")
            files = p.get("files_changed", [])
            files_str = ", ".join(files[:3]) if files else "—"
            if len(files) > 3:
                files_str += f" (+{len(files) - 3} more)"
            lines.append(f"- [{outcome}] \"{title}\" — {files_str}")

        logger.debug("Task history: %d similar past task(s) found", len(lines))
        return (
            "Similar past tasks (avoid duplicating completed work):\n"
            + "\n".join(lines)
        )

    async def record_task_outcome(
        self,
        task: Task,
        patch: PatchReport,
        outcome: str,
    ) -> None:
        """Persist a completed task into the task_history RAG collection.

        Called by the orchestrator after final approve or reject so future
        decompositions can see what has already been attempted.

        outcome: "approved" | "rejected"
        """
        if self.rag is None:
            return

        embed_text = f"{task.title}: {task.description}"
        payload = {
            "task_id": task.id,
            "title": task.title,
            "description": task.description[:400],
            "outcome": outcome,
            "branch": patch.branch,
            "files_changed": patch.files_changed,
        }
        try:
            await self.rag.upsert(
                self.companion,
                [embed_text],
                [payload],
                COLLECTION_TASK_HISTORY,
            )
            logger.info(
                "Task history: recorded [%s] %s → %s", task.id, outcome, task.title
            )
        except Exception as exc:
            logger.warning("Task history: failed to record outcome: %s", exc)

    # ── Task decomposition ────────────────────────────────────────────────────

    def _parse_tasks(self, raw: str, goal: str) -> list[Task]:
        """Parse LLM JSON into Task objects with a stub fallback."""
        try:
            data = json.loads(_extract_json(raw))
            if isinstance(data, dict):
                data = [data]
            tasks: list[Task] = []
            for item in data:
                if not isinstance(item, dict) or not item.get("title"):
                    continue
                tasks.append(
                    Task(
                        id=str(uuid.uuid4())[:8],
                        title=item["title"],
                        description=item.get("description", goal),
                        files_in_scope=item.get("files_in_scope", []),
                        constraints=item.get("constraints", []),
                        acceptance_criteria=item.get(
                            "acceptance_criteria",
                            ["All tests pass", "Linter clean"],
                        ),
                        branch=item.get(
                            "branch", f"feature/task-{str(uuid.uuid4())[:6]}"
                        ),
                    )
                )
            if tasks:
                logger.info("Decomposed into %d task(s)", len(tasks))
                return tasks
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        logger.warning("Could not parse task JSON — using stub task")
        return [
            Task(
                id=str(uuid.uuid4())[:8],
                title=f"Implement: {goal[:60]}",
                description=goal,
                files_in_scope=[],
                constraints=[],
                acceptance_criteria=["All tests pass", "Linter clean"],
                branch=f"feature/task-{str(uuid.uuid4())[:6]}",
            )
        ]

    async def decompose_next_goal(self) -> list[Task]:
        """Research + decompose the next goal into scoped tasks for Junior."""
        self.load_goals_from_file()

        if not self.goals:
            logger.info("No goals queued — waiting")
            return []

        goal = self.goals.pop(0)
        logger.info("Decomposing goal: %s", goal)

        research, history = await asyncio.gather(
            self._research(goal),
            self._task_history_context(goal),
        )

        system = (
            "You are a senior software architect. "
            "Break the given goal into 1–5 small, scoped coding tasks for a junior developer. "
            "Each task must touch at most 3 files and have clear acceptance criteria. "
            "Reply with ONLY a JSON array — no markdown, no explanation:\n"
            "[\n"
            "  {\n"
            '    "title": "short imperative title",\n'
            '    "description": "detailed description",\n'
            '    "files_in_scope": ["relative/path.py"],\n'
            '    "constraints": ["no new dependencies"],\n'
            '    "acceptance_criteria": ["all tests pass"],\n'
            '    "branch": "feature/short-name"\n'
            "  }\n"
            "]"
        )

        prompt_parts = [f"Goal: {goal}"]
        if history:
            prompt_parts.append(f"\n{history}")
        if research:
            prompt_parts.append(f"\nResearch context:\n{research}")
        prompt_parts.append(
            "\nDecompose this goal into tasks following the schema above."
        )
        prompt = "\n".join(prompt_parts)

        raw = await self._chat(prompt, system=system)
        logger.debug("Decomposition raw: %s", raw[:300])
        return self._parse_tasks(raw, goal)

    # ── Code review ───────────────────────────────────────────────────────────

    def _parse_review(self, raw: str, task_id: str) -> ReviewResult:
        """Parse LLM review JSON. Falls back to text heuristic on bad JSON."""
        try:
            data = json.loads(_extract_json(raw))
            return ReviewResult(
                task_id=task_id,
                approved=bool(data.get("approved", False)),
                feedback=str(data.get("feedback", raw[:400])),
                requested_changes=list(data.get("requested_changes", [])),
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        # Heuristic: look for approval / rejection keywords in plain text.
        low = raw.lower()
        approved = (
            ("approved" in low or "looks good" in low or "lgtm" in low)
            and "not approved" not in low
            and "rejected" not in low
            and "reject" not in low
        )
        logger.warning(
            "Could not parse review JSON — heuristic: approved=%s", approved
        )
        return ReviewResult(
            task_id=task_id,
            approved=approved,
            feedback=raw[:500],
            requested_changes=[],
        )

    async def review_patch(self, task: Task, patch: PatchReport) -> ReviewResult:
        """Review a patch report and return an approve/reject decision."""
        # Hard reject before calling the LLM — saves tokens and time.
        if not patch.test_results.passed or not patch.lint_passed:
            failed = []
            if not patch.test_results.passed:
                failed.append("tests")
            if not patch.lint_passed:
                failed.append("linter")
            return ReviewResult(
                task_id=task.id,
                approved=False,
                feedback=f"{', '.join(failed).capitalize()} failed. Fix before resubmitting.",
                requested_changes=[patch.test_results.summary],
            )

        system = (
            "You are a senior code reviewer. "
            "Evaluate the diff below against the acceptance criteria. "
            "Reply with ONLY a JSON object — no markdown, no explanation:\n"
            '{"approved": true, '
            '"feedback": "one concise paragraph", '
            '"requested_changes": ["specific change if any"]}'
        )
        diff_excerpt = patch.diff[:4000]
        if len(patch.diff) > 4000:
            diff_excerpt += "\n... [diff truncated]"

        prompt = (
            f"Task: {task.title}\n"
            f"Acceptance criteria: {task.acceptance_criteria}\n"
            f"Typecheck passed: {patch.typecheck_passed}\n"
            f"Risks: {patch.risks or 'none'}\n\n"
            f"Diff:\n{diff_excerpt}"
        )

        raw = await self._chat(prompt, system=system)
        logger.debug("Review raw: %s", raw[:300])
        return self._parse_review(raw, task.id)
