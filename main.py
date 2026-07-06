import asyncio
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

app = FastAPI(
    title="Co-op Purchase Coordinator API",
    description="Cooperative purchase and conflict resolution coordinator for NANDA Town agents",
    version="0.1.0"
)

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class RegistrationRequest(BaseModel):
    buyer_id: str
    seller_id: str
    product_id: str
    max_budget: int
    round: int

class RegistrationResponse(BaseModel):
    status: str  # "won_allocation", "lost_allocation", "bundled", "pending"
    action: str  # "buy", "pivot", "wait"
    suggested_price: int
    bundle_id: Optional[str] = None
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

# ---------------------------------------------------------------------------
# In-Memory Database State
# ---------------------------------------------------------------------------

# Key: f"{seller_id}:{round}" -> list of registration dicts
registrations: Dict[str, List[dict]] = {}

# Key: f"{seller_id}:{round}" -> Dict of buyer_id -> RegistrationResponse
decisions: Dict[str, Dict[str, RegistrationResponse]] = {}

# Key: f"{seller_id}:{round}" -> set of buyer_ids that have checked in / resolved
locks: Dict[str, asyncio.Lock] = {}

# Key: seller_id -> { "lower_bound": int, "upper_bound": int }
# Used to learn the seller's reservation price dynamically
seller_limits: Dict[str, dict] = {}

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
        "documentation": "/docs"
    }

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

@app.post("/coop/register", response_model=RegistrationResponse)
async def register_interest(req: RegistrationRequest):
    pool_key = f"{req.seller_id}:{req.round}"
    
    # Initialize lock for this pool if it doesn't exist
    if pool_key not in locks:
        locks[pool_key] = asyncio.Lock()
        
    async with locks[pool_key]:
        if pool_key not in registrations:
            registrations[pool_key] = []
        
        # Add or update registration
        # Remove existing if same buyer is re-registering
        registrations[pool_key] = [r for r in registrations[pool_key] if r["buyer_id"] != req.buyer_id]
        registrations[pool_key].append({
            "buyer_id": req.buyer_id,
            "product_id": req.product_id,
            "max_budget": req.max_budget
        })

    # Sleep briefly to allow other concurrent agent requests in the same simulation tick to register
    await asyncio.sleep(0.05)

    async with locks[pool_key]:
        # Run resolution logic if not already calculated
        if pool_key not in decisions or req.buyer_id not in decisions[pool_key]:
            _resolve_pool(req.seller_id, req.round)
            
        if pool_key in decisions and req.buyer_id in decisions[pool_key]:
            return decisions[pool_key][req.buyer_id]
            
        raise HTTPException(status_code=500, detail="Failed to resolve coordination for agent")


@app.post("/coop/report")
async def report_outcome(req: ReportRequest):
    seller_id = req.seller_id
    if seller_id not in seller_limits:
        # Default limits based on typical marketplace values
        seller_limits[seller_id] = {"lower_bound": 10, "upper_bound": 100}
        
    limits = seller_limits[seller_id]
    
    if req.success:
        # If transaction succeeded, the seller's minimum price is <= this price
        limits["upper_bound"] = min(limits["upper_bound"], req.price)
    else:
        # If transaction failed (rejected), the seller's minimum price is > this price
        limits["lower_bound"] = max(limits["lower_bound"], req.price + 1)
        
    # Ensure bounds stay valid
    if limits["lower_bound"] > limits["upper_bound"]:
        limits["lower_bound"] = limits["upper_bound"]
        
    return {"status": "ok", "seller_limits": limits}


@app.get("/coop/status", response_model=StatusResponse)
async def get_status():
    return StatusResponse(
        active_pools={k: v for k, v in registrations.items() if v},
        learned_seller_limits=seller_limits
    )


@app.post("/coop/clear")
async def clear_state():
    registrations.clear()
    decisions.clear()
    locks.clear()
    # We do NOT clear seller_limits because we want to preserve learned history across rounds/runs!
    return {"status": "cleared"}


# ---------------------------------------------------------------------------
# Helper Coordination Logic
# ---------------------------------------------------------------------------

