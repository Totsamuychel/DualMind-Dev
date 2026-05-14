"""Tests for tools/rag_store.py — RAGStore with mocked AsyncQdrantClient."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from tools.rag_store import (
    RAGStore,
    RAGHit,
    COLLECTION_CODEBASE,
    COLLECTION_TASK_HISTORY,
    COLLECTION_ERRORS,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_store() -> tuple[RAGStore, MagicMock]:
    """Return (store, mock_client) with _client replaced by a MagicMock."""
    store = RAGStore(url="http://localhost:6333", vector_dim=4)
    client = MagicMock()
    client.get_collections   = AsyncMock()
    client.create_collection = AsyncMock()
    client.delete_collection = AsyncMock()
    client.get_collection    = AsyncMock()
    client.upsert            = AsyncMock()
    client.search            = AsyncMock(return_value=[])
    client.delete            = AsyncMock()
    client.close             = AsyncMock()
    store._client = client
    return store, client


def _collections(*names: str) -> MagicMock:
    result = MagicMock()
    cols = []
    for n in names:
        col = MagicMock()
        col.name = n  # MagicMock(name=n) sets repr name, not the .name attribute
        cols.append(col)
    result.collections = cols
    return result


def _companion(vectors: list[list[float]] | None = None) -> MagicMock:
    c = MagicMock()
    c.embed = AsyncMock(return_value=vectors if vectors is not None else [[0.1, 0.2, 0.3, 0.4]])
    return c


# ── content_hash ──────────────────────────────────────────────────────────────


def test_content_hash_deterministic():
    assert RAGStore.content_hash("hello") == RAGStore.content_hash("hello")


def test_content_hash_differs_for_different_input():
    assert RAGStore.content_hash("foo") != RAGStore.content_hash("bar")


def test_content_hash_length():
    assert len(RAGStore.content_hash("anything")) == 16


# ── ensure_collection ─────────────────────────────────────────────────────────


async def test_ensure_collection_creates_when_absent():
    store, client = _make_store()
    client.get_collections.return_value = _collections()
    await store.ensure_collection("mytest")
    client.create_collection.assert_awaited_once()
    assert client.create_collection.call_args[0][0] == "mytest"


async def test_ensure_collection_skips_when_exists():
    store, client = _make_store()
    client.get_collections.return_value = _collections("mytest")
    await store.ensure_collection("mytest")
    client.create_collection.assert_not_awaited()


async def test_ensure_collection_recreates_when_flag_set():
    store, client = _make_store()
    client.get_collections.return_value = _collections("mytest")
    await store.ensure_collection("mytest", recreate=True)
    client.delete_collection.assert_awaited_once_with("mytest")
    client.create_collection.assert_awaited_once()


async def test_ensure_all_collections_creates_three():
    store, client = _make_store()
    client.get_collections.return_value = _collections()
    await store.ensure_all_collections()
    assert client.create_collection.await_count == 3


async def test_ensure_all_collections_names():
    store, client = _make_store()
    client.get_collections.return_value = _collections()
    await store.ensure_all_collections()
    created = {c[0][0] for c in client.create_collection.call_args_list}
    assert created == {COLLECTION_CODEBASE, COLLECTION_TASK_HISTORY, COLLECTION_ERRORS}


# ── upsert ────────────────────────────────────────────────────────────────────


async def test_upsert_calls_embed_and_client_upsert():
    store, client = _make_store()
    client.get_collections.return_value = _collections(COLLECTION_CODEBASE)
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    n = await store.upsert(companion, ["some text"], [{"file": "a.py"}], COLLECTION_CODEBASE)
    companion.embed.assert_awaited_once_with(["some text"])
    client.upsert.assert_awaited_once()
    assert n == 1


async def test_upsert_stores_text_in_payload():
    store, client = _make_store()
    client.get_collections.return_value = _collections(COLLECTION_CODEBASE)
    captured: list = []

    async def capture(collection_name, points):
        captured.extend(points)

    client.upsert.side_effect = capture
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    await store.upsert(companion, ["def foo(): pass"], [{}], COLLECTION_CODEBASE)
    assert captured[0].payload["text"] == "def foo(): pass"


async def test_upsert_does_not_overwrite_existing_text_in_payload():
    store, client = _make_store()
    client.get_collections.return_value = _collections(COLLECTION_CODEBASE)
    captured: list = []

    async def capture(collection_name, points):
        captured.extend(points)

    client.upsert.side_effect = capture
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    await store.upsert(
        companion, ["raw text"], [{"text": "custom text"}], COLLECTION_CODEBASE
    )
    assert captured[0].payload["text"] == "custom text"


async def test_upsert_empty_texts_returns_zero():
    store, _ = _make_store()
    n = await store.upsert(_companion(), [], [], COLLECTION_CODEBASE)
    assert n == 0


async def test_upsert_mismatched_lengths_raises():
    store, _ = _make_store()
    with pytest.raises(ValueError, match="mismatch"):
        await store.upsert(_companion(), ["a", "b"], [{}], COLLECTION_CODEBASE)


async def test_upsert_batches_correctly():
    store, client = _make_store()
    client.get_collections.return_value = _collections(COLLECTION_CODEBASE)
    texts = [f"text {i}" for i in range(5)]
    payloads = [{} for _ in texts]
    vecs = [[float(i)] * 4 for i in range(5)]
    companion = MagicMock()
    companion.embed = AsyncMock(side_effect=[vecs[:2], vecs[2:4], vecs[4:]])
    await store.upsert(companion, texts, payloads, COLLECTION_CODEBASE, batch_size=2)
    assert client.upsert.await_count == 3


# ── search ────────────────────────────────────────────────────────────────────


async def test_search_returns_rag_hits():
    store, client = _make_store()
    hit = MagicMock(score=0.9, payload={"text": "relevant code"})
    client.search.return_value = [hit]
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    results = await store.search(companion, "query", COLLECTION_CODEBASE)
    assert len(results) == 1
    assert isinstance(results[0], RAGHit)
    assert results[0].score == 0.9
    assert results[0].text() == "relevant code"


async def test_search_empty_embed_returns_empty():
    store, client = _make_store()
    companion = _companion([])
    results = await store.search(companion, "query", COLLECTION_CODEBASE)
    assert results == []
    client.search.assert_not_called()


async def test_search_client_exception_returns_empty():
    store, client = _make_store()
    client.search.side_effect = Exception("collection not found")
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    results = await store.search(companion, "query", COLLECTION_CODEBASE)
    assert results == []


async def test_search_with_filter_passes_filter_to_client():
    store, client = _make_store()
    client.search.return_value = []
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    await store.search(
        companion, "query", COLLECTION_CODEBASE,
        filter_field="file", filter_value="src/main.py",
    )
    call_kwargs = client.search.call_args.kwargs
    assert call_kwargs.get("query_filter") is not None


async def test_search_text_returns_strings():
    store, client = _make_store()
    client.search.return_value = [MagicMock(score=0.8, payload={"text": "some code"})]
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    results = await store.search_text(companion, "query", COLLECTION_CODEBASE)
    assert results == ["some code"]


async def test_search_text_skips_empty_text():
    store, client = _make_store()
    client.search.return_value = [
        MagicMock(score=0.9, payload={"text": "good"}),
        MagicMock(score=0.7, payload={}),
    ]
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    results = await store.search_text(companion, "query", COLLECTION_CODEBASE)
    assert results == ["good"]


# ── collection_count ──────────────────────────────────────────────────────────


async def test_collection_count_returns_vector_count():
    store, client = _make_store()
    client.get_collection.return_value = MagicMock(vectors_count=42)
    count = await store.collection_count(COLLECTION_CODEBASE)
    assert count == 42


async def test_collection_count_returns_zero_on_error():
    store, client = _make_store()
    client.get_collection.side_effect = Exception("not found")
    count = await store.collection_count(COLLECTION_CODEBASE)
    assert count == 0


# ── delete_by_filter ──────────────────────────────────────────────────────────


async def test_delete_by_filter_calls_client_delete():
    store, client = _make_store()
    await store.delete_by_filter(COLLECTION_CODEBASE, "file", "src/main.py")
    client.delete.assert_awaited_once()
    assert client.delete.call_args.kwargs["collection_name"] == COLLECTION_CODEBASE


# ── upsert_if_changed ─────────────────────────────────────────────────────────


async def test_upsert_if_changed_inserts_first_time():
    store, client = _make_store()
    client.get_collections.return_value = _collections(COLLECTION_CODEBASE)
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    cache: dict[str, str] = {}
    changed = await store.upsert_if_changed(
        companion, "code here", {"file": "a.py"}, COLLECTION_CODEBASE,
        hash_cache=cache, cache_key="a.py",
    )
    assert changed is True
    client.upsert.assert_awaited_once()


async def test_upsert_if_changed_skips_identical_content():
    store, client = _make_store()
    client.get_collections.return_value = _collections(COLLECTION_CODEBASE)
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    cache: dict[str, str] = {}
    text = "code here"
    await store.upsert_if_changed(
        companion, text, {}, COLLECTION_CODEBASE, hash_cache=cache, cache_key="a.py"
    )
    changed = await store.upsert_if_changed(
        companion, text, {}, COLLECTION_CODEBASE, hash_cache=cache, cache_key="a.py"
    )
    assert changed is False
    assert client.upsert.await_count == 1


async def test_upsert_if_changed_reruns_on_updated_content():
    store, client = _make_store()
    client.get_collections.return_value = _collections(COLLECTION_CODEBASE)
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    cache: dict[str, str] = {}
    await store.upsert_if_changed(
        companion, "original", {}, COLLECTION_CODEBASE, hash_cache=cache, cache_key="f.py"
    )
    changed = await store.upsert_if_changed(
        companion, "updated content", {}, COLLECTION_CODEBASE, hash_cache=cache, cache_key="f.py"
    )
    assert changed is True
    assert client.upsert.await_count == 2


async def test_upsert_if_changed_stores_hash_in_payload():
    store, client = _make_store()
    client.get_collections.return_value = _collections(COLLECTION_CODEBASE)
    captured: list = []

    async def capture(collection_name, points):
        captured.extend(points)

    client.upsert.side_effect = capture
    companion = _companion([[0.1, 0.2, 0.3, 0.4]])
    cache: dict[str, str] = {}
    await store.upsert_if_changed(
        companion, "content", {}, COLLECTION_CODEBASE, hash_cache=cache, cache_key="x.py"
    )
    assert "content_hash" in captured[0].payload


# ── RAGHit ────────────────────────────────────────────────────────────────────


def test_rag_hit_text():
    h = RAGHit(score=0.5, payload={"text": "hello"})
    assert h.text() == "hello"


def test_rag_hit_text_missing_key():
    h = RAGHit(score=0.5, payload={})
    assert h.text() == ""


def test_rag_hit_repr_truncates():
    h = RAGHit(score=0.75, payload={"text": "x" * 100})
    r = repr(h)
    assert "0.750" in r
    assert len(r) < 200
