# 🧠 DualMind Dev

> A hierarchical multi-agent local LLM development system where two AI agents collaborate over a structured task protocol — a **Lead Agent** (senior, high-end GPU) and a **Junior Agent** (executor, secondary machine) — exchanging tasks, diffs, progress reports, and test results to autonomously build software projects with human oversight.

---

## Architecture

```mermaid
graph TD
    H["👤 Human Oversight\n(approve commits / merges)"]

    H -->|"approves / rejects"| LA

    subgraph Lead["🖥️ Lead Agent — RTX 3090"]
        LA["Lead Agent\n• Project planning\n• Task decomposition\n• Code review\n• Architecture decisions"]
    end

    subgraph Junior["🖥️ Junior Agent — RTX 3060"]
        JA["Junior Agent\n• Executes scoped tasks\n• 1–3 file changes max\n• Runs tests & linter\n• Returns patch report"]
    end

    LA -->|"task (JSON)\nscoped task with constraints"| JA
    JA -->|"progress\nmid-task status update"| LA
    JA -->|"patch_report\ndiff + test results + risks"| LA
    LA -->|"review_result\napprove / reject + feedback"| JA

    subgraph Tools["🛠️ Tools Layer"]
        GT["git_tools.py\nSafe git operations"]
        FT["file_tools.py\nRead / Write / Diff"]
        TR["test_runner.py\npytest · ruff · mypy"]
    end

    JA --> GT
    JA --> FT
    JA --> TR

    subgraph Transport["🔗 Transport"]
        SSH["SSH Bridge\nparamiko"]
        ORC["Orchestrator\nTask queue & coordination"]
    end

    LA <--> SSH
    JA <--> SSH
    ORC --> LA
    ORC --> JA

    subgraph Queue["📂 Task Queue"]
        TODO["queue/tasks/todo/"]
        WIP["queue/tasks/in_progress/"]
        DONE["queue/tasks/done/"]
    end

    ORC --> TODO
    TODO -->|"picked up"| WIP
    WIP -->|"completed"| DONE
```

## Communication Protocol

All inter-agent messages are typed JSON objects:

| Message Type     | Direction          | Description                        |
|------------------|--------------------|-------------------------------------|
| `task`           | Lead → Junior      | Scoped task with constraints        |
| `progress`       | Junior → Lead      | Mid-task status update              |
| `patch_report`   | Junior → Lead      | Diff + test results + risks         |
| `review_result`  | Lead → Junior      | Approve / reject with feedback      |

---

## Project Structure

```
dualmind-dev/
├── agents/
│   ├── lead_agent.py        # Lead agent logic
│   └── junior_agent.py      # Junior agent logic
├── core/
│   ├── orchestrator.py      # Task queue & coordination
│   ├── protocol.py          # Message schemas (Pydantic)
│   └── ssh_bridge.py        # SSH transport layer
├── tools/
│   ├── git_tools.py         # Safe git operations
│   ├── file_tools.py        # Read/write/diff helpers
│   └── test_runner.py       # pytest / ruff / mypy runner
├── queue/
│   └── tasks/               # JSON task files (todo/in_progress/done)
├── config.yaml              # Machine addresses, model endpoints
├── main.py                  # Entry point
├── requirements.txt
└── README.md
```

---

## Quickstart

```bash
# 1. Clone and install
git clone https://github.com/Totsamuychel/DualMind-Dev
cd DualMind-Dev
pip install -r requirements.txt

# 2. Configure machines
cp config.yaml.example config.yaml
# Edit config.yaml with your machine IPs and model endpoints

# 3. Run
python main.py
```

## Safety Rules

- ✅ Junior agent works **only in feature branches**, never main
- ✅ All commits require **human approval** before merge
- ✅ Junior agent has **read-only SSH** except for its sandbox dir
- ✅ No autonomous `git push` to main or `git merge`
- ✅ Every change validated by tests + linter before reporting back

---

## Stack

- **Models**: Any Ollama-compatible local LLM (Qwen2.5-Coder, DeepSeek-Coder, etc.)
- **Transport**: SSH via `paramiko`
- **Schema**: Pydantic v2
- **Git ops**: GitPython
- **Tests**: pytest + ruff + mypy
