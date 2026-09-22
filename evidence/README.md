# Evidence for gap-report.md

Drop the raw dumps here so every `[verify]` tag in the report can be checked against a file, not memory. Nothing in this folder is submitted — it backs the report.

## 1. Log in and set the token

```powershell
$AS = "https://agentswitch.theschoolofai.in"

$body = @{ email = "team05@theschoolofai.in"; password = "YOUR_PASSWORD" } | ConvertTo-Json
$env:TOKEN = (Invoke-RestMethod -Method Post -Uri "$AS/api/auth/login" `
  -ContentType "application/json" -Body $body).token

# sanity check — should print your email + roles + allowed_apps, not 401
Invoke-RestMethod -Method Get -Uri "$AS/api/auth/me" `
  -Headers @{ Authorization = "Bearer $env:TOKEN" }
```

`$env:TOKEN` (environment variable) is what the dump commands below read. `$TOKEN` (plain shell variable) is a different thing — do not mix them. If you already ran the curl/python login into `$TOKEN`, just promote it: `$env:TOKEN = $TOKEN`.

## 2. Save the dumps

```powershell
$H = @{ Authorization = "Bearer $env:TOKEN" }

Invoke-RestMethod -Method Get -Uri "$AS/api/auth/me" -Headers $H `
  | ConvertTo-Json -Depth 10 | Out-File auth-me.json

Invoke-RestMethod -Method Get -Uri "$AS/api/schemas" -Headers $H `
  | ConvertTo-Json -Depth 20 | Out-File schemas.json

$body = '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
Invoke-RestMethod -Method Post -Uri "$AS/api/mcp" `
  -Headers $H -ContentType "application/json" -Body $body `
  | ConvertTo-Json -Depth 20 | Out-File tools-list.json
```

## 3. Checks the report depends on

Grep `tools-list.json` and `schemas.json` for the following. Presence or absence resolves the `[verify]` tags in [../gap-report.md](../gap-report.md):

| Report claim | Check |
|---|---|
| No unified channels view on our seat | `StorefrontOrder`, `WebOrder`, `Shopify`, `Amazon` — expect **absent** |
| No AI replenishment recommender | `recommendReorder`, `suggestReorder`, `replenishment` — expect **absent** |
| No forecasting entity | `Forecast`, `DemandForecast` — expect **absent** in `schemas.json` |
| BOM lives on manufacturing seat | `BOM.list`, `BOM.get` in `tools-list.json` — expect **absent** (if present, downgrade the "cross-seat wall" claim) |
| Warehouse + Bin entities exist | `Warehouse`, `Bin` — expect **present** in `schemas.json` |
| Batch / serial exist or not | `Batch`, `SerialNo`, `StockEntry.batch_no`, `StockEntry.serial_no` — record either way |
| Landed-cost handling | `LandedCostVoucher`, `PurchaseReceipt.landed_cost_taxes_and_charges` — record what exists |
| `MaterialRequest.create` on our seat | expect **present** |
| `StockReconciliation.create` on our seat | expect **present** |

Quick greps:

```powershell
Select-String -Path tools-list.json -Pattern 'StorefrontOrder|Shopify|Amazon|recommendReorder|replenishment|BOM\.|MaterialRequest\.create|StockReconciliation\.create|LandedCost'
Select-String -Path schemas.json    -Pattern 'Warehouse|"Bin"|Batch|SerialNo|Forecast|batch_no|serial_no|landed_cost'
```

## 4. Then resolve the [verify] tags

Edit [../gap-report.md](../gap-report.md): replace each `[verify]` with the confirming tool/field name in backticks, or delete the surrounding claim if it did not hold. Then ping me for the v1 tighten pass.
