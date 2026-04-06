# CLAUDE.md — ForecastBot AI Agent Instructions

## Analysis Rigor Rules (MANDATORY)

- NEVER state a config value from memory — always grep/read the actual line first
- NEVER claim a root cause without tracing the exact code path with line numbers
- NEVER propose a fix without first verifying current behaviour with data
- NEVER use bid prices or last-traded prices in gap calculations — ASK prices only, always
- If an earlier statement contradicts new evidence, flag it immediately as a correction
- Distinguish clearly: **CONFIRMED** (verified in code/data) vs **HYPOTHESIS** (needs verification)

---

## WAKE-UP PROTOCOL (READ FIRST AFTER COMPACTION OR NEW SESSION)

**Context Amnesia Warning:** If this session just started or was compacted, you have lost:
- Shell state (venv not active)
- Memory of what task you were working on
- Any files you previously read

**Before doing anything else, run these commands:**

```bash
# 1. Activate environment
source venv/bin/activate && python --version
# Expected: Python 3.11.x

# 2. Read the handoff document for full project context
cat HANDOFF.md

# 3. Check git status for uncommitted work
git status && git branch

# 4. Check bot status (if running)
tail -50 data/weather_condor.log

# 5. Verify IB Gateway connection (if doing bot work)
# IB Gateway must be running at 127.0.0.1:4001
nc -z 127.0.0.1 4001 && echo "UP" || echo "DOWN"
```

**Why this matters:**
- HANDOFF.md has the full project context, strategy, and current state
- You may have uncommitted changes from before compaction
- IB Gateway connection state is not preserved between sessions
- weather_condor.py uses clientId=57

---

## Current Phase: Phase 1 — Simulation

**weather_condor.py is the active bot.** Simulated orders with real IB market data.
9 weather cities, daily condor entries, CSV logging, Telegram alerts.

**Cumulative sim P&L: ~+$30** (9 positions taken across 4 trading days as of Apr 1, 2026)

---

## Build & Test Commands

```bash
# Setup (first time)
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt     # ib_async, python-dotenv, requests

# Run the condor bot (requires IB Gateway at 127.0.0.1:4001)
python3 weather_condor.py

# Or use launchd for persistence:
./scripts/manage_services.sh install
./scripts/manage_services.sh status

# Check output
tail -50 data/weather_condor.log    # Bot log
cat data/condor_daily.csv           # Daily P&L summary
cat data/condor_positions.csv       # All positions with outcomes
```

---

## Project Overview

**ForecastBot** trades weather prediction markets on **ForecastEx** (CME Event Contracts) via Interactive Brokers. The **Weather Condor Strategy** buys YES at (forecast−offset) and NO at (forecast+offset) across 9 US cities. At least one leg always pays $1.00 — profit when both legs hit.

See `HANDOFF.md` for the complete project description, strategy details, performance data, and known issues.

---

## Repository Structure

```
forecastbot/
├── CLAUDE.md                     # This file — AI agent instructions
├── HANDOFF.md                    # Full project handoff document
├── WEATHER_CARRY_STRATEGY.md     # Strategy theory document
├── README.md                     # Project overview
├── SPECV2.md                     # Full system specification
├── PROCESS.md                    # Workflow gates, commit contract
├── ERRORS.md                     # Known errors and solutions
│
├── weather_condor.py             # *** MAIN BOT *** — condor strategy, clientId=57
├── kill_shot.py                  # Parity gap scanner (Phase 0, clientId=10)
├── weather_edge.py               # Weather edge scanner (Phase 0, clientId=45)
├── depth_finder.py               # L2 depth analyzer (clientId=55)
├── discover_contracts.py         # Contract discovery tool
│
├── scripts/
│   ├── run_condor.sh             # Bot launcher with venv
│   ├── watchdog_gateway.sh       # Gateway health monitor + Telegram alerts
│   ├── manage_services.sh        # launchd service manager
│   ├── com.forecastbot.condor.plist          # launchd: bot KeepAlive
│   └── com.forecastbot.gateway-watchdog.plist # launchd: watchdog every 60s
│
├── .env                          # Local config (git-ignored)
├── requirements.txt              # ib_async, python-dotenv, requests
├── data/                         # CSV log output + bot logs
│   ├── condor_forecasts.csv      # Morning forecast + swing check per city
│   ├── condor_positions.csv      # All entries + settlement P&L
│   ├── condor_daily.csv          # Portfolio summary per trading day
│   ├── condor_prices.csv         # 15s price snapshots for all cities
│   └── weather_condor.log        # Full bot log
└── venv/                         # Python virtual environment
```

---

## Component Map

| Component | File | clientId | Status |
|-----------|------|----------|--------|
| **Weather Condor** | `weather_condor.py` | 57 | **ACTIVE** — main bot |
| **Parity Scanner** | `kill_shot.py` | 10 | Phase 0 — not running |
| **Weather Edge** | `weather_edge.py` | 45 | Phase 0 — not running |
| **Depth Finder** | `depth_finder.py` | 55 | Utility — run on demand |
| **Contract Discovery** | `discover_contracts.py` | — | Utility — run on demand |

---

## weather_condor.py Config Quick Reference

