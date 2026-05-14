"""Vector store layer using Qdrant for RAG (Retrieval-Augmented Generation).

Collections used by DualMind:
  codebase       — source file chunks, rebuilt/updated after each commit
  task_history   — completed tasks (approved), queried by Lead before decomposition
  error_patterns — failed test/lint outputs, queried by Junior before execution
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    VectorParams,
)

if TYPE_CHECKING:
    from tools.companion_client import CompanionClient

logger = logging.getLogger("dualmind.rag")

# ── Collection names ──────────────────────────────────────────────────────────

COLLECTION_CODEBASE = "codebase"
COLLECTION_TASK_HISTORY = "task_history"
COLLECTION_ERRORS = "error_patterns"

_STANDARD_COLLECTIONS = (
    COLLECTION_CODEBASE,
    COLLECTION_TASK_HISTORY,
    COLLECTION_ERRORS,
)

# ── Result type ───────────────────────────────────────────────────────────────


@dataclass
class RAGHit:
    """Single result from a vector search."""

    score: float
    payload: dict

    def text(self) -> str:
        return self.payload.get("text", "")

    def __repr__(self) -> str:
        preview = self.text()[:60].replace("\n", " ")
        return f"RAGHit(score={self.score:.3f}, text={preview!r})"


# ── Store ─────────────────────────────────────────────────────────────────────


class RAGStore:
    """Async Qdrant wrapper.

    Embeddings are always generated via companion.embed() so the same model
    is used for indexing and querying (all-MiniLM-L6-v2, dim=384 by default).

    Usage:
        async with RAGStore.from_config(cfg) as rag:
            await rag.ensure_all_collections()
            await rag.upsert(companion, texts, payloads, COLLECTION_CODEBASE)
            hits = await rag.search(companion, query, COLLECTION_CODEBASE)
    """

    def __init__(
        self,
        url: str = "http://localhost:6333",
        vector_dim: int = 384,
        api_key: Optional[str] = None,
    ):
        self.url = url
        self.vector_dim = vector_dim
        self._client = AsyncQdrantClient(url=url, api_key=api_key)

    @classmethod
    def from_config(cls, cfg: dict) -> "RAGStore":
        """Build from the top-level config dict (reads cfg['qdrant'])."""
        qdrant_cfg = cfg.get("qdrant", {})
        return cls(
            url=qdrant_cfg.get("url", "http://localhost:6333"),
            vector_dim=int(qdrant_cfg.get("vector_dim", 384)),
            api_key=qdrant_cfg.get("api_key") or None,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def content_hash(text: str) -> str:
        """Short SHA-256 hex digest for change detection (16 chars)."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # ── Collections ───────────────────────────────────────────────────────────

    async def ensure_collection(
        self,
        name: str,
        *,
        recreate: bool = False,
    ) -> None:
        """Create collection if absent. Pass recreate=True to drop and rebuild."""
        existing = {c.name for c in (await self._client.get_collections()).collections}
        if name in existing:
            if recreate:
                await self._client.delete_collection(name)
                logger.info("RAG: dropped collection %r (recreate)", name)
            else:
                return
        await self._client.create_collection(
            name,
            vectors_config=VectorParams(
                size=self.vector_dim, distance=Distance.COSINE
            ),
        )
        logger.info("RAG: created collection %r (dim=%d)", name, self.vector_dim)

    async def ensure_all_collections(self) -> None:
        """Ensure all three standard DualMind collections exist."""
        for name in _STANDARD_COLLECTIONS:
            await self.ensure_collection(name)

    async def delete_collection(self, name: str) -> None:
        await self._client.delete_collection(name)
        logger.info("RAG: deleted collection %r", name)

    async def collection_count(self, name: str) -> int:
        """Number of vectors in collection, or 0 if collection doesn't exist."""
        try:
            info = await self._client.get_collection(name)
            return info.vectors_count or 0
        except Exception:
            return 0

    # ── Write ─────────────────────────────────────────────────────────────────

    async def upsert(
        self,
        companion: "CompanionClient",
        texts: list[str],
        payloads: list[dict],
        collection: str,
        *,
        batch_size: int = 32,
    ) -> int:
        """Embed texts and upsert into collection.

        The text is stored inside the payload under key "text" so it can be
        retrieved without a second lookup. Existing "text" in payload takes
        precedence.

        Returns number of points actually written.
        """
        if not texts:
            return 0
        if len(texts) != len(payloads):
            raise ValueError(
                f"texts/payloads length mismatch: {len(texts)} vs {len(payloads)}"
            )

        await self.ensure_collection(collection)

        total = 0
        for batch_start in range(0, len(texts), batch_size):
            batch_texts = texts[batch_start : batch_start + batch_size]
            batch_payloads = payloads[batch_start : batch_start + batch_size]

            vectors = await companion.embed(batch_texts)
            if not vectors:
                logger.warning(
                    "RAG: embed returned empty for batch starting at %d", batch_start
                )
                continue
            if len(vectors) != len(batch_texts):
                logger.warning(
                    "RAG: embed returned %d vectors for %d texts",
                    len(vectors),
                    len(batch_texts),
                )
                continue

            points = []
            for vec, text, raw_payload in zip(vectors, batch_texts, batch_payloads):
                p = dict(raw_payload)
                p.setdefault("text", text)
                points.append(
                    PointStruct(id=str(uuid.uuid4()), vector=vec, payload=p)
                )

            await self._client.upsert(collection_name=collection, points=points)
            total += len(points)

        logger.debug("RAG: upserted %d point(s) into %r", total, collection)
        return total

    async def upsert_if_changed(
        self,
        companion: "CompanionClient",
        text: str,
        payload: dict,
        collection: str,
        *,
        hash_cache: dict[str, str],
        cache_key: str,
    ) -> bool:
        """Upsert only when content has changed since last index.

        hash_cache is a dict kept by the caller (e.g. the indexer) mapping
        cache_key → last known content hash.  Returns True if upsert ran.

        Example:
            cache: dict[str, str] = {}
            changed = await rag.upsert_if_changed(
                companion, source, payload, COLLECTION_CODEBASE,
                hash_cache=cache, cache_key=file_path,
            )
        """
        current_hash = self.content_hash(text)
        if hash_cache.get(cache_key) == current_hash:
            return False

        full_payload = dict(payload)
        full_payload["content_hash"] = current_hash
        await self.upsert(companion, [text], [full_payload], collection)
        hash_cache[cache_key] = current_hash
        return True

    # ── Read ──────────────────────────────────────────────────────────────────

    async def search(
        self,
        companion: "CompanionClient",
        query: str,
        collection: str,
        *,
        top_k: int = 5,
        score_threshold: float = 0.0,
        filter_field: Optional[str] = None,
        filter_value: Optional[str] = None,
    ) -> list[RAGHit]:
        """Embed query and return top-k nearest neighbours as RAGHit list.

        Optional exact-match pre-filter: filter_field / filter_value narrow
        results to points where payload[filter_field] == filter_value.
        """
        vectors = await companion.embed([query])
        if not vectors:
            logger.warning("RAG: embed returned empty for query")
            return []

        qdrant_filter: Optional[Filter] = None
        if filter_field is not None and filter_value is not None:
            qdrant_filter = Filter(
                must=[
                    FieldCondition(
                        key=filter_field, match=MatchValue(value=filter_value)
                    )
                ]
            )

        try:
            hits = await self._client.search(
                collection_name=collection,
                query_vector=vectors[0],
                limit=top_k,
                score_threshold=score_threshold if score_threshold > 0 else None,
                query_filter=qdrant_filter,
            )
        except Exception as exc:
            logger.warning("RAG: search failed in %r: %s", collection, exc)
            return []

        results = [RAGHit(score=h.score, payload=h.payload or {}) for h in hits]
        logger.debug("RAG: search in %r → %d hit(s)", collection, len(results))
        return results

    async def search_text(
        self,
        companion: "CompanionClient",
        query: str,
        collection: str,
        *,
        top_k: int = 5,
        score_threshold: float = 0.0,
    ) -> list[str]:
        """Like search(), but returns only the text strings of each hit."""
        hits = await self.search(
            companion,
            query,
            collection,
            top_k=top_k,
            score_threshold=score_threshold,
        )
        return [h.text() for h in hits if h.text()]

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete_by_filter(
        self,
        collection: str,
        field: str,
        value: str,
    ) -> None:
        """Delete all points where payload[field] == value.

        Used by the incremental indexer to remove stale chunks for a file
        before inserting updated ones:
            await rag.delete_by_filter(COLLECTION_CODEBASE, "file", "tools/git_tools.py")
        """
        await self._client.delete(
            collection_name=collection,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key=field, match=MatchValue(value=value))]
                )
            ),
        )
        logger.debug(
            "RAG: deleted points in %r where %s=%r", collection, field, value
        )

    # ── Context manager ───────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> "RAGStore":
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()
