"""Codebase indexer for RAG — Step R2 of the DualMind RAG plan.

Walks repository.path, splits source files into semantic chunks, embeds them
via companion.embed(), and stores them in the Qdrant 'codebase' collection.

Chunking strategy
-----------------
  .py files  — AST-based: one chunk per top-level function / class, plus a
               preamble chunk (imports, module docstring, constants).
               Definitions longer than CHUNK_MAX_LINES are sub-split with
               a sliding window so no chunk exceeds the LLM context budget.
  Other files — Sliding window: SLIDE_LINES lines with OVERLAP_LINES overlap.

Incremental updates
-------------------
A JSON hash cache at <repo_path>/.rag_cache.json records a content hash per
file.  On re-runs only changed or new files are re-indexed; removed files are
cleaned from Qdrant.  Pass recreate=True to wipe and rebuild from scratch.
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, TYPE_CHECKING

from tools.rag_store import RAGStore, COLLECTION_CODEBASE

if TYPE_CHECKING:
    from tools.companion_client import CompanionClient

logger = logging.getLogger("dualmind.indexer")

# ── Tuning constants ──────────────────────────────────────────────────────────

CHUNK_MAX_LINES = 100    # AST nodes longer than this get sub-split
SLIDE_LINES = 60         # lines per sliding-window chunk
OVERLAP_LINES = 15       # overlap between consecutive sliding-window chunks
MAX_FILE_BYTES = 300_000 # files larger than this are skipped
MIN_CHUNK_LINES = 3      # chunks shorter than this are discarded

_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "dist", "build", ".tox", "htmlcov",
})

_EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "py",
    ".js": "js",
    ".ts": "ts",
    ".tsx": "tsx",
    ".rs": "rs",
    ".go": "go",
    ".java": "java",
    ".rb": "rb",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
}

# ── Internal chunk representation ─────────────────────────────────────────────


@dataclass
class _Chunk:
    text: str
    start_line: int
    end_line: int
    symbol: str = ""


# ── Chunking helpers ──────────────────────────────────────────────────────────


def _sliding_window(
    lines: list[str],
    start_offset: int = 1,
) -> list[_Chunk]:
    """Split a line list into overlapping windows."""
    chunks: list[_Chunk] = []
    i = 0
    while i < len(lines):
        window = lines[i : i + SLIDE_LINES]
        text = "\n".join(window).strip()
        if text and len(window) >= MIN_CHUNK_LINES:
            chunks.append(
                _Chunk(
                    text=text,
                    start_line=start_offset + i,
                    end_line=start_offset + i + len(window) - 1,
                )
            )
        step = max(1, SLIDE_LINES - OVERLAP_LINES)
        i += step
    return chunks


def _python_chunks(source: str) -> list[_Chunk]:
    """AST-based chunking for Python source.

    Returns one chunk per top-level definition (FunctionDef, AsyncFunctionDef,
    ClassDef).  A preamble chunk carries imports, constants, and the module
    docstring.  Definitions exceeding CHUNK_MAX_LINES are sub-split.
    Falls back to sliding window on SyntaxError.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _sliding_window(source.splitlines())

    lines = source.splitlines()

    # Collect top-level definitions (col_offset == 0).
    top_level = sorted(
        (
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
        ),
        key=lambda n: n.lineno,
    )

    chunks: list[_Chunk] = []

    # Preamble: everything before the first top-level definition.
    preamble_end = (top_level[0].lineno - 1) if top_level else len(lines)
    preamble_lines = lines[:preamble_end]
    preamble_text = "\n".join(preamble_lines).strip()
    if preamble_text and len(preamble_lines) >= MIN_CHUNK_LINES:
        chunks.append(
            _Chunk(
                text=preamble_text,
                start_line=1,
                end_line=preamble_end,
                symbol="<module>",
            )
        )

    # One chunk per top-level definition.
    for node in top_level:
        node_lines = lines[node.lineno - 1 : node.end_lineno]
        node_text = "\n".join(node_lines).strip()
        if not node_text:
            continue

        if len(node_lines) <= CHUNK_MAX_LINES:
            chunks.append(
                _Chunk(
                    text=node_text,
                    start_line=node.lineno,
                    end_line=node.end_lineno,
                    symbol=node.name,
                )
            )
        else:
            # Large definition — sub-split with sliding window, keep symbol.
            for sub in _sliding_window(node_lines, start_offset=node.lineno):
                sub.symbol = node.name
                chunks.append(sub)

    # If the file has no top-level defs (e.g. pure data / config), use sliding window.
    if not chunks:
        return _sliding_window(lines)

    return chunks