All values are constants at the top of `weather_condor.py`. Always grep the file — never assume.

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `PORTFOLIO_USD` | 20,000 | Total sim capital |
| `MAX_CITY_BUDGET` | 2,000 | Hard cap per city |
| `MAX_ENTRY_SUM` | 1.80 | Max combined cost (min $0.20 edge) |
| `SCAN_HOUR` | 7 (AM ET) | Forecast scan time |
| `ENTRY_HOUR:ENTRY_MINUTE` | 9:30 (AM ET) | Earliest entry (book liquidity) |
| `SETTLE_HOUR` | 19 (7 PM ET) | Settlement time |
| `FORECAST_DRIFT_EXTREME_F` | 6 | Skip if drift ≥ 6°F at entry time |
| `YES_SL_LOCAL_HOUR` | 15 | YES stop-loss only after 3 PM local |
| `ACTIVE_PRICE_POLL_SEC` | 15 | IB price read interval |
| `ACTIVE_WU_POLL_SEC` | 300 | WU temp poll interval (5 min) |
| `HOURLY_SWING_THRESHOLD_F` | 25 | NWS hourly range skip threshold |
| `SWEEP_RANGE` | 0.03 | L2 sweep: up to +3c above best ask |
| `MAX_LEG_PRICE` | 0.97 | Never pay > $0.97 per leg |
| `TAKE_PROFIT` | 0.20 | Exit when sum rises $0.20 above entry |

---

## ForecastEx-Specific API Notes

```python
# ForecastEx contracts in IBKR TWS API:
secType  = "OPT"    # All weather contracts (UHxxx)
exchange = "FORECASTX"
YES      = Call (right="C")
NO       = Put  (right="P")

# Weather symbols: UHMDW, UHLAX, UHSFO, UHAUS, UHDCA, UHPHL, UHSEA, UHMSP, UHATL
# (UHLGA disabled — zero OI)

# IB Gateway: 127.0.0.1:4001
# clientId=57 (weather_condor.py)

# IB library: ib_async (NOT ib_insync)
# WU API key: e1f10a1e78da46f5b10a1e78da96f525

# No sell orders on ForecastEx — to close a position:
# Close YES by buying NO at same strike → hedged pair always pays $1.00
# Close NO by buying YES at same strike → hedged pair always pays $1.00

# Settlement: "Exceed X°F" = strictly > X
# YES K55 pays if actual high > 55°F
# NO K58 pays if actual high ≤ 58°F

# IB reqMktDepth: max 3 concurrent streams
# Bot subscribes one strike at a time (2 streams per strike: YES+NO)
```

---

## Critical Rules — Never Violate

1. **ASK prices only.** `yes_ask + no_ask` — never bid, never last traded.
2. **No sell orders on ForecastEx.** To exit, buy the opposing leg at the same strike.
3. **"Exceed X" = strictly > X.** WU 75°F does NOT pay K75 YES.
4. **IB reqMktDepth max 3 concurrent.** Subscribe L2 one strike at a time, cancel before next.
5. **Log every skip with a reason.** Use once-per-city flags to avoid log spam.
6. **WU current temp can diverge from METAR.** At settlement, use max(tracked, API 24h high).
7. **IB Gateway requires manual restart** after nightly disconnect (~11:45 PM). No automation possible (GUI + 2FA required).

---

## Known Issues (April 2026)

1. **WU temp tracking unreliable for Chicago** — 10°F gap between WU current temp and METAR on Mar 14. The bot's WU geocode may resolve to a different sensor than KMDW ASOS.
2. **IB Gateway nightly disconnect** — Bot retries infinitely. Should cap retries or pause on weekends.
3. **ForecastEx illiquidity** — DC and Philadelphia rarely have both-leg prices. Some cities go full days with empty books. Bot handles this correctly (polls every 15s, enters when liquidity appears).
4. **San Francisco large loss** — Mar 29: forecast 80°F, actual 84°F (+4°F error), no stop-loss triggered in time. Offset of 2 may be too tight for SF's microclimate variability.

---

## Lessons from Live Data

1. **Forecast accuracy is the #1 risk.** Both losses >$200 came from forecast misses (+7°F Chicago, +4°F SF).
2. **Stop-loss hedges cap losses well.** Minneapolis -$24, Atlanta -$35 instead of -$700+ without hedging.
3. **Best prices appear 11 AM - 5 PM ET.** The 9:30 AM entry gate prevents premature entries on thin books.
4. **Both-win rate:** 4 of 9 positions had both legs win. Strategy is profitable when forecast error < offset.
5. **WU settlement source matters.** Use max(tracked_high, API_24h_high) — the API caught Chicago's real high when tracking missed it.

---

## Recent Work Log

| Date | Change |
|------|--------|
| Apr 1, 2026 | Two positions: UHLAX both-won +$34.50, UHATL SL hedge -$4.50 |
| Mar 29, 2026 | Four positions: SEA both-won +$241.50, MSP/ATL SL hedged, SFO big loss -$880 |
| Mar 27, 2026 | Budget increased to $20K, per-city $2K. L2 Error 309 fixed (sequential subscribe). Table formatting fixed (ANSI alignment). Seattle both-won +$297.73 |
| Mar 25, 2026 | Telegram setup. launchd persistence scripts. Scan moved to 7 AM, entry to 9:30 AM. Forecast drift detection added (skip at ≥6°F). Log spam suppression. |
| Mar 14, 2026 | weather_condor.py v1.0 created. First positions: Chicago lost (-$281), Atlanta won (+$88). |
| Mar 11, 2026 | WEATHER_CARRY_STRATEGY.md written. depth_finder.py created. |
| Mar 5, 2026 | weather_edge.py v4.0, kill_shot.py v2.0, Phase 0 observation started. |

---

*ForecastBot CLAUDE.md v2.0 — April 6, 2026*
