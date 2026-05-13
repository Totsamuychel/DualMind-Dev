"""Junior Agent — scoped executor on the secondary GPU machine."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import httpx

from core.protocol import Task, PatchReport, TestResults
from core.ssh_bridge import SSHBridge
from tools.companion_client import CompanionClient
import tools.git_tools as git_tools
import tools.test_runner as test_runner

logger = logging.getLogger("dualmind.junior")

MAX_TOOL_ITERATIONS = 20  # hard cap on the tool loop to prevent runaway tasks

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

    def __init__(self, config: dict):
        self.endpoint = config["model_endpoint"]
        self.model = config["model_name"]
        self.sandbox_dir = config["sandbox_dir"]
        self.companion = CompanionClient(
            config.get("companion_url", "http://127.0.0.1:8765")
        )
        self.ssh = SSHBridge(
            host=config["host"],
            user=config["ssh_user"],
            key_path=config["ssh_key"],
        )

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

    # ── Task execution ────────────────────────────────────────────────────────

    def _build_messages(self, task: Task) -> list[dict]:
        system = (
            "You are a junior software developer executing a scoped coding task. "
            "Use read_file to understand existing code before editing. "
            "Use write_file to apply your changes (only files in files_in_scope). "
            "Use execute for introspection (grep, ls, python -c, git status). "
            "When finished, reply with plain text summarising what you changed and why."
        )
        user = (
            f"Task: {task.title}\n"
            f"Description: {task.description}\n\n"
            f"Files in scope: {task.files_in_scope}\n"
            f"Constraints: {task.constraints}\n"
            f"Acceptance criteria: {task.acceptance_criteria}\n\n"
            "Read the relevant files, implement the changes, then summarise."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    async def execute_task(self, task: Task) -> PatchReport:
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
        messages = self._build_messages(task)
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

        # ── 3. Stage + commit ────────────────────────────────────────────────
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
        else:
            logger.warning("Task [%s]: no files written — nothing to commit", task.id)

        # ── 4. Quality checks ────────────────────────────────────────────────
        checks = await test_runner.run_checks(self.companion, self.sandbox_dir)

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
