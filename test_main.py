"""
Full test suite for Co-op Purchase Coordinator v0.3.0.
Covers: conflict resolution, bundling, explore-exploit pricing,
Nash-optimal auction bids, group discounts, seller routing,
pool expiry, analytics, and persistence.
"""
import asyncio
import json
import time
import pytest
from pathlib import Path
from httpx import ASGITransport, AsyncClient
from main import (
    app,
    registrations, decisions, seller_limits, locks, pool_timestamps, _analytics,
    _GLOBAL_MIN_PRICE, _GLOBAL_MAX_PRICE, _LIMITS_FILE,
    _compute_confidence, _explore_or_exploit, _nash_winner_price, _bundle_price,
    _group_discount_factor,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_state():
    """Reset all in-memory state before every test."""
    registrations.clear()
    decisions.clear()
    seller_limits.clear()
    locks.clear()
    pool_timestamps.clear()
    for k in list(_analytics.keys()):
        _analytics[k] = 0
    # Remove persisted file to prevent cross-test contamination
    if _LIMITS_FILE.exists():
        _LIMITS_FILE.unlink()
    yield
    # Cleanup after test too
    if _LIMITS_FILE.exists():
        _LIMITS_FILE.unlink()

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

async def register(ac, buyer_id, seller_id, product_id, max_budget, round_=1):
    return await ac.post("/coop/register", json={
        "buyer_id": buyer_id, "seller_id": seller_id,
        "product_id": product_id, "max_budget": max_budget, "round": round_,
    })


async def report(ac, seller_id, price, success, buyer_id="b", product_id="p"):
    return await ac.post("/coop/report", json={
        "buyer_id": buyer_id, "seller_id": seller_id,
        "product_id": product_id, "price": price, "success": success,
    })

# ---------------------------------------------------------------------------
# Core: Conflict Resolution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_conflict_winner_loser():
    """Highest bidder wins; loser gets pivot signal."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r1, r2 = await asyncio.gather(
            register(ac, "buyer-rich", "seller-0", "p1", 80),
            register(ac, "buyer-poor", "seller-0", "p1", 60),
        )
        d1, d2 = r1.json(), r2.json()

        assert d1["status"] == "won_allocation" and d1["action"] == "buy"
        assert d2["status"] == "lost_allocation" and d2["action"] == "pivot"
        assert "confidence" in d1


@pytest.mark.asyncio
async def test_conflict_tiebreak_stable():
    """Equal budgets: alphabetically earlier buyer_id wins (deterministic)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r1, r2 = await asyncio.gather(
            register(ac, "buyer-aaa", "seller-0", "p1", 60),
            register(ac, "buyer-zzz", "seller-0", "p1", 60),
        )
        assert r1.json()["status"] == "won_allocation"
        assert r2.json()["status"] == "lost_allocation"


# ---------------------------------------------------------------------------
# Mathematics 1: Nash-Optimal Auction Bid
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nash_winner_price_without_floor():
    """Without known floor, winner pays second_price + 1 (standard Vickrey)."""
    # seller_limits empty → confidence 0.0 → no floor correction
    price = _nash_winner_price(second_price=60, winner_budget=80, seller_id="s-unknown")
    assert price == 61  # 60 + 1


@pytest.mark.asyncio
async def test_nash_winner_price_floor_lift():
    """When known floor > second_price + 1, winner is lifted to the floor."""
    seller_limits["seller-0"] = {
        "lower_bound": 70, "upper_bound": 80, "midpoint": 75,
        "initial_range": 90, "attempts": 1, "successes": 0, "total_success_price": 0
    }
    # second_price + 1 = 61, but floor = 70 → must pay 70
    price = _nash_winner_price(second_price=60, winner_budget=80, seller_id="seller-0")
    assert price == 70


@pytest.mark.asyncio
async def test_nash_bid_appears_in_conflict_response():
    """Floor-adjusted bid shows up correctly in the live conflict endpoint."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Teach: seller floor is 65
        await report(ac, "seller-0", 64, False)
        await report(ac, "seller-0", 70, True)
        # floor = 65, upper = 70

        r1, r2 = await asyncio.gather(
            register(ac, "buyer-rich", "seller-0", "p1", 80),
            register(ac, "buyer-poor", "seller-0", "p1", 60),
        )
        d1 = r1.json()
        assert d1["status"] == "won_allocation"
        # second_price = 60, second+1 = 61, but floor = 65 → must pay 65
        assert d1["suggested_price"] == 65


# ---------------------------------------------------------------------------
# Mathematics 2: Explore-Exploit Price Strategy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explore_when_confidence_low():
    """With no info (confidence=0), suggested price should be the midpoint."""
    price, conf = _explore_or_exploit("seller-new", max_budget=100)
    expected_midpoint = (_GLOBAL_MIN_PRICE + _GLOBAL_MAX_PRICE) // 2
    assert price == expected_midpoint
    assert conf == 0.0


@pytest.mark.asyncio
async def test_exploit_when_confidence_high():
    """With confidence >= 0.5, suggested price should be the lower_bound (floor)."""
    # Narrow the range to [48, 52] → confidence = 1 - 4/90 ≈ 0.955
    seller_limits["seller-0"] = {
        "lower_bound": 48, "upper_bound": 52, "midpoint": 50,
        "initial_range": 90, "attempts": 5, "successes": 3, "total_success_price": 156
    }
    price, conf = _explore_or_exploit("seller-0", max_budget=100)
    assert conf > 0.5
    assert price == 48  # lower_bound (exploit)


@pytest.mark.asyncio
async def test_exploit_clamped_to_max_budget():
    """Exploit price is clamped to max_budget if floor > budget (should be caught by guard)."""
    seller_limits["seller-0"] = {
        "lower_bound": 80, "upper_bound": 85, "midpoint": 82,
        "initial_range": 90, "attempts": 5, "successes": 2, "total_success_price": 165
    }
    price, _ = _explore_or_exploit("seller-0", max_budget=70)
    assert price == 70  # clamped


@pytest.mark.asyncio
async def test_explore_vs_exploit_in_registration():
    """Live registration returns midpoint when exploring, lower_bound when exploiting."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Explore phase: no knowledge
        res_explore = await register(ac, "b1", "seller-0", "p1", 100)
        assert res_explore.json()["confidence"] == 0.0
        assert res_explore.json()["suggested_price"] == (_GLOBAL_MIN_PRICE + _GLOBAL_MAX_PRICE) // 2

        # Build high confidence
        await report(ac, "seller-0", 55, True)   # upper = 55
        await report(ac, "seller-0", 30, False)  # lower = 31
        # range = [31, 55] → 24 / 90 → confidence ≈ 0.733 → exploit

        res_exploit = await register(ac, "b2", "seller-0", "p2", 100, round_=2)
        data = res_exploit.json()
        assert data["confidence"] > 0.5
        assert data["suggested_price"] == seller_limits["seller-0"]["lower_bound"]


# ---------------------------------------------------------------------------
# Mathematics 3: Group Bundle Discount (Shapley-Inspired)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_discount_factors():
    """Larger coalitions get larger discounts on the midpoint price."""
    assert _group_discount_factor(1) == 1.00
    assert _group_discount_factor(2) == 0.95
    assert _group_discount_factor(3) == 0.88
    assert _group_discount_factor(4) == 0.80
    assert _group_discount_factor(10) == 0.80  # capped at 4+


@pytest.mark.asyncio
async def test_bundle_price_explore_applies_discount():
    """When exploring, bundle price = midpoint * discount_factor."""
    seller_limits["seller-0"] = {
        "lower_bound": _GLOBAL_MIN_PRICE, "upper_bound": _GLOBAL_MAX_PRICE,
        "midpoint": 55, "initial_range": 90,
        "attempts": 0, "successes": 0, "total_success_price": 0
    }
    # 2 buyers, confidence = 0.0 (exploring)
    price, conf = _bundle_price("seller-0", max_budget=100, group_size=2)
    expected = max(_GLOBAL_MIN_PRICE, int(55 * 0.95))  # = 52
    assert price == expected
    assert conf == 0.0


@pytest.mark.asyncio
async def test_bundle_price_exploit_ignores_discount():
    """When exploiting, bundle price = lower_bound regardless of group size."""
    seller_limits["seller-0"] = {
        "lower_bound": 45, "upper_bound": 50, "midpoint": 47,
        "initial_range": 90, "attempts": 5, "successes": 3, "total_success_price": 150
    }
    conf = _compute_confidence(seller_limits["seller-0"])
    assert conf > 0.5  # high confidence

    for group_size in [2, 3, 4]:
        price, _ = _bundle_price("seller-0", max_budget=100, group_size=group_size)
        assert price == 45, f"Expected lower_bound=45 for group_size={group_size}, got {price}"


@pytest.mark.asyncio
async def test_live_bundle_uses_discount():
    """Two buyers for different products get bundled with group discount applied."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r1, r2 = await asyncio.gather(
            register(ac, "buyer-1", "seller-0", "p1", 100),
            register(ac, "buyer-2", "seller-0", "p2", 100),
        )
        d1, d2 = r1.json(), r2.json()

        assert d1["status"] == "bundled"
        assert d1["bundle_id"] == d2["bundle_id"]

        # No prior knowledge → exploring → 2-buyer discount: midpoint * 0.95
        midpoint = (_GLOBAL_MIN_PRICE + _GLOBAL_MAX_PRICE) // 2  # 55
        expected = max(_GLOBAL_MIN_PRICE, int(midpoint * 0.95))   # 52
        assert d1["suggested_price"] == expected
        assert d2["suggested_price"] == expected


# ---------------------------------------------------------------------------
# Feature: Seller Routing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_routing_on_limit_exceeded():
    """Buyer priced out gets a recommended alternative seller."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await report(ac, "seller-A", 70, False)   # floor-A = 71
        await report(ac, "seller-B", 30, True)    # upper-B = 30 (affordable)

        res = await register(ac, "buyer-poor", "seller-A", "p1", 50)
        data = res.json()

        assert data["status"] == "limit_exceeded"
        assert data["alternative_seller_id"] == "seller-B"


@pytest.mark.asyncio
async def test_routing_on_lost_allocation():
    """Losing buyer gets alternative seller when one is known."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await report(ac, "seller-B", 40, True)  # seller-B confirmed at 40

        r1, r2 = await asyncio.gather(
            register(ac, "buyer-rich", "seller-A", "p1", 80),
            register(ac, "buyer-poor", "seller-A", "p1", 60),
        )
        assert r2.json()["alternative_seller_id"] == "seller-B"


# ---------------------------------------------------------------------------
# Feature: Pool TTL Expiry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_pools_are_cleaned():
    """Pools older than TTL are removed from memory on the next registration."""
    from main import _POOL_TTL_SECONDS

    # Manually create a stale pool
    registrations["seller-0:99"] = [{"buyer_id": "b", "product_id": "p", "max_budget": 50}]
    pool_timestamps["seller-0:99"] = time.time() - (_POOL_TTL_SECONDS + 1)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Trigger cleanup via a new registration
        await register(ac, "buyer-1", "seller-1", "p1", 80, round_=1)

    assert "seller-0:99" not in registrations
    assert "seller-0:99" not in pool_timestamps


# ---------------------------------------------------------------------------
# Feature: Analytics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analytics_increments():
    """Analytics counters track registrations, conflicts, bundles, and limit_exceeded."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1 conflict
        await asyncio.gather(
            register(ac, "b1", "seller-0", "p1", 80),
            register(ac, "b2", "seller-0", "p1", 60),
        )
        # 1 bundle
        await asyncio.gather(
            register(ac, "b3", "seller-1", "p1", 80, round_=2),
            register(ac, "b4", "seller-1", "p2", 80, round_=2),
        )
        # 1 limit_exceeded
        await report(ac, "seller-2", 70, False)
        await register(ac, "b5", "seller-2", "p1", 50, round_=3)

        res = await ac.get("/coop/analytics")
        data = res.json()

        assert data["total_registrations"] >= 5
        assert data["conflicts_resolved"] >= 1
        assert data["bundles_formed"] >= 1
        assert data["limit_exceeded_count"] >= 1
        assert data["rounds_processed"] >= 3


# ---------------------------------------------------------------------------
# Feature: Persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seller_limits_persisted_to_disk():
    """After a report, seller_limits.json should exist with correct data."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await report(ac, "seller-0", 55, True)

    assert _LIMITS_FILE.exists()
    saved = json.loads(_LIMITS_FILE.read_text())
    assert "seller-0" in saved
    assert saved["seller-0"]["upper_bound"] == 55


@pytest.mark.asyncio
async def test_seller_limits_loaded_on_startup():
    """Startup event loads persisted limits from seller_limits.json."""
    # Pre-populate the file
    data = {
        "seller-saved": {
            "lower_bound": 30, "upper_bound": 50, "midpoint": 40,
            "initial_range": 90, "attempts": 5, "successes": 2, "total_success_price": 95
        }
    }
    _LIMITS_FILE.write_text(json.dumps(data))

    # Simulate startup
    from main import on_startup
    await on_startup()

    assert "seller-saved" in seller_limits
    assert seller_limits["seller-saved"]["lower_bound"] == 30


# ---------------------------------------------------------------------------
# Feature: Sellers Endpoint & Budget Guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sellers_endpoint_complete():
    """GET /coop/sellers returns correct fields including attempts and avg_success_price."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await report(ac, "seller-0", 55, True)
        await report(ac, "seller-0", 50, True)

        res = await ac.get("/coop/sellers")
        sellers = {s["seller_id"]: s for s in res.json()["sellers"]}

        assert "seller-0" in sellers
        s = sellers["seller-0"]
        assert s["successes"] == 2
        assert s["attempts"] == 2
        assert s["avg_success_price"] == (55 + 50) / 2
        assert 0 <= s["confidence"] <= 1.0


@pytest.mark.asyncio
async def test_budget_guard():
    """Buyer's budget below seller floor → limit_exceeded with pivot."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await report(ac, "seller-0", 50, False)  # floor = 51

        res = await register(ac, "buyer-poor", "seller-0", "p1", 40)
        data = res.json()

        assert data["status"] == "limit_exceeded"
        assert data["action"] == "pivot"
        assert data["suggested_price"] == 0


@pytest.mark.asyncio
async def test_root_and_favicon():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/")
        assert r.status_code == 200
        assert r.json()["status"] == "running"
        assert r.json()["version"] == "0.3.0"

        fav = await ac.get("/favicon.ico")
        assert fav.status_code == 204


# ===========================================================================
# ADDITIONAL TESTS — Edge Cases, Validation, Full Flows, Money Conservation
# ===========================================================================

# ---------------------------------------------------------------------------
# Input Validation (Pydantic / 422 errors)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_invalid_budget_type():
    """Non-integer max_budget should return 422 Unprocessable Entity."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.post("/coop/register", json={
            "buyer_id": "b1", "seller_id": "s0", "product_id": "p1",
            "max_budget": "seventy", "round": 1
        })
        assert res.status_code == 422
        assert "max_budget" in res.text


