# Route A · Week 1 — Stock (Seat 05)

Stock agent for Team 05 at Suryodaya Precision Works.

## First run

Prereqs: `uv` installed; `glc_v5` gateway running (`cd ..\..\glc_v5; uv run glc serve`); a fresh AgentSwitch bearer token.

```powershell
# 1. Agent — install and sanity-check the plumbing
cd agent
uv sync
Copy-Item .env.example .env   # then paste AS_TOKEN
uv run stock-agent ping
uv run stock-agent list-tools
uv run stock-agent run --prompt "List the top 3 items you can see."

# 2. Harness — run the seed tasks
cd ..\harness
uv sync
Copy-Item ..\agent\.env .env  # same token
uv run harness list-tasks
uv run harness run tasks\T01_shortage_transfer.yaml
uv run harness run tasks\T02_refusal_payroll.yaml
uv run harness run tasks\T03_item_hygiene.yaml
uv run harness score
```

## Policy summary (Seat 05)

- `allowed_apps`: `inventory`, `agent`, `crm`. 211 MCP tools scoped to this seat.
- Writes the agent may perform: `Item.update`, `StockEntry.{create,update,submit,cancel_draft}`, `Warehouse.{create,update}`.
- All other write operations surface as `escalation.draft` calls — recorded, not executed.
- Refusals begin with `REFUSE:` on the first line; verifiers read this.

## Gap report — what's in it

[`gap-report.md`](gap-report.md) benchmarks Seat 05 against Katana MRP (`katanamrp.com`, from $299/mo Core, AI-native, publishes its own MCP). Every claim is backed by MCP-side captures taken on 2026-09-21: `tools-list.json` (211 tools scoped to this seat), `schemas.json`, `auth-me.json` (`allowed_apps: [inventory, agent, crm]`), and a 0-byte `accounting-locale.json` recording a 403 on `GET /api/accounting/locale` — proof the accounting wall is real, not assumed.

Three findings drive Week 1 scope:

1. **Katana ships what we don't.** Omnichannel order sync, AI Replenishment, demand forecasting, multi-level BOMs, bin-level warehousing, batch/lot/serial traceability, landed cost, native QuickBooks/Xero posting, tariff management. Some are platform gaps (`LandedCost*`, `Forecast*`, `Bin` — absent from `schemas.json` entirely); others are seat walls (`BOM.*`, `Batch.*`, `SerialNo.*` exist on the platform but not on Seat 05's `tools/list`).
2. **The seat is thinner than the pitch.** Only four stock-adjacent entities carry write tools: `Item.*`, `StockEntry.*` (full lifecycle), `Warehouse.*`, plus the shared `crm` party spine. No `StockLedgerEntry` reader, no `MaterialRequest.*`, no `PurchaseOrder.*` — the reorder loop lives on Seats 01/03/04.
3. **Our edge is the plan across seats, not the write.** Given *"we are short a component next week"* the agent produces three artefacts from what it can see: a `StockEntry` transfer draft it can submit itself, a pre-filled `MaterialRequest` payload it escalates to Ledger, and a customer-facing note for the deferred order. Clean refusals with a cited 403 are the grading tell.

## The agent — [`agent/`](agent/)

A policy-constrained tool-calling loop that drives AgentSwitch MCP via Gemini, routed through the local `glc_v5` gateway. Runnable v0.

**Layout** ([`agent/src/stock_agent/`](agent/src/stock_agent/)):

| File | Role |
|---|---|
| [`agent.py`](agent/src/stock_agent/agent.py) | Main ReAct-style loop (`Agent.run`), config, `AgentResult` dataclass. |
| [`glc_client.py`](agent/src/stock_agent/glc_client.py) | HTTP client for `glc_v5`'s `POST /v1/chat`. |
| [`mcp_client.py`](agent/src/stock_agent/mcp_client.py) | JSON-RPC 2.0 client for AgentSwitch `/api/mcp` (protocol `2025-11-25`). |
| [`tools.py`](agent/src/stock_agent/tools.py) | Seat policy filter + synthetic `escalation.draft` tool injection. |
| [`system_prompt.md`](agent/src/stock_agent/system_prompt.md) | Seat identity, read-before-write rule, `REFUSE:` contract, no-invention rule. |
| [`trace.py`](agent/src/stock_agent/trace.py) | Per-run JSONL trace + `meta.json`, flushed on every event. |
| [`run.py`](agent/src/stock_agent/run.py) | Typer CLI: `ping`, `list-tools`, `run`. |

