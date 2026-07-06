"""
Co-op Purchase Coordinator — v0.3.0
Cooperative purchase and conflict resolution coordinator for NANDA Town agents.

Mathematical foundations:
- Binary search + explore-exploit for price convergence
- Nash-optimal second-price auction with seller floor awareness
- Shapley-inspired group discount tiers for wholesale bundles
- Confidence-gated exploit: bid minimum viable price when floor is known
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GLOBAL_MIN_PRICE = 10      # NANDA Town marketplace minimum (from marketplace.py)
_GLOBAL_MAX_PRICE = 100     # NANDA Town marketplace maximum (from marketplace.py)
_POOL_TTL_SECONDS = 300     # Stale pools older than this are cleaned up (5 min)
_LIMITS_FILE = Path(__file__).parent / "seller_limits.json"

# Group discount factors: larger coalition = lower probe price
# Applied only when exploring (confidence < 0.5).
# At high confidence, floor is known — cannot go below it.
_GROUP_DISCOUNT: Dict[int, float] = {
    1: 1.00,   # solo: no discount
    2: 0.95,   # 2 buyers: 5% off midpoint
    3: 0.88,   # 3 buyers: 12% off midpoint
}
_GROUP_DISCOUNT_4PLUS = 0.80  # 4+ buyers: 20% off midpoint

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    """Load persisted seller limits before serving requests."""
    _load_limits()
    yield


app = FastAPI(
    title="Co-op Purchase Coordinator API",
    description=(
        "Cooperative purchase and conflict resolution coordinator for NANDA Town agents. "
        "Features: binary-search price convergence, explore-exploit pricing, "
        "Nash-optimal auctions, Shapley-inspired bundle discounts, seller routing."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class RegistrationRequest(BaseModel):
    buyer_id: str
    seller_id: str
    product_id: str
    max_budget: int   # private reservation price for THIS transaction, not total wallet
    round: int

    @field_validator("max_budget")
    @classmethod
    def budget_must_be_non_negative(cls, v: int) -> int:
        """Reject negative reservation prices — they are nonsensical."""
        if v < 0:
            raise ValueError("max_budget must be >= 0 (it is a reservation price, not a loan)")
        return v


class RegistrationResponse(BaseModel):
    status: str               # won_allocation | lost_allocation | bundled | limit_exceeded
    action: str               # buy | pivot
    suggested_price: int
    bundle_id: Optional[str] = None
    confidence: float         # 0.0 (no info) → 1.0 (exact seller price known)
    alternative_seller_id: Optional[str] = None
    message: str


class ReportRequest(BaseModel):
    buyer_id: str
    seller_id: str
    product_id: str
    price: int
    success: bool


class StatusResponse(BaseModel):
    active_pools: Dict[str, List[dict]]
    learned_seller_limits: Dict[str, dict]


class SellerInfo(BaseModel):
    seller_id: str
    lower_bound: int
    upper_bound: int
    midpoint: int
    confidence: float
    attempts: int
    successes: int
    avg_success_price: Optional[float]


# ---------------------------------------------------------------------------
# In-Memory State
# ---------------------------------------------------------------------------

registrations: Dict[str, List[dict]] = {}              # pool_key → [buyer dicts]
decisions: Dict[str, Dict[str, RegistrationResponse]] = {}  # pool_key → buyer_id → decision
locks: Dict[str, asyncio.Lock] = {}                    # pool_key → lock
pool_timestamps: Dict[str, float] = {}                 # pool_key → creation time (unix)
seller_limits: Dict[str, dict] = {}                    # seller_id → bounds + stats

# Aggregate analytics counters
_analytics: Dict[str, int] = {
    "total_registrations": 0,
    "bundles_formed": 0,
    "conflicts_resolved": 0,
    "limit_exceeded_count": 0,
    "rounds_processed": 0,
}

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load_limits() -> None:
    """Load seller_limits from disk if available (survives server restarts)."""
    if _LIMITS_FILE.exists():
        try:
            data = json.loads(_LIMITS_FILE.read_text())
            seller_limits.update(data)
        except Exception:
            pass  # corrupt file — start fresh


def _save_limits() -> None:
    """Persist seller_limits to disk after each report."""
    try:
        _LIMITS_FILE.write_text(json.dumps(seller_limits, indent=2))
    except Exception:
        pass  # non-critical — best effort


async def on_startup():
    """Called by lifespan; also directly callable in tests."""
    _load_limits()


# ---------------------------------------------------------------------------
# Pool TTL Cleanup
# ---------------------------------------------------------------------------

def _cleanup_stale_pools() -> None:
    """Lazily expire pools older than POOL_TTL_SECONDS to prevent memory growth."""
    now = time.time()
    expired = [k for k, t in pool_timestamps.items() if now - t > _POOL_TTL_SECONDS]
    for k in expired:
        registrations.pop(k, None)
        decisions.pop(k, None)
        locks.pop(k, None)
        pool_timestamps.pop(k, None)


# ---------------------------------------------------------------------------
# Price Intelligence: Core Helpers
# ---------------------------------------------------------------------------

def _init_seller_limits(seller_id: str) -> dict:
    """Initialize a seller's learned limits with marketplace-wide defaults."""
    if seller_id not in seller_limits:
        seller_limits[seller_id] = {
            "lower_bound": _GLOBAL_MIN_PRICE,
            "upper_bound": _GLOBAL_MAX_PRICE,
            "midpoint": (_GLOBAL_MIN_PRICE + _GLOBAL_MAX_PRICE) // 2,
            "initial_range": _GLOBAL_MAX_PRICE - _GLOBAL_MIN_PRICE,
            # Per-seller analytics
            "attempts": 0,
            "successes": 0,
            "total_success_price": 0,
        }
    return seller_limits[seller_id]


