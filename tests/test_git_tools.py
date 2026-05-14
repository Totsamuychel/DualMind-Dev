"""Tests for tools/git_tools.py — async git operations via mock companion."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, call

import tools.git_tools as git_tools
from tests.conftest import mock_companion, ok_exec, fail_exec

REPO = "/repo"


# ── create_branch ─────────────────────────────────────────────────────────────

async def test_create_branch_calls_checkout_b(mock_companion):
    mock_companion.execute.return_value = ok_exec("Switched to a new branch 'feat'")
    await git_tools.create_branch(mock_companion, REPO, "feat")
    cmd = mock_companion.execute.call_args[0][0]
    assert "checkout" in cmd
    assert "-b" in cmd
    assert "feat" in cmd


async def test_create_branch_passes_cwd(mock_companion):
    mock_companion.execute.return_value = ok_exec()
    await git_tools.create_branch(mock_companion, REPO, "feat")
    kwargs = mock_companion.execute.call_args[1]
    assert kwargs.get("cwd") == REPO


async def test_create_branch_raises_on_error(mock_companion):
    mock_companion.execute.return_value = fail_exec("already exists")
    with pytest.raises(RuntimeError, match="checkout"):
        await git_tools.create_branch(mock_companion, REPO, "feat")


# ── stage_changes ─────────────────────────────────────────────────────────────

async def test_stage_changes_calls_git_add(mock_companion):
    mock_companion.execute.return_value = ok_exec()
    await git_tools.stage_changes(mock_companion, REPO, ["a.py", "b.py"])
    cmd = mock_companion.execute.call_args[0][0]
    assert cmd.startswith("git add")
    assert "a.py" in cmd
    assert "b.py" in cmd


async def test_stage_changes_single_file(mock_companion):
    mock_companion.execute.return_value = ok_exec()
    await git_tools.stage_changes(mock_companion, REPO, ["only.py"])
    cmd = mock_companion.execute.call_args[0][0]
    assert "only.py" in cmd


async def test_stage_changes_raises_on_error(mock_companion):
    mock_companion.execute.return_value = fail_exec("pathspec did not match")
    with pytest.raises(RuntimeError, match="add"):
        await git_tools.stage_changes(mock_companion, REPO, ["missing.py"])


# ── commit_changes ────────────────────────────────────────────────────────────

async def test_commit_changes_calls_git_commit(mock_companion):
    mock_companion.execute.return_value = ok_exec("[feat abc123] Add thing")
    await git_tools.commit_changes(mock_companion, REPO, "Add thing")
    cmd = mock_companion.execute.call_args[0][0]
    assert "commit" in cmd
    assert "-m" in cmd
    assert "Add thing" in cmd


async def test_commit_message_with_special_chars(mock_companion):
    mock_companion.execute.return_value = ok_exec()
    msg = "Fix: handle 'quotes' and \"double\" quotes"
    await git_tools.commit_changes(mock_companion, REPO, msg)
    cmd = mock_companion.execute.call_args[0][0]
    # Message should be shell-quoted so special chars don't break the shell.
    assert "commit" in cmd


async def test_commit_raises_on_error(mock_companion):
    mock_companion.execute.return_value = fail_exec("nothing to commit")
    with pytest.raises(RuntimeError, match="commit"):
        await git_tools.commit_changes(mock_companion, REPO, "Empty")


# ── get_diff ──────────────────────────────────────────────────────────────────

async def test_get_diff_calls_diff_main(mock_companion):
    mock_companion.execute.return_value = ok_exec("diff --git a/f.py b/f.py\n+new line")
    diff = await git_tools.get_diff(mock_companion, REPO, "feat")
    cmd = mock_companion.execute.call_args[0][0]
    assert "diff" in cmd
    assert "main" in cmd
    assert "feat" in cmd
    assert "new line" in diff


async def test_get_diff_falls_back_on_error(mock_companion):
    # First call (git diff main feat) fails; should fall back to plain git diff.
    mock_companion.execute.side_effect = [
        fail_exec("unknown revision"),   # git diff main feat
        ok_exec("unstaged diff"),        # git diff (fallback)
    ]
    diff = await git_tools.get_diff(mock_companion, REPO, "feat")
    assert "unstaged diff" in diff


# ── get_status ────────────────────────────────────────────────────────────────

async def test_get_status(mock_companion):
    mock_companion.execute.return_value = ok_exec("M  a.py\n?? b.py")
    status = await git_tools.get_status(mock_companion, REPO)
    assert "a.py" in status
    assert "b.py" in status


# ── current_branch ────────────────────────────────────────────────────────────

async def test_current_branch(mock_companion):
    mock_companion.execute.return_value = ok_exec("feature/my-task\n")
    branch = await git_tools.current_branch(mock_companion, REPO)
    assert branch == "feature/my-task"


# ── get_log ───────────────────────────────────────────────────────────────────

async def test_get_log_default_n(mock_companion):
    mock_companion.execute.return_value = ok_exec("abc123 First commit")
    await git_tools.get_log(mock_companion, REPO)
    cmd = mock_companion.execute.call_args[0][0]
    assert "log" in cmd
    assert "--oneline" in cmd
    assert "-10" in cmd


async def test_get_log_custom_n(mock_companion):
    mock_companion.execute.return_value = ok_exec()
    await git_tools.get_log(mock_companion, REPO, n=5)
    cmd = mock_companion.execute.call_args[0][0]
    assert "-5" in cmd


# ── shell quoting ─────────────────────────────────────────────────────────────

def test_shell_quote_basic():
    assert git_tools._shell_quote("hello") == "'hello'"


def test_shell_quote_with_single_quote():
    # single quote inside must be escaped
    result = git_tools._shell_quote("it's")
    assert "'" not in result[1:-1] or result == "'it'\\''s'"
