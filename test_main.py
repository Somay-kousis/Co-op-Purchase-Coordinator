import asyncio
import pytest
from httpx import ASGITransport, AsyncClient
from main import app, registrations, decisions, seller_limits

@pytest.fixture(autouse=True)
def clear_db():
    registrations.clear()
    decisions.clear()
    seller_limits.clear()

@pytest.mark.asyncio
async def test_conflict_resolution():
    # Test that when two buyers compete for the same product, the one with the higher budget wins
    # and pays second_price + 1
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Register Buyer 1 with max_budget 80
        req1 = ac.post("/coop/register", json={
            "buyer_id": "buyer-1",
            "seller_id": "seller-0",
            "product_id": "product-1",
            "max_budget": 80,
            "round": 1
        })
        # Register Buyer 2 with max_budget 60
        req2 = ac.post("/coop/register", json={
            "buyer_id": "buyer-2",
            "seller_id": "seller-0",
            "product_id": "product-1",
            "max_budget": 60,
            "round": 1
        })
        
        res1, res2 = await asyncio.gather(req1, req2)
        
        assert res1.status_code == 200
        assert res2.status_code == 200
        
        data1 = res1.json()
        data2 = res2.json()
        
        # Buyer 1 (higher budget) should win and pay 61 (second-price + 1)
        assert data1["status"] == "won_allocation"
        assert data1["action"] == "buy"
        assert data1["suggested_price"] == 61
        
        # Buyer 2 should lose
        assert data2["status"] == "lost_allocation"
        assert data2["action"] == "pivot"
        assert data2["suggested_price"] == 0

@pytest.mark.asyncio
async def test_wholesale_bundling():
    # Test that when two buyers want different products from the same seller,
    # they get a bundle discount (default 20% off max budget)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        req1 = ac.post("/coop/register", json={
            "buyer_id": "buyer-1",
            "seller_id": "seller-0",
            "product_id": "product-1",
            "max_budget": 100,
            "round": 1
        })
        req2 = ac.post("/coop/register", json={
            "buyer_id": "buyer-2",
            "seller_id": "seller-0",
            "product_id": "product-2",
            "max_budget": 80,
            "round": 1
        })
        
        res1, res2 = await asyncio.gather(req1, req2)
        
        assert res1.status_code == 200
        assert res2.status_code == 200
        
        data1 = res1.json()
        data2 = res2.json()
        
        # Should be bundled with a shared bundle_id
        assert data1["status"] == "bundled"
        assert data2["status"] == "bundled"
        assert data1["bundle_id"] == data2["bundle_id"]
        
        # Default discount is 20%
        assert data1["suggested_price"] == 80
        assert data2["suggested_price"] == 64

@pytest.mark.asyncio
async def test_learned_seller_limits():
    # Test that reports refine the suggested price
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Report a failed purchase at 40 (meaning seller's reservation price is > 40)
        rep1 = await ac.post("/coop/report", json={
            "buyer_id": "buyer-1",
            "seller_id": "seller-0",
            "product_id": "product-1",
            "price": 40,
            "success": False
        })
        assert rep1.status_code == 200
        assert seller_limits["seller-0"]["lower_bound"] == 41
        
        # Report a successful purchase at 50 (meaning seller's reservation price is <= 50)
        rep2 = await ac.post("/coop/report", json={
            "buyer_id": "buyer-1",
            "seller_id": "seller-0",
            "product_id": "product-1",
            "price": 50,
            "success": True
        })
        assert rep2.status_code == 200
        assert seller_limits["seller-0"]["upper_bound"] == 50
        
        # Now, registering a buyer should result in a suggested price of R = lower_bound = 41
        # (since R is narrowed down to [41, 50])
        res = await ac.post("/coop/register", json={
            "buyer_id": "buyer-2",
            "seller_id": "seller-0",
            "product_id": "product-2",
            "max_budget": 80,
            "round": 1
        })
        assert res.status_code == 200
        data = res.json()
        assert data["suggested_price"] == 41

@pytest.mark.asyncio
async def test_budget_guard():
    # Test that if a seller's learned lower bound exceeds the buyer's budget,
    # the buyer is advised to pivot
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Learn that seller-0 has lower bound of 51
        await ac.post("/coop/report", json={
            "buyer_id": "buyer-1",
            "seller_id": "seller-0",
            "product_id": "product-1",
            "price": 50,
            "success": False
        })
        
        # Now, register a buyer with budget 40 (which is < 51)
        res = await ac.post("/coop/register", json={
            "buyer_id": "buyer-2",
            "seller_id": "seller-0",
            "product_id": "product-1",
            "max_budget": 40,
            "round": 1
        })
        
        assert res.status_code == 200
        data = res.json()
        
        assert data["status"] == "limit_exceeded"
        assert data["action"] == "pivot"
        assert data["suggested_price"] == 0
        assert "exceeds your budget" in data["message"]

@pytest.mark.asyncio
async def test_root_and_favicon():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res_root = await ac.get("/")
        assert res_root.status_code == 200
        data = res_root.json()
        assert "title" in data
        assert data["status"] == "running"
        
        res_fav = await ac.get("/favicon.ico")
        assert res_fav.status_code == 204
