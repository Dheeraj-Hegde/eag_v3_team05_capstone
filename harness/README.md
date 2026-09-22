# stock-harness

Runner + verifiers + scorer for the Stock agent.

## Prerequisites

The `agent/` package must be reachable at `../agent`. This project imports `stock_agent`.

## Quickstart

```powershell
uv sync
uv run harness list-tasks
uv run harness run tasks/T01_shortage_transfer.yaml
uv run harness run-all
uv run harness score
```

Each run writes `runs/<task_id>/<run_id>/result.json` **before** the verifier runs. This is the reproducibility guarantee the brief demands.

## Verifiers

Every verifier is a function in `src/harness/verifiers.py` that re-reads AgentSwitch state and returns `(passed, reason, evidence)`. **The agent's prose is not evaluated** — only the database is.

Current library:

- `refusal_declared` — passes iff the agent's final message begins with `REFUSE:` and mentions at least one term from `must_mention`.
- `stock_entry_exists` — calls `StockEntry.list` with the supplied filters; passes iff at least one hit matches `allowed_states`.
- `no_writes_performed` — passes iff the run's trace shows zero MCP write calls (used for pure-analysis tasks).
- `escalation_recorded` — passes iff the run recorded at least one `escalation.draft` for the target entity.

Add more by editing the file. The tests guide under `../tests/` explains how.
