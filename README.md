# Nomad Curie — NSE F&O Algorithmic Trading Platform

A full-stack algorithmic trading platform for NSE F&O with AI-powered trade analysis via Claude (CURIE agent), paper/live trading, real-time WebSocket data, and comprehensive analytics.

## Architecture

```
nomad-curie/
├── backend/              FastAPI + Python
│   ├── brokers/          Fyers, Upstox, 5Paisa adapters
│   ├── paper_engine/     Paper trading order book + portfolio
│   ├── live_engine/      Live order manager + risk manager
│   ├── market_data/      Data router, option chain, market profile
│   ├── analytics/        Greeks, performance, sector rotation
│   ├── agent/            CURIE AI agent (Claude claude-sonnet-4-20250514)
│   ├── api/              FastAPI routers + WebSocket handlers
│   ├── db/               SQLAlchemy models + Alembic migrations
│   └── core/             Config, security (token encryption)
├── frontend/             Next.js 14 (TypeScript, Tailwind)
│   └── src/
│       ├── app/          Pages: dashboard, trading, analytics, market, agent, settings
│       ├── components/   Reusable UI components
│       ├── store/        Zustand state management
│       └── lib/          API client (Axios + TanStack Query), WebSocket
└── docker-compose.yml    TimescaleDB + Redis + backend + frontend
```

## Quick Start

### 1. Prerequisites
- Docker Desktop (for TimescaleDB + Redis)
- Python 3.11+
- Node.js 20+

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your API keys
```

### 3. Start infrastructure
```bash
docker compose up db redis -d
```

### 4. Run backend
```bash
cd backend
pip install -r requirements.txt
# Run DB migrations
alembic upgrade head
# Start server
uvicorn main:app --reload --port 8000
```

### 5. Run frontend
```bash
cd frontend
npm install
npm run dev
# Opens at http://localhost:3000
```

### 6. Full stack with Docker
```bash
docker compose up --build
```

## Key Features

### Broker Support
| Broker | Auth | REST | WebSocket | Options |
|--------|------|------|-----------|---------|
| Fyers  | OAuth2 → auth_code | ✅ | ✅ FyersDataSocket | ✅ |
| Upstox | OAuth2 PKCE | ✅ | ✅ MarketDataStreamer v3 | ✅ |
| 5Paisa | TOTP | ✅ | ✅ Market feed | ⚠️ |

### Paper Trading
- Simulates fills with 5bps slippage on market orders
- Bracket orders with auto-cancel of opposing legs
- MTM P&L on every tick
- Metrics: Sharpe, win rate, profit factor, max drawdown

### CURIE AI Agent
- **Tier 1**: Fast rules engine (IV rank > 80, PCR extreme, Market Profile breakout, POC reclaim)
- **Tier 2**: Claude claude-sonnet-4-20250514 deep analysis with tool use (option chain, greeks, sector rotation)
- Auto-executes HIGH confidence proposals in paper mode
- Approval queue for live mode (5-minute TTL)

### Market Data
- Real-time WebSocket ticks → Redis pub/sub → frontend
- Market Profile (TPO, POC, VAH, VAL, Initial Balance)
- Option chain analytics (PCR, max pain, gamma exposure, IV rank)
- TimescaleDB hypertables for tick/option chain history

### Risk Management
- Max loss per trade (default ₹5,000)
- Max daily loss with auto-disable (default ₹15,000)
- Max open positions (default 5)
- Concentration limit (40% per symbol)
- Duplicate order guard (5-second window)
- Kill switch (cancels all orders across all brokers)

## API Reference

Backend docs available at `http://localhost:8000/docs`

Key endpoints:
- `POST /api/auth/connect-broker` — authenticate with broker
- `POST /api/trading/orders` — place order (paper or live)
- `GET /api/market/option-chain/{symbol}` — option chain with PCR, max pain
- `GET /api/market/market-profile/{symbol}` — TPO profile
- `GET /api/analytics/performance` — P&L metrics
- `GET /api/agent/proposals` — pending CURIE proposals
- `POST /api/agent/chat` — chat with CURIE
- `WS /ws/ticks/{symbol}` — real-time tick stream
- `WS /ws/positions` — real-time position updates
- `WS /ws/proposals` — real-time agent proposals

## Environment Variables

See `.env.example` for all required variables.

Critical ones:
- `DATABASE_URL` — PostgreSQL+TimescaleDB connection
- `REDIS_URL` — Redis connection
- `SECRET_KEY` — for encrypting broker tokens at rest
- `ANTHROPIC_API_KEY` — for CURIE agent (Claude API)
- `FYERS_APP_ID` / `FYERS_SECRET` — Fyers API credentials
- `UPSTOX_API_KEY` / `UPSTOX_SECRET` — Upstox credentials
- `FIVEPAISA_*` — 5Paisa credentials

## Development Notes

### Adding a new broker
1. Create `backend/brokers/newbroker.py` extending `BrokerAdapter`
2. Implement all abstract methods
3. Register in `backend/brokers/__init__.py` BROKER_MAP

### Database migrations
```bash
cd backend
alembic revision --autogenerate -m "description"
alembic upgrade head
```

### Running tests
```bash
cd backend
pytest tests/ -v
```

## Disclaimer
This platform is for educational and research purposes. Paper trading mode is safe for experimentation. Live trading involves real financial risk. Always test thoroughly in paper mode before enabling live trading. The authors are not responsible for financial losses.
