"""DualMind Dev — Entry point."""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import yaml

from core.orchestrator import Orchestrator
from tools.companion_client import CompanionClient
from tools.rag_store import RAGStore
from tools.indexer import CodebaseIndexer

logger = logging.getLogger("dualmind")

# ── Required config keys (dotted path) ───────────────────────────────────────

_REQUIRED_KEYS: list[tuple[str, ...]] = [
    ("lead_agent", "model_endpoint"),
    ("lead_agent", "model_name"),
    ("junior_agent", "model_endpoint"),
    ("junior_agent", "model_name"),
    ("junior_agent", "host"),
    ("junior_agent", "ssh_user"),
    ("junior_agent", "ssh_key"),
    ("junior_agent", "sandbox_dir"),
]


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        sys.exit(
            f"[dualmind] Config file not found: {cfg_path.resolve()}\n"
            "Copy config.yaml.example to config.yaml and fill in your values."
        )
    with cfg_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate_config(cfg: dict) -> None:
    """Raise SystemExit with a helpful message if any required key is missing."""
    missing = []
    for key_path in _REQUIRED_KEYS:
        node = cfg
        for part in key_path:
            if not isinstance(node, dict) or part not in node:
                missing.append(".".join(key_path))
                break
            node = node[part]
    if missing:
        sys.exit(
            "[dualmind] Missing required config key(s):\n"
            + "\n".join(f"  - {k}" for k in missing)
        )


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(cfg: dict) -> None:
    log_cfg = cfg.get("logging", {})
    level_name = str(log_cfg.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    log_file = log_cfg.get("file", "")
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)


# ── Companion server ──────────────────────────────────────────────────────────

async def _ping_companion(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{url}/ping")
            return r.status_code == 200
    except Exception:
        return False


async def _wait_for_companion(url: str, timeout: float = 15.0) -> None:
    """Poll /ping until the companion server responds or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _ping_companion(url):
            logger.info("Companion server ready at %s", url)
            return
        await asyncio.sleep(0.5)
    sys.exit(
        f"[dualmind] Companion server did not respond within {timeout:.0f}s.\n"
        f"Check that SlopLobster-companion.py is running on {url}."
    )


def _start_companion(port: int) -> subprocess.Popen:
    companion_script = Path(__file__).parent / "SlopLobster-companion.py"
    if not companion_script.exists():
        sys.exit(
            "[dualmind] SlopLobster-companion.py not found next to main.py.\n"
            "Set companion_server.autostart: false and start it manually."
        )
    proc = subprocess.Popen(
        [sys.executable, str(companion_script), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info(
        "Started companion server (PID %d) on port %d", proc.pid, port
    )
    return proc


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    cfg = load_config()
    validate_config(cfg)
    setup_logging(cfg)

    logger.info("DualMind Dev starting")

    companion_cfg = cfg.get("companion_server", {})
    port = int(companion_cfg.get("port", 8765))
    autostart = bool(companion_cfg.get("autostart", True))
    companion_url = cfg.get("lead_agent", {}).get(
        "companion_url", f"http://localhost:{port}"
    )

    companion_proc: Optional[subprocess.Popen] = None

    if autostart:
        if await _ping_companion(companion_url):
            logger.info("Companion server already running at %s", companion_url)
        else:
            companion_proc = _start_companion(port)
            await _wait_for_companion(companion_url)
    else:
        logger.info(
            "autostart=false — expecting companion server at %s", companion_url
        )
        await _wait_for_companion(companion_url)

    # ── RAG: open store and index codebase ────────────────────────────────────
    qdrant_cfg = cfg.get("qdrant", {})
    rag: Optional[RAGStore] = None
    if qdrant_cfg.get("url"):
        lead_companion = CompanionClient(companion_url)
        rag = RAGStore.from_config(cfg)
        await rag.ensure_all_collections()
        indexer = CodebaseIndexer.from_config(cfg, rag, lead_companion)
        n = await indexer.index_all()
        if n:
            logger.info("RAG: indexed %d chunk(s) into Qdrant", n)
        else:
            logger.info("RAG: codebase up-to-date (no changes since last run)")
    else:
        logger.info("RAG: qdrant.url not set — skipping codebase indexing")

    orchestrator = Orchestrator(cfg, rag=rag)

    # ── Telegram bot (background task) ────────────────────────────────────────
    bot_task: Optional[asyncio.Task] = None
    if cfg.get("telegram", {}).get("token"):
        from tg_bot.bot import run_polling as _tg_run_polling
        bot_task = asyncio.create_task(_tg_run_polling(cfg), name="telegram-bot")

    loop = asyncio.get_running_loop()

    def _handle_signal(sig, _frame=None):
        logger.info("Received signal %s — shutting down", sig)
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler for all signals.
            signal.signal(sig, _handle_signal)

    try:
        await orchestrator.run()
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
    finally:
        if bot_task is not None and not bot_task.done():
            bot_task.cancel()
            try:
                await bot_task
            except (asyncio.CancelledError, Exception):
                pass
        if rag is not None:
            await rag.close()
        if companion_proc is not None:
            logger.info("Terminating companion server (PID %d)", companion_proc.pid)
            companion_proc.terminate()
            try:
                companion_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                companion_proc.kill()

    logger.info("DualMind Dev stopped")


if __name__ == "__main__":
    asyncio.run(main())
