# Co-op Purchase Coordinator

A small FastAPI service that buyer agents in NANDA Town call before they bid on a seller. It stops agents from outbidding each other for the same item, groups agents buying from the same seller into one discounted bid, and learns each seller's lowest price from past outcomes. v0.3.0, Python 3.12+.

## What it does

Each buyer registers its intent (seller, product, max budget) before buying and reports the result after. From those reports the service decides:

- **Same product, several buyers:** a second-price auction. The highest budget wins and is told to offer `max(second_highest_budget + 1, known_seller_floor)`, capped at its own budget, so it does not offer below a price the seller already rejected. Losers are told to pivot, with the cheapest known seller they can afford.
- **Different products, same seller:** a group bid with a discount on the probe price: 2 buyers 5% off, 3 buyers 12% off, 4 or more 20% off.
- **Seller price:** binary search over the marketplace range 10 to 100. A rejected offer raises the lower bound, an accepted one lowers the upper bound. Probing the midpoint halves the range each round, so the floor is found in about 7 rounds (log2 90). `confidence = 1 - current_range / 90`. Below 0.5 the service probes the midpoint; at 0.5 or above it bids the known floor.
- **Budget guard:** if the known floor is above a buyer's budget, the buyer is told to pivot and pointed at the cheapest known seller it can afford.

46 tests cover conflict resolution, bundling, pricing, routing, pool expiry, analytics, persistence, input validation, concurrent buyers, and regressions for 3 fixed bugs.

## API

| Method | Path | Use |
|--------|------|-----|
| `POST` | `/coop/register` | Register intent. Call before every buy attempt. |
| `POST` | `/coop/report` | Report the outcome. Call after every attempt. |
| `GET` | `/coop/sellers` | Known sellers with bounds, confidence and success stats |
| `GET` | `/coop/analytics` | Totals across all rounds |
| `GET` | `/coop/status` | Active pools and learned seller limits |
| `POST` | `/coop/clear` | Reset pools. Keeps what it learned about sellers. |

[SKILL.md](SKILL.md) is the integration guide for agents, with request and response examples.

## Run it

```bash
pip install "fastapi>=0.110.0" "uvicorn[standard]>=0.28.0" "pydantic>=2.6.0"
uvicorn main:app --reload
# docs at http://localhost:8000/docs
```

Tests:

```bash
pip install pytest pytest-asyncio anyio httpx
pytest -v
```

Docker:

```bash
docker build -t coop-coordinator .
docker run -p 8080:8080 coop-coordinator
```

Google Cloud Run:

```bash
gcloud run deploy coop-coordinator --source . --region us-central1 --allow-unauthenticated --port 8080
```

## Limitations

- Pools live in memory and the container runs one uvicorn worker. Running more than one instance splits the pools.
- Learned seller limits are saved to `seller_limits.json` next to `main.py`. On Cloud Run that disk is wiped when the instance restarts.
- No authentication. Any caller can register, report or clear.
- Pools older than 5 minutes are dropped.
