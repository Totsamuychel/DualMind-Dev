"""Junior Agent — scoped executor on the secondary GPU machine."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from core.protocol import Task, PatchReport, TestResults
from core.ssh_bridge import SSHBridge
from tools.companion_client import CompanionClient
from tools.rag_store import RAGStore, COLLECTION_CODEBASE, COLLECTION_ERRORS
from tools.test_runner import CheckResults
from tools.indexer import reindex_file_content
import tools.git_tools as git_tools
import tools.test_runner as test_runner

logger = logging.getLogger("dualmind.junior")

MAX_TOOL_ITERATIONS = 20  # hard cap on the tool loop to prevent runaway tasks

# ── RAG retrieval settings ────────────────────────────────────────────────────
RAG_TOP_K = 5
RAG_SCORE_THRESHOLD = 0.30   # ignore hits below this cosine similarity
RAG_CONTEXT_CHARS = 3_000    # total character budget for all RAG snippets

ERROR_TOP_K = 3
ERROR_SCORE_THRESHOLD = 0.35  # slightly higher — errors must be closely related
ERROR_SUMMARY_CHARS = 150     # max chars per error line in the prompt

# Shell commands the LLM must never invoke via the execute tool.
_BLOCKED_PREFIXES = (
    "git push", "git merge", "git rebase", "git reset --hard", "git clean",
    "sudo", "rm -rf",
)

# ── Ollama tool definitions ───────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full content of a file in the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the sandbox root.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write (or overwrite) a file in the sandbox. "
                "Only files listed in files_in_scope may be written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the sandbox root.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete new content of the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute",
            "description": (
                "Run a shell command in the sandbox for introspection "
                "(ls, grep, python -c, git status/diff/log). "
                "No git push / merge / reset --hard."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run.",
                    },
                },
                "required": ["command"],
            },
        },
    },
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_blocked(command: str) -> bool:
    low = command.strip().lower()
    return any(low.startswith(p) for p in _BLOCKED_PREFIXES)


def _parse_args(raw) -> dict:
    """Ollama may return tool arguments as a dict or a JSON string; normalise."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


# ── Agent ─────────────────────────────────────────────────────────────────────

