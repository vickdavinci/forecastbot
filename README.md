# ForecastBot

**24/7 Parity Arbitrage Bot — ForecastEx Event Contracts via IBKR**

---

## What This Is

ForecastBot monitors ForecastEx prediction market contracts (BTC, S&P 500, Nasdaq) 24/7 via Interactive Brokers. When YES_ask + NO_ask on any contract drops below $0.93, it executes both legs simultaneously, locking in guaranteed profit regardless of outcome.

**The math is permanent.** YES + NO always pays $1.00 at settlement. Buying both for less than $1.00 is risk-free profit. The edge exists because ForecastEx is illiquid and nobody else is automating it.

---

## Shared Agent Workflow

- Agent contract for both Claude Code and Codex: `PROCESS.md`
- Wake-up protocol after compaction: top of `CLAUDE.md`
- Current task: `WORKBOARD.md`
- Known errors: `ERRORS.md`

---

## Strategy

| Strategy | Logic | When Active |
|----------|-------|-------------|
| **S1 — Parity Arb** | YES_ask + NO_ask < $0.93 → buy both → lock profit | During catalyst events (3–8×/month) |
| **S2 — Carry Harvest** | Hedged pairs earning 3.14% APY on idle capital | Always — capital never idle |

**Phase 1 only.** No MUTEX baskets, no monotonic chains, no calendar spreads. Those are Phase 2 after parity engine is proven.

---

## Contract Universe

Three categories. All others excluded.

| Category | Why | Hours |
|----------|-----|-------|
| **BTC Highest Price 2026** | 24/7 underlying, weekend gaps, no competition 3AM | 168h/week |
| **S&P 500 Futures (monthly)** | Highest OI (31.9K+), gaps on every NFP/CPI/FOMC | ~65h/week |
| **Nasdaq Futures (monthly)** | Moves with ES, larger gaps ($0.09 confirmed) | ~65h/week |

**ATM filter:** Only monitor contracts where YES_bid is between 15% and 85%. Recalculated every 60 seconds.

---

## Market Reality (Confirmed Live March 4, 2026)

```
S&P Above 6,600:
  Last trade: 2 days ago
  Trades today: 0
  Current sum: $1.01 — NO arb right now

NO price 1-month chart:
  Hit $0.14 in late February (sum ~$0.89, gap $0.11)
  Persisted for hours
  Nobody captured it — that's the entire edge
```

**Gaps are episodic, not continuous.** The bot does nothing most of the time, then executes with full conviction during catalyst events.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│               CONTRACT UNIVERSE                       │
│   BTC (24/7)     S&P (mkt hours)    NQ (mkt hours)   │
│   ATM filter — near-ATM only, updates every 60s      │
└──────────────────────┬───────────────────────────────┘
                       │ bid/ask ticks via ib_async