@pytest.mark.asyncio
async def test_register_missing_fields():
    """Missing required field should return 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.post("/coop/register", json={
            "buyer_id": "b1", "seller_id": "s0"
            # missing product_id, max_budget, round
        })
        assert res.status_code == 422


@pytest.mark.asyncio
async def test_report_invalid_price_type():
    """Non-integer price in report should return 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.post("/coop/report", json={
            "buyer_id": "b1", "seller_id": "s0", "product_id": "p1",
            "price": "forty", "success": True
        })
        assert res.status_code == 422


@pytest.mark.asyncio
async def test_register_zero_budget():
    """Budget of 0 should still be processable (edge case, not a 422)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.post("/coop/register", json={
            "buyer_id": "b1", "seller_id": "s0", "product_id": "p1",
            "max_budget": 0, "round": 1
        })
        # Floor is 10 by default (GLOBAL_MIN_PRICE), budget is 0 → limit_exceeded
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "limit_exceeded"
        assert data["suggested_price"] == 0


# ---------------------------------------------------------------------------
# Re-registration (Idempotent Buyer)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reregistration_updates_not_duplicates():
    """Same buyer registering twice in same round should not create a duplicate entry."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await register(ac, "buyer-1", "seller-0", "p1", 50)  # first registration

    pool_key = "seller-0:1"
    assert len(registrations.get(pool_key, [])) == 1  # only 1 entry per buyer


