# ForecastBot Phase 0 — Run Guide

**Goal:** Validate in 14-30 days whether the parity arb and weather edge opportunities exist with sufficient frequency and depth.

---

## Pre-Requisites

### 1. IB Gateway
- Download from: https://www.interactivebrokers.com/en/trading/ibgateway.php
- Log in with your trading credentials
- Enable API:
  - `Configure -> API -> Settings`
  - Enable ActiveX and Socket Clients
  - Socket port: **4001**
  - Allow connections from: 127.0.0.1

### 2. Python environment
```bash
cd forecastbot/
python3.11 -m venv venv
source venv/bin/activate          # Mac/Linux
pip install -r requirements.txt
```

### 3. .env file
```bash
# Create .env with:
IBKR_HOST=127.0.0.1
IBKR_PORT=4001
IBKR_CLIENT_ID=10              # kill_shot.py
IBKR_CLIENT_ID_WEATHER=45     # weather_edge.py
TELEGRAM_BOT_TOKEN=            # Optional
TELEGRAM_CHAT_ID=              # Optional
GAP_THRESHOLD=0.03
MIN_DEPTH=50
LOG_DIR=./data
```

---

## Step 1 — Discover Contracts (30 min)

```bash
python3 discover_contracts.py
```

**What to look for:**

| Output | Meaning | Action |
|--------|---------|--------|
| `live prices, N near-ATM` | API works | Continue |
| `bid=0.00 ask=0.00` for all | API blocked | See troubleshooting |
| `NO CONTRACTS FOUND` for all | Wrong symbol names | See troubleshooting |
| `DEPTH DATA` in depth test | reqMktDepth works | Continue |
| `NO DEPTH` in depth test | Level 2 blocked | Note it, still continue |

---

## Step 2 — Run Parity Scanner (14-30 days)

```bash
python3 kill_shot.py
```

kill_shot.py v2.0 is event-driven streaming — fires on every price tick, not a timer. Features:
- 7-contract universe (CBBTC, METLS, FES, FF, YXHBT, PNFED, JPDEC)
- 3-tick confirmation before gap alerts
- Auto-reconnect on IB disconnect (up to 10 attempts)
- Daily contract refresh at 09:31 ET
- Logs to `data/all_ticks.csv`, `data/gap_events.csv`, `data/gap_alerts.csv`
- Telegram alerts on profitable gaps (sum < $0.93)

To stop: `Ctrl+C` — prints final analysis automatically.

---

## Step 2b — Run Weather Edge Scanner (Optional, alongside)

```bash
# In a separate terminal:
python3 weather_edge.py
```

weather_edge.py v3.0 runs alongside kill_shot.py using a separate clientId (45). Features:
- Monitors UHLAX (LA daily high temperature) contracts
- Polls NWS API (KLAX station) every 5 minutes for actual temperature
- Santa Ana wind filter: suppresses NWS signals when wind > 25mph offshore
- Bidirectional alerts: BUY_YES and BUY_NO with 30-min cooldowns
- Full async architecture (single event loop, no threading)

**Known issue:** IB price feed may show n/a for all 14 strikes during initial testing. If prices don't populate after 20s warmup, see Troubleshooting.

---

## Step 3 — Evaluate Decision Matrix

After 14-30 days, `Ctrl+C` on kill_shot.py prints final analysis.

Or run analysis manually on the CSV:
```python
import pandas as pd
df = pd.read_csv("data/gap_events.csv")
print(f"Gap events: {len(df)}")
print(f"Avg depth: {df['min_depth'].mean():.0f}")
print(f"Avg gap: {df['gap'].mean():.4f}")
print(f"By contract:\n{df.groupby('symbol')['gap'].count()}")
```

---

## Troubleshooting

### `ConnectionRefusedError` — can't connect to IB Gateway
- IB Gateway not running -> start it
- Wrong port -> check `.env` IBKR_PORT (should be 4001)
- API not enabled -> Configure -> API -> Settings -> Enable Socket

### `bid=0 ask=0` for all contracts — no market data via API
1. Check Client Portal -> Settings -> Market Data Subscriptions
2. Call IBKR support: "reqMktData returns bid=0 ask=0 for FORECASTX contracts"
3. REST API fallback: `requests.get("https://api.ibkr.com/v1/api/iserver/marketdata/snapshot")`

### `NO CONTRACTS FOUND` for a symbol
- Symbol name may differ from IBKR internal name
- Open ForecastTrader UI, right-click contract -> Contract Info -> Symbol

### weather_edge.py shows n/a for all 14 UHLAX strikes
- v3.0 should fix this (full async event loop). If still n/a:
  - Try: `yt = self.ib.reqMktData(contract, genericTickList="", snapshot=False)`
  - Verify clientId=45 is not conflicting with another connection
  - Check IB Gateway log for subscription errors

### reqMktDepth returns no data
- Level 2 may not be available for ForecastEx via API
- kill_shot.py will still run, depth column will show 0
- Gap frequency can still be measured, depth cannot

---

## File Reference

```
forecastbot/
├── kill_shot.py              <- Parity gap scanner (run continuously)
├── weather_edge.py           <- Weather edge scanner (run alongside)
├── discover_contracts.py     <- Run first to find contracts
├── data/
│   ├── all_ticks.csv         <- Every tick with valid data
│   ├── gap_events.csv        <- Gaps only (below breakeven)
│   ├── gap_alerts.csv        <- Profitable gaps only
│   └── daily_summary.csv     <- Daily stats
├── .env                      <- Your config (git-ignored)
└── requirements.txt
```

---

## CONSTRAINTS (READ BEFORE RUNNING)

```
NO order submission
NO capital at risk
NO position manager
NO database

reqMktData only (Level 1)
reqMktDepth only (Level 2) — called only at gap events
CSV logging only
Observer only

If you see placeOrder() anywhere in the code -> delete it.
```
