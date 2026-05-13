# Merge Plan: SlopLobster + DualMind-Dev

## Что откуда берём

| Компонент | Откуда | Зачем |
|-----------|--------|-------|
| `SlopLobster-companion.py` | Slop-Dualmind | Сервер-инструментов: shell, git, web-search, browser, AST, embeddings |
| `SlopLobster.html` | Slop-Dualmind | Готовый UI: чат, файлы, диффы, multi-agent панель, human-review |
| Агенты, протокол, SSH | DualMind-Dev | Архитектура Lead→Junior, Pydantic-схемы, оркестратор |

---

## Итоговая структура файлов

```
DualMind-Dev/
├── agents/
│   ├── lead_agent.py          # + research_goal() через /search
│   └── junior_agent.py        # + реальные tool calls через companion
├── core/
│   ├── orchestrator.py        # + SSE/WS события для UI
│   ├── protocol.py            # без изменений
│   └── ssh_bridge.py          # без изменений
├── tools/
│   ├── companion_client.py    # НОВЫЙ: httpx-обёртка над companion server
│   ├── file_tools.py          # оставить как fallback / локальные операции
│   ├── git_tools.py           # рефактор: вызывать companion_client.execute()
│   └── test_runner.py         # рефактор: вызывать companion_client.execute()
├── ui/
│   └── SlopLobster.html       # адаптированный под DualMind (Ollama + task UI)
├── SlopLobster-companion.py   # расширить: добавить /tasks /goal /approve /agents
├── config.yaml.example        # + секция companion_server
├── main.py                    # + запуск companion server в фоне
└── requirements.txt           # + playwright, sentence-transformers
```

---

## Шаг 1 — Создать `tools/companion_client.py`

Единый httpx-клиент для обращений к companion server.

```python
# tools/companion_client.py
import httpx
from typing import AsyncIterator

class CompanionClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765"):
        self.base = base_url

    async def execute(self, command: str, cwd: str = ".") -> dict:
        """Shell command. Returns {stdout, stderr, exit_code}."""
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{self.base}/execute", json={"command": command, "cwd": cwd})
            return r.json()

    async def search(self, query: str, num_results: int = 5) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base}/search", json={"query": query, "num_results": num_results})
            return r.json().get("results", [])

    async def fetch(self, url: str) -> str:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base}/fetch", json={"url": url})
            return r.json().get("content", "")

    async def ast_signatures(self, source: str, language: str) -> str:
        # source = file content as string (NOT a path)
        # language = 'py', 'js', 'ts', 'rs', 'go', etc.
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base}/ast_signatures", json={"source": source, "language": language})
            return r.json().get("outline", "")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{self.base}/embed", json={"texts": texts})
            return r.json().get("embeddings", [])
```

---

## Шаг 2 — Рефакторинг `tools/git_tools.py`

Заменить `subprocess.run` на `companion_client.execute()`.

```python
# Было:
def _run(cmd: list[str], cwd: str) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    ...

# Стало: async, через companion
async def get_diff(companion: CompanionClient, repo_path: str, branch: str) -> str:
    result = await companion.execute(f"git diff main {branch}", cwd=repo_path)
    return result["stdout"]

async def create_branch(companion: CompanionClient, repo_path: str, branch_name: str) -> str:
    result = await companion.execute(f"git checkout -b {branch_name}", cwd=repo_path)
    return result["stdout"]

async def commit_changes(companion: CompanionClient, repo_path: str, message: str) -> str:
    result = await companion.execute(f'git commit -m "{message}"', cwd=repo_path)
    return result["stdout"]
```

Плюсы: git-команды выполняются через companion на машине Junior, 
вывод стримится в UI, нет дублирования subprocess-кода.

---

## Шаг 3 — Рефакторинг `tools/test_runner.py`

