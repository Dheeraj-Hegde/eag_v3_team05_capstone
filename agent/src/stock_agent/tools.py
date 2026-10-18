"""Translate MCP tool descriptors to OpenAI function tools and apply the seat policy.

Reads are open. Writes are allowlisted. Off-seat writes cannot be called
because the tool is not in tools/list on this seat — but the agent might still
try to invoke a fictional one, so we also inject a synthetic `escalation.draft`
tool that captures those intents without hitting MCP.

OpenAI function names must match ^[a-zA-Z0-9_-]{1,64}$, so MCP names like
`StockEntry.create` are exposed to the model as `StockEntry__create` and mapped
back before dispatch.
"""

from __future__ import annotations

import re
from typing import Any

READ_TOOL = re.compile(r"\.(list|get|read|search|describe)$")

WRITE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "StockEntry.create",
        "StockEntry.submit",
        "StockEntry.cancel_draft",
        "StockEntry.update",
        "Item.update",
        "Warehouse.create",
        "Warehouse.update",
    }
)

# Entities exposed to the LLM. The seat sees ~115 allowed tools; the CRM and
# agent-admin ones are irrelevant to stock work and cost ~3x the prompt size.
# This only trims the model's view — verifiers call MCP directly.
LLM_ENTITIES: frozenset[str] = frozenset(
    {
        "Item",
        "StockEntry",
        "StockLedger",
        "StockReservation",
        "Warehouse",
        "InventoryPreferences",
        "Party",
        "Company",
    }
)

MAX_DESCRIPTION_CHARS = 300

ESCALATION_TOOL_NAME = "escalation.draft"

ESCALATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_seat": {
            "type": "string",
            "description": "Which seat should execute this (e.g. '01 Ledger', '04 Production').",
        },
        "entity": {
            "type": "string",
            "description": "The AgentSwitch entity (e.g. 'MaterialRequest', 'PurchaseOrder').",
        },
        "operation": {
            "type": "string",
            "enum": ["create", "update", "submit", "cancel", "delete"],
        },
        "payload": {
            "type": "object",
            "description": "Pre-filled request body the receiving seat should execute.",
        },
        "rationale": {
            "type": "string",
            "description": "One-line reason this escalation is necessary.",
        },
    },
    "required": ["target_seat", "entity", "operation", "payload", "rationale"],
}

ESCALATION_DESCRIPTION = (
    "Record an off-seat write intent as an escalation for another team, "
    "without calling AgentSwitch. Use this whenever the correct action is "
    "a write against MaterialRequest, PurchaseOrder, StockReconciliation, "
    "Bin, BOM, or anything else not in your write allowlist."
)

_DROP_SCHEMA_KEYS = frozenset({"$schema", "$id"})


def is_allowed(name: str) -> bool:
    return bool(READ_TOOL.search(name)) or name in WRITE_ALLOWLIST


def entity_of(name: str) -> str:
    return name.split(".", 1)[0]


def to_llm_name(mcp_name: str) -> str:
    return mcp_name.replace(".", "__")


def from_llm_name(llm_name: str, name_map: dict[str, str] | None = None) -> str:
    if name_map and llm_name in name_map:
        return name_map[llm_name]
    return llm_name.replace("__", ".")


def _clean_schema(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _clean_schema(v) for k, v in node.items() if k not in _DROP_SCHEMA_KEYS}
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node


def _function_tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    params = _clean_schema(schema) if schema else {"type": "object", "properties": {}}
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    return {
        "type": "function",
        "function": {
            "name": to_llm_name(name),
            "description": (description or name)[:MAX_DESCRIPTION_CHARS],
            "parameters": params,
        },
    }


def select_mcp_tools(
    mcp_tools: list[dict[str, Any]],
    *,
    entities: frozenset[str] | None = LLM_ENTITIES,
) -> list[dict[str, Any]]:
    """Seat-policy filter, then (optionally) the LLM entity trim."""
    out = []
    for t in mcp_tools:
        name = t.get("name", "")
        if not is_allowed(name):
            continue
        if entities is not None and entity_of(name) not in entities:
            continue
        out.append(t)
    return out


def mcp_tools_to_llm(
    mcp_tools: list[dict[str, Any]],
    *,
    entities: frozenset[str] | None = LLM_ENTITIES,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return (OpenAI tool list, llm_name -> mcp_name map). Always includes escalation.draft."""
    tools: list[dict[str, Any]] = []
    name_map: dict[str, str] = {}
    for t in select_mcp_tools(mcp_tools, entities=entities):
        name = t["name"]
        tools.append(
            _function_tool(
                name,
                t.get("description", ""),
                t.get("inputSchema") or t.get("input_schema") or {},
            )
        )
        name_map[to_llm_name(name)] = name
    tools.append(_function_tool(ESCALATION_TOOL_NAME, ESCALATION_DESCRIPTION, ESCALATION_SCHEMA))
    name_map[to_llm_name(ESCALATION_TOOL_NAME)] = ESCALATION_TOOL_NAME
    return tools, name_map