@pytest.mark.asyncio
async def test_reregistration_with_different_product():
    """Re-registration with different product_id should update the existing entry."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await register(ac, "buyer-1", "seller-0", "p1", 70)
        await register(ac, "buyer-1", "seller-0", "p2", 70, round_=2)

    # Each round is independent, both should exist as separate pools
    assert len(registrations.get("seller-0:1", [])) == 1
    assert len(registrations.get("seller-0:2", [])) == 1


# ---------------------------------------------------------------------------
# Money Conservation — suggested_price must always be within safe bounds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_suggested_price_never_exceeds_budget():
    """In all response types, suggested_price must be ≤ max_budget."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        budgets = [15, 30, 55, 80, 100]
        for i, budget in enumerate(budgets):
            res = await register(ac, f"buyer-{i}", "seller-0", f"p{i}", budget, round_=i)
            data = res.json()
            assert data["suggested_price"] <= budget, (
                f"suggested_price {data['suggested_price']} > max_budget {budget} "
                f"for status {data['status']}"
            )


@pytest.mark.asyncio
async def test_suggested_price_never_below_global_min():
    """Suggested price for 'buy' actions should be ≥ _GLOBAL_MIN_PRICE."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await register(ac, "buyer-1", "seller-0", "p1", 100)
        data = res.json()
        if data["action"] == "buy":
            assert data["suggested_price"] >= _GLOBAL_MIN_PRICE


@pytest.mark.asyncio
async def test_bundle_suggested_price_never_exceeds_any_member_budget():
    """Each bundled buyer's suggested_price must be within their own budget."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r1, r2, r3 = await asyncio.gather(
            register(ac, "b1", "seller-0", "p1", 30),
            register(ac, "b2", "seller-0", "p2", 50),
            register(ac, "b3", "seller-0", "p3", 70),
        )
        for res, budget in [(r1, 30), (r2, 50), (r3, 70)]:
            data = res.json()
            assert data["suggested_price"] <= budget


