"""Tests for JuniorAgent remote file I/O — _read_remote and _write_remote."""

from __future__ import annotations

import base64

import pytest
from unittest.mock import AsyncMock, call

from agents.junior_agent import JuniorAgent
from tests.conftest import JUNIOR_CONFIG, mock_companion, ok_exec, fail_exec


@pytest.fixture
def junior(mock_companion):
    agent = JuniorAgent(JUNIOR_CONFIG, rag=None)
    agent.companion = mock_companion
    return agent


# ── _read_remote ──────────────────────────────────────────────────────────────

async def test_read_remote_returns_stdout(junior, mock_companion):
    mock_companion.execute.return_value = ok_exec(stdout="print('hello')")
    content = await junior._read_remote("/sandbox/foo.py")
    assert content == "print('hello')"


async def test_read_remote_uses_cat(junior, mock_companion):
    mock_companion.execute.return_value = ok_exec(stdout="x=1")
    await junior._read_remote("/sandbox/src/main.py")
    cmd = mock_companion.execute.call_args[0][0]
    assert "cat" in cmd
    assert "/sandbox/src/main.py" in cmd


async def test_read_remote_raises_file_not_found(junior, mock_companion):
    mock_companion.execute.return_value = fail_exec(stderr="No such file or directory")
    with pytest.raises(FileNotFoundError, match="/sandbox/missing.py"):
        await junior._read_remote("/sandbox/missing.py")


async def test_read_remote_propagates_stderr_in_error(junior, mock_companion):
    mock_companion.execute.return_value = fail_exec(stderr="permission denied")
    with pytest.raises(FileNotFoundError, match="permission denied"):
        await junior._read_remote("/sandbox/secret.py")


# ── _write_remote ─────────────────────────────────────────────────────────────

async def test_write_remote_creates_parent_dir(junior, mock_companion):
    mock_companion.execute.return_value = ok_exec()
    await junior._write_remote("/sandbox/src/new.py", "content")
    calls = [c[0][0] for c in mock_companion.execute.call_args_list]
    mkdir_calls = [c for c in calls if "mkdir" in c]
    assert len(mkdir_calls) >= 1
    assert "/sandbox/src" in mkdir_calls[0].replace("\\", "/")


async def test_write_remote_uses_base64(junior, mock_companion):
    mock_companion.execute.return_value = ok_exec()
    content = "def hello():\n    return 'world'\n"
    await junior._write_remote("/sandbox/greet.py", content)
    calls = [c[0][0] for c in mock_companion.execute.call_args_list]
    write_calls = [c for c in calls if "base64" in c]
    assert len(write_calls) >= 1
    # The base64-encoded content must appear in the command.
    b64 = base64.b64encode(content.encode()).decode()
    assert b64 in write_calls[0]


async def test_write_remote_round_trip_content(junior, mock_companion):
    """Whatever content is passed, the base64 encoding must be correct."""
    content = 'print("hello world")\n# comment with \'quotes\'\n'
    captured_b64 = []

    async def capture_execute(cmd, **kwargs):
        if "base64" in cmd:
            # Extract the b64 portion between quotes
            start = cmd.index("'") + 1
            end = cmd.index("'", start)
            captured_b64.append(cmd[start:end])
        return ok_exec()

    mock_companion.execute.side_effect = capture_execute
    await junior._write_remote("/sandbox/test.py", content)
    assert len(captured_b64) == 1
    decoded = base64.b64decode(captured_b64[0]).decode()
    assert decoded == content


async def test_write_remote_raises_on_write_failure(junior, mock_companion):
    mock_companion.execute.side_effect = [
        ok_exec(),           # mkdir
        fail_exec("disk full"),  # base64 | write
    ]
    with pytest.raises(RuntimeError, match="disk full"):
        await junior._write_remote("/sandbox/big.py", "data")


# ── _abs helper ───────────────────────────────────────────────────────────────

def test_abs_joins_sandbox_and_relative(junior):
    assert junior._abs("src/main.py").replace("\\", "/") == "/sandbox/src/main.py"


def test_abs_with_nested_path(junior):
    assert junior._abs("a/b/c.py").replace("\\", "/") == "/sandbox/a/b/c.py"
