# Bug report — AgentSwitch (Seat 05, Team 05)

**Reporter:** Suryodaya Precision Works · Stock (Seat 05) · 2026-09-22
**Bearer / seat context:** `auth-me.json` shows `allowed_apps: [inventory, agent, crm]`; roles include `inventory_user`. Every repro below uses only tools in this seat's `tools/list` and matches the write allowlist in [`agent/src/stock_agent/tools.py`](agent/src/stock_agent/tools.py).
**Evidence root:** [`probes/findings/`](probes/findings/) — every `evidence.json` referenced below is a byte-for-byte capture of the anomaly run.

## Summary

| # | Tool | One-line bug | Severity | Category | Evidence | Status |
|---|---|---|---|---|---|---|
| 1 | `StockEntry.update` | mutates a cancelled (terminal) row | High | Workflow-state bypass | [P04 evidence](probes/findings/P04/P04-20260922T180525Z-869d27/evidence.json) | ✅ filed |
| 2 | `StockEntry.update` | accepts change to `type` (`receive`→`issue`) | High | Immutable-field bypass | [P13 evidence](probes/findings/P13/P13-20260922T200957Z-447e4d/evidence.json) | to file |
| 3 | `Item.update` | accepts change to `type` (`product`→`service`) | High | Immutable-field bypass | [P18 evidence](probes/findings/P18/P18-20260922T201235Z-9a04b0/evidence.json) | to file |
| 4 | `Item.update` | accepts change to `uom` (`pcs`→`kg`) | High | Immutable-field bypass | [P18 evidence](probes/findings/P18/P18-20260922T201235Z-9a04b0/evidence.json) | to file |
| 5 | `Item.update` | accepts change to `code` (SKU rewrite) | Medium | Immutable-field bypass | [P18 evidence](probes/findings/P18/P18-20260922T201235Z-9a04b0/evidence.json) | to file |
| 6 | `StockEntry.update` | accepts `rate=0` on `receive`, which `create` rejects | Medium | Business-rule bypass on update | [P19 evidence](probes/findings/P19/P19-20260922T201245Z-06ce41/evidence.json) | to file |

Common thread across bugs 2–6: **write validations are enforced on `create` but silently skipped on `update`**. This lets the update path be used as a back door around the state machine, the immutable-field contract, and the business rules — none of which are re-checked when the payload arrives via `PUT`.

---

## Bug 1 — `StockEntry.update` mutates a cancelled row

**Status:** ✅ filed via `POST /api/bug-report`.
**Severity:** High. Cancelled documents are the terminal state of `StockEntryFlow`; downstream reports, audit trails, and stock ledger reconciliation all key off the promise that a cancelled row is immutable.

**Probe:** `P04 illegal_transitions`.
**Evidence:** [`probes/findings/P04/P04-20260922T180525Z-869d27/evidence.json`](probes/findings/P04/P04-20260922T180525Z-869d27/evidence.json).

**Reproduce:**

1. `StockEntry.create` a Draft `type=receive` entry → returns `id`.
2. `StockEntry.cancel_draft {id}` → `status="cancelled"`, `docstatus=2`.
3. `StockEntry.cancel_draft {id}` again → **correctly refused** with `"From 'cancelled' you can go to: nowhere — it is terminal"`.
4. `StockEntry.submit {id}` → **correctly refused** with the same "nowhere — terminal" message.
5. `StockEntry.update {id, notes: "..."}` → **accepted**. Response shape `{content, structuredContent, isError}` with the mutation persisted.

**Contradiction:** the flow engine calls the state terminal for `cancel_draft` and `submit`, but the `update` handler does not consult the flow. Two edges of the state machine are guarded, one is not.

**Note:** A later probe run ([`P16 update_uncancel`](probes/findings/P16/P16-20260922T201037Z-3cb71d/evidence.json)) shows the server now returns `"Cannot modify StockEntry in 'cancelled' status. Only notes/tags/assignments can be changed."` for `status`/`docstatus` patches. If this bug was fixed for structural fields after the P04 capture, re-run P04 to confirm; the report should be updated with the fix version.

