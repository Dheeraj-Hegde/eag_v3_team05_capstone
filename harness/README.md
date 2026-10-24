# stock-harness

Runner + verifiers + scorer for the Stock agent.

## Prerequisites

The `agent/` package must be reachable at `../agent`. This project imports `stock_agent`. Environment variables are the same as the agent's (`AGENTSWITCH_*`, `OPENAI_*`).

## Official run (AgentSwitch server)

From the repo root, exactly what `agentswitch-harness.toml` runs:

```powershell
pip install -r requirements.txt
python -m harness                         # all tasks → results.json
python -m harness --only T02,T05          # subset
python -m harness --validate results.json # check the format before submitting
```

`python -m harness` always writes a valid `results.json` (rewritten after every task), keeps a 17-min budget with a 180 s per-task deadline (`HARNESS_BUDGET_S`, `HARNESS_TASK_DEADLINE_S`) and a 200k-token run budget with a 40k per-task cap (`HARNESS_TOKEN_BUDGET`, `HARNESS_TASK_TOKEN_BUDGET`; also `--token-budget`, `--task-token-budget`), turns crashes/config errors into `passed: false` with the reason as evidence, and exits 0. Tasks that can't get at least 10k tokens are skipped and marked failed.

## Local quickstart

```powershell
uv sync
uv run harness list-tasks
uv run harness run tasks/T01_shortage_transfer.yaml
uv run harness run-all
uv run harness score
```

Each run writes `runs/<task_id>/<run_id>/result.json` **before** anything else observes it. This is the reproducibility guarantee the brief demands.

## Verifiers

Every verifier is a function in `src/harness/verifiers.py` that re-reads AgentSwitch state (or the recorded trace) and returns `(passed, reason, evidence)`. **The agent's prose is not trusted for state claims.**

- `refusal_declared` — final message begins with `REFUSE:` and mentions at least one term from `must_mention`.
- `stock_entry_exists` — only StockEntries created by this run count; each is re-read via `StockEntry.get` and checked for `type`, `allowed_status`, and optional `require_transfer_legs`.
- `no_writes_performed` — zero MCP write calls in the trace.
- `no_blocked_calls` — zero off-allowlist tool attempts.
- `escalation_recorded` — at least one `escalation.draft` for the target entity (spelling variants like `Purchase Order` or `PO` count; failures report what was recorded, `refused` and the stop reason).
- `tool_called` — a named tool was called successfully.
- `answer_matches_ground_truth` — the final answer names the rows a live list call returns (e.g. every `Warehouse.list` name); case, typographic dashes and special spaces are ignored.
- `all_of` / `any_of` — composites: `spec.checks: [{fn, spec}, ...]`.

Tasks use `expected: produce | refuse | either`. Add verifiers by registering them in `REGISTRY`.
