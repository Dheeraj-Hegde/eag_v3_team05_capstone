# stock-agent — Seat 05

Stock agent for Team 05 (Suryodaya Precision Works). Drives the AgentSwitch MCP surface via Gemini, routed through the local `glc_v5` gateway.

## Prerequisites

1. `glc_v5` gateway running on `http://127.0.0.1:8111` — see `../../glc_v5/README.md`.
2. A fresh AgentSwitch bearer token in `AS_TOKEN`. Tokens expire; re-login when a call returns 401.

## Quickstart

```powershell
uv sync
Copy-Item .env.example .env    # then fill AS_TOKEN
uv run stock-agent list-tools  # sanity check: should print your ~211 allowed tools
uv run stock-agent run --prompt "List the top 5 items missing a reorder level."
```

Every run writes `traces/<run_id>/{trace.jsonl, meta.json}` before returning.

## Policy

- **Reads:** open across `Item`, `StockEntry`, `Warehouse`, and the CRM party spine.
- **Writes:** allowlisted to `StockEntry.{create,submit,cancel_draft,update}`, `Item.update`, `Warehouse.{create,update}`. Every other write must surface as a synthetic `escalation.draft` tool call — the runner records these; nothing hits MCP.
- **Refusals:** when the agent cannot answer inside its seat, its final message starts with `REFUSE:` and names the boundary. Verifiers key on this.
- **Concurrency:** other seats may write to the same tenant (§3 of the brief). The system prompt requires a re-read before any write.