---

## Bug 2 — `StockEntry.update` accepts a change to `type`

**Status:** to file.
**Severity:** High. `type` selects the flavour of a `StockEntry` (`receive` / `issue` / `transfer`) and drives which warehouses are read/written, which validations fire, and which downstream ledgers the entry posts to. Retroactively flipping it invalidates every row of `items[]` (a `receive` needs `to_warehouse_id`; an `issue` needs `from_warehouse_id`) and every reconciliation that has already read the row.

**Probe:** `P13 update_immutable_fields`.
**Evidence:** [`probes/findings/P13/P13-20260922T200957Z-447e4d/evidence.json`](probes/findings/P13/P13-20260922T200957Z-447e4d/evidence.json).

**Reproduce:**

1. `StockEntry.create {type: "receive", items: [...]}` → Draft with `type="receive"`.
2. `StockEntry.update {id, type: "issue"}` → **accepted**; response `type` is now `"issue"`.
3. For comparison, adjacent immutability checks in the same probe **are enforced**:
   - `status: "submitted"` → refused: `"State fields ['status'] cannot be changed via PUT. Use POST /api/StockEntry/{id}/transition instead."`
   - `docstatus: 1` → refused: `"Invalid tool arguments."`

**Contradiction:** the server refuses direct `status`/`docstatus` writes to protect the state machine but leaves `type` — an equally identifying field — writable.

**Fix direction:** add `type` to the reject list that already covers `status`/`docstatus` in the update handler.

---

## Bug 3 — `Item.update` accepts a change to `type`

**Status:** to file.
**Severity:** High. `Item.type` distinguishes `product` vs `service`. Every `StockEntry`, `SalesOrder`, and `PurchaseReceipt` referencing the item was created under the assumption the item is stockable (or not). Flipping `product`→`service` on a row that already has stock movements poisons historical data and any subsequent valuation report.

**Probe:** `P18 item_update_immutable`.
**Evidence:** [`probes/findings/P18/P18-20260922T201235Z-9a04b0/evidence.json`](probes/findings/P18/P18-20260922T201235Z-9a04b0/evidence.json).

**Reproduce:**

1. `Item.list {limit: 1}` → pick an item; baseline `{type: "product", uom: "pcs", is_stock_item: 1, code: "ITEM-2026-00103"}`.
2. `Item.update {id, type: "service"}` → **accepted**; `fields_after: {type: "service"}`.
3. For comparison, `Item.update {id, is_stock_item: 0}` in the same run **is refused** (`Invalid tool arguments.`).

**Contradiction:** `is_stock_item` is protected (or unlisted in the input schema), but `type` — which implies `is_stock_item` — is not. The protection is inconsistent across fields that identify the same semantic class.

**Cleanup caveat:** the probe's revert step (`Item.update` back to baseline) itself returned `Invalid tool arguments.`, so the test item is left in a mutated state. See Bug 4/5 for the same effect on other fields.

---

## Bug 4 — `Item.update` accepts a change to `uom`

**Status:** to file.
**Severity:** High. Unit of measure is the denominator of every quantity ever recorded against the item. A row created at `uom="pcs"` with `qty=100` means one hundred pieces; silently rewriting `uom` to `"kg"` retroactively re-labels that row as one hundred kilograms without touching any historical `qty`. There is no conversion audit.

**Probe:** `P18 item_update_immutable`.
**Evidence:** [`probes/findings/P18/P18-20260922T201235Z-9a04b0/evidence.json`](probes/findings/P18/P18-20260922T201235Z-9a04b0/evidence.json).

**Reproduce:**

1. `Item.list {limit: 1}` → baseline `uom: "pcs"`.
2. `Item.update {id, uom: "kg"}` → **accepted**; `fields_after: {uom: "kg"}`.

