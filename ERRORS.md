# ERRORS.md — ForecastBot Known Errors and Solutions

**When something breaks, check here first before debugging from scratch.**

---

## kill_shot.py — Known Issues

| Error | Cause | Fix |
|-------|-------|-----|
| Daily contracts not refreshing | Auto-refresh triggers at 09:31 ET — if started after, stale contracts | Restart kill_shot.py or wait until next day |
| Gap alert fires on single bad tick | Data quality issue — should require 3 consecutive confirming ticks | Check tick confirmation counter in kill_shot.py |
| FES contracts use wrong secType | S&P futures use FOP not OPT on FORECASTX | Ensure contract definition uses `secType="FOP"` for FES |
| `bid=0 ask=0` for all contracts | Market data subscription not active or contract not found | Check IBKR market data subscriptions, verify contract symbols |
| Auto-reconnect exhausted | IB Gateway down for extended period (>10 attempts) | Restart IB Gateway, then restart kill_shot.py |

---

## weather_edge.py — Known Issues

| Error | Cause | Fix |
|-------|-------|-----|
| All 14 UHLAX strikes show `n/a` prices after warmup | IB event loop was blocked (threading in v1/v2). v3.0 fixes with full async | Verify running v3.0: check for `async def main()` and `asyncio.run(main())` |
| `n/a` persists in v3.0 | `reqMktData` may need different params for FORECASTX | Try: `yt = self.ib.reqMktData(contract, genericTickList="", snapshot=False)` |
| NWS API returns stale observation | NWS updates every ~10 min, sometimes delayed | Check `observation.timestamp` vs current time. If > 30 min stale, log warning |
| Santa Ana filter false positive | Wind > 25mph but not from offshore direction | Verify wind direction is N/NNW/NNE/NE/ENE before suppressing NWS signal |
| `ConnectionRefusedError` on clientId=45 | IB Gateway not running or port wrong | Check .env: IBKR_PORT=4001, IBKR_CLIENT_ID_WEATHER=45 |
| NWS API 503/timeout | api.weather.gov rate limited or down | Uses `run_in_executor` — non-blocking. Will retry next 5-min cycle |

---

## Connection Errors

### `ConnectionRefusedError: [Errno 111]` on startup
**Cause:** IB Gateway not running or wrong port.
```bash
# Check if gateway is running
ps aux | grep -i gateway

# Port: 4001 (verify in .env)
# Enable: Configure -> API -> Settings -> Enable ActiveX and Socket Clients
```

### `ERROR: No market data for contract`
**Cause:** ForecastEx market data subscription not active OR contract not found correctly.
```bash
# Verify contract details
python3 discover_contracts.py
```
Check: `secType="OPT"` for most contracts, `secType="FOP"` for FES. `exchange="FORECASTX"`, `right="C"` for YES, `right="P"` for NO.

### `ib_insync not found` or import errors
**Cause:** Wrong library installed. `ib_insync` is dead (maintainer passed away 2024).
```bash
pip uninstall ib_insync
pip install ib_async
# Update imports: from ib_async import IB, Contract
```

### IB Gateway disconnects
**Cause:** IBKR periodic re-authentication or network issue.
- kill_shot.py has auto-reconnect (up to 10 attempts)
- weather_edge.py runs in single async loop — reconnect TBD
- If reconnect fails: restart the script manually

---

## Future Components — Not Yet Built

The sections below document errors for components that will be built in later gates.

---

## Drop Code Reference (Gate 1+)

When the execution engine is built, pull drop code summary to debug silence:

| Drop Code | Root Cause | Fix |
|-----------|-----------|-----|
| `STALE_YES_PRICE` | YES ask not updated in > 300s | Check ask age tracking in market_data.py |
| `STALE_NO_PRICE` | Same as above for NO leg | Same fix |
| `INSUFFICIENT_GAP` | sum >= 0.93 — no genuine arb | Normal. Check gap_log for sum < 0.97 events |
| `BELOW_MIN_PROFIT` | Gap too small after slippage | Check SLIPPAGE_ESTIMATE_PER_LEG in config |
| `OI_LIMIT` | Position would exceed 1% of OI | Reduce MAX_SINGLE_TRADE_USD or check OI staleness |
| `INSUFFICIENT_CAPITAL` | No free capital — carry blocking arb | Check carry unwind queue |
| `POSITION_LIMIT` | Already at 3 open arb positions | Normal if 3 positions open |
| `DUPLICATE` | Already in this contract pair | Check position_manager duplicate detection |
| `NOT_ATM` | YES ask outside 0.12-0.88 window | ATM filter may need recalibration |
| `RISK_TIER_2` | T2 defensive mode active | Check what triggered T2, resolve, then reset |
| `RISK_TIER_3` | T3 kill switch active | Manual restart required. Check Telegram |

---

## Execution Errors (Gate 3+)

### Both legs submitted but only one fills
**Cause:** Thin order book — second leg missed in fill window.
- Phase 2 (chase) kicks in automatically
- Phase 3 (unwind) if chase exhausted

### `FillError: Cannot sell on ForecastEx`
**Cause:** Code attempting sell order. ForecastEx has no sell orders.
- Closes must be buy orders on opposing leg (YES -> buy NO, NO -> buy YES)

### P&L showing negative when gap was confirmed positive
**Cause:** P&L calculated as `sell_price - buy_price` instead of `$1.00 - (yes_cost + no_cost)`.

---

## Carry Harvest Errors (Gate 3+)

### Carry positions blocking arb capital
**Cause:** Carry unwind queue not prioritising by exit cost.
- Sort by `closing_leg_ask` ascending

### Carry position stuck (can't close)
**Cause:** Opposing leg has no ask (deep ITM/OTM).
- Flag as ORPHAN, close manually

---

## Risk Engine Errors (Gate 4+)

### T2 triggered but bot won't reset
**Cause:** T2 requires manual reset (by design).

### T3 kill switch fired but positions still open
**Cause:** Close orders submitted but fills pending on thin books.
- If open after 10 minutes: close manually from IBKR portal

---

*Update this file whenever a new error is encountered and resolved.*
*Format: Error -> Root Cause -> Exact Fix*
