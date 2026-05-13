"""Safe async git operations via companion server — no auto-push to main."""

from __future__ import annotations

import logging

from tools.companion_client import CompanionClient, ExecResult

logger = logging.getLogger("dualmind.git")


def _shell_quote(s: str) -> str:
    """Single-quote a string for POSIX shell, handling embedded single quotes."""
    return "'" + s.replace("'", "'\\''") + "'"


async def _git(companion: CompanionClient, args: str, cwd: str) -> ExecResult:
    """Run one git subcommand. Raises RuntimeError on non-zero exit."""
    result = await companion.execute(f"git {args}", cwd=cwd)
    if not result.ok:
        verb = args.split()[0]
        detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
        raise RuntimeError(f"git {verb}: {detail}")
    return result


async def create_branch(
    companion: CompanionClient, repo_path: str, branch_name: str
) -> str:
    result = await _git(companion, f"checkout -b {_shell_quote(branch_name)}", cwd=repo_path)
    logger.info("Created branch %r", branch_name)
    return result.stdout.strip()


async def stage_changes(
    companion: CompanionClient, repo_path: str, files: list[str]
) -> str:
    quoted = " ".join(_shell_quote(f) for f in files)
    result = await _git(companion, f"add {quoted}", cwd=repo_path)
    return result.stdout.strip()


async def commit_changes(
    companion: CompanionClient, repo_path: str, message: str
) -> str:
    """Commit staged changes to the current feature branch.

    ⛔ Never call on main — Junior always commits to a feature branch.
    Human merges manually after Lead approves the patch report.
    """
    result = await _git(companion, f"commit -m {_shell_quote(message)}", cwd=repo_path)
    logger.info("Committed: %s", message[:80])
    return result.stdout.strip()


async def get_diff(
    companion: CompanionClient, repo_path: str, branch: str
) -> str:
    """Diff of feature branch vs main. Read-only."""
    try:
        result = await _git(
            companion, f"diff main {_shell_quote(branch)}", cwd=repo_path
        )
        return result.stdout
    except RuntimeError:
        # Branch may not exist yet (nothing committed) — return unstaged diff.
        result = await companion.execute("git diff", cwd=repo_path)
        return result.stdout


async def get_status(companion: CompanionClient, repo_path: str) -> str:
    result = await _git(companion, "status --short", cwd=repo_path)
    return result.stdout.strip()


async def current_branch(companion: CompanionClient, repo_path: str) -> str:
    result = await _git(companion, "rev-parse --abbrev-ref HEAD", cwd=repo_path)
    return result.stdout.strip()


async def get_log(
    companion: CompanionClient, repo_path: str, n: int = 10
) -> str:
    result = await _git(companion, f"log --oneline -{n}", cwd=repo_path)
    return result.stdout.strip()


# ⛔ No git push, git merge, or git commit to main.
# All merges happen manually by the human after Lead approves the patch report.
