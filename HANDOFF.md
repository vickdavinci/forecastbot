# HANDOFF.md — ForecastBot Project Handoff

## What Is This Project?

**ForecastBot** trades weather prediction markets on **ForecastEx** (CME Event Contracts) via Interactive Brokers. ForecastEx offers daily binary contracts on whether a city's high temperature will "exceed X°F". YES (Call) pays $1.00 if temp > strike. NO (Put) pays $1.00 if temp ≤ strike.

Accessible in Canada through IBKR's ForecastTrader interface. Two IBKR accounts: U17169341 (RRSP, no ForecastEx access), U5648068 (margin, trades ForecastEx).

---

## The Strategy: Weather Condor

**Core thesis:** Buy YES at (forecast − offset) and NO at (forecast + offset). Temperature can't be both below the YES strike AND above the NO strike, so **at least one leg always pays $1.00**. If temp lands between both strikes (the forecast is right), **both legs pay** = $2.00 return on ~$1.75 entry.

**Example:** Forecast 57°F, offset 2.
- Buy YES K55 ($0.77) + NO K58 ($0.97) = $1.74 entry
- If actual = 57°F: YES wins (57>55) AND NO wins (57≤58) → $2.00 payout, +$0.26 profit
- If actual = 60°F: only YES wins → $1.00 payout, −$0.74 loss
- One leg ALWAYS pays → max loss is capped

**9 cities monitored:** Chicago (UHMDW), Los Angeles (UHLAX), San Francisco (UHSFO), Austin (UHAUS), Washington DC (UHDCA), Philadelphia (UHPHL), Seattle (UHSEA), Minneapolis (UHMSP), Atlanta (UHATL). New York (UHLGA) disabled — zero open interest.

---

## Components

### Active (Phase 1 — Simulation)

| File | clientId | Purpose |
|------|----------|---------|
| `weather_condor.py` | 57 | **Main bot** — daily state machine, 9-city condor strategy, simulated orders, real IB prices |
| `scripts/run_condor.sh` | — | Bot launcher with venv activation |
| `scripts/watchdog_gateway.sh` | — | Checks IB Gateway port 4001 every 60s, Telegram alert on down/recovery |
| `scripts/manage_services.sh` | — | Install/uninstall/start/stop/status for launchd services |
| `scripts/com.forecastbot.condor.plist` | — | launchd KeepAlive service for the bot |
| `scripts/com.forecastbot.gateway-watchdog.plist` | — | launchd StartInterval=60 for gateway watchdog |

### Phase 0 (Observation — still present, not actively running)

| File | clientId | Purpose |
|------|----------|---------|
| `kill_shot.py` | 10 | Parity gap scanner (YES+NO < $0.93) |
| `weather_edge.py` | 45 | UHLAX directional temperature edge scanner |
| `depth_finder.py` | 55 | L2 order book depth analyzer |
| `discover_contracts.py` | — | Contract discovery tool |

---

## weather_condor.py — How It Works

### Daily State Machine
```
WAITING → SCANNING → ACTIVE → SETTLING → REPORTING → WAITING
```

| Phase | Trigger | Action |
|-------|---------|--------|
| WAITING | Startup / daily reset | Idle, check clock every 60s, skip weekends |
| SCANNING | 7:00 AM ET | Fetch WU forecasts + NWS 4-source swing detection for all 9 cities |
| ACTIVE | Scan complete | Monitor prices (15s), WU temp (5min); at 9:30 AM drift-check + entries |
| SETTLING | 7:00 PM ET | Fetch WU settlement high, compute P&L per position |
| REPORTING | Settlement done | Print summary, write CSVs, send Telegram, reset |

### Two-Stage Morning
- **7:00 AM ET** — Scan: fetch WU forecasts (morning models are accurate), run NWS swing detection (alerts, AFD keywords, hourly range)
- **9:30 AM ET** — Entry: re-fetch WU for drift check, update strikes if forecast changed, sweep into live order book. Skips only on extreme drift (≥6°F)

### Entry Logic
- Per-city budget: $2,000 hard cap (`MAX_CITY_BUDGET`)
- Portfolio: $20,000 (`PORTFOLIO_USD`)
- Max entry sum: $1.80 (`MAX_ENTRY_SUM`) — minimum $0.20 edge per contract
- L2 depth sweep: subscribes one strike at a time (IB max 3 concurrent reqMktDepth), reads real order book
- Accumulates contracts across multiple L2 sweeps until budget full

### Stop-Loss / Take-Profit
- **Stop-loss:** When temp breaches the losing strike, buy opposing contract at same strike → locks in $1.00 hedged pair. YES stop-loss only after 3 PM local (peak heating). NO stop-loss immediate.
- **Take-profit:** When current YES+NO sum rises $0.20 above entry cost

### Swing Detection (4 sources)
1. **NWS Alerts** — active weather watches/warnings → skip
2. **NWS AFD** — Area Forecast Discussion keywords (frontal passage, uncertain, etc.) → skip
3. **WU Narrative** — storm/gusty/record language → skip
4. **NWS Hourly Range** — if max−min spread > 25°F in the forecast period → skip

### Settlement
- `wu_high` = max of: tracked WU current temp throughout day, OR WU API `temperatureMax24Hour`
- YES wins if wu_high > yes_strike (strictly greater)
- NO wins if wu_high ≤ no_strike
- P&L = payout − total_cost

---

## Configuration (.env)

```
IBKR_HOST=127.0.0.1
IBKR_PORT=4001
IBKR_CLIENT_ID=11
IBKR_CLIENT_ID_WEATHER=54
IBKR_CLIENT_ID_DEPTH=55
IBKR_CLIENT_ID_CONDOR=57
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<chat_id>
GAP_THRESHOLD=0.03
MIN_DEPTH=50
LOG_DIR=./data
```

