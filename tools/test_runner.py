"""Run pytest, ruff, and mypy in the sandbox directory."""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger("dualmind.tests")


def _run(cmd: list[str], cwd: str) -> tuple[bool, str]:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    ok = result.returncode == 0
    output = result.stdout + result.stderr
    return ok, output.strip()[:1000]


def run_checks(sandbox_dir: str) -> dict:
    results = {}

    # Pytest
    tests_ok, tests_out = _run(["python", "-m", "pytest", "--tb=short", "-q"], cwd=sandbox_dir)
    results["tests_passed"] = tests_ok
    results["summary"] = tests_out

    # Ruff (linter)
    lint_ok, lint_out = _run(["ruff", "check", "."], cwd=sandbox_dir)
    results["lint_passed"] = lint_ok
    results["lint_output"] = lint_out

    # Mypy (type check)
    type_ok, type_out = _run(["mypy", ".", "--ignore-missing-imports"], cwd=sandbox_dir)
    results["typecheck_passed"] = type_ok
    results["typecheck_output"] = type_out

    logger.info(
        f"Checks: tests={'✅' if tests_ok else '❌'} "
        f"lint={'✅' if lint_ok else '❌'} "
        f"types={'✅' if type_ok else '❌'}"
    )
    return results
