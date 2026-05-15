# DualMind Dev — Что нужно доделать

Скелет проекта готов: протоколы (Pydantic), SSH-бридж, инструменты (git, файлы, тесты), оркестратор и оба агента. Ниже — всё расписано по приоритету.

---

## ✅ Выполнено (merge-plan шаги 1–9, Критично 1–4)

| # | Что | Файл |
|---|-----|------|
| ✅ | `companion_client.py` — async httpx обёртка над companion server | `tools/companion_client.py` |
| ✅ | `git_tools.py` — рефакторинг: async через companion, `commit_changes`, `get_log`, `current_branch` | `tools/git_tools.py` |
| ✅ | `test_runner.py` — рефакторинг: `CheckResults`, async через companion | `tools/test_runner.py` |
| ✅ | Companion server расширен: `/goal`, `/tasks`, `/approve/<id>`, `/reject/<id>`, `/agents/status` | `SlopLobster-companion.py` |
| ✅ | Junior Agent: tool loop (read_file / write_file / execute), stage+commit, quality checks | `agents/junior_agent.py` |
| ✅ | Lead Agent: `_parse_tasks`, `_parse_review`, `_research` через companion, `load_goals_from_file` | `agents/lead_agent.py` |
| ✅ | HTML UI: DualMind вкладка, polling агентов, кнопки approve/reject, `.dm-*` CSS | `ui/SlopLobster.html` |
| ✅ | `config.yaml.example` обновлён: companion_server, logging, UI-секция | `config.yaml.example` |
| ✅ | `main.py`: `validate_config`, `setup_logging`, autostart companion, graceful shutdown | `main.py` |
| ✅ | **Персистентная очередь** — recovery подхватывает TODO и IN_PROGRESS | `core/orchestrator.py` |
| ✅ | **Human approval** — оркестратор поллит файлы-сентинели | `core/orchestrator.py` |
| ✅ | **Retry-логика** — Junior получает фидбек Lead при повторе | `core/orchestrator.py` |
| ✅ | **SSH-туннели** — автоматический проброс портов companion/model | `core/ssh_bridge.py`, `agents/junior_agent.py` |

---

## Важно — система работает, но неполноценно

### 5. `repository.path` из конфига нигде не используется
`repository.path` задан в конфиге, но ни агент, ни оркестратор его не читают. Нужно решить, где он применяется (Lead-машина? Junior?), и прокинуть в `git_tools`.

### 6. `ProgressReport` нигде не отправляется
`core/protocol.py` — класс определён, но Junior не шлёт промежуточные апдейты Lead. Добавить отправку через companion после каждого значимого шага в tool loop.

---

## RAG через Qdrant

Векторная память для агентов: Junior читает файлы релевантные задаче без лишних tool-итераций, Lead не декомпозирует то, что уже было сделано.

### Шаг R1. Инфраструктура — `tools/rag_store.py`

Создать обёртку над `qdrant-client`:

```python
class RAGStore:
    async def ensure_collection(self, name, dim=1024)
    async def upsert(self, companion, texts, payloads, collection)
    async def search(self, companion, query, top_k=5, collection) -> list[dict]
    async def delete_collection(self, name)
```

- `embed()` делегируется `companion.embed(texts)` (companion уже имеет этот эндпоинт).
- Коллекции: `codebase`, `task_history`, `error_patterns`.
- Конфиг: добавить секцию `qdrant: {url: http://localhost:6333}` в `config.yaml.example`.

---

### Шаг R2. Индексация кодовой базы

**Когда:** при старте `main.py`, после запуска companion.  
**Что:** рекурсивно обойти `repository.path` по `*.py` файлам, разбить на чанки по функциям/классам (`ast_signatures` через companion), векторизовать.

```python
# tools/indexer.py
async def index_codebase(rag: RAGStore, companion, repo_path: str) -> int:
    """Returns number of chunks indexed."""
```

Payload каждого чанка: `{file, start_line, end_line, text, language}`.

---

### Шаг R3. RAG-контекст в промпт Junior

В `JuniorAgent.execute_task()` перед `_build_messages()`:

```python
chunks = await rag.search(companion, task.description, top_k=5, collection="codebase")
context = "\n\n".join(c["text"] for c in chunks)
# добавить context в system prompt как "Relevant existing code:"
```

Эффект: Junior видит похожие функции до того, как начинает читать файлы через tool calls → меньше итераций, меньше дублирования.

---

### Шаг R4. Память задач для Lead

После завершения каждой задачи (approve) — векторизовать и сохранить:
```python
payload = {
    "task_id": task.id,
    "title": task.title,
    "description": task.description,
    "outcome": "approved",
    "files_changed": patch.files_changed,
}
await rag.upsert(companion, [task.description], [payload], collection="task_history")
```

В `LeadAgent.decompose_next_goal()` — искать похожие задачи и добавлять в промпт:
```
Similar past tasks:
- "Add commit_changes to git_tools" → approved, changed tools/git_tools.py
```

---

### Шаг R5. Память ошибок

Если `patch.test_results.passed == False` → сохранить в `error_patterns`:
```python
payload = {
    "task_id": task.id,
    "error": checks.summary,
    "file": patch.files_changed,
}
```

Junior перед началом задачи ищет похожие ошибки → в промпт как "Known pitfalls:".

---

### Шаг R6. Переиндексация при изменениях

- После `commit_changes` в Junior — обновить только изменённые файлы в коллекции `codebase`.
- Не переиндексировать всё целиком при каждом запуске.
- Хранить хэш файла в payload — сравнивать при старте, обновлять только изменившиеся.

---

## Telegram бот

Управление системой и уведомления без HTML-интерфейса. Бот не трогает агентов напрямую — только companion HTTP API.

