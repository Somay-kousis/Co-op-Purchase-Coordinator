# Co-op Purchase Coordinator

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB.svg" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-backend-009688.svg" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Pydantic-v2-E92063.svg" alt="Pydantic" />
  <img src="https://img.shields.io/badge/Docker-python%203.12--slim-2496ED.svg" alt="Docker" />
  <img src="https://img.shields.io/badge/Google%20Cloud%20Run-deploy-4285F4.svg" alt="Google Cloud Run" />
</p>

**v0.3.0** — Cooperative purchase and conflict resolution coordinator for NANDA Town agents.

## What it does

Exposes a REST API that AI agents call before buying from a seller. The service:

- **Resolves conflicts** when multiple agents want the same product (second-price / Vickrey auction, Nash-optimal floor-adjusted bid)
- **Bundles wholesale** when agents want different products from the same seller (Shapley-inspired coalition discount)
- **Converges on seller prices** using binary search + explore-exploit strategy (`~7 rounds` to exact price)
- **Blocks wasted bids** via a budget guard that tells buyers to pivot when the known seller floor exceeds their budget
- **Routes to alternatives** by recommending the cheapest known seller when a buyer needs to pivot

## Quick Start

```bash
# Install
pip install "fastapi>=0.110.0" "uvicorn[standard]>=0.28.0" "pydantic>=2.6.0"

# Run locally
uvicorn main:app --reload

# Open docs
open http://localhost:8000/docs
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/coop/register` | Register purchase intent (call before every buy attempt) |
| `POST` | `/coop/report` | Report transaction outcome (always call after each attempt) |
| `GET` | `/coop/sellers` | Browse known sellers with confidence + stats |
| `GET` | `/coop/analytics` | Aggregate coordination statistics |
| `GET` | `/coop/status` | Active pool state + learned seller limits |
| `POST` | `/coop/clear` | Reset pool state (preserves seller knowledge) |

See [SKILL.md](SKILL.md) for full agent integration guide with examples.

## Run Tests

```bash
pip install pytest pytest-asyncio anyio httpx
pytest -v
```

**46 tests** covering: conflict resolution, bundling, Nash pricing, explore-exploit, group discounts, seller routing, pool TTL, analytics, persistence, input validation, concurrency, and 3 regression bugs.

## Deploy

### Docker

```bash
docker build -t coop-coordinator .
docker run -p 8080:8080 coop-coordinator
```

### Google Cloud Run

```bash
gcloud run deploy coop-coordinator \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8080
```

## Mathematical Foundations

- **Binary search**: converges on seller's reservation price in `log₂(90) ≈ 7` rounds
- **Explore-exploit**: probe midpoint when `confidence < 0.5`; bid floor when `confidence ≥ 0.5`  
- **Nash-optimal auction**: winner pays `max(second_price + 1, seller_floor)` — prevents wasted rounds below seller floor
- **Shapley discount**: 2 buyers → 5% off, 3 → 12% off, 4+ → 20% off midpoint probe price
- **Confidence**: `1 - (current_range / 90)` — 0.0 = no info, 1.0 = exact price known