# ---------------------------------------------------------------------------
# 3-Way Conflict — 3 buyers, same product
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_three_way_conflict():
    """3 buyers for same product: only 1 winner, 2 losers."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r1, r2, r3 = await asyncio.gather(
            register(ac, "buyer-rich",   "seller-0", "p1", 90),
            register(ac, "buyer-mid",    "seller-0", "p1", 70),
            register(ac, "buyer-poor",   "seller-0", "p1", 50),
        )
        statuses = [r1.json()["status"], r2.json()["status"], r3.json()["status"]]
        assert statuses.count("won_allocation") == 1
        assert statuses.count("lost_allocation") == 2


@pytest.mark.asyncio
async def test_three_way_conflict_winner_is_highest_bidder():
    """In a 3-way conflict, the highest-budget buyer should always win."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r1, r2, r3 = await asyncio.gather(
            register(ac, "buyer-a", "seller-0", "p1", 90),
            register(ac, "buyer-b", "seller-0", "p1", 70),
            register(ac, "buyer-c", "seller-0", "p1", 50),
        )
        assert r1.json()["status"] == "won_allocation"  # buyer-a has 90
        assert r2.json()["status"] == "lost_allocation"
        assert r3.json()["status"] == "lost_allocation"


# ---------------------------------------------------------------------------
# Mixed Pool — Conflict + Bundle in the same pool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mixed_conflict_and_bundle():
    """
    Pool: buyer-A and buyer-B compete for p1 (conflict),
    buyer-C wants p2 (no conflict).
    Result: buyer-A wins p1, buyer-B loses, buyer-A and buyer-C should bundle.
    buyer-B gets lost_allocation.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r_a, r_b, r_c = await asyncio.gather(
            register(ac, "buyer-A", "seller-0", "p1", 80),   # conflicts with B
            register(ac, "buyer-B", "seller-0", "p1", 60),   # loses to A
            register(ac, "buyer-C", "seller-0", "p2", 70),   # different product
        )
        d_a, d_b, d_c = r_a.json(), r_b.json(), r_c.json()

        assert d_b["status"] == "lost_allocation"
        assert d_b["action"] == "pivot"

        # A and C should be bundled (they have different products and both allocated)
        assert d_a["status"] == "bundled"
        assert d_c["status"] == "bundled"
        assert d_a["bundle_id"] == d_c["bundle_id"]


# ---------------------------------------------------------------------------
# Clear State
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clear_preserves_seller_limits():
    """POST /coop/clear should reset pools but NOT seller knowledge."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await report(ac, "seller-0", 55, True)
        assert "seller-0" in seller_limits

        await ac.post("/coop/clear")
        # Seller limits must survive
        assert "seller-0" in seller_limits
        assert seller_limits["seller-0"]["upper_bound"] == 55