### Шаг T1. Инфраструктура — `tg_bot/bot.py`

- Библиотека: `aiogram 3.x` (async, современный API).
- Конфиг: добавить секцию в `config.yaml.example`:

```yaml
telegram:
  token: "BOT_TOKEN_HERE"
  allowed_users: [123456789]   # Telegram user IDs, кто может управлять
  companion_url: http://localhost:8765
```

- Запускать как отдельный процесс рядом с `main.py`, или интегрировать в asyncio event loop.
- `allowed_users" — middleware, отклоняющий сообщения от неизвестных пользователей.

---

### Шаг T2. Команды управления

| Команда | Действие |
|---------|---------|
| `/goal <текст>` | POST `/goal` → добавить цель в очередь |
| `/status` | GET `/agents/status` → Lead/Junior статус + текущая задача |
| `/tasks` | GET `/tasks` → список по статусам (todo/in_progress/done) |
| `/approve <task_id>` | POST `/approve/<id>` → одобрить патч |
| `/reject <task_id> <причина>` | POST `/reject/<id>` → отклонить с причиной |
| `/log [N]` | Последние N строк из `logs/dualmind.log` (дефолт 20) |
| `/help` | Список команд |

---

### Шаг T3. Inline кнопки для approve/reject

Когда задача переходит в статус `pending_approval` — бот автоматически отправляет сообщение:

```
Task #a3f2 ready for review
"Add commit_changes to git_tools"

Tests: ✅  Lint: ✅  Diff: 42 lines

[✅ Approve]  [❌ Reject]
```

- Кнопки через `InlineKeyboardMarkup` с `callback_data="approve:a3f2"`.
- После нажатия — редактировать сообщение (не слать новое).
- Companion server уведомляет бота: добавить POST `/notify` эндпоинт в companion, бот принимает его через `aiohttp` webhook или простой HTTP-сервер.

---

### Шаг T4. Push-уведомления

Добавить в companion server эндпоинт `POST /notify`:

```json
{"event": "task_started", "task_id": "a3f2", "title": "..."}
{"event": "task_ready", "task_id": "a3f2", "diff_lines": 42, "tests": true}
{"event": "task_approved", "task_id": "a3f2"}
{"event": "task_rejected", "task_id": "a3f2", "reason": "..."}
{"event": "system_idle", "duration_minutes": 10}
```

Бот подписывается на `/notify` (long poll или callback URL). Оркестратор вызывает companion при каждом переходе статуса.

---

### Шаг T5. Мониторинг и алерты

- Если Junior не шлёт прогресс более N минут (из конфига) → бот пишет "⚠️ Junior stuck on task #X".
- Если система idle более 30 мин → "💤 DualMind idle — no goals queued".
- `/log` команда показывает хвост лога с фильтром по уровню (ошибки красным).

---

## Тесты

`tools/test_runner.py` умеет запускать pytest, но в проекте нет ни одного `test_*.py`.

Минимум что нужно покрыть:
- `tests/test_protocol.py` — сериализация/десериализация всех Pydantic-моделей.
- `tests/test_git_tools.py` — `create_branch`, `get_diff`, `stage_changes`, `commit_changes` на временном репо с моком companion.
- `tests/test_file_tools.py` — `_write_remote` / `_read_remote` через мок companion.
- `tests/test_orchestrator.py` — approve/reject flow с замоканными агентами.
- `tests/test_rag_store.py" — upsert/search против локального Qdrant (integration).
- `tests/test_tg_bot.py" — обработчики команд с мок-companion (unit).

---

## Мелкие улучшения (nice to have)

| # | Что | Где |
|---|-----|-----|
| 1 | Стриминг ответов LLM (`"stream": True`) | `lead_agent._chat()`, `junior_agent._chat_with_tools()` |
| 2 | Обрезка промпта при большом диффе (сейчас `patch.diff[:4000]`) | `lead_agent.review_patch()` |
| 3 | `ruff format` в дополнение к `ruff check` | `tools/test_runner.py` |
| 4 | `.gitignore` расширить: `logs/`, `config.yaml`, `queue/tasks/done/`, `queue/tasks/in_progress/` | `.gitignore` |
| 5 | Веб-хук режим для Telegram бота вместо long polling | `tg_bot/bot.py` |
| 6 | Дашборд скорости: сколько задач/час, среднее время итерации | `ui/SlopLobster.html` |

---

## Порядок работы (рекомендуемая очерёдность)

```
── Стабилизация ядра ──────────────────────────────────────────────────
 1. Персистентная очередь в оркестраторе (#1)   ← DONE
 2. Human approval wait (#2)                    ← DONE
 3. Retry-логика (#3)                           ← DONE
 4. SSH-туннель (#4)                            ← DONE

── RAG ────────────────────────────────────────────────────────────────
 5. R1: RAGStore + Qdrant infra                 ← основа для всего RAG
 6. R2: индексация кодовой базы                 ← Junior читает меньше файлов
 7. R3: RAG-контекст в промпт Junior            ← меньше tool-итераций
 8. R4: память задач для Lead                   ← не повторять выполненное
 9. R5: память ошибок                           ← не наступать на те же грабли
10. R6: инкрементная переиндексация             ← производительность

── Telegram ───────────────────────────────────────────────────────────
11. T1: инфраструктура бота, allowed_users      ← скелет
12. T2: команды /goal /status /tasks /log       ← минимальный полезный бот
13. T3: inline кнопки approve/reject            ← удобный human-in-the-loop
14. T4: push-уведомления от companion           ← не надо поллить статус руками
15. T5: мониторинг и алерты о зависании        ← опциональный, но полезный

── Тесты ──────────────────────────────────────────────────────────────
16. test_protocol, test_git_tools, test_orchestrator
17. test_rag_store, test_tg_bot
```