---

## Performance Data (as of April 1, 2026)

### Positions Taken

| Date | City | Fcst | YES K | NO K | Entry | Contracts | WU High | Result | P&L |
|------|------|:---:|:---:|:---:|:---:|:---:|:---:|--------|------:|
| Mar 14 | Chicago | 40°F | 38 | 42 | $1.73 | 385 | **47°F** | YES only | -$280.78 |
| Mar 14 | Atlanta | 78°F | 76 | 80 | $1.77 | 377 | 79°F | Both won | +$88.22 |
| Mar 27 | Seattle | 57°F | 55 | 58 | $1.74 | 1148 | 57°F | Both won | +$297.73 |
| Mar 29 | Minneapolis | 66°F | 64 | 68 | $1.48 | 1353 | **70°F** | SL hedge | -$23.95 |
| Mar 29 | Atlanta | 66°F | 64 | 68 | $1.70 | 1176 | 65°F | SL hedge | -$35.28 |
| Mar 29 | San Francisco | 80°F | 78 | 82 | $1.79 | 1118 | **84°F** | YES only | -$880.62 |
| Mar 29 | Seattle | 47°F | 45 | 49 | $1.77 | 1050 | 46°F | Both won | +$241.50 |
| Apr 1 | Atlanta | 84°F | 82 | 86 | $1.47 | 75 | 84°F | SL hedge | -$4.50 |
| Apr 1 | Los Angeles | 67°F | 65 | 69 | $1.77 | 150 | 67°F | Both won | +$34.50 |

**Cumulative P&L: +$30.00** (after losses, but strategy works when forecast is accurate)

### Key Insights from Live Data
1. **Forecast accuracy is everything.** Chicago Mar 14 (+7°F error) and SF Mar 29 (+4°F error) caused the biggest losses.
2. **Stop-loss hedges work.** Minneapolis and Atlanta Mar 29 losses capped at -$24 and -$35 instead of -$700+ thanks to hedging.
3. **Best prices appear 11 AM - 5 PM ET**, not at market open. The 9:30 AM gate prevents premature entries.
4. **ForecastEx is very illiquid.** DC and Philadelphia rarely have prices on both legs. Some cities go entire days with empty books.
5. **WU current temp can be unreliable.** Chicago Mar 14: WU tracking showed max 37°F all day, but actual METAR high was 47°F (10°F gap). WU API `temperatureMax24Hour` caught it at settlement.
6. **IB Gateway disconnects nightly** (~11:45 PM) for 24h re-auth. Must be manually restarted. The watchdog script detects this and sends Telegram alerts but can't restart Gateway (requires GUI + 2FA).

---

## CSV Data Files (in data/)

| File | Purpose |
|------|---------|
| `condor_forecasts.csv` | Morning forecast + swing check per city per day |
| `condor_positions.csv` | All simulated entries with settlement P&L |
| `condor_daily.csv` | Portfolio summary per trading day |
| `condor_prices.csv` | Every 15s price snapshot for all tracked cities |
| `weather_condor.log` | Full bot log (INFO level) |

---

## Known Issues & Pending Work

### Bugs / Issues
1. **WU temp tracking unreliable for some cities** — Chicago showed 10°F discrepancy between WU current temp and METAR. Consider cross-referencing NWS METAR observations.
2. **IB Gateway can't auto-restart** — Requires manual GUI login with 2FA after nightly disconnect.
3. **Bot retry loop is infinite** — After Gateway disconnect, retries every 5 min forever (429+ attempts observed). Should cap retries or pause during weekends.

### Improvements to Consider
1. **Temperature trajectory gate** — Before entry, check if current temp is already near/above NO strike. Would catch some forecast misses (but wouldn't have caught Chicago — WU data was wrong).
2. **METAR cross-validation** — Compare WU current temp with NWS METAR for the same station. Flag cities where they diverge >3°F.
3. **Later entry window** — Data shows best prices often appear 2-5 PM ET. Could improve entry prices but risks missing liquidity windows.
4. **Chicago geocode investigation** — Verify the WU geocode `41.79,-87.75` resolves to KMDW station for current temp, not a nearby PWS.

---

## ForecastEx API Notes

```python
secType  = "OPT"           # All weather contracts
exchange = "FORECASTX"
YES      = Call (right="C")
NO       = Put  (right="P")

# No sell orders on ForecastEx — to exit:
# Close YES by buying NO at same strike → hedged pair pays $1.00
# Close NO by buying YES at same strike → hedged pair pays $1.00

# Settlement: "Exceed X°F" = strictly > X
# YES K55 pays if actual > 55 (i.e., 56°F or higher)
# NO K58 pays if actual ≤ 58

# P&L for condor: payout - entry_cost
# Both win: $2.00 - entry
# One wins: $1.00 - entry (loss if entry > $1.00, which it always is)

# IB library: ib_async (NOT ib_insync)
# WU API key: e1f10a1e78da46f5b10a1e78da96f525
```

---

## How to Run

```bash
# 1. Start IB Gateway (manual — requires GUI login)
# Connect to port 4001, enable API connections

# 2. Activate environment
cd "/Users/vigneshwaranarumugam/Documents/Trading Github/forecastbot"
source venv/bin/activate

# 3. Run the bot
python3 weather_condor.py

# Or use launchd for persistence:
./scripts/manage_services.sh install    # installs both condor + watchdog
./scripts/manage_services.sh status     # check status
./scripts/manage_services.sh stop       # stop both services
```

---

*ForecastBot Handoff — April 6, 2026*