def _generic_chunks(source: str) -> list[_Chunk]:
    return _sliding_window(source.splitlines())


def _chunks_for(source: str, lang: str) -> list[_Chunk]:
    """Dispatch chunking strategy by language."""
    return _python_chunks(source) if lang == "py" else _generic_chunks(source)


# ── Standalone re-index helper (used by JuniorAgent after commit) ─────────────


async def reindex_file_content(
    rag: RAGStore,
    companion: "CompanionClient",
    content: str,
    rel_path: str,
    *,
    extensions: dict[str, str] = _EXTENSION_LANGUAGE,
) -> int:
    """Re-index a single file from already-loaded content.

    Used by JuniorAgent (R6) after commit_changes() to keep the Qdrant
    codebase collection current without waiting for the next full index_all().

    rel_path must be the path relative to the repository root (forward slashes).
    Returns the number of chunks written, or 0 for unsupported file types.
    """
    lang = extensions.get(Path(rel_path).suffix.lower())
    if not lang:
        return 0

    chunks = _chunks_for(content, lang)
    if not chunks:
        return 0

    # Remove stale chunks for this file before inserting fresh ones.
    await rag.delete_by_filter(COLLECTION_CODEBASE, "file", rel_path)

    texts = [c.text for c in chunks]
    payloads = [
        {
            "file": rel_path,
            "language": lang,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "symbol": c.symbol,
        }
        for c in chunks
    ]

    n = await rag.upsert(companion, texts, payloads, COLLECTION_CODEBASE)
    logger.debug("reindex_file_content: %s → %d chunk(s)", rel_path, n)
    return n


# ── Indexer ───────────────────────────────────────────────────────────────────