class JuniorAgent:
    """Runs on the secondary machine (RTX 3060).

    All file I/O and shell commands go through the companion server
    (SlopLobster-companion.py) running on the Junior machine.
    """

    def __init__(self, config: dict, rag: Optional[RAGStore] = None):
        self.endpoint = config["model_endpoint"]
        self.model = config["model_name"]
        self.sandbox_dir = config["sandbox_dir"]
        self.companion_url = config.get("companion_url", "http://127.0.0.1:8765")
        
        self.ssh = SSHBridge(
            host=config["host"],
            user=config["ssh_user"],
            key_path=config["ssh_key"],
        )
        
        if config.get("use_ssh", False):
            self.ssh.connect()
            self._setup_tunnels(config)

        self.companion = CompanionClient(self.companion_url)
        self.rag = rag

    def _setup_tunnels(self, config: dict):
        """Forward remote ports to localhost to reach them through SSH."""
        # Tunnel companion server if not already on localhost
        comp_url = urlparse(self.companion_url)
        if comp_url.hostname not in ("127.0.0.1", "localhost"):
            local_port = 8766  # use a different local port for the tunnel
            self.ssh.open_tunnel(comp_url.port or 8765, local_port)
            self.companion_url = f"{comp_url.scheme}://127.0.0.1:{local_port}"
            logger.info(f"SSH Tunneled companion: {self.companion_url}")

        # Tunnel model endpoint if not already on localhost
        model_url = urlparse(self.endpoint)
        if model_url.hostname not in ("127.0.0.1", "localhost"):
            local_port = 11435  # example: Ollama's 11434 + 1
            self.ssh.open_tunnel(model_url.port or 11434, local_port)
            self.endpoint = f"{model_url.scheme}://127.0.0.1:{local_port}"
            logger.info(f"SSH Tunneled model: {self.endpoint}")

    # ── LLM ──────────────────────────────────────────────────────────────────

    async def _chat_with_tools(self, messages: list[dict]) -> dict:
        """One round-trip to Ollama with tools enabled. Returns the message dict."""
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{self.endpoint}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": TOOLS,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            return resp.json()["message"]

    # ── Remote file I/O ───────────────────────────────────────────────────────

    def _abs(self, relative: str) -> str:
        return str(Path(self.sandbox_dir) / relative)

    async def _read_remote(self, abs_path: str) -> str:
        result = await self.companion.execute(f"cat '{abs_path}'")
        if not result.ok:
            raise FileNotFoundError(f"{abs_path}: {result.stderr.strip()}")
        return result.stdout

    async def _write_remote(self, abs_path: str, content: str) -> None:
        """Write arbitrary text to a file on the Junior machine.

        Uses echo base64 | base64 -d to avoid shell quoting issues with
        arbitrary file content (newlines, quotes, backslashes, etc.).
        """
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        parent = str(Path(abs_path).parent)
        await self.companion.execute(f"mkdir -p '{parent}'")
        result = await self.companion.execute(
            f"echo '{b64}' | base64 -d > '{abs_path}'"
        )
        if not result.ok:
            raise RuntimeError(f"write_file {abs_path}: {result.stderr.strip()}")

    # ── Tool dispatcher ───────────────────────────────────────────────────────

    async def _dispatch(self, name: str, args: dict, task: Task) -> str:
        try:
            if name == "read_file":
                path = args.get("path", "")
                if not path:
                    return "Error: path is required"
                content = await self._read_remote(self._abs(path))
                # Cap to avoid blowing up the LLM context window
                if len(content) > 8000:
                    content = content[:8000] + "\n... [truncated]"
                return content

            if name == "write_file":
                path = args.get("path", "")
                content = args.get("content", "")
                if not path:
                    return "Error: path is required"
                if task.files_in_scope and path not in task.files_in_scope:
                    allowed = ", ".join(task.files_in_scope)
                    return (
                        f"Error: {path!r} is not in files_in_scope. "
                        f"Allowed: {allowed}"
                    )
                await self._write_remote(self._abs(path), content)
                logger.info("Junior wrote: %s", path)
                return f"Written: {path}"

            if name == "execute":
                command = args.get("command", "")
                if not command:
                    return "Error: command is required"
                if _is_blocked(command):
                    return f"Error: command not allowed: {command!r}"
                result = await self.companion.execute(command, cwd=self.sandbox_dir)
                output = result.output
                if len(output) > 2000:
                    output = output[:2000] + "\n... [truncated]"
                return output or "(no output)"

            return f"Unknown tool: {name!r}"

        except Exception as exc:
            logger.warning("Tool %r raised: %s", name, exc)
            return f"Error: {exc}"

    # ── RAG context ───────────────────────────────────────────────────────────

    async def _rag_context(self, task: Task) -> str:
        """Search the codebase collection for code relevant to this task.

        Returns a formatted string ready to be injected into the user prompt,
        or an empty string when RAG is unavailable or finds nothing useful.
        """
        if self.rag is None:
            return ""

        query = f"{task.title}\n{task.description[:200]}"
        try:
            hits = await self.rag.search(
                self.companion,
                query,
                COLLECTION_CODEBASE,
                top_k=RAG_TOP_K,
                score_threshold=RAG_SCORE_THRESHOLD,
            )
        except Exception as exc:
            logger.debug("RAG context fetch failed: %s", exc)
            return ""

        if not hits:
            return ""

        snippets: list[str] = []
        total_chars = 0

        for hit in hits:
            p = hit.payload
            file_rel = p.get("file", "?")
            start, end = p.get("start_line", "?"), p.get("end_line", "?")
            symbol = p.get("symbol", "")
            sym_part = f", {symbol}" if symbol and symbol != "<module>" else ""

            header = f"# {file_rel} (lines {start}–{end}{sym_part})"
            code = hit.text()

            # Truncate this snippet so total stays within budget.
            budget = RAG_CONTEXT_CHARS - total_chars - len(header) - 4
            if budget < 80:
                break
            if len(code) > budget:
                code = code[:budget] + "\n... [truncated]"

            chunk = f"{header}\n{code}"
            snippets.append(chunk)
            total_chars += len(chunk) + 2  # +2 for the blank line separator

        if not snippets:
            return ""

        logger.debug("RAG: injecting %d snippet(s) into Junior prompt", len(snippets))
        return (
            "Relevant existing code (codebase search — use as reference, "
            "do not copy blindly):\n\n" + "\n\n".join(snippets)
        )

    async def _error_context(self, task: Task) -> str:
        """Search error_patterns for failures on tasks similar to this one.

        Returns a compact "Known pitfalls" block for the prompt, or "" when
        RAG is unavailable or no matching errors are found.
        """
        if self.rag is None:
            return ""

        query = f"{task.title}\n{task.description[:200]}"
        try:
            hits = await self.rag.search(
                self.companion,
                query,
                COLLECTION_ERRORS,
                top_k=ERROR_TOP_K,
                score_threshold=ERROR_SCORE_THRESHOLD,
            )
        except Exception as exc:
            logger.debug("Error context fetch failed: %s", exc)
            return ""

        if not hits:
            return ""

        lines: list[str] = []
        for hit in hits:
            p = hit.payload
            title = p.get("title", "?")
            error_type = p.get("error_type", "?")
            summary = p.get("error_summary", "")[:ERROR_SUMMARY_CHARS].replace("\n", " ")
            files = p.get("files_changed", [])
            files_str = ", ".join(files[:2]) if files else "—"
            lines.append(f"- [{error_type}] \"{title}\" ({files_str}): {summary}")

        logger.debug("Error context: %d known pitfall(s) found", len(lines))
        return "Known pitfalls for similar tasks:\n" + "\n".join(lines)

    async def _record_error(
        self,
        task: Task,
        checks: CheckResults,
        files_changed: list[str],
    ) -> None:
        """Persist a failed quality check into the error_patterns RAG collection.

        Called after run_checks() when tests or lint did not pass so future
        Junior runs on similar tasks can see what went wrong.
        """
        if self.rag is None:
            return

        error_parts: list[str] = []
        if not checks.tests_passed:
            error_parts.append("tests")
        if not checks.lint_passed:
            error_parts.append("lint")
        error_type = "+".join(error_parts) if error_parts else "unknown"

        # Prefer test output; fall back to lint output.
        error_detail = (
            checks.summary if not checks.tests_passed else checks.lint_output or checks.summary
        )

        # Embed text: task context + error so similarity search finds related failures.
        embed_text = (
            f"{task.title}: {task.description[:200]}\n"
            f"Error ({error_type}): {error_detail[:300]}"
        )

        payload = {
            "task_id": task.id,
            "title": task.title,
            "error_type": error_type,
            "error_summary": checks.summary[:500],
            "lint_output": (checks.lint_output or "")[:300],
            "files_changed": files_changed,
        }

        try:
            await self.rag.upsert(
                self.companion,
                [embed_text],
                [payload],
                COLLECTION_ERRORS,
            )
            logger.info(
                "Error patterns: recorded [%s] %s → %s", task.id, error_type, task.title
            )
        except Exception as exc:
            logger.warning("Error patterns: failed to record: %s", exc)

    async def _reindex_written_files(self, files: list[str]) -> None:
        """Re-index files Junior just committed into the Qdrant codebase collection.

        Reads each file back from the remote sandbox, re-chunks it, and
        replaces the stale Qdrant entries so subsequent tasks get up-to-date
        code snippets in their RAG context.  Errors are logged and skipped so
        a Qdrant outage never blocks task execution.
        """
        if self.rag is None or not files:
            return

        total_chunks = 0
        for rel_path in files:
            try:
                content = await self._read_remote(self._abs(rel_path))
                n = await reindex_file_content(
                    self.rag, self.companion, content, rel_path
                )
                total_chunks += n
                if n:
                    logger.debug("R6: re-indexed %s → %d chunk(s)", rel_path, n)
            except Exception as exc:
                logger.warning("R6: failed to re-index %s: %s", rel_path, exc)

        if total_chunks:
            logger.info(
                "R6: refreshed codebase index — %d chunk(s) across %d file(s)",
                total_chunks,
                len(files),
            )

    # ── Task execution ────────────────────────────────────────────────────────

    def _build_messages(
        self,
        task: Task,
        rag_context: str = "",
        error_context: str = "",
        feedback: str = "",
    ) -> list[dict]:
        system = (
            "You are a junior software developer executing a scoped coding task. "
            "Use read_file to understand existing code before editing. "
            "Use write_file to apply your changes (only files in files_in_scope). "
            "Use execute for introspection (grep, ls, python -c, git status). "
            "When finished, reply with plain text summarising what you changed and why."
        )
        user_parts: list[str] = []

        # Lead's rejection feedback comes first — LLM must fix this before anything else.
        if feedback:
            user_parts += [
                "PREVIOUS ATTEMPT REJECTED",
                "The Lead agent reviewed your last patch and gave this feedback:",
                feedback,
                "Address every point above before submitting again.",
                "",
            ]

        user_parts += [
            f"Task: {task.title}",
            f"Description: {task.description}",
            "",
            f"Files in scope: {task.files_in_scope}",
            f"Constraints: {task.constraints}",
            f"Acceptance criteria: {task.acceptance_criteria}",
        ]
        # Error pitfalls before reference code.
        if error_context:
            user_parts += ["", error_context]
        if rag_context:
            user_parts += ["", rag_context]
        user_parts += ["", "Read the relevant files, implement the changes, then summarise."]

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(user_parts)},
        ]

    async def execute_task(self, task: Task, *, feedback: str = "") -> PatchReport:
        logger.info("Junior executing task [%s]: %s", task.id, task.title)

        # ── 1. Feature branch ────────────────────────────────────────────────
        try:
            await git_tools.create_branch(
                self.companion, self.sandbox_dir, task.branch
            )
        except RuntimeError:
            # Branch already exists (retry). Just check it out.
            await self.companion.execute(
                f"git checkout '{task.branch}'", cwd=self.sandbox_dir
            )

        # ── 2. Tool loop ─────────────────────────────────────────────────────
        rag_context, error_context = await asyncio.gather(
            self._rag_context(task),
            self._error_context(task),
        )
        messages = self._build_messages(task, rag_context, error_context, feedback)
        files_written: list[str] = []
        final_summary = ""

        for iteration in range(MAX_TOOL_ITERATIONS):
            message = await self._chat_with_tools(messages)
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                # LLM produced a plain text reply — it's done.
                final_summary = message.get("content", "")
                logger.info(
                    "Junior finished in %d iteration(s): %s",
                    iteration + 1,
                    final_summary[:120],
                )
                break

            # Append the assistant turn with its tool calls.
            messages.append({
                "role": "assistant",
                "content": message.get("content", ""),
                "tool_calls": tool_calls,
            })

            # Dispatch every call in this turn and collect results.
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                args = _parse_args(fn.get("arguments", {}))

                result_text = await self._dispatch(name, args, task)

                if name == "write_file" and "path" in args:
                    files_written.append(args["path"])

                messages.append({"role": "tool", "content": result_text})

            # Publish progress so orchestrator and UI can follow along.
            await self.companion.dm_update_agents_status(
                lead="waiting",
                junior="executing",
                task_id=task.id,
                iteration=iteration + 1,
                files=list(dict.fromkeys(files_written)),
            )

        else:
            # for-else fires when the loop exhausted all iterations without break.
            final_summary = f"Hit iteration limit ({MAX_TOOL_ITERATIONS})."
            logger.warning("Task [%s] hit MAX_TOOL_ITERATIONS", task.id)

        # ── 3. Stage + commit + re-index ─────────────────────────────────────
        unique_files = list(dict.fromkeys(files_written))  # dedup, preserve order
        if unique_files:
            await git_tools.stage_changes(
                self.companion, self.sandbox_dir, unique_files
            )
            await git_tools.commit_changes(
                self.companion,
                self.sandbox_dir,
                f"[{task.id}] {task.title[:60]}",
            )
            await self._reindex_written_files(unique_files)
        else:
            logger.warning("Task [%s]: no files written — nothing to commit", task.id)

        # ── 4. Quality checks ────────────────────────────────────────────────
        checks = await test_runner.run_checks(self.companion, self.sandbox_dir)

        if not checks.tests_passed or not checks.lint_passed:
            await self._record_error(task, checks, unique_files)

        # ── 5. Diff for Lead review ──────────────────────────────────────────
        diff = await git_tools.get_diff(
            self.companion, self.sandbox_dir, task.branch
        )

        await self.companion.dm_update_agents_status(
            lead="reviewing", junior="idle", task_id=task.id
        )

        return PatchReport(
            task_id=task.id,
            branch=task.branch,
            diff=diff,
            files_changed=unique_files,
            test_results=TestResults(
                passed=checks.tests_passed,
                summary=checks.summary,
            ),
            lint_passed=checks.lint_passed,
            typecheck_passed=checks.typecheck_passed,
            risks=[],
            notes=final_summary[:500],
        )

