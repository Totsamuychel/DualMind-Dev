"""Safe git operations — no auto-push to main."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("dualmind.git")


def _run(cmd: list[str], cwd: str) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git error: {result.stderr.strip()}")
    return result.stdout.strip()


def create_branch(repo_path: str, branch_name: str) -> str:
    return _run(["git", "checkout", "-b", branch_name], cwd=repo_path)


def stage_changes(repo_path: str, files: list[str]) -> str:
    return _run(["git", "add"] + files, cwd=repo_path)


def get_diff(repo_path: str, branch: str) -> str:
    """Get diff of current branch vs main. Read-only."""
    try:
        return _run(["git", "diff", "main", branch], cwd=repo_path)
    except Exception:
        return _run(["git", "diff"], cwd=repo_path)


def get_status(repo_path: str) -> str:
    return _run(["git", "status", "--short"], cwd=repo_path)


# ⛔ No git push, git merge, or git commit to main.
# All merges happen manually by the human after review.
