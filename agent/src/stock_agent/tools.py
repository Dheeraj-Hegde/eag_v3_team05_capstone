"""Translate MCP tool descriptors to GLC ToolDefs and apply the seat policy.

Reads are open. Writes are allowlisted. Off-seat writes cannot be called
because the tool is not in tools/list on this seat — but the agent might still
try to invoke a fictional one, so we also inject a synthetic `escalation.draft`
tool that captures those intents without hitting MCP.
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

ESCALATION_TOOL_NAME = "escalation.draft"

ESCALATION_TOOL: dict[str, Any] = {
    "name": ESCALATION_TOOL_NAME,
    "description": (
        "Record an off-seat write intent as an escalation for another team, "
        "without calling AgentSwitch. Use this whenever the correct action is "
        "a write against MaterialRequest, PurchaseOrder, StockReconciliation, "
        "Bin, BOM, or anything else not in your write allowlist."
    ),
    "input_schema": {
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
                "enum": ["create", "update", "submit", "cancel"],
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
        "additionalProperties": False,
    },
}


def is_allowed(name: str) -> bool:
    return bool(READ_TOOL.search(name)) or name in WRITE_ALLOWLIST


def mcp_tools_to_glc(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in mcp_tools:
        name = t.get("name", "")
        if not is_allowed(name):
            continue
        out.append(
            {
                "name": name,
                "description": t.get("description", ""),
                "input_schema": t.get("inputSchema") or t.get("input_schema") or {},
            }
        )
    out.append(ESCALATION_TOOL)
    return out
