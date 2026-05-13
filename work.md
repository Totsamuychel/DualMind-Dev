# DualMind Dev — Что нужно доделать

Скелет проекта готов: протоколы (Pydantic), SSH-бридж, инструменты (git, файлы, тесты), оркестратор и оба агента. Но ключевая бизнес-логика — заглушки. Ниже всё расписано по приоритету.

---

## Критично — без этого система не работает

### 1. Junior не применяет изменения к файлам
**Файл:** `agents/junior_agent.py:67` — `# TODO: wire LLM output to actual file edits via tool calls`

Junior отправляет промпт в LLM, получает ответ в виде текста и… ничего не делает с файлами. Это главная дыра.

Что нужно:
- Определиться со стратегией: либо **structured tool calls** (LLM возвращает JSON с вызовами `write_file`/`read_file`), либо парсинг **diff-блоков** из ответа LLM и применение через `file_tools.write_file`.
- Реализовать применение изменений в `execute_task()`.
- После изменений вызывать `git_tools.create_branch()` и `git_tools.stage_changes()` (сейчас они импортированы, но не вызываются).

---

### 2. Lead не парсит ответ LLM — всегда возвращает заглушку
**Файл:** `agents/lead_agent.py:58` — `# TODO: parse LLM response into Task objects`
**Файл:** `agents/lead_agent.py:97` — `# TODO: parse LLM response properly`

`decompose_next_goal()` всегда возвращает один stub-таск независимо от цели.  
`review_patch()` всегда возвращает `approved=True`.

Что нужно:
- Добавить парсинг JSON из ответа LLM (с fallback при невалидном JSON).
- `decompose_next_goal()` должен возвращать реальный список `Task` из ответа LLM.
- `review_patch()` должен парсить `{approved, feedback, requested_changes}` из ответа LLM.

---

### 3. Нет способа добавить цель — система стартует с пустым списком
**Файл:** `agents/lead_agent.py:24` — `self.goals: list[str] = []  # Populated by human or loaded from file`

Оркестратор вызывает `lead.decompose_next_goal()`, который возвращает `[]`, и система крутится вхолостую.

Что нужно (выбрать одно или оба):
- **Вариант А**: читать цели из файла `queue/goals.txt` (одна цель — одна строка).
- **Вариант Б**: интерактивный ввод в `main.py` — `input("Enter goal: ")` перед стартом.

---

### 4. Очередь задач только в памяти — нет персистентности
**Файл:** `core/orchestrator.py:21` — `self.task_queue: list[Task] = []`

Задачи живут только в RAM. При рестарте всё теряется. В `queue/tasks/` есть `.gitkeep`, но нет папок `todo/`, `in_progress/`, `done/` и логики для работы с ними.

Что нужно:
- Создать папки `queue/tasks/todo/`, `queue/tasks/in_progress/`, `queue/tasks/done/`.
- В оркестраторе: при создании таска — сохранять JSON-файл в `todo/`, при старте — перекладывать в `in_progress/`, при завершении — в `done/`.
- При старте оркестратора — подбирать незавершённые задачи из `in_progress/` (recovery после краша).

---

### 5. SSH в JuniorAgent подключается, но не используется
**Файл:** `agents/junior_agent.py:26-31`

`SSHBridge` создаётся в `__init__`, но `connect()` никогда не вызывается. `_chat()` обращается к `self.endpoint` напрямую через httpx — это работает только если порт 11434 открыт снаружи на машине Junior.

Что нужно:
- Открывать SSH-соединение при старте Junior (или через `__enter__`).
- Использовать SSH-туннель для проброса порта Ollama: `SSHBridge` уже умеет `run()` — добавить метод `open_tunnel(remote_port, local_port)` через `paramiko.Transport`.
- Либо явно задокументировать, что порт должен быть открыт (и закрыть `self.ssh` как неиспользуемый).

---

### 6. Junior никогда не делает git commit
**Файл:** `tools/git_tools.py` — есть `create_branch`, `stage_changes`, `get_diff`, но нет `commit_changes()`

Junior должен зафиксировать изменения в feature-ветке (не в main). Без коммита `get_diff(branch)` ничего не покажет.

Что нужно добавить в `git_tools.py`:
```python
def commit_changes(repo_path: str, message: str) -> str:
    return _run(["git", "commit", "-m", message], cwd=repo_path)
```
И вызывать его в `junior_agent.execute_task()` после `stage_changes()`.

