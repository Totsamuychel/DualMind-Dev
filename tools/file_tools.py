"""File read/write/diff helpers for agents."""

from __future__ import annotations

import difflib
from pathlib import Path


def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_file(path: str, content: str):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def unified_diff(original: str, modified: str, filename: str = "file") -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )


def list_files(directory: str, extensions: list[str] | None = None) -> list[str]:
    base = Path(directory)
    files = [p for p in base.rglob("*") if p.is_file()]
    if extensions:
        files = [f for f in files if f.suffix in extensions]
    return [str(f.relative_to(base)) for f in files]