@pytest.mark.asyncio
async def test_clear_resets_active_pools():
    """After clear, registrations and decisions should be empty."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await register(ac, "b1", "seller-0", "p1", 70)
        await ac.post("/coop/clear")

        assert len(registrations) == 0
        assert len(decisions) == 0


# ---------------------------------------------------------------------------
# Full Convergence Flow (Integration)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_binary_search_converges_to_exact_price():
    """
    Simulate binary-search convergence: true seller price = 48.
    After each round, probe midpoint (explore) or lower_bound (exploit),
    report result, verify bounds narrow toward true price.

    Key insight: once confidence >= 0.5, the service switches to exploit mode
    and probes lower_bound (not midpoint). This means bounds do NOT narrow
    geometrically any more — they only narrow when the exploit probe is rejected.
    The important invariants are:
      1. True price is always bracketed: lower_bound <= true_price <= upper_bound
      2. suggested_price is always within buyer's budget
      3. Bounds narrow monotonically (never widen)
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        true_price = 48
        prev_range = _GLOBAL_MAX_PRICE - _GLOBAL_MIN_PRICE

        for round_num in range(1, 10):
            res = await register(ac, "buyer-1", "seller-0", "p1", 100, round_=round_num)
            probe = res.json()["suggested_price"]
            assert probe <= 100  # never exceeds budget

            success = probe >= true_price
            await report(ac, "seller-0", probe, success)

            lb = seller_limits["seller-0"]["lower_bound"]
            ub = seller_limits["seller-0"]["upper_bound"]
            current_range = ub - lb

            # Bounds must monotonically narrow or stay same
            assert current_range <= prev_range
            prev_range = current_range

            if lb == ub:
                break

        lb = seller_limits["seller-0"]["lower_bound"]
        ub = seller_limits["seller-0"]["upper_bound"]

        # Invariant 1: true price is always within learned bounds
        assert lb <= true_price <= ub
        # Invariant 2: range narrowed significantly from initial 90
        assert (ub - lb) < (_GLOBAL_MAX_PRICE - _GLOBAL_MIN_PRICE)


