# ForecastBot — Claude Code Handoff Prompt

## How To Use This
Paste everything below the line into Claude Code as your first message.

---

## PROMPT START

You are taking over an active trading system project called **ForecastBot**. 
Read this entire prompt before touching any files.

---

## Project Overview

ForecastBot is a prediction market scanner and edge detector trading on 
**ForecastEx** (CME Event Contracts) via Interactive Brokers (IBKR).
It is accessible in Canada through IBKR's ForecastTrader interface.

The system has two components running simultaneously:

### 1. kill_shot.py — Parity Arbitrage Scanner
Scans for YES+NO pairs that sum to less than $0.99 (should always = $1.00).
When YES_ask + NO_ask < $0.99, buy both legs = guaranteed profit.
Breakeven threshold: $0.07 gap (exchange fee $0.01 + spread $0.06).
Alert threshold: $0.93 sum or below.
Runs continuously, streams tick data from IB for all daily contracts.

**Contracts monitored:**
- CBBTC — BTC daily close price
- METLS — Silver daily price  
- FES — S&P 500 daily futures price
- UHLAX — LA daily temperature high (added recently)

### 2. weather_edge.py — Directional Edge Scanner (UHLAX focus)
Monitors KLAX (LAX airport) actual temperature observations via NWS API
every 5 minutes. Compares actual temperature trajectory to market-implied
probability on UHLAX contracts.

**Core thesis:** Retail participants open the app, see the Weather Underground
forecast, place a bet, and walk away. They do NOT watch intraday temperature
changes. When actual temperature diverges from forecast — especially during
Santa Ana wind events — the market price goes stale for 30-90 minutes.
We catch that window.

**Edge is bidirectional:**
- Market overprices YES (temp trending below threshold) → BUY NO
- Market underprices YES (Santa Ana spike incoming) → BUY YES

**Key data sources:**
- Actual obs: `https://api.weather.gov/stations/KLAX/observations/latest`
- Forecast: `https://api.weather.gov/gridpoints/LOX/150,36/forecast/hourly`
- Market prices: IB streaming via ib_async (UHLAX, secType=OPT, exchange=FORECASTX)

**Santa Ana filter:** When wind > 25mph from N/NNW/NNE/NE/ENE, NWS dramatically
underforecasts temperature. Filter suppresses NWS-based signals; uses only
trajectory probability during Santa Ana events.

---

## Tech Stack

- Platform: QuantConnect + Interactive Brokers (live trading infra)
- IB API library: `ib_async` (async Python)
- IB Gateway: running locally at 127.0.0.1:4001
- clientId=40 → kill_shot.py
- clientId=45 → weather_edge.py
- Python 3.11, macOS (local dev), Ubuntu VPS (production target)
- No Telegram configured yet (tokens in .env, optional)
- Data logs: ./data/ directory (CSV files)

---

## Document Status — ALL DOCUMENTS ARE OUTDATED

You will find these files in the project directory. **Read them for context
but do NOT treat them as the current implementation. They are all behind.**

| File | Status | What's outdated |
|------|--------|-----------------|
| CLAUDE.md | Outdated | Missing weather_edge, Santa Ana filter, async fixes |
| kill_shot.py | Mostly current | UHLAX was added but verify it's in DAILY_SYMBOLS |
| weather_edge.py | **Actively broken** | Multiple iterations happened in chat — see below |
| implementation guide (docx) | Outdated | Predates weather_edge.py entirely |

---

## weather_edge.py — Current State and Known Issues

This file went through several broken iterations in chat. The latest version
(v3.0) is the correct one. Here is what changed and why:

**Root problem that was never fully resolved:**
IB `ib_async` requires its event loop to run continuously to receive tick 
data (streaming market data callbacks). Previous versions used threading or 
blocking `run_until_complete()` which stopped the loop after connect, 
preventing tick callbacks from firing. Result: 14 strikes subscribed but 
all prices showed `n/a`.

**v3.0 fix (correct architecture):**
- `main()` is now `async def main()` called via `asyncio.run(main())`
- `IBPriceFeed.start()` is `async` — awaited directly inside the same event loop
- NWS HTTP fetches use `loop.run_in_executor(None, fetch_obs)` so they don't block IB
- IB stays connected for the full session; prices stream continuously
- Each 5-min poll just reads cached ticker values — zero reconnect overhead