```python
# Стало: async, через companion
async def run_checks(companion: CompanionClient, sandbox_dir: str) -> dict:
    tests_out = await companion.execute("python -m pytest --tb=short -q", cwd=sandbox_dir)
    lint_out  = await companion.execute("ruff check .", cwd=sandbox_dir)
    type_out  = await companion.execute("mypy . --ignore-missing-imports", cwd=sandbox_dir)
    return {
        "tests_passed": tests_out["exit_code"] == 0,
        "lint_passed":  lint_out["exit_code"] == 0,
        "typecheck_passed": type_out["exit_code"] == 0,
        "summary": tests_out["stdout"][:1000],
    }
```

---

## Шаг 4 — Lead Agent: добавить research через `/search`

```python
# agents/lead_agent.py — новый метод:
async def research_goal(self, goal: str) -> str:
    """Ищет контекст в интернете перед декомпозицией."""
    results = await self.companion.search(goal, num_results=3)
    return "\n".join(f"- {r['title']}: {r['snippet']}" for r in results)

# decompose_next_goal():
research = await self.research_goal(goal)
prompt = f"Research context:\n{research}\n\nGoal: {goal}"
raw = await self._chat(prompt, system=system)
```

---

## Шаг 5 — Junior Agent: реальные tool calls через companion

Сейчас Junior получает ответ LLM как текст и ничего с ним не делает.
Нужно дать LLM инструменты в формате Ollama tool use:

```python
# agents/junior_agent.py
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a file",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write content to a file",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}
        }, "required": ["path", "content"]}
    }},
    {"type": "function", "function": {
        "name": "execute", "description": "Run a shell command",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}, "cwd": {"type": "string"}
        }, "required": ["command"]}
    }},
]

# Tool dispatcher:
async def _dispatch_tool(self, name: str, args: dict) -> str:
    if name == "read_file":
        return file_tools.read_file(args["path"])
    if name == "write_file":
        file_tools.write_file(args["path"], args["content"])
        return f"Written: {args['path']}"
    if name == "execute":
        result = await self.companion.execute(args["command"], args.get("cwd", self.sandbox_dir))
        return result["stdout"] + result["stderr"]
```

---

## Шаг 6 — Расширить `SlopLobster-companion.py`

Добавить DualMind-специфичные эндпоинты (в конце файла, новый раздел):

```python
# --- DualMind endpoints ---

import json
from pathlib import Path

TASK_DIR = Path("./queue/tasks")

@app.route("/tasks", methods=["GET"])
def get_tasks():
    """Вернуть все задачи из очереди."""
    tasks = {}
    for status in ("todo", "in_progress", "done"):
        folder = TASK_DIR / status
        folder.mkdir(parents=True, exist_ok=True)
        tasks[status] = [json.loads(f.read_text()) for f in folder.glob("*.json")]
    return jsonify(tasks)

@app.route("/goal", methods=["POST"])
def add_goal():
    """Добавить цель (human → Lead)."""
    goal = request.json.get("goal", "").strip()
    goals_file = Path("./queue/goals.txt")
    with goals_file.open("a") as f:
        f.write(goal + "\n")
    return jsonify({"ok": True, "goal": goal})

@app.route("/approve/<task_id>", methods=["POST"])
def approve_task(task_id: str):
    """Человек одобряет задачу."""
    flag = TASK_DIR / "done" / f"{task_id}.approved"
    flag.touch()
    return jsonify({"ok": True, "task_id": task_id})

@app.route("/reject/<task_id>", methods=["POST"])
def reject_task(task_id: str):
    data = request.json or {}
    flag = TASK_DIR / "done" / f"{task_id}.rejected"
    flag.write_text(data.get("reason", ""))
    return jsonify({"ok": True, "task_id": task_id})

@app.route("/agents/status", methods=["GET"])
def agents_status():
    status_file = Path("./queue/agents_status.json")
    if status_file.exists():
        return jsonify(json.loads(status_file.read_text()))
    return jsonify({"lead": "idle", "junior": "idle"})
```

---

## Шаг 7 — Адаптировать `SlopLobster.html` для DualMind

### 7.1 Изменить LLM endpoint: LM Studio → Ollama