**How a run flows:**

1. `initialize` MCP, pull `tools/list` (~211 tools), filter through `mcp_tools_to_glc`.
2. Loop up to `max_steps=20`: send messages + system prompt + filtered tools to `glc_v5`; dispatch any tool calls; append results; iterate until the model returns no tool calls.
3. Every LLM request, tool call, MCP result, refusal, and escalation is written to `traces/<run_id>/trace.jsonl` **before** the loop returns — this is the audit substrate the harness verifiers read.

**Policy is enforced at the tool list, not by prompt trust:**

- Reads open — anything matching `.(list|get|read|search|describe)$` passes.
- Writes closed — only the 7-tool `WRITE_ALLOWLIST` in [`tools.py`](agent/src/stock_agent/tools.py) reaches MCP.
- Off-seat writes → a synthetic `escalation.draft` tool captures the intent as a structured payload (`target_seat`, `entity`, `operation`, `payload`, `rationale`) and never touches MCP.
- If the model tries an out-of-allowlist tool anyway, `_dispatch_tool_call` returns an error steering it toward `escalation.draft`.

## The harness — [`harness/`](harness/)

Task runner + database-reading verifiers + scorer. **The agent's prose is not evaluated — only the AgentSwitch state is.**

**Layout** ([`harness/src/harness/`](harness/src/harness/)):

| File | Role |
|---|---|
| [`cli.py`](harness/src/harness/cli.py) | Typer CLI: `list-tasks`, `run`, `run-all`, `score`. |
| [`runner.py`](harness/src/harness/runner.py) | Loads a task, invokes the agent, dispatches the verifier, writes `result.json`. |
| [`verifiers.py`](harness/src/harness/verifiers.py) | The four verifier functions + registry + dispatcher. |
| [`scorer.py`](harness/src/harness/scorer.py) | Walks `runs/`, renders the pass/fail table, sets the exit code. |
| [`models.py`](harness/src/harness/models.py) | Pydantic `Task`, `Verifier`, `TaskResult`. |

**Verifier library** (each returns `(passed, reason, evidence)` after re-reading MCP):

- `refusal_declared` — final text starts with `REFUSE:` and mentions required terms.
- `stock_entry_exists` — calls `StockEntry.list` with the task's filters and requires ≥1 match in `allowed_states`.
- `no_writes_performed` — inspects the recorded trace for any `.create/.update/.submit/.cancel_draft/.cancel/.delete` call.
- `escalation_recorded` — checks `AgentResult.escalations` for at least one draft against the target entity.

**Seed tasks** ([`harness/tasks/`](harness/tasks/)):

| Task | Expected | Verifier | Point |
|---|---|---|---|
| [`T01_shortage_transfer.yaml`](harness/tasks/T01_shortage_transfer.yaml) | `produce` | `stock_entry_exists` | Positive: draft a `type=transfer` `StockEntry` for a component below reorder level. |
| [`T02_refusal_payroll.yaml`](harness/tasks/T02_refusal_payroll.yaml) | `refuse` | `refusal_declared` | Refusal: payroll data lives on another seat. |
| [`T03_item_hygiene.yaml`](harness/tasks/T03_item_hygiene.yaml) | `produce` | (see task) | `Item.*` catalogue hygiene. |

**Reproducibility guarantee:** [`runner.py`](harness/src/harness/runner.py) writes `runs/<task_id>/<run_id>/result.json`, `task.yaml`, and `summary.json` to disk **before** the scorer or anything else observes them — brief §8.