**Other fixes in v3.0:**
- Depth filter: `read_all()` skips strikes where YES or NO depth < 50 contracts
- Safer reads: `hasattr + None` guards on `yt.ask`, `yt.askSize`
- Bidirectional alerts: `BUY_NO` and `BUY_YES` with separate 30-min cooldowns
- `_read()` returns 4-tuple `(yes_ask, no_ask, yes_depth, no_depth)`

**Remaining uncertainty:** v3.0 was written correctly in chat but has NOT been
run successfully yet. The IB price feed (14 strikes subscribed, all n/a) issue
was the last known state. v3.0 should fix it but needs to be tested.

---

## Your First Tasks

1. **Read all files** in the project directory to understand current state
2. **Identify which version of weather_edge.py is on disk** — check the 
   version string in the docstring (`v1.0`, `v2.0`, `v2.1`, or `v3.0`)
3. **If not v3.0:** the correct v3.0 code was provided in the conversation.
   Key markers of v3.0:
   - `async def main()` at the bottom
   - `asyncio.run(main())` as entrypoint
   - `async def start(self)` in IBPriceFeed
   - `await loop.run_in_executor(None, fetch_obs)` in poll loop
   - `_read()` returns 4-tuple including depth

4. **Run weather_edge.py** with IB Gateway connected and verify:
   - IB connects on clientId=45
   - 14 UHLAX strikes subscribe
   - After 20s warmup, price ladder shows actual YES/NO prices (not n/a)
   - 5-min poll prints the full strike table with prices

5. **If prices still show n/a after warmup**, debug the IB tick subscription.
   Likely cause: `reqMktData` on FORECASTX contracts may need different 
   `genericTickList` or `snapshot` parameter. Try:
   ```python
   yt = self.ib.reqMktData(yes_map[s], genericTickList="", snapshot=False)
   ```

6. **Do not modify kill_shot.py** unless specifically asked. It is mostly stable.

---

## Environment

```
# .env file (create if missing)
IBKR_HOST=127.0.0.1
IBKR_PORT=4001
IBKR_CLIENT_ID=40          # kill_shot
IBKR_CLIENT_ID_WEATHER=45  # weather_edge
LOG_DIR=./data

# Optional — leave blank if no Telegram yet
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

```
# Install deps
pip install ib_async requests python-dotenv
```

---

## What Success Looks Like Today

```
weather_edge.py running, IB connected, showing:

  ── 11:45:00 PT  🌬 SANTA ANA — NWS suppressed
  Temp=71.2°F  High=71.2°F  Threshold=73°F  Forecast=72.0°F
  Wind=54mph NNW  Cond=Clear  Falling=0.0h

     K    YES     NO   MKT%   NWS%  TRAJ%    EDGE    YD      ND    SIGNAL
  ────────────────────────────────────────────────────────────────────────
     69  0.980  0.030  0.980  0.997   n/a  -0.017    125     210      NONE
     70  0.960  0.050  0.960  0.990   n/a  -0.030    340     180      NONE
     71  0.920  0.090  0.920  0.960   n/a  +0.010    275     260      NONE
     72  0.880  0.130  0.880  0.896   n/a  -0.016    520     410      NONE
  ⚡  73  0.860  0.150  0.860  0.336   n/a  +0.524    375     125   BUY_NO
     74  0.620  0.390  0.620  0.115   n/a  +0.505    200      75   BUY_NO
     75  0.430  0.580  0.430  0.031   n/a  +0.399     85      60   BUY_NO

  ⚡ BEST: K73  BUY NO @ $0.150  profit=$0.850/contract
```

That table with real prices = the system is working.

---

## Background Context

- Owner is building toward financial independence through algorithmic trading
- ForecastBot is a side system complementing Alpha NextGen (QQQ options, VASS engine)
- Both run on IBKR. ForecastBot uses a separate $5K-$10K capital allocation
- This is observation-only phase — no live orders yet
- Goal: 30 days of data to validate edge frequency and depth before executing