Найти в HTML:
```javascript
// LM Studio format:
const API_URL = "http://localhost:1234/v1/chat/completions";
```
Заменить на:
```javascript
// Ollama OpenAI-compatible:
const API_URL = "http://localhost:11434/v1/chat/completions";
// или если использовать Ollama native API:
const API_URL = "http://localhost:11434/api/chat";
```

> Ollama поддерживает OpenAI-совместимый формат `/v1/chat/completions` — 
> менять логику парсинга не нужно, только URL.

### 7.2 Добавить DualMind-панель

В HTML уже есть `.swarm-panel` (multi-agent). Нужно добавить:

```html
<!-- DualMind Task Queue Panel -->
<div class="dualmind-panel" id="dualmind-panel">
  <div class="panel-header">
    <span>DualMind</span>
    <button onclick="loadTasks()">↻</button>
  </div>
  
  <!-- Goal input -->
  <div class="goal-input">
    <input id="goal-input" placeholder="Describe your goal..." />
    <button onclick="submitGoal()">Send to Lead</button>
  </div>
  
  <!-- Task queue -->
  <div id="task-list">
    <!-- Populated by loadTasks() -->
  </div>
</div>

<script>
const COMPANION = "http://127.0.0.1:8765";

async function loadTasks() {
  const r = await fetch(`${COMPANION}/tasks`);
  const data = await r.json();
  renderTasks(data);
}

async function submitGoal() {
  const goal = document.getElementById("goal-input").value.trim();
  if (!goal) return;
  await fetch(`${COMPANION}/goal`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({goal})
  });
}

async function approveTask(taskId) {
  await fetch(`${COMPANION}/approve/${taskId}`, {method: "POST"});
  loadTasks();
}

async function rejectTask(taskId, reason) {
  await fetch(`${COMPANION}/reject/${taskId}`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({reason})
  });
  loadTasks();
}

function renderTasks(data) {
  const container = document.getElementById("task-list");
  // Render todo/in_progress/done with approve/reject buttons
  // Use existing .diff-approval-bar pattern from SlopLobster
}
</script>
```

---

## Шаг 8 — Обновить `config.yaml.example`

```yaml
# Добавить секцию:
companion_server:
  lead_url: http://localhost:8765      # на машине Lead
  junior_url: http://192.168.1.100:8765  # на машине Junior (через SSH-туннель)
  port: 8765
```

---

## Шаг 9 — Обновить `main.py`

Запускать companion server автоматически при старте:

```python
import subprocess, sys

async def main():
    # Запустить companion server в фоне
    companion_proc = subprocess.Popen(
        [sys.executable, "SlopLobster-companion.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        logger.info("Companion server started on :8765")
        config = load_config()
        orchestrator = Orchestrator(config)
        await orchestrator.run()
    finally:
        companion_proc.terminate()
```

---

## Шаг 10 — Обновить `requirements.txt`

```
# Добавить:
playwright>=1.44          # browser automation (companion)
sentence-transformers>=3  # embeddings (companion)
flask>=3.0                # companion server HTTP
```

---

## Порядок реализации

```
1. tools/companion_client.py              ← новый файл, ~60 строк
2. tools/git_tools.py рефактор           ← async + companion
3. tools/test_runner.py рефактор         ← async + companion
4. SlopLobster-companion.py расширение   ← добавить /tasks /goal /approve
5. agents/junior_agent.py tool calls     ← главная задача (см. work.md #1)
6. agents/lead_agent.py парсинг + search ← (см. work.md #2 + #4)
7. SlopLobster.html адаптация            ← URL + DualMind-панель
8. config.yaml.example обновить
9. main.py запуск companion
10. Тесты
```

---

## Что уже готово в SlopLobster и идёт без изменений

- Весь companion server (shell, git exec, web search, browser, AST, embeddings)
- HTML UI: чат, файловый дерево, диффы, терминал, git-панель
- Diff viewer с approve/reject кнопками (`.diff-approval-bar`)  
- Multi-agent swarm panel — переименовать в Lead/Junior статусы
- Streaming LLM responses
- Context window meter