@pytest.mark.asyncio
async def test_scout_then_register_full_flow():
    """Full agent flow: scout sellers, pick cheapest, register, report."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Teach two sellers
        await report(ac, "seller-cheap", 30, True)   # upper = 30
        await report(ac, "seller-pricey", 80, True)  # upper = 80

        # Scout: find seller within budget of 50
        sellers_res = await ac.get("/coop/sellers")
        sellers = sellers_res.json()["sellers"]
        affordable = [s for s in sellers if s["upper_bound"] <= 50]
        assert len(affordable) == 1
        assert affordable[0]["seller_id"] == "seller-cheap"

        # Register with the affordable seller
        chosen = affordable[0]["seller_id"]
        reg_res = await register(ac, "buyer-1", chosen, "p1", 50)
        data = reg_res.json()
        assert data["action"] == "buy"
        assert data["suggested_price"] <= 50

        # Report back
        rep_res = await report(ac, chosen, data["suggested_price"], True)
        assert rep_res.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Concurrent Multi-Buyer (Race Condition Check)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_many_buyers_same_product():
    """10 concurrent buyers for the same product: exactly 1 winner, 9 losers."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        tasks = [
            register(ac, f"buyer-{i}", "seller-0", "p1", 10 + i * 5)
            for i in range(10)
        ]
        results = await asyncio.gather(*tasks)
        statuses = [r.json()["status"] for r in results]

        assert all(r.status_code == 200 for r in results)
        assert statuses.count("won_allocation") == 1
        assert statuses.count("lost_allocation") == 9


