# stock-agent — Seat 05

Stock agent for Team 05 (Suryodaya Precision Works). Drives the AgentSwitch MCP surface via any OpenAI-compatible model with tool calling (the `openai` package). On the AgentSwitch harness server that is the platform model injected as `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`.

## Prerequisites

1. A fresh AgentSwitch bearer token in `AGENTSWITCH_TOKEN` (or `AS_TOKEN` as a local fallback). Tokens expire; re-login when a call returns 401.
2. An OpenAI-compatible endpoint in `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`. Locally, Gemini's `https://generativelanguage.googleapis.com/v1beta/openai/` works.

`.env` is optional and only for local use — the server provides the variables directly.

## Quickstart

```powershell
uv sync
Copy-Item .env.example .env    # then fill AGENTSWITCH_TOKEN and OPENAI_*
uv run stock-agent ping        # MCP handshake + one tiny model call
uv run stock-agent list-tools  # 211 raw → 115 seat-allowed → 24 shown to the model
uv run stock-agent run --prompt "List the top 5 items missing a reorder level."
```

Every run writes `traces/<run_id>/{trace.jsonl, meta.json}` before returning.

## Policy

- **Reads:** open across the seat; the model sees only inventory entities (`LLM_ENTITIES` in `src/stock_agent/tools.py`: `Item`, `StockEntry`, `StockLedger`, `StockReservation`, `Warehouse`, `InventoryPreferences`, `Party`, `Company`).
- **Writes:** allowlisted to `StockEntry.{create,submit,cancel_draft,update}`, `Item.update`, `Warehouse.{create,update}`. Every other write must surface as a synthetic `escalation.draft` tool call — the runner records these; nothing hits MCP. Off-allowlist attempts are blocked and recorded as `blocked_calls`.
- **Tool names:** OpenAI function names can't contain `.`, so `StockEntry.create` is exposed as `StockEntry__create` and mapped back before dispatch.
- **Refusals:** when the agent cannot answer inside its seat, its final message starts with `REFUSE:` and names the boundary. Escalating is not refusing. Verifiers key on this.
- **Concurrency:** other seats may write to the same tenant (§3 of the brief). The system prompt requires a re-read before any write.
