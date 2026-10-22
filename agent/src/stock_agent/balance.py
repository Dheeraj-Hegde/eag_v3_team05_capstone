"""Synthetic `stock.balance` read tool.

Answers "how much of item X is free in warehouse Y" in one model turn. It wraps
the platform's read-only `endpoint.inventory.stock_balance` (exact on_hand /
reserved / free per item-warehouse bin, the same numbers StockEntry.create
validates against), pages through it, and returns a compact table. One call
replaces 4-6 model round-trips over StockLedger/Item/Warehouse, each of which
would re-send the whole conversation.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

STOCK_BALANCE_TOOL_NAME = "stock.balance"
SOURCE_TOOL = "endpoint.inventory.stock_balance"

STOCK_BALANCE_DESCRIPTION = (
    "Current on-hand and FREE stock per item and warehouse (names + ids), plus every "
    "warehouse, in ONE call. Use it for any on-hand / shortage / surplus / lowest-stock / "
    "transfer question."
)

STOCK_BALANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "group_by": {
            "type": "string",
            "enum": ["item_warehouse", "item"],
            "description": "item_warehouse (default): one row per item per warehouse. item: totals per item.",
        },
        "item_id": {"type": "string", "description": "Only this item."},
        "warehouse_id": {"type": "string", "description": "Only this warehouse."},
        "search": {"type": "string", "description": "Match item name or code."},
        "below_reorder": {"type": "boolean", "description": "Only items whose total on-hand is below reorder_level."},
        "sort": {"type": "string", "enum": ["asc", "desc"], "description": "By free qty; default asc (lowest first)."},
        "limit": {"type": "integer", "description": "Rows to return, default 25, max 100."},
    },
    "required": [],
}

_PAGE = 100
_MAX_PAGES = 10

CallFn = Callable[[str, dict[str, Any]], Any]


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _qty(v: float) -> float | int:
    v = round(float(v), 3)
    return int(v) if v.is_integer() else v


def _bins(payload: Any) -> tuple[list[dict[str, Any]], Any]:
    body = payload.get("result", payload) if isinstance(payload, dict) else {}
    rows = body.get("rows") if isinstance(body, dict) else None
    return ([r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []), (
        body.get("total") if isinstance(body, dict) else None
    )


def stock_balance(call: CallFn, args: dict[str, Any]) -> dict[str, Any]:
    """`call(name, args)` performs one MCP read and returns its payload."""
    group_by = args.get("group_by") or "item_warehouse"
    limit = max(1, min(int(args.get("limit") or 25), 100))
    descending = (args.get("sort") or "asc") == "desc"

    filters = {k: args[k] for k in ("item_id", "warehouse_id", "search") if args.get(k)}
    bins: list[dict[str, Any]] = []
    for page in range(_MAX_PAGES):
        rows, total = _bins(call(SOURCE_TOOL, {**filters, "limit": _PAGE, "offset": page * _PAGE}))
        bins.extend(rows)
        if len(rows) < _PAGE or (isinstance(total, int) and len(bins) >= total):
            break

    reorder: dict[str, float] = {}
    if args.get("below_reorder") or group_by == "item":
        payload = call("Item.list", {"limit": 1000})
        data = payload.get("data") if isinstance(payload, dict) else None
        for r in data or []:
            if isinstance(r, dict) and r.get("id"):
                reorder[r["id"]] = _num(r.get("reorder_level"))

    def label(b: dict[str, Any]) -> str:
        return " ".join(str(x) for x in (b.get("item_label"), b.get("item_name")) if x) or str(b.get("item_id"))

    item_on_hand: dict[str, float] = defaultdict(float)
    for b in bins:
        item_on_hand[b.get("item_id")] += _num(b.get("on_hand"))

    def keep(item_id: str) -> bool:
        if not args.get("below_reorder"):
            return True
        lvl = reorder.get(item_id, 0.0)
        return lvl > 0 and item_on_hand[item_id] < lvl

    out_rows: list[dict[str, Any]]
    if group_by == "item":
        agg: dict[str, dict[str, Any]] = {}
        for b in bins:
            i = b.get("item_id")
            a = agg.setdefault(i, {"item_id": i, "item": label(b), "on_hand": 0.0, "free": 0.0, "warehouses": 0})
            a["on_hand"] += _num(b.get("on_hand"))
            a["free"] += _num(b.get("free"))
            a["warehouses"] += 1
        out_rows = []
        for i, a in agg.items():
            if i and keep(i):
                a["on_hand"], a["free"] = _qty(a["on_hand"]), _qty(a["free"])
                a["reorder_level"] = _qty(reorder.get(i, 0.0))
                out_rows.append(a)
    else:
        out_rows = [
            {
                "item_id": b.get("item_id"),
                "item": label(b),
                "warehouse_id": b.get("warehouse_id"),
                "warehouse": b.get("warehouse_label"),
                "on_hand": _qty(_num(b.get("on_hand"))),
                "free": _qty(_num(b.get("free"))),
            }
            for b in bins
            if b.get("item_id") and b.get("warehouse_id") and keep(b.get("item_id"))
        ]

    out_rows.sort(key=lambda r: (r["free"], r["item"]), reverse=descending)
    # Every warehouse (also empty ones) so a transfer destination needs no extra call.
    wh_payload = call("Warehouse.list", {"limit": 200})
    wh_rows = wh_payload.get("data") if isinstance(wh_payload, dict) else None
    warehouses = [
        {"id": w.get("id"), "name": w.get("name")} for w in wh_rows or [] if isinstance(w, dict) and w.get("id")
    ]
    return {
        "group_by": group_by,
        "row_count": len(out_rows),
        "returned": min(limit, len(out_rows)),
        "rows": out_rows[:limit],
        "warehouses": warehouses,
        "note": (
            "Complete as of now — do not re-query. free = on_hand - reserved; transfer at most `free` "
            "from a source warehouse. `warehouses` lists every warehouse, including empty ones."
        ),
    }
