# Cooperative Purchase Coordinator — v0.3.0

An intelligent multi-agent coordination service for NANDA Town buyers. It uses four mathematical strategies to maximize agent efficiency:

1. **Binary search + explore-exploit** — converges on seller's exact reservation price in ~7 rounds
2. **Nash-optimal second-price auction** — conflict winner never bids below the seller's floor
3. **Shapley-inspired coalition discount** — larger buyer groups probe at lower prices, gaining leverage
4. **Budget guard + seller routing** — stops agents from wasting rounds on unaffordable sellers

## Base URL
`http://localhost:8000`

---

## Endpoints

### 1. `POST /coop/register` — Register Purchase Intent

Call this **before** sending any offer to a seller. Coordinates with all other agents targeting the same seller in the same round.

#### Request Body
```json
{
  "buyer_id": "buyer-1",
  "seller_id": "seller-0",
  "product_id": "product-1",
  "max_budget": 80,
  "round": 1
}
```

> **Important**: `max_budget` is your **private reservation price for this transaction** (how much this product is worth to you), NOT your total wallet balance.

#### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `won_allocation` / `lost_allocation` / `bundled` / `limit_exceeded` |
| `action` | string | `buy` or `pivot` |
| `suggested_price` | int | Optimal price to offer (see pricing strategy below) |
| `confidence` | float | 0.0 = no info, 1.0 = exact seller price known |
| `bundle_id` | string? | Set when grouped in a wholesale bundle |
| `alternative_seller_id` | string? | Recommended alternative when `action == "pivot"` |
| `message` | string | Human-readable explanation |

#### Response Examples

**Won (no conflict, single buyer):**
```json
{
  "status": "won_allocation", "action": "buy",
  "suggested_price": 55, "confidence": 0.0,
  "message": "No conflict. Price 55 via explore (probing) (confidence 0%)."
}
```

**Won conflict (Nash-optimal auction):**
```json
{
  "status": "won_allocation", "action": "buy",
  "suggested_price": 65, "confidence": 0.73,
  "message": "Won conflict for product-1. Nash-optimal bid: 65 (second-price 61, floor-adjusted)."
}
```

**Bundled (wholesale coalition):**
```json
{
  "status": "bundled", "action": "buy",
  "suggested_price": 52, "bundle_id": "bundle-seller-0-3",
  "confidence": 0.0,
  "message": "Bundle bundle-seller-0-3 (2 buyers, 5% group discount): coordinated price 52 (confidence 0%)."
}
```

**Lost allocation:**
```json
{
  "status": "lost_allocation", "action": "pivot",
  "suggested_price": 0, "alternative_seller_id": "seller-3",
  "confidence": 0.0,
  "message": "Conflict for product-1 lost. Try seller seller-3 instead."
}
```

**Limit exceeded (can't afford seller):**
```json
{
  "status": "limit_exceeded", "action": "pivot",
  "suggested_price": 0, "alternative_seller_id": "seller-5",
  "confidence": 0.89,
  "message": "Seller floor (71) exceeds your budget (50). Try seller seller-5 instead."
}
```

---

### 2. `POST /coop/report` — Report Transaction Outcome

**Always call this after every transaction attempt**, success or failure. This triggers a binary-search update that halves price uncertainty.

```json
{ "buyer_id": "buyer-1", "seller_id": "seller-0", "product_id": "p", "price": 55, "success": true }
```

**Response:**
```json
{
  "status": "ok",
  "seller_limits": { "lower_bound": 10, "upper_bound": 55, "midpoint": 32 },
  "confidence": 0.5,
  "avg_success_price": 55.0
}
```

---

### 3. `GET /coop/sellers` — Scout Sellers

Returns all known sellers with bounds, confidence, and historical stats. Call before selecting which seller to approach.

```json
{
  "sellers": [
    {
      "seller_id": "seller-0", "lower_bound": 45, "upper_bound": 55,
      "midpoint": 50, "confidence": 0.89,
      "attempts": 5, "successes": 3, "avg_success_price": 52.3
    }
  ]
}
```

---

### 4. `GET /coop/analytics` — Coordination Statistics
```json
{
  "total_registrations": 84, "bundles_formed": 12,
  "conflicts_resolved": 9, "limit_exceeded_count": 3,
  "rounds_processed": 21, "active_pools": 2, "known_sellers": 50
}
```

### 5. `GET /coop/status` · `POST /coop/clear`
Status: view all active pools + raw seller limits.
Clear: reset active pool state (preserves learned seller limits on disk).

---

## How the Agent Should Use This

### Optional Pre-step — Scout
```
GET /coop/sellers
```
Filter for sellers where `upper_bound <= your max_budget`. This avoids wasting a registration on a seller you can't afford.

### Step 1 — Register
```
POST /coop/register  { buyer_id, seller_id, product_id, max_budget, round }
```

### Step 2 — Read the Response

| `action` | `status` | What to do |
|----------|----------|-----------|
| `buy` | `won_allocation` | Offer exactly `suggested_price` to the seller |
| `buy` | `bundled` | Offer exactly `suggested_price` to the seller |
| `pivot` | `lost_allocation` | Skip this seller; try `alternative_seller_id` if set |
| `pivot` | `limit_exceeded` | Skip this seller; try `alternative_seller_id` if set |

**Using confidence:**
- `confidence < 0.3` — early probe, seller may reject (that's ok, informs next round)
- `confidence 0.3–0.7` — reasonable estimate, likely to succeed
- `confidence > 0.7` — high trust, suggestion is near-exact seller floor
- `confidence = 1.0` — exact price known, guaranteed to succeed if within budget

### Step 3 — Report
```
POST /coop/report  { buyer_id, seller_id, product_id, price, success: true/false }
```

---

## Mathematical Details

### Binary Search Convergence
Starting from `[10, 100]` (NANDA Town defaults), each report halves the range:
- `success=true` at price P → seller price ≤ P → upper_bound = min(upper, P)
- `success=false` at price P → seller price > P → lower_bound = max(lower, P+1)
- Next midpoint = `(lower + upper) // 2`

Convergence: `log₂(90) ≈ 7 rounds` to pinpoint exact reservation price.

### Explore-Exploit Strategy
- **Explore** (confidence < 0.5): bid at midpoint — gather information fast
- **Exploit** (confidence ≥ 0.5): bid at lower_bound — pay the minimum viable price

### Nash-Optimal Auction Bid
Winner in a conflict pays `max(second_price + 1, seller_floor)`.
This prevents wasting a round on a bid that would win the auction but be rejected by the seller (because it's below the seller's reservation price).

### Shapley-Inspired Bundle Discount
Coalition size → probe price multiplier (when exploring):
- 2 buyers → 95% of midpoint (5% off)
- 3 buyers → 88% of midpoint (12% off)
- 4+ buyers → 80% of midpoint (20% off)

At high confidence (exploit mode), the floor is the floor — no discount is applied since the seller won't accept below their minimum regardless of group size.

### Confidence Formula
```
confidence = 1 - (current_range / initial_range)
           = 1 - ((upper - lower) / 90)
```
