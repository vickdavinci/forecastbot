# WORKBOARD.md — ForecastBot Task Tracking

**Current Phase: PHASE 0 — Kill-Shot Test**
**Status: NOT STARTED**
**Last Updated: March 4, 2026**

---

## ⚡ ACTIVE TASK — PHASE 0 KILL-SHOT (READ THIS FIRST)

```
TASK: Phase 0 Kill-Shot — Depth + Frequency Truth
HOURS: 2–4 total
COST: $0 (paper account only — NO real capital)
OUTPUT: Two numbers that decide everything:
          (1) Order book depth at ask price
          (2) Gap frequency per week

DO NOT BUILD ANYTHING ELSE UNTIL THIS IS DONE.
DO NOT START GATE 1 UNTIL DECISION MATRIX IS EVALUATED.
```

### What to Build — Two Scripts Only

```
scripts/
├── discover_contracts.py   ← Run first (30 min)
└── kill_shot.py            ← Run second (30 min)

That is the entire Phase 0 codebase.
Nothing else. No database. No position manager.
No execution engine. No Telegram bot beyond a simple alert.
```

---

### Script 1 — discover_contracts.py

**Purpose:** Find all active ForecastEx contracts, print conIds for near-ATM strikes.
**Output:** A list to populate WATCH_CONTRACTS in kill_shot.py.

```
READ FIRST: SPEC.md §3 (Contract Universe) and §5 (Contract Universe Manager)

TASK:
  Connect to IBKR paper account (port 7497 — NOT 7496)
  Call reqContractDetails for three categories:
    symbol="ES",  secType="OPT", exchange="FORECASTX"
    symbol="BTC", secType="OPT", exchange="FORECASTX"
    symbol="NQ",  secType="OPT", exchange="FORECASTX"

  For each contract returned:
    Print: symbol, strike, expiry, right (C=YES / P=NO), conId

  Filter and highlight near-ATM only:
    YES bid between 15% and 85% of $1.00
    (Skip contracts with no bid — deep ITM or OTM)

  Output format (one line per contract):
    BTC   90000   20261231   YES(C)   conId=12345   bid=0.61  ask=0.64
    BTC   90000   20261231   NO(P)    conId=12346   bid=0.22  ask=0.25
    SP    6600    20260331   YES(C)   conId=23456   bid=0.74  ask=0.78
    SP    6600    20260331   NO(P)    conId=23457   bid=0.18  ask=0.21

DO NOT TOUCH: kill_shot.py (not written yet)

TEST: Script runs, prints contracts, exits cleanly. No errors.

COMMIT: feat(phase0): discover_contracts — list near-ATM ForecastEx contracts
```

---

### Script 2 — kill_shot.py

**Purpose:** Pull live depth + bid/ask on all near-ATM contracts. Log every gap >= $0.02.
Send Telegram alert per gap found. Run for 2 weeks and output daily summary.

```
READ FIRST: SPEC.md §4 (Liquidity Engine) and §6.1 (Validation Gates 1-2)

POPULATE: WATCH_CONTRACTS list using output from discover_contracts.py
          (manually copy the near-ATM conIds before running)

TASK:
  1. Connect to IBKR paper account (port 7497)

  2. For each contract pair in WATCH_CONTRACTS:
       Call reqMktDepth(contract, numRows=5)   <- THE KEY CALL
       Call reqMktData(contract)
       Wait 2 seconds for data to arrive

       Extract:
         yes_ask   = yes_ticker.ask
         no_ask    = no_ticker.ask
         yes_depth = yes_ticker.domAsks[0].size (first level ask depth)
         no_depth  = no_ticker.domAsks[0].size

       Calculate:
         sum              = yes_ask + no_ask
         gap              = 1.00 - sum
         depth            = min(yes_depth, no_depth)
         max_position_usd = depth * sum
         max_profit_usd   = depth * gap

  3. Log every contract regardless of gap:
       Print: contract, yes_ask, no_ask, sum, gap, depth, max_profit
       Append to kill_shot_log.csv (timestamp, contract, sum, gap, depth)

  4. If gap >= 0.02 AND depth > 0:
       Send Telegram alert:
         "GAP: {contract}
          Sum={sum:.3f}  Gap={gap:.3f}
          Depth={depth} contracts
          Max position: ${max_position_usd:.0f}
          Max profit:   ${max_profit_usd:.0f}"

  5. Repeat scan every 60 seconds (NORMAL mode)
     Switch to every 10 seconds for 4 hours around:
       NFP:  March 6 08:30 EST
       CPI:  March 12 08:30 EST
       FOMC: March 18 14:00 EST

  6. At end of each day, print summary to console AND send Telegram:
       "Daily Summary:
        Gaps found today: N
        Contracts scanned: N
        Deepest order book: {contract} {depth} contracts
        Largest gap today: {contract} {gap:.3f}"

IMPORTANT CONSTRAINTS:
  - NO order submission. reqMktDepth and reqMktData ONLY.
  - NO database. Log to kill_shot_log.csv only.
  - NO position manager. Observer only.
  - NO carry harvest. NO execution engine.
  - Paper account only. Port 7497.

DO NOT TOUCH: anything else. Only these two scripts exist in Phase 0.

TEST: Script connects, scans contracts, logs to CSV, prints daily summary. No orders placed.

COMMIT: feat(phase0): kill_shot scanner — depth + gap frequency observer
```

