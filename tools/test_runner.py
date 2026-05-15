"""Async quality checks (pytest, ruff, mypy) via companion server."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from tools.companion_client import CompanionClient

logger = logging.getLogger("dualmind.tests")

OUTPUT_LIMIT = 2000  # chars kept per tool output


@dataclass
class CheckResults:
    tests_passed: bool
    lint_passed: bool
    typecheck_passed: bool
    summary: str           # pytest short output
    lint_output: str
    typecheck_output: str
    errors: list[str] = field(default_factory=list)  # failed tool names

    @property
    def all_passed(self) -> bool:
        return self.tests_passed and self.lint_passed and self.typecheck_passed

    def __str__(self) -> str:
        icon = lambda ok: "✅" if ok else "❌"
        lines = [
            f"tests={icon(self.tests_passed)} lint={icon(self.lint_passed)} types={icon(self.typecheck_passed)}",
        ]
        if not self.tests_passed:
            lines.append(f"[pytest]\n{self.summary}")
        if not self.lint_passed:
            lines.append(f"[ruff]\n{self.lint_output}")
        if not self.typecheck_passed:
            lines.append(f"[mypy]\n{self.typecheck_output}")
        return "\n".join(lines)


async def run_checks(
    companion: CompanionClient,
    sandbox_dir: str,
    *,
    run_tests: bool = True,
    run_lint: bool = True,
    run_typecheck: bool = True,
) -> CheckResults:
    """Run pytest / ruff / mypy in sandbox_dir via companion /execute.

    Each tool runs independently so a crash in one doesn't block the others.
    Flags let callers skip individual checks (e.g. skip typecheck on first pass).
    """
    tests_ok = True
    tests_out = ""
    lint_ok = True
    lint_out = ""
    type_ok = True
    type_out = ""
    errors: list[str] = []

    if run_tests:
        r = await companion.execute(
            "python -m pytest --tb=short -q", cwd=sandbox_dir
        )
        # Exit code 5 = no tests collected — treat as pass (empty sandbox is fine).
        tests_ok = r.ok or r.exit_code == 5
        tests_out = r.output[:OUTPUT_LIMIT]
        if not tests_ok:
            errors.append("pytest")

    if run_lint:
        r = await companion.execute("python -m ruff check .", cwd=sandbox_dir)
        lint_ok = r.ok
        lint_out = r.output[:OUTPUT_LIMIT]
        if not lint_ok:
            errors.append("ruff")

    if run_typecheck:
        r = await companion.execute(
            "python -m mypy . --ignore-missing-imports", cwd=sandbox_dir
        )
        type_ok = r.ok
        type_out = r.output[:OUTPUT_LIMIT]
        if not type_ok:
            errors.append("mypy")

    results = CheckResults(
        tests_passed=tests_ok,
        lint_passed=lint_ok,
        typecheck_passed=type_ok,
        summary=tests_out,
        lint_output=lint_out,
        typecheck_output=type_out,
        errors=errors,
    )
    logger.info("Checks: %s", results)
    return results
