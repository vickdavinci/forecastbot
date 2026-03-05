# WORKBOARD.md — ForecastBot Task Tracking

**Current Phase: PHASE 0 — Kill-Shot Test + Weather Edge**
**Status: IN PROGRESS**
**Last Updated: March 5, 2026**

---

## ACTIVE TASK — WEATHER_EDGE.PY IB PRICE FEED VERIFICATION

```
TASK: Verify weather_edge.py v3.0 receives live IB prices for UHLAX strikes
BLOCKER: 14 strikes subscribe but all show n/a prices
STATUS: NEEDS TESTING — v3.0 async architecture should fix it

DO NOT MODIFY kill_shot.py — it is stable and running.
```

### Known Blocker
IB `ib_async` requires its event loop to run continuously for tick callbacks.
Previous versions (v1/v2) used threading or blocking `run_until_complete()`
which stopped the loop after connect, preventing tick callbacks from firing.
v3.0 uses full async architecture — needs live test to confirm fix.

### Debug Steps If n/a Persists
```python
# Try different reqMktData parameters:
yt = self.ib.reqMktData(contract, genericTickList="", snapshot=False)
```

---

## COMPLETED TASKS

### discover_contracts.py — COMPLETE
- Discovers all active ForecastEx contracts
- Prints conIds for near-ATM strikes
- Located at: `discover_contracts.py` (root directory)

### kill_shot.py v2.0 — COMPLETE AND RUNNING
- Event-driven streaming parity gap detector (tick-by-tick, not timer)
- 7-contract universe: CBBTC, METLS, FES, FF, YXHBT, PNFED, JPDEC
- 3-tick confirmation before gap alert fires
- Auto-reconnect on IB Gateway disconnect (up to 10 attempts)
- Daily contract refresh at 09:31 ET
- CSV logging: data/all_ticks.csv, data/gap_events.csv, data/gap_alerts.csv
- Telegram alerts on profitable gaps (sum < $0.93)
- Located at: `kill_shot.py` (root directory)
- IB Gateway: 127.0.0.1:4001, clientId from .env

### weather_edge.py v3.0 — WRITTEN, NEEDS TESTING
- Directional edge scanner for UHLAX (LA daily temperature) contracts
- NWS API: actual observations from KLAX station every 5 min
- Santa Ana wind filter: suppresses NWS signal when wind > 25mph offshore
- Bidirectional: BUY_YES and BUY_NO with separate 30-min cooldowns
- Depth filter: skips strikes with YES or NO depth < 50 contracts
- Full async architecture: single event loop, no threading
- Located at: `weather_edge.py` (root directory)
- IB Gateway: 127.0.0.1:4001, clientId=45

---

## Decision Matrix — Evaluate After 14-30 Days

Run kill_shot.py for 14-30 days. Then count from CSV logs:

```
gaps_per_week    = total_gaps_found / weeks_observed
avg_depth        = average min(yes_depth, no_depth) across gap events
max_profit_trade = avg_depth * avg_gap_size
annual_estimate  = max_profit_trade * gaps_per_week * 52
```

| Depth at Ask     | Gaps/Week  | Decision               | Est. Annual |
|------------------|------------|------------------------|-------------|
| >= 500 contracts | >= 5/week  | FULL BUILD  -> Gate 1  | ~$36K+      |
| 200-500          | >= 2/week  | LIGHT BUILD -> Gate 1  | ~$10K       |
| >= 500           | 1/month    | PASSIVE -> minimal build | ~$3K      |
| < 200 contracts  | Any        | PIVOT -> save dev time | N/A         |

```
IF PIVOT:
  CSV logs prove the market is too thin.
  File it. Redirect time to Alpha NextGen or Anahata. No regret.

IF ANY OTHER OUTCOME:
  Update Gate Status table below.
  Proceed to Gate 1.
```

---

## Environment

```bash
# IB Gateway
IBKR_HOST=127.0.0.1
IBKR_PORT=4001
IBKR_CLIENT_ID=10          # kill_shot.py
IBKR_CLIENT_ID_WEATHER=45  # weather_edge.py

# Run order
python3 kill_shot.py            # Parity gap scanner (runs continuously)
python3 weather_edge.py         # Weather edge scanner (runs alongside, separate clientId)
```

---

## Gate Status

| Phase | Name | Status | Decision Date |
|-------|------|--------|---------------|
| **0** | Kill-Shot Test + Weather Edge | **IN PROGRESS** | — |
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
- Scale to full capital
- All contract categories live

---

## Known Decisions

| Date | Decision | Rationale |
|------|----------|-----------|
| Mar 4, 2026 | Phase 0 before any build | Order book depth unknown — could kill viability |
| Mar 4, 2026 | Port 4001 for IB Gateway | Local IB Gateway connection |
| Mar 4, 2026 | Observer only in Phase 0 | No orders, no capital, zero risk |
| Mar 4, 2026 | 14 days observation minimum | One confirmed gap is not enough for capital decision |
| Mar 5, 2026 | kill_shot.py v2.0 streaming | Event-driven tick-by-tick replaces timer polling |
| Mar 5, 2026 | weather_edge.py v3.0 async | Full async fixes IB event loop blocking issue |
| Mar 5, 2026 | UHLAX added to universe | Weather contracts offer directional edge via NWS divergence |

---

*Update Active Task section when Phase 0 completes.*
*Do not touch Gate 1+ backlog until Phase 0 decision is made.*
