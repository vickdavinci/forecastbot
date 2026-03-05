# ForecastBot Phase 0 — Run Guide

**Goal:** Validate in 14 days whether the parity arb opportunity exists at ask-price level with sufficient order book depth.

---

## Pre-Requisites

### 1. IB Gateway (paper account)
- Download from: https://www.interactivebrokers.com/en/trading/ibgateway.php
- Log in with **paper trading** credentials (separate from live login)
- Enable API:
  - `Configure → API → Settings`
  - ✓ Enable ActiveX and Socket Clients
  - ✓ Socket port: **7497** (paper)
  - ✓ Allow connections from: 127.0.0.1

### 2. Python environment
```bash
cd forecastbot/
python3.11 -m venv venv
source venv/bin/activate          # Mac/Linux
# venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

### 3. .env file
```bash
cp .env.example .env
# Edit .env — fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID if you want alerts
# Leave IBKR settings as default (127.0.0.1:7497)
```

---

## Step 1 — Discover Contracts (30 min)

```bash
python scripts/discover_contracts.py
```

**What to look for:**

| Output | Meaning | Action |
|--------|---------|--------|
| `live prices, N near-ATM` | API works ✓ | Continue |
| `bid=0.00 ask=0.00` for all | API blocked | See troubleshooting |
| `NO CONTRACTS FOUND` for all | Wrong symbol names | See troubleshooting |
| `✓ DEPTH DATA` in depth test | reqMktDepth works ✓ | Continue |
| `✗ NO DEPTH` in depth test | Level 2 blocked | Note it, still continue |

**Copy the `WATCH_CONTRACTS` block** from the output into `kill_shot.py`.

---

## Step 2 — Run Observer (14 days)

```bash
python scripts/kill_shot.py
```

Let it run for at least 14 days. The script:
- Scans every 60s (TIER1), 5min (TIER2), 15min (TIER3)
- Automatically switches to faster cadence during NFP/CPI/FOMC windows
- Logs every scan to `data/kill_shot_log.csv`
- Logs gap events separately to `data/gap_events.csv`
- Sends Telegram alerts on any gap ≥ $0.03
- Sends daily summary at 23:55 ET

To stop: `Ctrl+C` — prints final analysis automatically.

---

## Step 3 — Evaluate Decision Matrix

After 14 days, `Ctrl+C` prints:

```
PHASE 0 FINAL ANALYSIS
  Days observed:       14
  Total scans:         20160
  Gap events (≥$0.03): N
  Gaps per week:       N.N
  Avg depth at gap:    NNN contracts
  Annual estimate:     $NNN,NNN

DECISION MATRIX:
  ┌────────────────────────────────────────────────────────┐
  │  ✓ FULL BUILD → Gate 1  (depth≥500, gaps≥2/week)      │  ← or whichever applies
  └────────────────────────────────────────────────────────┘
```

Or run the analysis manually on the CSV:
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
- IB Gateway not running → start it
- Wrong port → check `.env` IBKR_PORT (7497 = paper, 7496 = live)
- API not enabled → Configure → API → Settings → Enable Socket

### `bid=0 ask=0` for all contracts — no market data via API
**This is the critical failure mode.** If reqMktData returns zeros:
1. Check Client Portal → Settings → Market Data Subscriptions
   - Look for any ForecastEx or FORECASTX feed
   - If none exists, proceed to step 2
2. Call IBKR support:
   - "Does ForecastEx market data require a subscription for TWS API access?"
   - "reqMktData returns bid=0 ask=0 for FORECASTX contracts"
3. REST API fallback:
   - Use `requests.get("https://api.ibkr.com/v1/api/iserver/marketdata/snapshot")`
   - Fields: 84=bid, 86=ask, 85=ask_size, 88=bid_size
   - This is documented to work for Event Contracts (see IBKR Campus Event Contracts guide)

### `NO CONTRACTS FOUND` for a symbol
- Symbol name may differ from what IBKR uses internally
- Open ForecastTrader UI, find the contract, right-click → Contract Info → Symbol
- Common alternatives: `FF` may be `USINTERESTRATE`, `BTC` may be `BTCUSD`

### reqMktDepth returns no data
- Level 2 (order book) may not be available for ForecastEx via API
- This is expected — kill_shot.py will still run, depth column will show 0
- Gap frequency can still be measured, depth cannot
- If depth=0 everywhere: adjust decision matrix — use UI order book screenshots manually

---

## File Reference

```
forecastbot/
├── scripts/
│   ├── discover_contracts.py   ← Run first
│   └── kill_shot.py            ← Run second (14 days)
├── data/
│   ├── kill_shot_log.csv       ← Every scan
│   └── gap_events.csv          ← Gaps only (≥$0.03)
├── .env.example
├── .env                        ← Your config (git-ignored)
└── requirements.txt
```

---

## CONSTRAINTS (READ BEFORE RUNNING)

```
✗ NO order submission
✗ NO capital at risk
✗ NO position manager
✗ NO database

✓ reqMktData only (Level 1)
✓ reqMktDepth only (Level 2) — called only at gap events
✓ CSV logging only
✓ Observer only

If you see placeOrder() anywhere in the code → delete it.
```