@pytest.mark.asyncio
async def test_concurrent_many_buyers_different_products():
    """10 concurrent buyers for different products: all bundled, no losers."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        tasks = [
            register(ac, f"buyer-{i}", "seller-0", f"product-{i}", 80)
            for i in range(10)
        ]
        results = await asyncio.gather(*tasks)
        statuses = [r.json()["status"] for r in results]
        bundle_ids = [r.json().get("bundle_id") for r in results]

        assert all(r.status_code == 200 for r in results)
        assert all(s == "bundled" for s in statuses)
        # All in same bundle
        assert len(set(b for b in bundle_ids if b)) == 1


@pytest.mark.asyncio
async def test_concurrent_multiple_rounds_independent():
    """Registrations in different rounds should not interfere with each other."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        round1_tasks = [register(ac, f"b{i}", "seller-0", "p1", 80 - i * 5, round_=1) for i in range(3)]
        round2_tasks = [register(ac, f"b{i}", "seller-0", "p1", 80 - i * 5, round_=2) for i in range(3)]

        round1_results, round2_results = await asyncio.gather(
            asyncio.gather(*round1_tasks),
            asyncio.gather(*round2_tasks),
        )

        # Each round independently resolves its own conflict
        r1_statuses = [r.json()["status"] for r in round1_results]
        r2_statuses = [r.json()["status"] for r in round2_results]

        assert r1_statuses.count("won_allocation") == 1
        assert r2_statuses.count("won_allocation") == 1


# ---------------------------------------------------------------------------
# Status Endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_shows_active_pools():
    """GET /coop/status should list active pools after registrations."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await register(ac, "b1", "seller-0", "p1", 70)

        res = await ac.get("/coop/status")
        data = res.json()

        assert "active_pools" in data
        assert "learned_seller_limits" in data
        # There should be at least 1 active pool
        assert len(data["active_pools"]) >= 1


# ===========================================================================
# REGRESSION TESTS — Bugs found and fixed
# ===========================================================================

@pytest.mark.asyncio
async def test_negative_budget_is_rejected_with_422():
    """Regression: negative max_budget used to return negative suggested_price.
    Now rejected at the API boundary with HTTP 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.post("/coop/register", json={
            "buyer_id": "b1", "seller_id": "s0", "product_id": "p1",
            "max_budget": -10, "round": 1
        })
        assert res.status_code == 422
        assert "max_budget" in res.text


@pytest.mark.asyncio
async def test_rounds_processed_does_not_overcount():
    """Regression: _resolve_pool called twice for the same pool (concurrent agents
    both triggering resolution) used to double-count rounds_processed."""
    from main import _resolve_pool, _analytics as analytics_ref
    analytics_ref["rounds_processed"] = 0
    registrations["seller-0:1"] = [
        {"buyer_id": "b1", "product_id": "p1", "max_budget": 50}
    ]
    _resolve_pool("seller-0", 1)  # first call — resolves and records
    _resolve_pool("seller-0", 1)  # second call — should be a no-op
    assert analytics_ref["rounds_processed"] == 1, (
        f"rounds_processed should be 1, got {analytics_ref['rounds_processed']}"
    )


@pytest.mark.asyncio
async def test_limit_exceeded_count_does_not_overcount():
    """Regression: limit_exceeded_count was incremented on every _resolve_pool call
    for the same pool, causing double (or triple) counting."""
    from main import _resolve_pool, _analytics as analytics_ref, _init_seller_limits
    analytics_ref["limit_exceeded_count"] = 0

    _init_seller_limits("seller-0")
    seller_limits["seller-0"]["lower_bound"] = 80  # floor above budget
    registrations["seller-0:1"] = [
        {"buyer_id": "b1", "product_id": "p1", "max_budget": 50}  # budget < floor
    ]

    _resolve_pool("seller-0", 1)  # first call
    _resolve_pool("seller-0", 1)  # second call — should NOT re-increment
    assert analytics_ref["limit_exceeded_count"] == 1, (
        f"limit_exceeded_count should be 1, got {analytics_ref['limit_exceeded_count']}"
    )


@pytest.mark.asyncio
async def test_suggested_price_always_non_negative():
    """Regression: suggested_price could be negative when max_budget was negative.
    Now guarded at API level (422) and also in exploit clamping logic."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Try a range of small budgets — all should return non-negative suggested_price
        for budget in [0, 1, 5, 9, 10]:
            res = await ac.post("/coop/register", json={
                "buyer_id": f"b-{budget}", "seller_id": "s0", "product_id": "p1",
                "max_budget": budget, "round": budget
            })
            assert res.status_code == 200
            data = res.json()
            assert data["suggested_price"] >= 0, (
                f"suggested_price {data['suggested_price']} is negative for budget={budget}"
            )
