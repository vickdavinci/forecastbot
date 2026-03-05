# ForecastBot

**Prediction Market Scanner & Edge Detector — ForecastEx Event Contracts via IBKR**

---

## Current Status

**Phase 0 — Observation Only (March 2026)**
- Two scanners running, no orders placed, no capital at risk
- Collecting data to validate edge frequency and depth before building execution engine
- Target: 14-30 days of observation data

---

## What This Is

ForecastBot monitors ForecastEx (CME Event Contracts) prediction market contracts via Interactive Brokers. It has two components running simultaneously:

### 1. kill_shot.py — Parity Arbitrage Scanner
Scans for YES+NO pairs that sum to less than $0.99 (should always = $1.00). When `YES_ask + NO_ask < $0.93`, buying both legs locks in guaranteed profit.

- Event-driven streaming — fires on every price tick, not a timer
- 3-tick confirmation before alerting (prevents false positives)
- Auto-reconnect, daily contract refresh at 09:31 ET

### 2. weather_edge.py — Directional Weather Edge Scanner
Monitors KLAX (LAX airport) actual temperature vs market-implied probability on UHLAX contracts.

**Core thesis:** Retail participants see the Weather Underground forecast, place a bet, and walk away. When actual temperature diverges from forecast — especially during Santa Ana wind events — the market price goes stale for 30-90 minutes. We catch that window.

- Bidirectional: BUY_YES (Santa Ana spike) and BUY_NO (temp trending below threshold)
- Santa Ana wind filter: suppresses NWS-based signals during offshore wind events
- NWS API observations every 5 minutes

---

## Strategy

| Strategy | Logic | Scanner |
|----------|-------|---------|
| **Parity Arb** | YES_ask + NO_ask < $0.93 → buy both → lock profit | kill_shot.py |
| **Weather Edge** | Actual temp diverges from forecast → market mispriced | weather_edge.py |

---

## Contract Universe

| Symbol | Category | Type | Scanner |
|--------|----------|------|---------|
| **CBBTC** | BTC daily close | OPT/FORECASTX | kill_shot |
| **METLS** | Silver daily price | OPT/FORECASTX | kill_shot |
| **FES** | S&P 500 daily futures | FOP/FORECASTX | kill_shot |
| **FF** | Fed Decision | OPT/FORECASTX | kill_shot |
| **YXHBT** | Bitcoin Highest Price 2026 | OPT/FORECASTX | kill_shot |
| **PNFED** | Presidential Fed Chair | OPT/FORECASTX | kill_shot |
| **JPDEC** | Bank of Japan Decision | OPT/FORECASTX | kill_shot |
| **UHLAX** | LA daily high temperature | OPT/FORECASTX | weather_edge |

---

## Architecture (Current — Phase 0)

```
┌─────────────────────────────────────────────┐
│           IB Gateway (127.0.0.1:4001)       │
│           ib_async streaming ticks          │
└────────┬──────────────────────┬─────────────┘
         │ clientId=10          │ clientId=45
         │                      │
┌────────▼──────────┐  ┌───────▼──────────────┐
│  kill_shot.py     │  │  weather_edge.py      │
│  Parity scanner   │  │  Weather edge scanner │
│  7 contracts      │  │  UHLAX + NWS API      │
│  Tick-by-tick     │  │  5-min poll cycle      │
│  3-tick confirm   │  │  Santa Ana filter      │
└────────┬──────────┘  └───────┬──────────────┘
         │                      │
┌────────▼──────────────────────▼──────────────┐
│            Output (observation only)          │
│  data/*.csv          Telegram alerts          │
│  all_ticks.csv       Gap alerts               │
│  gap_events.csv      Weather edge signals     │
│  gap_alerts.csv                               │
└──────────────────────────────────────────────┘
```

**No orders. No database. No execution engine.** Phase 0 is observation only.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11 |
| IBKR library | `ib_async` (NOT ib_insync) |
| Weather data | NWS API (api.weather.gov) |
| Persistence | CSV files in `data/` |
| Alerting | Telegram Bot API (optional) |
| Config | `.env` + python-dotenv |

---

## Project Structure

```
forecastbot/
├── CLAUDE.md                 # AI agent instructions
├── CLAUDE_CODE_HANDOFF.md    # Handoff context document
├── README.md                 # This file
├── WORKBOARD.md              # Current task tracking
├── ERRORS.md                 # Known errors and solutions
├── PROCESS.md                # Workflow gates, commit contract
├── SPECV2.md                 # Full system specification
├── README_PHASE0.md          # Phase 0 run guide
│
├── kill_shot.py              # Parity gap scanner (v2.0)
├── weather_edge.py           # Weather edge scanner (v3.0)
├── weather_edge_old.py       # Previous version (archive)
├── discover_contracts.py     # Contract discovery tool
├── discover_contracts1.py    # Earlier discovery version
├── what_exists.py            # Utility script
│
├── requirements.txt          # ib_async, python-dotenv, requests
├── .env                      # Local config (git-ignored)
├── data/                     # CSV log output
├── archive/                  # Archived files
└── venv/                     # Python virtual environment
```

---

## Getting Started

```bash
# Prerequisites: Python 3.11, IB Gateway running at 127.0.0.1:4001

cd forecastbot
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create .env (see .env for reference)
# IBKR_HOST=127.0.0.1
# IBKR_PORT=4001
# IBKR_CLIENT_ID=10
# IBKR_CLIENT_ID_WEATHER=45

# Step 1: Discover contracts
python3 discover_contracts.py

# Step 2: Run parity scanner (continuous)
python3 kill_shot.py

# Step 3: Run weather edge scanner (alongside, separate terminal)
python3 weather_edge.py
```

---

## Capital Allocation

| Bucket | Amount | Purpose |
|--------|--------|---------|
| Observation allocation | $5K-$10K USD | Available when execution engine built |
| Currently at risk | **$0** | Phase 0 — observation only |

---

## Phase Gates

| Gate | Pass Condition | Status |
|------|----------------|--------|
| **0 — Observation** | 14-30 days data collected, Decision Matrix evaluated | **IN PROGRESS** |
| 1 — Infrastructure | ib_async connects, PostgreSQL, Telegram, ATM filter | BLOCKED |
| 2 — Execution | Both-leg fills on paper > 80%, carry deployed | BLOCKED |
| 3 — Risk + Live Probe | Kill switch tiers, $2K CAD live probe | BLOCKED |
| 4 — Full Deployment | Full capital, 7 days unattended, first catalyst captured | BLOCKED |

---

## Critical Rules (Apply When Execution Engine Built)

1. **ASK prices only** — never bid, never last traded
2. **Single order submitter** — only one component places orders
3. **Risk engine runs before execution** — every time
4. **Reconcile positions on every startup**
5. **ForecastEx P&L = $1.00 - (yes_cost + no_cost)** — no sell orders exist
6. **Log every rejected opportunity with a drop code**

---

*Private — Not for distribution*
