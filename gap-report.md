# Gap Report — Stock (Seat 05) vs Katana MRP

**Team 05 · Suryodaya Precision Works (India, Ind AS / GST) · 2026-09-21**
**Comparator:** Katana MRP (`katanamrp.com`) — cloud inventory + light manufacturing, AI-native, publishes an MCP server, from $299/mo Core.

Every claim is backed by dumps in [`evidence/`](evidence/): `tools-list.json` (211 tools scoped to this seat), `schemas.json`, `auth-me.json` (`allowed_apps`: `inventory`, `agent`, `crm`), plus the 0-byte `accounting-locale.json` recording a 403 on `GET /api/accounting/locale`.

---

## 1. What Katana does that we do not

- **Omnichannel order sync as a first-class primitive.** Katana natively ingests sales orders from Shopify, Amazon, BigCommerce, WooCommerce and eBay into one stock picture. AgentSwitch has a `WebOrder` schema at platform level, but no `WebOrder.*` or `StorefrontOrder.*` tools appear in our `tools/list` — the storefront (seat 08) is behind the wall §6 describes ("a tool you may not use is absent from the list").
- **AI Replenishment.** Shipped Katana Core feature; recommends reorder quantities from live demand + lead time. We expose the concept on `Item` reorder-level fields but there is no `Item.recommendReorder` or replenishment tool anywhere in our catalogue — the recommendation logic is agent work, not a platform feature.
- **Planning and real-time inventory planner** driven by sales history. No `Forecast*` or `DemandForecast` entity appears in `schemas.json` at all.
- **Multi-level BOMs, MTS + MTO, contract manufacturing** as inventory-app concerns. No `BOM.*` tools on our seat. Manufacturing (seat 04) is behind the wall — answering *"what can we still build"* requires escalation.
- **Warehouse app** with barcode picking, bin locations, receiving and partial receiving. `Bin` is not in our schemas or tools; `Warehouse.*` is CRUD only (no `submit`/`cancel` workflow). No pick/pack endpoints.
- **Batch / lot / serial traceability with expiry** (Katana add-on, $249/mo). The `Batch` entity and `serial_no` / `expiry_date` fields exist in `schemas.json`, but no `Batch.*` or `SerialNo.*` tools are exposed on our seat — the platform *has* traceability, our agent cannot *drive* it.
- **Landed cost / added costs, multicurrency purchasing** — Katana ships this in Core. No `LandedCostVoucher` in schemas, no `landed_cost_*` fields anywhere. Real platform gap, not seat scoping.
- **Native QuickBooks / Xero posting.** Our ledger lives in-house (seats 01–03). `GET /api/accounting/locale` returned 403 from Stock — same platform, cross-seat, still a wall.
- **Tariff management as a named solution.** Nothing analogous.
- **Katana MCP.** Katana publishes an MCP server over its ERP. "Agent drives the ERP" is the emerging category, not a moat. **We match on interface; we must beat on agent behaviour.**

Triangulation: Cin7 Core and Unleashed ship comparable batch/serial and landed-cost stories at similar price points — this is table stakes in modern stock software.

## 2. Which of those gaps an agent can close today

**The seat is thinner than the pitch suggests.** Only four stock-adjacent entities carry write tools on Team 05: `Item.*` (CRUD), `StockEntry.*` (full lifecycle including `submit` and `cancel_draft`), `Warehouse.*` (CRUD), plus the shared `crm` party spine. That reshapes what's possible.

**Orchestration — mine to build with what I have:**

- **Shortage-to-build analysis via `StockEntry.list` aggregation.** Sum inflow/outflow per item across warehouses to derive on-hand (there is no `StockLedgerEntry` reader on our seat — this is the only path), join to `Item` reorder levels, flag the shortfall. Answers the *"we are short a component"* grading question with only tools we actually have.
- **Inter-warehouse re-allocation as a stock-transfer `StockEntry`.** For the *"what can we still build"* half of the same question, if the same component sits in another warehouse we can draft the transfer end-to-end (`create` → `submit`). This is the one shortage-response workflow our seat can finish alone.
- **Item catalogue hygiene.** Dedupe near-identical SKUs, flag items missing reorder levels or valuation method, surface dormant SKUs. Pure `Item.*` reads and updates.
- **Escalation drafts as first-class agent output.** Because `MaterialRequest.*`, `PurchaseOrder.*`, `StockReconciliation.*` are all off-seat, the agent's job is to *prepare the payload and hand it to seats 01, 03 or 04*, not run the write. The "here is the pre-filled request, please approve" pattern is the real value on a scoped seat.

**Platform work — belongs to AgentSwitch, not us:**

- No `StockLedgerEntry` reader on `inventory` — anomaly detection (negative bins, valuation swings, out-of-hours movements) cannot be built without one.
- No `MaterialRequest.*` or `PurchaseOrder.*` on `inventory` — the reorder loop the brief implies is inventory work sits behind a seat wall.
- No `Bin` entity — bin-level allocation cannot be modelled; only warehouse-level.
- No `LandedCost*` anywhere — landed cost is not a platform concept yet.
- No forecasting entity, no channel connectors, no barcode/mobile endpoints.

## 3. What our agent can do that Katana's product cannot

Katana's UI makes a stock controller faster; a human still drives every step. On our thin seat the agent's edge is not the write — it is the **plan across seats**. Given *"we are short a component next week"* the agent walks its `StockEntry` history, re-reads after another team moves stock (§3), simulates which open orders can still ship complete using only what our seat can see, and produces three artefacts: a `StockEntry` transfer draft it *can* submit itself, a pre-filled `MaterialRequest` payload it *cannot* (escalated to Ledger with a one-line rationale), and a customer-facing note for the deferred order. It refuses cleanly when the data is not readable — *"`GET /api/accounting/locale` returned 403 from this seat; ask team 01 whether the cost centre allows expedited freight"* — and cites the 403 as the reason. The refusal, backed by an evidence artefact, is the grading tell.

---

*Katana feature claims cited to `katanamrp.com/features/` and `katanamrp.com/pricing/` (accessed 2026-09-21). AgentSwitch claims verified against `evidence/tools-list.json` (211 tools) and `evidence/schemas.json` on 2026-09-21.*