def _update_midpoint(limits: dict) -> None:
    """Recalculate binary-search midpoint after any bound update."""
    limits["midpoint"] = (limits["lower_bound"] + limits["upper_bound"]) // 2


def _compute_confidence(limits: dict) -> float:
    """
    Confidence = 1 - (current_range / initial_range).

    Measures how much of the price uncertainty has been eliminated:
      0.0 → no information beyond global defaults
      1.0 → exact seller reservation price is pinpointed (lower == upper)
    """
    initial_range = limits.get("initial_range", 1) or 1
    current_range = limits["upper_bound"] - limits["lower_bound"]
    return round(max(0.0, min(1.0, 1.0 - current_range / initial_range)), 4)


def _group_discount_factor(group_size: int) -> float:
    """Return the probe-price multiplier for a given coalition size."""
    return _GROUP_DISCOUNT.get(group_size, _GROUP_DISCOUNT_4PLUS)


# ---------------------------------------------------------------------------
# Price Intelligence: Strategy Functions
# ---------------------------------------------------------------------------

def _explore_or_exploit(seller_id: str, max_budget: int) -> Tuple[int, float]:
    """
    Explore-exploit pricing strategy.

    - Explore (confidence < 0.5): bid at binary-search midpoint.
      Goal is to gather information about seller's floor as fast as possible.

    - Exploit (confidence >= 0.5): bid at lower_bound.
      We know enough — pay the minimum the seller will accept, saving money.

    - Full knowledge (confidence == 1.0): bid the exact pinpointed price.

    Returns (suggested_price, confidence).
    """
    limits = _init_seller_limits(seller_id)
    conf = _compute_confidence(limits)

    if conf >= 0.5:
        # Exploit: we have enough info — pay the minimum viable price
        price = limits["lower_bound"]
    else:
        # Explore: probe at midpoint to halve uncertainty
        price = limits["midpoint"]

    return min(price, max_budget), conf


def _nash_winner_price(second_price: int, winner_budget: int, seller_id: str) -> int:
    """
    Nash-optimal price for a conflict auction winner.

    Standard Vickrey/second-price: pay second_price + 1.
    Problem: if second_price + 1 < seller's known floor, the offer will be rejected
    even though the winner won the internal auction — wasting a full round.

    Fix: pay max(second_price + 1, lower_bound) when we have learned the seller floor.
    This guarantees the offer clears the seller's minimum threshold.
    """
    base = second_price + 1
    limits = seller_limits.get(seller_id)
    if limits and _compute_confidence(limits) > 0:
        base = max(base, limits["lower_bound"])
    return min(base, winner_budget)


