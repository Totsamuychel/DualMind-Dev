"""Async HTTP client for the SlopLobster companion server (port 8765)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger("dualmind.companion")


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class ExecResult:
    """Result of a shell command executed via /execute (NDJSON stream)."""
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """Combined stdout + stderr, whitespace-stripped."""
        parts = [p for p in (self.stdout, self.stderr) if p.strip()]
        return "\n".join(parts)

    def __str__(self) -> str:
        return self.output


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    content: str = ""  # Only set when fetch_top > 0


# ── Client ────────────────────────────────────────────────────────────────────

class CompanionClient:
    """Wraps every endpoint of SlopLobster-companion.py with typed async methods.

    Start the server first:
        python SlopLobster-companion.py

    Then instantiate:
        companion = CompanionClient("http://127.0.0.1:8765")

    Or read the URL from config:
        companion = CompanionClient(config["companion_server"]["url"])
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8765", default_timeout: int = 120):
        self.base = base_url.rstrip("/")
        self.default_timeout = default_timeout

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    # ── Health ────────────────────────────────────────────────────────────────

    async def ping(self) -> dict:
        """GET /status — returns platform, python version, shell, env info."""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(self._url("/status"))
            r.raise_for_status()
            return r.json()

    async def is_alive(self) -> bool:
        """Return True if companion server is reachable."""
        try:
            await self.ping()
            return True
        except Exception:
            return False

    # ── Shell execution ───────────────────────────────────────────────────────

    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> ExecResult:
        """POST /execute — run a shell command, collect streamed NDJSON output.

        Server streams lines:
            {"t": "o", "d": "<stdout>"}
            {"t": "e", "d": "<stderr>"}
            {"t": "d", "d": "<exit_code>"}
        """
        payload: dict = {"command": command}
        if cwd is not None:
            payload["cwd"] = cwd
        if timeout is not None:
            payload["timeout"] = timeout

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = 1

        async with httpx.AsyncClient(timeout=self.default_timeout) as c:
            async with c.stream("POST", self._url("/execute"), json=payload) as resp:
                resp.raise_for_status()
                async for raw in resp.aiter_lines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    kind, data = msg.get("t", ""), msg.get("d", "")
                    if kind == "o":
                        stdout_parts.append(data)
                    elif kind == "e":
                        stderr_parts.append(data)
                    elif kind == "d":
                        try:
                            exit_code = int(data)
                        except (ValueError, TypeError):
                            pass

        result = ExecResult(
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            exit_code=exit_code,
        )
        logger.debug("execute %r → exit=%d", command[:80], exit_code)
        return result

    # ── Web search ────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        num_results: int = 5,
        fetch_top: int = 0,
    ) -> list[SearchResult]:
        """POST /search — DuckDuckGo search.

        fetch_top: also fetch full page content for the top N results.
        """
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                self._url("/search"),
                json={"query": query, "num_results": num_results, "fetch_top": fetch_top},
            )
            r.raise_for_status()
            data = r.json()

        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("snippet", ""),
                content=item.get("content", ""),
            )
            for item in data.get("results", [])
        ]

    # ── URL fetching ──────────────────────────────────────────────────────────

    async def fetch(
        self,
        url: str,
        mode: str = "text",
        max_bytes: int = 500_000,
    ) -> str:
        """POST /fetch — download and extract text from a URL.

        mode: 'text' (HTML stripped) or 'raw' (original content).
        """
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                self._url("/fetch"),
                json={"url": url, "mode": mode, "max_bytes": max_bytes},
            )
            r.raise_for_status()
            return r.json().get("content", "")

    # ── AST / code outline ────────────────────────────────────────────────────

    async def ast_signatures(self, source: str, language: str) -> str:
        """POST /ast_signatures — extract function/class signatures from source.

        source:   full file content as a string (not a path).
        language: 'py', 'js', 'ts', 'tsx', 'rs', 'go', 'rb', 'java', 'c', 'cpp'.

        Returns a compact text outline (signatures only, not full source).
        Useful for giving Lead agent a structural overview without sending
        the entire file into the LLM context.
        """
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                self._url("/ast_signatures"),
                json={"source": source, "language": language},
            )
            r.raise_for_status()
            return r.json().get("outline", "")

    # ── Embeddings ────────────────────────────────────────────────────────────

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """POST /embed — generate sentence embeddings (all-MiniLM-L6-v2).

        Requires sentence-transformers on the companion machine.
        Max 100 texts per call. Returns [] on missing dependency.
        """
        if not texts:
            return []
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(self._url("/embed"), json={"texts": texts})
            r.raise_for_status()
            data = r.json()
        if data.get("error"):
            logger.warning("embed error: %s", data["error"])
            return []
        return data.get("embeddings", [])

    # ── Browser automation ────────────────────────────────────────────────────

    async def browser_status(self) -> dict:
        """POST /browser_status — playwright availability + current URL."""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(self._url("/browser_status"), json={})
            return r.json()

    async def browser_launch(
        self, url: Optional[str] = None, headless: bool = True
    ) -> dict:
        """POST /browser_launch — start a Playwright browser session."""
        payload: dict = {"headless": headless}
        if url:
            payload["url"] = url
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(self._url("/browser_launch"), json=payload)
            return r.json()

    async def browser_navigate(
        self, url: str, wait_until: str = "domcontentloaded"
    ) -> dict:
        """POST /browser_navigate — navigate to URL."""
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                self._url("/browser_navigate"),
                json={"url": url, "wait_until": wait_until},
            )
            return r.json()

    async def browser_screenshot(
        self,
        selector: Optional[str] = None,
        full_page: bool = False,
    ) -> str:
        """POST /browser_screenshot — returns 'data:image/png;base64,...'."""
        payload: dict = {"full_page": full_page}
        if selector:
            payload["selector"] = selector
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(self._url("/browser_screenshot"), json=payload)
            return r.json().get("screenshot", "")

    async def browser_get_content(
        self,
        selector: Optional[str] = None,
        mode: str = "text",
        max_length: int = 30_000,
    ) -> str:
        """POST /browser_get_content — extract text or HTML from page/element."""
        payload: dict = {"mode": mode, "max_length": max_length}
        if selector:
            payload["selector"] = selector
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(self._url("/browser_get_content"), json=payload)
            return r.json().get("content", "")

    async def browser_click(self, selector: str) -> dict:
        """POST /browser_click."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(self._url("/browser_click"), json={"selector": selector})
            return r.json()

    async def browser_type(
        self, selector: str, text: str, submit: bool = False
    ) -> dict:
        """POST /browser_type — fill an input field, optionally press Enter."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                self._url("/browser_type"),
                json={"selector": selector, "text": text, "submit": submit},
            )
            return r.json()

    async def browser_evaluate(self, script: str) -> str:
        """POST /browser_evaluate — run JavaScript in the page context."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(self._url("/browser_evaluate"), json={"script": script})
            return r.json().get("result", "")

    async def browser_wait_for(
        self, selector: str, state: str = "visible", timeout: int = 10_000
    ) -> bool:
        """POST /browser_wait_for — wait for an element. Returns True if found."""
        async with httpx.AsyncClient(timeout=max(timeout / 1000 + 5, 15)) as c:
            r = await c.post(
                self._url("/browser_wait_for"),
                json={"selector": selector, "state": state, "timeout": timeout},
            )
            return r.json().get("ok", False)

    async def browser_hover(self, selector: str) -> dict:
        """POST /browser_hover."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(self._url("/browser_hover"), json={"selector": selector})
            return r.json()

    async def browser_select_option(self, selector: str, value: str) -> dict:
        """POST /browser_select_option."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                self._url("/browser_select_option"),
                json={"selector": selector, "value": value},
            )
            return r.json()

    async def browser_press_key(self, key: str) -> dict:
        """POST /browser_press_key — e.g. 'Enter', 'Tab', 'Escape'."""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(self._url("/browser_press_key"), json={"key": key})
            return r.json()

    async def browser_scroll(
        self, direction: str = "down", amount: int = 500
    ) -> dict:
        """POST /browser_scroll — direction: 'up'|'down'|'top'|'bottom'."""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                self._url("/browser_scroll"),
                json={"direction": direction, "amount": amount},
            )
            return r.json()

    async def browser_go_back(self) -> dict:
        """POST /browser_go_back."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(self._url("/browser_go_back"), json={})
            return r.json()

    async def browser_go_forward(self) -> dict:
        """POST /browser_go_forward."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(self._url("/browser_go_forward"), json={})
            return r.json()

    async def browser_console(
        self,
        types: Optional[list[str]] = None,
        since: Optional[str] = None,
    ) -> dict:
        """POST /browser_console — retrieve filtered browser console messages."""
        payload: dict = {}
        if types:
            payload["types"] = types
        if since:
            payload["since"] = since
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(self._url("/browser_console"), json=payload)
            return r.json()

    async def browser_close(self) -> None:
        """POST /browser_close — terminate the browser session."""
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(self._url("/browser_close"), json={})

    # ── Dev server management ─────────────────────────────────────────────────

    async def dev_start(
        self, command: str, port: int = 3000, cwd: Optional[str] = None
    ) -> dict:
        """POST /dev_start — spawn a long-running process (e.g. 'npm run dev')."""
        payload: dict = {"command": command, "port": port}
        if cwd:
            payload["cwd"] = cwd
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(self._url("/dev_start"), json=payload)
            return r.json()

    async def dev_status(self, port: int = 3000) -> dict:
        """POST /dev_status — alive + HTTP-ready state for a dev process."""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(self._url("/dev_status"), json={"port": port})
            return r.json()

    async def dev_output(self, port: int = 3000, tail: int = 100) -> str:
        """POST /dev_output — tail of dev process stdout."""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(self._url("/dev_output"), json={"port": port, "tail": tail})
            return r.json().get("output", "")

    async def dev_stop(self, port: int = 3000) -> dict:
        """POST /dev_stop — terminate dev process."""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(self._url("/dev_stop"), json={"port": port})
            return r.json()