def _resolve_pool(seller_id: str, round_id: int):
    pool_key = f"{seller_id}:{round_id}"
    pool = registrations.get(pool_key, [])
    if not pool:
        return

    # Initialize decisions container for this pool
    if pool_key not in decisions:
        decisions[pool_key] = {}

    # Step 1: Group by product_id to detect conflicts (same product)
    by_product: Dict[str, List[dict]] = {}
    for r in pool:
        by_product.setdefault(r["product_id"], []).append(r)

    allocated_buyers: List[dict] = []
    
    # Conflict Resolution
    for product_id, buyers in by_product.items():
        if len(buyers) == 1:
            # No conflict
            allocated_buyers.append(buyers[0])
        else:
            # Conflict! Sort by budget descending
            sorted_buyers = sorted(buyers, key=lambda x: x["max_budget"], reverse=True)
            winner = sorted_buyers[0]
            allocated_buyers.append(winner)
            
            # Second-price auction logic for suggested price
            second_price = sorted_buyers[1]["max_budget"]
            suggested = min(second_price + 1, winner["max_budget"])
            
            decisions[pool_key][winner["buyer_id"]] = RegistrationResponse(
                status="won_allocation",
                action="buy",
                suggested_price=suggested,
                message=f"You won conflict allocation for {product_id}. Suggested price is {suggested}."
            )
            
            # Mark all other buyers in this conflict pool as losers
            for loser in sorted_buyers[1:]:
                decisions[pool_key][loser["buyer_id"]] = RegistrationResponse(
                    status="lost_allocation",
                    action="pivot",
                    suggested_price=0,
                    message=f"Conflict for {product_id} won by another agent with a higher budget."
                )

    # Step 2: Check for wholesale bundling (different products from same seller)
    # Only buyers who won/got allocated products participate in bundling
    if len(allocated_buyers) >= 2:
        # We can bundle!
        bundle_id = f"bundle-{seller_id}-{round_id}"
        
        # Calculate pricing for each buyer in the bundle
        for b in allocated_buyers:
            buyer_id = b["buyer_id"]
            max_budget = b["max_budget"]
            
            # Get learned limits for this seller if available
            limits = seller_limits.get(seller_id)
            if limits and limits["lower_bound"] <= limits["upper_bound"]:
                # If we have narrowed down the seller's minimum price,
                # we can safely suggest the lower bound (plus 1 for safety),
                # saving the buyer money without risking rejection.
                suggested = min(limits["lower_bound"], max_budget)
            else:
                # Default wholesale discount: 20% off max budget
                suggested = max(10, int(max_budget * 0.8))
                
            decisions[pool_key][buyer_id] = RegistrationResponse(
                status="bundled",
                action="buy",
                suggested_price=suggested,
                bundle_id=bundle_id,
                message=f"Bundled wholesale purchase with other buyers. Coordinated price: {suggested}."
            )
    elif len(allocated_buyers) == 1:
        # Single buyer, no bundling possible
        single = allocated_buyers[0]
        buyer_id = single["buyer_id"]
        
        # If this buyer hasn't already been resolved via conflict logic above:
        if buyer_id not in decisions[pool_key]:
            limits = seller_limits.get(seller_id)
            if limits:
                suggested = min(limits["lower_bound"], single["max_budget"])
            else:
                suggested = single["max_budget"]
                
            decisions[pool_key][buyer_id] = RegistrationResponse(
                status="won_allocation",
                action="buy",
                suggested_price=suggested,
                message=f"No conflict. Suggested price is {suggested}."
            )

    # Step 3: Budget Guard / Limit Exceeded checks
    for r in pool:
        buyer_id = r["buyer_id"]
        max_budget = r["max_budget"]
        limits = seller_limits.get(seller_id)
        if limits and limits["lower_bound"] > max_budget:
            decisions[pool_key][buyer_id] = RegistrationResponse(
                status="limit_exceeded",
                action="pivot",
                suggested_price=0,
                message=f"Seller minimum price ({limits['lower_bound']}) exceeds your budget ({max_budget})."
            )
