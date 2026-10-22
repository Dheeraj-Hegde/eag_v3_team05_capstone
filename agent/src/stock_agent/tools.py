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

from stock_agent.balance import (
    STOCK_BALANCE_DESCRIPTION,
    STOCK_BALANCE_SCHEMA,
    STOCK_BALANCE_TOOL_NAME,
)

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
# agent-admin ones are irrelevant to stock work and cost tokens on every call.
# This only trims the model's view — verifiers call MCP directly.
LLM_ENTITIES: frozenset[str] = frozenset(
    {
        "Item",
        "StockEntry",
        "StockLedger",
        "Warehouse",
        "Party",
        "Company",
    }
)

MAX_DESCRIPTION_CHARS = 160
MAX_PROPERTY_DESCRIPTION_CHARS = 80

# `.get` tools the model doesn't need (the matching `.list` with `search` covers them).
HIDDEN_LLM_TOOLS: frozenset[str] = frozenset(
    {"Company.get", "Party.get", "Warehouse.get", "StockLedger.get"}
)

# Fields kept in `.list` results shown to the model (rows otherwise carry 10-40
# columns; an Item row is ~1.1 kB). Entities not listed keep every field.
LIST_RESULT_FIELDS: dict[str, frozenset[str]] = {
    "Item": frozenset({"id", "code", "name", "uom", "reorder_level", "reorder_qty", "safety_stock"}),
    "StockLedger": frozenset(
        {"item_id", "warehouse_id", "qty_change", "balance_qty", "voucher_type", "posting_date"}
    ),
    "StockEntry": frozenset({"id", "number", "type", "date", "status", "items", "total_value"}),
    "Warehouse": frozenset({"id", "name", "type", "company_id"}),
    "Party": frozenset({"id", "name", "type"}),
    "Company": frozenset({"id", "name", "default_currency", "country"}),
}

# Update tools accept every column; only these are shown for Item.update.
UPDATE_TOOL_PROPERTIES: dict[str, frozenset[str]] = {
    "Item.update": frozenset(
        {"id", "name", "status", "reorder_level", "reorder_qty", "safety_stock", "lead_time_days", "max_stock_qty"}
    ),
}

# Arguments never shown to (and stripped from) the model. A model-invented
# back-date makes the server check availability in the past ("Available=0").
HIDDEN_ARGUMENTS: dict[str, frozenset[str]] = {
    "StockEntry.create": frozenset({"date"}),
    "StockEntry.update": frozenset({"date"}),
}

# List tools accept every column as a filter (~40 props on Item.list). Only
# these are shown to the model; paging/sort/search plus the useful filters.
LIST_TOOL_PROPERTIES: frozenset[str] = frozenset(
    {
        "limit",
        "offset",
        "sort_by",
        "sort_order",
        "search",
        "type",
        "status",
        "name",
        "code",
        "item_id",
        "warehouse_id",
        "company_id",
        "party_id",
        "voucher_type",
        "voucher_id",
        "is_stock_item",
        "item_group",
    }
)

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

# Validation-only keywords: the server validates anyway, the model doesn't need them.
_DROP_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "$id",
        "default",
        "format",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "examples",
        "title",
        "additionalProperties",
    }
)


def is_allowed(name: str) -> bool:
    return bool(READ_TOOL.search(name)) or name in WRITE_ALLOWLIST or name == STOCK_BALANCE_TOOL_NAME


def is_action(name: str) -> bool:
    """A write this seat may perform, or an off-seat escalation."""
    return name in WRITE_ALLOWLIST or name == ESCALATION_TOOL_NAME


def entity_of(name: str) -> str:
    return name.split(".", 1)[0]


def to_llm_name(mcp_name: str) -> str:
    return mcp_name.replace(".", "__")


def from_llm_name(llm_name: str, name_map: dict[str, str] | None = None) -> str:
    if name_map and llm_name in name_map:
        return name_map[llm_name]
    return llm_name.replace("__", ".")


def _clean_schema(node: Any) -> Any:
    """Drop validation keywords and shorten descriptions, at any depth."""
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in _DROP_SCHEMA_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            # Keys here are property names, not keywords — never filter them.
            out[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
        elif k == "description" and isinstance(v, str):
            out[k] = v[:MAX_PROPERTY_DESCRIPTION_CHARS]
        else:
            out[k] = _clean_schema(v)
    return out


def _slim_list_schema(schema: dict[str, Any], keep: frozenset[str] = LIST_TOOL_PROPERTIES) -> dict[str, Any]:
    props = schema.get("properties") or {}
    kept = {k: v for k, v in props.items() if k in keep}
    out = dict(schema, properties=kept)
    if isinstance(schema.get("required"), list):
        out["required"] = [r for r in schema["required"] if r in kept]
    return out


def project_list_result(mcp_name: str, payload: Any) -> Any:
    """Keep only LIST_RESULT_FIELDS for rows of a `.list` result."""
    fields = LIST_RESULT_FIELDS.get(entity_of(mcp_name))
    if not fields or not mcp_name.endswith(".list") or not isinstance(payload, dict):
        return payload
    rows = payload.get("data")
    if not isinstance(rows, list):
        return payload
    slim = [{k: v for k, v in r.items() if k in fields} if isinstance(r, dict) else r for r in rows]
    return {k: v for k, v in payload.items() if k in ("total", "limit", "offset")} | {"data": slim}


def _function_tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    if schema and name.endswith(".list"):
        schema = _slim_list_schema(schema)
    elif schema and name in UPDATE_TOOL_PROPERTIES:
        schema = _slim_list_schema(schema, UPDATE_TOOL_PROPERTIES[name])
    if schema and name in HIDDEN_ARGUMENTS:
        props = schema.get("properties") or {}
        schema = _slim_list_schema(schema, frozenset(props) - HIDDEN_ARGUMENTS[name])
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
        if entities is not None and name in HIDDEN_LLM_TOOLS:
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
    if entities is None or "Item" in entities:
        tools.append(_function_tool(STOCK_BALANCE_TOOL_NAME, STOCK_BALANCE_DESCRIPTION, STOCK_BALANCE_SCHEMA))
        name_map[to_llm_name(STOCK_BALANCE_TOOL_NAME)] = STOCK_BALANCE_TOOL_NAME
    return tools, name_map