class CodebaseIndexer:
    """Indexes a local repository into the Qdrant 'codebase' collection."""

    def __init__(
        self,
        rag: RAGStore,
        companion: "CompanionClient",
        repo_path: str,
        *,
        cache_file: Optional[Path] = None,
        skip_dirs: frozenset[str] = _SKIP_DIRS,
        extensions: dict[str, str] = _EXTENSION_LANGUAGE,
    ):
        self.rag = rag
        self.companion = companion
        self.repo = Path(repo_path)
        self.cache_path = cache_file or (self.repo / ".rag_cache.json")
        self.skip_dirs = skip_dirs
        self.extensions = extensions

    @classmethod
    def from_config(
        cls,
        cfg: dict,
        rag: RAGStore,
        companion: "CompanionClient",
    ) -> "CodebaseIndexer":
        """Build from the top-level config dict (reads cfg['repository']['path'])."""
        repo_path = cfg.get("repository", {}).get("path", ".")
        return cls(rag, companion, repo_path)

    # ── Hash cache ────────────────────────────────────────────────────────────

    def _load_cache(self) -> dict[str, str]:
        """Load {relative_path: content_hash} from disk. Returns {} if absent."""
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_cache(self, cache: dict[str, str]) -> None:
        self.cache_path.write_text(
            json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8"
        )

    # ── File discovery ────────────────────────────────────────────────────────

    def _walk_files(self) -> Iterator[tuple[Path, str]]:
        """Yield (absolute_path, language) for all indexable files."""
        if not self.repo.is_dir():
            logger.warning("Indexer: repo path %r not found", str(self.repo))
            return

        for path in sorted(self.repo.rglob("*")):
            if not path.is_file():
                continue
            # Skip directories in the blacklist (check all parents).
            if any(part in self.skip_dirs for part in path.parts):
                continue
            lang = self.extensions.get(path.suffix.lower())
            if lang is None:
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                logger.debug("Indexer: skipping large file %s", path)
                continue
            yield path, lang

    # ── Chunking dispatch ─────────────────────────────────────────────────────

    def _chunks_for(self, source: str, lang: str) -> list[_Chunk]:
        return _chunks_for(source, lang)

    # ── Single-file indexing ──────────────────────────────────────────────────

    async def _index_file(
        self,
        path: Path,
        lang: str,
        hash_cache: dict[str, str],
    ) -> int:
        """Index one file.  Returns number of chunks upserted (0 = skipped)."""
        rel = str(path.relative_to(self.repo)).replace("\\", "/")

        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Indexer: cannot read %s: %s", rel, exc)
            return 0

        current_hash = RAGStore.content_hash(source)
        if hash_cache.get(rel) == current_hash:
            return 0  # unchanged

        # Remove stale chunks before inserting updated ones.
        if rel in hash_cache:
            await self.rag.delete_by_filter(COLLECTION_CODEBASE, "file", rel)

        chunks = self._chunks_for(source, lang)
        if not chunks:
            return 0

        texts = [c.text for c in chunks]
        payloads = [
            {
                "file": rel,
                "language": lang,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "symbol": c.symbol,
            }
            for c in chunks
        ]

        n = await self.rag.upsert(
            self.companion, texts, payloads, COLLECTION_CODEBASE
        )
        hash_cache[rel] = current_hash
        logger.debug("Indexer: %s → %d chunk(s)", rel, n)
        return n

    # ── Public API ────────────────────────────────────────────────────────────

    async def index_all(self, *, recreate: bool = False) -> int:
        """Index the entire repository.

        recreate=False (default): incremental — only changed / new files.
        recreate=True:            wipe the collection and rebuild from scratch.

        Returns total number of chunks written.
        """
        await self.rag.ensure_collection(COLLECTION_CODEBASE, recreate=recreate)

        hash_cache: dict[str, str] = {} if recreate else self._load_cache()
        total = 0
        files_indexed = 0
        files_skipped = 0
        current_rels: set[str] = set()

        for path, lang in self._walk_files():
            current_rels.add(str(path.relative_to(self.repo)).replace("\\", "/"))
            n = await self._index_file(path, lang, hash_cache)
            if n:
                total += n
                files_indexed += 1
            else:
                files_skipped += 1

        stale = [k for k in hash_cache if k not in current_rels]
        for rel in stale:
            await self.rag.delete_by_filter(COLLECTION_CODEBASE, "file", rel)
            del hash_cache[rel]
            logger.info("Indexer: removed stale entry %s", rel)

        self._save_cache(hash_cache)
        logger.info(
            "Indexer: done — %d chunk(s) from %d file(s) (%d skipped unchanged)",
            total, files_indexed, files_skipped,
        )
        return total

    async def index_path(self, file_path: str) -> int:
        """Re-index a single file unconditionally (called after git commit in R6).

        file_path may be absolute or relative to repo root.
        Returns number of chunks written, or 0 if the file is not indexable.
        """
        path = Path(file_path)
        if not path.is_absolute():
            path = self.repo / path
        path = path.resolve()

        lang = self.extensions.get(path.suffix.lower())
        if lang is None:
            return 0
        if not path.is_file():
            logger.warning("Indexer.index_path: %s not found", path)
            return 0

        # Load cache, force-update by removing the existing hash entry.
        hash_cache = self._load_cache()
        rel = str(path.relative_to(self.repo)).replace("\\", "/")
        hash_cache.pop(rel, None)  # force re-index regardless of hash

        # Also remove old chunks so we don't accumulate duplicates.
        await self.rag.delete_by_filter(COLLECTION_CODEBASE, "file", rel)

        n = await self._index_file(path, lang, hash_cache)
        self._save_cache(hash_cache)
        logger.info("Indexer.index_path: %s → %d chunk(s)", rel, n)
        return n

    async def stats(self) -> dict:
        """Return a summary dict: {total_chunks, cached_files, repo_path}."""
        count = await self.rag.collection_count(COLLECTION_CODEBASE)
        cache = self._load_cache()
        return {
            "total_chunks": count,
            "cached_files": len(cache),
            "repo_path": str(self.repo),
            "cache_file": str(self.cache_path),
        }