---

### Decision Matrix — Evaluate After 14 Days

Run kill_shot.py for 14 days. Then count from kill_shot_log.csv:

```
gaps_per_week    = total_gaps_found / 2
avg_depth        = average min(yes_depth, no_depth) across gap events
max_profit_trade = avg_depth * avg_gap_size
annual_estimate  = max_profit_trade * gaps_per_week * 52
```

| Depth at Ask     | Gaps/Week  | Decision               | Est. Annual |
|------------------|------------|------------------------|-------------|
| >= 500 contracts | >= 2/week  | FULL BUILD  -> Gate 1  | ~$36K+      |
| 200-500          | >= 1/week  | LIGHT BUILD -> Gate 1  | ~$10K       |
| >= 500           | 1/month    | PASSIVE -> minimal build alongside Alpha NextGen | ~$3K |
| < 200 contracts  | Any        | PIVOT -> save 40hr dev | N/A         |

```
IF PIVOT:
  kill_shot_log.csv proves the market is too thin.
  File it. Redirect time to Alpha NextGen or Anahata. No regret.

IF ANY OTHER OUTCOME:
  Update Gate Status table below.
  Hand CLAUDE.md + SPEC.md to Claude Code.
  First prompt: Gate 1 task (see Backlog).
```

---

### Environment Setup

```bash
# 1. Run locally on MacBook for Phase 0 — no VPS needed yet
#    (VPS only needed when bot runs 24/7 in production)

# 2. IB Gateway paper account
#    Download: https://www.interactivebrokers.com/en/trading/ibgateway.php
#    Port: 7497 (paper) — NOT 7496 (live)
#    Enable: Configure -> API -> Settings -> Enable ActiveX and Socket Clients

# 3. Python environment
python3.11 -m venv venv
source venv/bin/activate
pip install ib_async requests python-dotenv

# 4. .env file
cp .env.example .env
# Fill in:
#   TELEGRAM_BOT_TOKEN=
#   TELEGRAM_CHAT_ID=
#   IBKR_PORT=7497
#   IBKR_HOST=127.0.0.1

# 5. Run order
python scripts/discover_contracts.py    # Step 1: find contracts
# Manually copy near-ATM conIds into WATCH_CONTRACTS in kill_shot.py
python scripts/kill_shot.py             # Step 2: run observer for 14 days
```

---

## Gate Status

| Phase | Name | Status | Decision Date |
|-------|------|--------|---------------|
| **0** | Kill-Shot Test | NOT STARTED | — |
| 1 | Infrastructure | BLOCKED on Phase 0 | — |
| 2 | Catalyst Monitor | BLOCKED | — |
| 3 | Execution | BLOCKED | — |
| 4 | Risk + Live Probe | BLOCKED | — |
| 5 | Full Deployment | BLOCKED | — |

---

## Backlog (Only Unlocked After Phase 0 Decision)

### Gate 1 — Infrastructure
- ib_async full connection with auto-reconnect
- Contract universe with ATM filter (dynamic, every 60s)
- PostgreSQL schema: positions, trades, opportunities, contracts
- Full Telegram alerting system
- scripts/test_connection.py

### Gate 2 — Catalyst Monitor
- Catalyst calendar: NFP/CPI/FOMC/BTC auto-trigger
- Two-speed scanner: NORMAL (10s) <-> CATALYST (500ms)
- GapFormationDetector: sum velocity tracking
- ATM recalculation and subscription management

### Gate 3 — Execution
- S1 parity scanner with all 8 validation gates + drop codes
- Dual-leg executor Phase 1/2/3
- S2 carry harvest with unwind priority queue
- Position manager + startup reconciler

### Gate 4 — Risk + Live Probe
- Tiered kill switch T1/T2/T3
- Daily P&L Telegram summary
- Live probe: $2,000 CAD real capital

### Gate 5 — Full Deployment
- Scale to $30,000 CAD
- All 3 contract categories live

---

## Known Decisions

| Date | Decision | Rationale |
|------|----------|-----------|
| Mar 4, 2026 | Phase 0 before any build | Order book depth unknown — could kill viability |
| Mar 4, 2026 | Port 7497 for paper | 7496 is live — never use live for Phase 0 |
| Mar 4, 2026 | No Fed/BOJ/CPI event contracts | MUTEX complexity, captured via SP/NQ anyway |
| Mar 4, 2026 | BTC annual contracts included | 24/7 underlying justifies longer hold |
| Mar 4, 2026 | S&P + NQ monthly only (14-45 DTE) | Capital velocity priority |
| Mar 4, 2026 | Observer only in Phase 0 | No orders, no capital, zero risk |
| Mar 4, 2026 | 14 days observation minimum | One confirmed gap is not enough for capital decision |

---

*Update Active Task section when Phase 0 completes.*
*Do not touch Gate 1+ backlog until Phase 0 decision is made.*