---

## Важно — система работает, но неполноценно

### 7. ProgressReport нигде не отправляется
**Файл:** `core/protocol.py:42` — класс `ProgressReport` определён, но не используется нигде.

Junior должен слать промежуточные апдейты Lead во время выполнения задачи (особенно для долгих задач). Добавить отправку в `execute_task()` после каждого значимого шага.

---

### 8. Retry-логика не реализована
**Файл:** `config.yaml.example:24` — `max_retries: 2`, но в оркестраторе нет повторных попыток.

При `review.approved == False` задача просто помечается `REJECTED`. Нужно:
- Счётчик попыток в `Task` (или в оркестраторе).
- Повтор `execute_task()` с фидбеком Lead в промпте Junior (не более `max_retries` раз).
- Только после исчерпания попыток — `REJECTED`.

---

### 9. Логирование в файл не работает
**Файл:** `main.py:11`, `config.yaml.example:29` — `logging.file: ./logs/dualmind.log` есть в конфиге, но в `main.py` настроен только `StreamHandler`.

Что нужно:
- Читать `config["logging"]` в `main.py`.
- Добавлять `FileHandler` если задан путь к лог-файлу.
- Создавать директорию `logs/` если не существует.

---

### 10. Конфиг не валидируется
**Файл:** `main.py:18` — `yaml.safe_load(f)` без валидации.

При отсутствии обязательного ключа (например, `ssh_key`) будет `KeyError` в глубине кода. Нужно добавить Pydantic-схему для конфига или хотя бы `assert`-проверки ключей при старте.

---

### 11. `repository.path` из конфига нигде не используется
**Файл:** `config.yaml.example:18` — `repository.path` задан, но ни агент, ни оркестратор его не читают. `sandbox_dir` у Junior — отдельный параметр.

Нужно решить: это путь к репозиторию на машине Lead, или на Junior, или оба? И прокинуть в `git_tools` правильный путь.

---

### 12. Human approval блокирующий — но его нет
**Файл:** `core/orchestrator.py:55` — `_notify_human()` просто печатает в консоль и возвращается. Оркестратор сразу продолжает к следующей задаче.

Нужен механизм ожидания: либо `input("Approve? [y/n]: ")`, либо создание файла-флага `queue/tasks/done/<id>.approved`, который человек создаёт вручную.

---

## Нет тестов для самого проекта

**Файл:** `tools/test_runner.py` умеет запускать pytest, но в проекте нет ни одного `test_*.py`.

Минимум что нужно покрыть:
- `test_protocol.py` — сериализация/десериализация всех Pydantic-моделей.
- `test_git_tools.py` — `create_branch`, `get_diff`, `stage_changes` на временном репо.
- `test_file_tools.py` — `read_file`, `write_file`, `unified_diff`.
- `test_orchestrator.py` — логика approve/reject с замоканными агентами.

---

## Мелкие улучшения (nice to have)

| # | Что | Где |
|---|-----|-----|
| 1 | Стриминг ответов LLM (`"stream": True`) | `lead_agent._chat()`, `junior_agent._chat()` |
| 2 | Обрезка промпта при большом диффе (сейчас `patch.diff[:3000]` — хардкод) | `lead_agent.review_patch()` |
| 3 | Загрузка нескольких целей из файла за один запуск | `agents/lead_agent.py` |
| 4 | `ruff format` в дополнение к `ruff check` | `tools/test_runner.py` |
| 5 | Graceful shutdown по Ctrl+C в главном цикле | `main.py`, `core/orchestrator.py` |
| 6 | `.gitignore` расширить: `logs/`, `config.yaml`, `queue/tasks/done/`, `queue/tasks/in_progress/` | `.gitignore` |

---

## Порядок работы (рекомендуемая очерёдность)

```
1. Механизм ввода целей (#3)          ← без него не запустить
2. Парсинг LLM-ответов (#2)           ← без него Lead всегда stub
3. Применение изменений к файлам (#1) ← без него Junior ничего не делает
4. git commit в Junior (#6)           ← без него diff пустой
5. Персистентная очередь (#4)         ← надёжность
6. Human approval wait (#12)          ← безопасность
7. Retry-логика (#8)
8. Логирование в файл (#9)
9. Валидация конфига (#10)
10. Тесты (#13)
```