**Expected behaviour:** either refuse (as `is_stock_item` is refused in the same run), or require an explicit `uom_conversion_factor` field so historical quantities can be rescaled atomically. Neither is enforced.

---

## Bug 5 — `Item.update` accepts a change to `code`

**Status:** to file.
**Severity:** Medium. `Item.code` is the human-readable SKU, referenced in printed labels, external supplier catalogues, and barcodes. Overwriting it silently detaches every physical label and every external system's reference from the row.

**Probe:** `P18 item_update_immutable`.
**Evidence:** [`probes/findings/P18/P18-20260922T201235Z-9a04b0/evidence.json`](probes/findings/P18/P18-20260922T201235Z-9a04b0/evidence.json).

**Reproduce:**

1. `Item.list {limit: 1}` → baseline `code: "ITEM-2026-00103"`.
2. `Item.update {id, code: "REBRANDED-BY-PROBE"}` → **accepted**; `fields_after: {code: "REBRANDED-BY-PROBE"}`.

**Expected behaviour:** SKU rewrites should require either an explicit rename API (that also updates label/barcode secondary indices) or an admin-only role. Neither gate is present in the standard `Item.update` handler.

---

## Bug 6 — `StockEntry.update` accepts `rate=0` on a `receive`, which `create` rejects

**Status:** to file.
**Severity:** Medium. Any inbound valuation of zero flows into the stock ledger as a free receipt, distorting weighted-average cost. The business rule *"rate must be > 0 for receive entries"* is enforced on `create` — verified in every P03/P08 attempt — but not on `update`, so a Draft can be walked around the rule by creating with `rate=1.0` and then patching to `0.0` before submit.

**Probe:** `P19 update_bypass_business_rules`.
**Evidence:** [`probes/findings/P19/P19-20260922T201245Z-06ce41/evidence.json`](probes/findings/P19/P19-20260922T201245Z-06ce41/evidence.json).

**Reproduce:**

1. `StockEntry.create {type: "receive", items: [{item_id, qty: 1, rate: 1.0, to_warehouse_id}]}` → Draft.
2. `StockEntry.update {id, items: [{item_id, qty: 1, rate: 0.0, to_warehouse_id}]}` → **accepted**; response items show `rate: 0.0`.
3. For comparison, the same probe found that `qty=0` and `items=[]` on update **are** refused (`Items row 1: Qty must be greater than zero` / `Items must have at least 1 row`).

**Contradiction:** two of three business rules are re-checked on update; the `rate>0` rule is not. That means the enforcement is *ad hoc per rule*, not a shared validation pass.

**Fix direction:** run the same `_validate_receive_items` (or equivalent) function on both the `create` and `update` handlers, not just the former.

---

## How to reproduce end-to-end

```powershell
cd probes
uv sync
Copy-Item ..\agent\.env .env   # reuses AS_TOKEN
uv run probes run P04
uv run probes run P13
uv run probes run P18
uv run probes run P19
```

Each command writes a new `probes/findings/<Pxx>/<Pxx>-<timestamp>-<short>/evidence.json` and prints an `ANOMALY` line for the anomalies documented above. See [`probes/README.md`](probes/README.md) for safety notes (P04/P13/P19 create a Draft `StockEntry` and cancel it in the same run; P18 mutates a real `Item` and its revert step currently fails — see Bug 3 caveat).

## Filing checklist

For each of bugs 2–6, file via `POST /api/bug-report` with:

- `page`: N/A (probe-generated, not UI).
- `agent_seat`: `05` (Team 05, Stock).
- `job_id`: the `findings_dir` string from the referenced `evidence.json` (e.g. `P13-20260922T200957Z-447e4d`) — this is a stable pointer back to the exact capture.
- `title` and `description`: copy the "one-line bug" from the summary table and the "Reproduce" block from the section.
- Attach the referenced `evidence.json` verbatim.