def _bundle_price(seller_id: str, max_budget: int, group_size: int) -> Tuple[int, float]:
    """
    Shapley-inspired bundle pricing.

    When exploring (low confidence):
      Larger coalitions get lower probe prices (more bargaining leverage).
      suggested = midpoint * group_discount_factor

    When exploiting (high confidence):
      We know the seller's floor — discount cannot go below it.
      suggested = lower_bound (floor is floor regardless of group size)

    Returns (suggested_price, confidence).
    """
    limits = _init_seller_limits(seller_id)
    conf = _compute_confidence(limits)

    if conf >= 0.5:
        # Exploit: pay floor — group size irrelevant, seller won't go lower
        price = limits["lower_bound"]
    else:
        # Explore with coalition discount: probe lower to find floor faster
        discount = _group_discount_factor(group_size)
        price = max(_GLOBAL_MIN_PRICE, int(limits["midpoint"] * discount))

    return min(price, max_budget), conf


def _find_best_alternative(seller_id: str, max_budget: int) -> Optional[str]:
    """
    Find the most affordable known alternative seller within the buyer's budget.
    Prefers the seller with the lowest confirmed transaction price (upper_bound).
    """
    best: Optional[str] = None
    best_price = max_budget + 1

    for sid, limits in seller_limits.items():
        if sid == seller_id:
            continue
        # Affordable if a confirmed transaction price is within budget
        if limits["upper_bound"] <= max_budget and limits["upper_bound"] < best_price:
            best_price = limits["upper_bound"]
            best = sid

    return best


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root():
    return {
        "title": app.title,
        "description": app.description,
        "version": app.version,
        "status": "running",
        "documentation": "/docs",
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.post("/coop/register", response_model=RegistrationResponse)
async def register_interest(req: RegistrationRequest):
    """
    Register purchase intent. The service coordinates with all other agents
    targeting the same seller in the same round, then returns a resolution.
    """
    pool_key = f"{req.seller_id}:{req.round}"

    # Lazy stale-pool cleanup on every new registration
    _cleanup_stale_pools()

    if pool_key not in locks:
        locks[pool_key] = asyncio.Lock()
        pool_timestamps[pool_key] = time.time()

    async with locks[pool_key]:
        if pool_key not in registrations:
            registrations[pool_key] = []
        registrations[pool_key] = [r for r in registrations[pool_key] if r["buyer_id"] != req.buyer_id]
        registrations[pool_key].append({
            "buyer_id": req.buyer_id,
            "product_id": req.product_id,
            "max_budget": req.max_budget,
        })
        _analytics["total_registrations"] += 1

    # Brief wait to allow concurrent agents in the same tick to register before resolution
    await asyncio.sleep(0.05)

    async with locks[pool_key]:
        if pool_key not in decisions or req.buyer_id not in decisions[pool_key]:
            _resolve_pool(req.seller_id, req.round)

        if pool_key in decisions and req.buyer_id in decisions[pool_key]:
            return decisions[pool_key][req.buyer_id]

        raise HTTPException(status_code=500, detail="Resolution failed — try again next round.")


@app.post("/coop/report")
async def report_outcome(req: ReportRequest):
    """
    Report a transaction outcome. Updates the binary-search bounds and triggers
    a midpoint recalculation. Persists the updated seller knowledge to disk.
    """
    limits = _init_seller_limits(req.seller_id)
    limits["attempts"] += 1

    if req.success:
        limits["upper_bound"] = min(limits["upper_bound"], req.price)
        limits["successes"] += 1
        limits["total_success_price"] = limits.get("total_success_price", 0) + req.price
    else:
        limits["lower_bound"] = max(limits["lower_bound"], req.price + 1)

    # Clamp: lower cannot exceed upper (pinned when exact price found)
    if limits["lower_bound"] > limits["upper_bound"]:
        limits["lower_bound"] = limits["upper_bound"]

    _update_midpoint(limits)
    _save_limits()  # persist to disk

    avg = (limits["total_success_price"] / limits["successes"]) if limits["successes"] > 0 else None

    return {
        "status": "ok",
        "seller_limits": {
            "lower_bound": limits["lower_bound"],
            "upper_bound": limits["upper_bound"],
            "midpoint": limits["midpoint"],
        },
        "confidence": _compute_confidence(limits),
        "avg_success_price": round(avg, 2) if avg is not None else None,
    }


@app.get("/coop/status", response_model=StatusResponse)
async def get_status():
    return StatusResponse(
        active_pools={k: v for k, v in registrations.items() if v},
        learned_seller_limits=seller_limits,
    )


@app.get("/coop/sellers")
async def list_sellers():
    """
    Returns all known sellers with bounds, midpoint, confidence, and success stats.
    Use this to scout affordable sellers before registering.
    """
    result = []
    for sid, limits in seller_limits.items():
        succ = limits.get("successes", 0)
        total_sp = limits.get("total_success_price", 0)
        avg = round(total_sp / succ, 2) if succ > 0 else None
        result.append(SellerInfo(
            seller_id=sid,
            lower_bound=limits["lower_bound"],
            upper_bound=limits["upper_bound"],
            midpoint=limits["midpoint"],
            confidence=_compute_confidence(limits),
            attempts=limits.get("attempts", 0),
            successes=succ,
            avg_success_price=avg,
        ))
    return {"sellers": [s.model_dump() for s in result]}


@app.get("/coop/analytics")
async def get_analytics():
    """
    Returns aggregate coordination statistics across all rounds.
    """
    active_pools = sum(1 for v in registrations.values() if v)
    
    total_attempts = sum(limits.get("attempts", 0) for limits in seller_limits.values())
    total_successes = sum(limits.get("successes", 0) for limits in seller_limits.values())
    
    deal_success_rate = (total_successes / total_attempts) if total_attempts > 0 else 0.0
    
    rounds = _analytics.get("rounds_processed", 0)
    throughput = (total_successes / rounds) if rounds > 0 else 0.0
    
    total_regs = _analytics.get("total_registrations", 0)
    friction_rate = (_analytics.get("limit_exceeded_count", 0) / total_regs) if total_regs > 0 else 0.0
    
    return {
        **_analytics,
        "active_pools": active_pools,
        "known_sellers": len(seller_limits),
        "total_attempts": total_attempts,
        "total_successes": total_successes,
        "deal_success_rate": round(deal_success_rate, 4),
        "throughput": round(throughput, 2),
        "friction_rate": round(friction_rate, 4),
    }


@app.post("/coop/clear")
async def clear_state():
    """Clears active pool state. Does NOT reset learned seller limits or analytics."""
    registrations.clear()
    decisions.clear()
    locks.clear()
    pool_timestamps.clear()
    return {"status": "cleared"}


# ---------------------------------------------------------------------------
# Pool Resolution Logic
# ---------------------------------------------------------------------------

def _resolve_pool(seller_id: str, round_id: int) -> None:
    """
    Core coordination algorithm. Called once per pool after the aggregation window.

    Steps:
      1. Conflict resolution — second-price auction (Nash-optimal bid)
      2. Wholesale bundling — Shapley-inspired group discount
      3. Budget guard — override with pivot if seller floor > buyer budget
    """
    pool_key = f"{seller_id}:{round_id}"
    pool = registrations.get(pool_key, [])
    if not pool:
        return

    if pool_key not in decisions:
        decisions[pool_key] = {}
    elif decisions[pool_key]:  # pool already fully resolved — skip re-counting analytics
        return

    # Track unique rounds
    _analytics["rounds_processed"] += 1

    # -----------------------------------------------------------------------
    # Step 1: Group by product → detect conflicts
    # -----------------------------------------------------------------------
    by_product: Dict[str, List[dict]] = {}
    for r in pool:
        by_product.setdefault(r["product_id"], []).append(r)

    allocated_buyers: List[dict] = []

    for product_id, buyers in by_product.items():
        if len(buyers) == 1:
            allocated_buyers.append(buyers[0])
        else:
            _analytics["conflicts_resolved"] += 1

            # Sort: highest budget first; buyer_id is stable tiebreaker
            sorted_buyers = sorted(
                buyers,
                key=lambda x: (-x["max_budget"], x["buyer_id"]),
            )
            winner = sorted_buyers[0]
            allocated_buyers.append(winner)

            second_price = sorted_buyers[1]["max_budget"]
            # Nash-optimal: ensure offer clears seller floor, not just second-price
            suggested = _nash_winner_price(second_price, winner["max_budget"], seller_id)
            conf = _compute_confidence(_init_seller_limits(seller_id))

            decisions[pool_key][winner["buyer_id"]] = RegistrationResponse(
                status="won_allocation",
                action="buy",
                suggested_price=suggested,
                confidence=conf,
                message=(
                    f"Won conflict for {product_id}. "
                    f"Nash-optimal bid: {suggested} "
                    f"(second-price {second_price + 1}, floor-adjusted)."
                ),
            )

            for loser in sorted_buyers[1:]:
                alt = _find_best_alternative(seller_id, loser["max_budget"])
                decisions[pool_key][loser["buyer_id"]] = RegistrationResponse(
                    status="lost_allocation",
                    action="pivot",
                    suggested_price=0,
                    confidence=0.0,
                    alternative_seller_id=alt,
                    message=(
                        f"Conflict for {product_id} lost. "
                        + (f"Try seller {alt} instead." if alt else "No affordable alternative known yet.")
                    ),
                )

    # -----------------------------------------------------------------------
    # Step 2: Wholesale bundling
    # -----------------------------------------------------------------------
    group_size = len(allocated_buyers)

    if group_size >= 2:
        bundle_id = f"bundle-{seller_id}-{round_id}"
        _analytics["bundles_formed"] += 1

        for b in allocated_buyers:
            buyer_id = b["buyer_id"]
            max_budget = b["max_budget"]
            # Shapley-inspired: larger group → lower probe price
            suggested, conf = _bundle_price(seller_id, max_budget, group_size)
            discount_pct = int((1.0 - _group_discount_factor(group_size)) * 100)

            decisions[pool_key][buyer_id] = RegistrationResponse(
                status="bundled",
                action="buy",
                suggested_price=suggested,
                bundle_id=bundle_id,
                confidence=conf,
                message=(
                    f"Bundle {bundle_id} ({group_size} buyers, {discount_pct}% group discount): "
                    f"coordinated price {suggested} (confidence {conf:.0%})."
                ),
            )

    elif group_size == 1:
        single = allocated_buyers[0]
        buyer_id = single["buyer_id"]

        if buyer_id not in decisions[pool_key]:
            suggested, conf = _explore_or_exploit(seller_id, single["max_budget"])
            strategy = "exploit (floor known)" if conf >= 0.5 else "explore (probing)"
            decisions[pool_key][buyer_id] = RegistrationResponse(
                status="won_allocation",
                action="buy",
                suggested_price=suggested,
                confidence=conf,
                message=f"No conflict. Price {suggested} via {strategy} (confidence {conf:.0%}).",
            )

    # -----------------------------------------------------------------------
    # Step 3: Budget Guard — override if seller floor > buyer budget
    # -----------------------------------------------------------------------
    for r in pool:
        buyer_id = r["buyer_id"]
        max_budget = r["max_budget"]
        limits = seller_limits.get(seller_id)
        if limits and limits["lower_bound"] > max_budget:
            # Only count if we're changing an existing buy decision to a pivot
            # (avoids double-counting on re-resolution)
            prev = decisions[pool_key].get(buyer_id)
            if prev is None or prev.action == "buy":
                _analytics["limit_exceeded_count"] += 1
            alt = _find_best_alternative(seller_id, max_budget)
            decisions[pool_key][buyer_id] = RegistrationResponse(
                status="limit_exceeded",
                action="pivot",
                suggested_price=0,
                confidence=_compute_confidence(limits),
                alternative_seller_id=alt,
                message=(
                    f"Seller floor ({limits['lower_bound']}) exceeds your budget ({max_budget}). "
                    + (f"Try seller {alt} instead." if alt else "No affordable alternative known yet.")
                ),
            )