┌──────────────────────▼───────────────────────────────┐
│              CATALYST CALENDAR                        │
│  NORMAL (10s scan)  ←→  CATALYST (500ms scan)        │
│  Switches T-30min before HIGH/CRITICAL events        │
│  Stays CATALYST for 4h after event                   │
└──────────────────────┬───────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────┐
│              GAP DETECTOR                             │
│  sum = yes_ask + no_ask (ASK ONLY — never bid)       │
│  GapSignal.EXECUTE  → sum < 0.93                     │
│  GapSignal.ALERT    → sum < 0.97 and falling fast    │
│  GapSignal.NONE     → no action                      │
└──────────────────────┬───────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────┐
│              VALIDATOR (8 gates)                      │
│  Stale price → OI limit → capital → position limit   │
│  Every rejection logged with DROP CODE               │
└──────────────────────┬───────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────┐
│              EXECUTOR (ONLY order submitter)          │
│  Phase 1: both legs within 200ms                     │
│  Phase 2: chase unfilled leg ($0.01 × 3 retries)    │
│  Phase 3: unwind filled leg if hedge impossible      │
└──────────────────────┬───────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────┐
│        POSITION MANAGER + RISK ENGINE                 │
│  Capital state: CARRY_ONLY → ALERT → PARTIAL_ARB     │
│  Kill switch: T1 WARNING / T2 DEFENSIVE / T3 KILL    │
│  Carry harvest: idle capital earning 3.14% APY       │
└──────────────────────────────────────────────────────┘
```

**Key rule:** `executor.py` is the ONLY component that submits orders to IBKR. Nothing else calls `ib.placeOrder()`.

---

## Capital Allocation

| Bucket | Amount USD | Purpose |
|--------|-----------|---------|
| Total working capital | $22,050 | $30K CAD × 0.735 |
| Reserve (never deployed) | $2,205 | 10% hard floor |
| Active (arb + carry) | $19,845 | 90% always working |
| Max single arb trade | $8,000 | Position sizing cap |
| Min single arb trade | $2,000 | Skip below this |
| Max concurrent positions | 3 | Hard limit |

---

## Risk Management

| Tier | Trigger | Action |
|------|---------|--------|
| **T1 WARNING** | Unhedged leg > 15s OR loss > $50 | No new arb, prioritise fill |
| **T2 DEFENSIVE** | Unhedged > 40s OR daily loss > $200 | Stop new arb, manage existing |
| **T3 KILL** | Unhedged > 90s OR daily loss > $500 OR API disconnect > 60s | Close all, halt, alert, manual restart |

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11 |
| IBKR library | `ib_async` (NOT ib_insync — dead since 2024) |
| Architecture | Single-process asyncio |
| Database | PostgreSQL |
| Deployment | VPS Ubuntu 22.04, IB Gateway daemon |
| Alerting | Telegram Bot API |
| Scheduling | APScheduler |
| Logging | structlog JSON |

---

## Project Structure

```
forecastbot/
├── config.py                 # ALL tunable parameters — never hardcode
├── main.py                   # Async entry point
├── CLAUDE.md                 # AI agent instructions
├── PROCESS.md                # Workflow gates, commit contract
├── WORKBOARD.md              # Current task tracking
├── SPEC.md                   # Full system specification
├── ERRORS.md                 # Known errors and solutions
│
├── core/                     # Connection, market data, models
├── universe/                 # Contract discovery, catalyst calendar, gap detector
├── strategies/               # S1 parity, S2 carry
├── execution/                # Validator, executor, fill tracker
├── positions/                # Position manager, reconciler, P&L
├── risk/                     # Kill switch, Telegram alerts
├── persistence/              # PostgreSQL
├── scripts/                  # test_connection, discover_contracts, simulate_gap
└── tests/                    # Unit + integration tests
```

---

## Getting Started

```bash
# Prerequisites
# - Python 3.11
# - PostgreSQL running
# - IB Gateway (paper account) running on port 7497
# - Telegram bot token + chat ID in .env

git clone <repo>
cd forecastbot
cp .env.example .env          # Fill in IBKR credentials, Telegram token
make setup                    # Create venv, install deps
python scripts/test_connection.py   # Verify IBKR connects
make test                     # All tests pass
```

---

## Phase Gates

| Gate | Pass Condition |
|------|----------------|
| **0 — Infrastructure** | ib_async connects, contracts discovered, bid/ask streaming, PostgreSQL writes, Telegram fires |
| **1 — Catalyst Monitor** | Mode switches NORMAL↔CATALYST, gap formation detected, ATM filter working |
| **2 — Execution** | Both-leg fills on paper > 80%, zero unhedged > 30s, carry deployed, positions survive restart |
| **3 — Risk + Live Probe** | Kill switch tiers all working, live $2K CAD fills within $0.01 of detected gap |
| **4 — Full Deployment** | $30K CAD live, 7 days without manual intervention, first catalyst event captured |

---

## Expected Returns (Base Case)

| Scenario | Gap Events/Month | Monthly USD | Monthly CAD | Annual Return |
|----------|-----------------|-------------|-------------|---------------|
| Pessimistic | 1 | ~$880 | ~$1,197 | ~49% |
| **Base Case** | **4** | **~$3,960** | **~$5,386** | **~196%** |
| Optimistic | 8 | ~$8,800 | ~$11,968 | ~470% |
| Carry baseline | — | ~$51 | ~$69 | floor |

*Base case unconfirmed — 2-week observation at Gate 1 establishes actual frequency.*

---

## Key Configuration

All parameters in `config.py`. Never hardcode.

```python
ENTRY_THRESHOLD = 0.93          # Max YES_ask + NO_ask to enter
MIN_NET_PROFIT = 0.02           # Min profit after slippage estimate
MAX_SINGLE_TRADE_USD = 8000     # Hard cap per trade
MAX_CONCURRENT_POSITIONS = 3    # Hard limit
SCAN_INTERVAL_NORMAL_SEC = 10   # Normal mode
SCAN_INTERVAL_CATALYST_MS = 500 # Catalyst mode
KILL_T3_DAILY_LOSS_USD = 500    # Nuclear stop
```

---

## Critical Rules

1. **ASK prices only** — never bid, never last traded
2. **executor.py is the only order submitter** — nothing else calls `ib.placeOrder()`
3. **Risk engine runs before execution** — every time
4. **Reconcile positions on every startup** — DB vs IBKR broker
5. **Catalyst mode for 4h post-event** — most gaps open after, not at, release time
6. **ForecastEx P&L = $1.00 − (yes_cost + no_cost)** — no sell orders exist
7. **Log every rejected opportunity with a drop code** — this is how you debug silence

---

*Private — Not for distribution*
