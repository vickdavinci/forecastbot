# ERRORS.md — ForecastBot Known Errors and Solutions

**When something breaks, check here first before debugging from scratch.**

---

## Drop Code Reference (Bot Is Silent — Not Trading)

When the bot is running but not executing, pull the drop code summary:

```bash
python scripts/drop_code_summary.py --hours 24
```

| Drop Code | Root Cause | Fix |
|-----------|-----------|-----|
| `STALE_YES_PRICE` | YES ask not updated in > 300s — last traded price being used | Check `market_data.py` ask age tracking. Confirm `reqMktData()` is subscribed to tick type 1 (bid/ask), not just last price. |
| `STALE_NO_PRICE` | Same as above for NO leg | Same fix. |
| `INSUFFICIENT_GAP` | sum >= 0.93 — no genuine arb right now | Normal. Check gap_log to see if any sum < 0.97 events exist. If nothing in 48h → check contract ATM filter. |
| `BELOW_MIN_PROFIT` | Gap exists but too small after slippage | Check `SLIPPAGE_ESTIMATE_PER_LEG` in config. May need adjustment based on observed fills. |
| `OI_LIMIT` | Position would exceed 1% of OI | Reduce `MAX_SINGLE_TRADE_USD` or check if OI data is stale. |
| `INSUFFICIENT_CAPITAL` | No free capital — carry positions blocking arb | Check carry position unwind queue. Verify carry positions are near-ATM with < 14 DTE (instant exit). |
| `POSITION_LIMIT` | Already at 3 open arb positions | Normal if 3 positions open. Check if positions are settling correctly and capital recycling. |
| `DUPLICATE` | Already in this exact contract pair | Check `position_manager.py` duplicate detection logic. |
| `NOT_ATM` | YES ask outside 0.12–0.88 window | ATM filter may need recalibration. BTC price moved — check which contracts are now near-ATM. |
| `RISK_TIER_2` | T2 defensive mode active — no new arb | Check what triggered T2 (unhedged leg or loss). Resolve trigger, then manual reset. |
| `RISK_TIER_3` | T3 kill switch active | Bot requires manual restart. Check Telegram for kill reason. |

**No drop codes at all** = scanner is not reaching the validator. Check:
1. Is `gap_detector.py` receiving ticks? (Check DEBUG logs)
2. Is `s1_parity.py` consuming GapSignal? (May be in CATALYST mode only)
3. Is risk engine blocking before validator? (Check risk tier)

---

## Connection Errors

### `ConnectionRefusedError: [Errno 111]` on startup
**Cause:** IB Gateway not running or wrong port.
```bash
# Check if gateway is running
ps aux | grep -i gateway

# Paper account port: 7497
# Live account port: 7496
# Verify in config.py: IBKR_PORT = 7497
```

### `ERROR: No market data for contract`
**Cause:** ForecastEx market data subscription not active OR contract not found correctly.
```bash
# Verify contract details
python scripts/discover_contracts.py --symbol BTC
```
Check: `secType="OPT"`, `exchange="FORECASTX"`, `right="C"` for YES, `right="P"` for NO.

### `ib_insync not found` or import errors
**Cause:** Wrong library installed. `ib_insync` is dead (maintainer passed away 2024).
```bash
pip uninstall ib_insync
pip install ib_async
# Update imports: from ib_async import IB, Contract
```

### IB Gateway disconnects every 24h
**Cause:** IBKR mandatory re-authentication.
- `core/connection.py` handles auto-reconnect.
- After reconnect, `reconciler.py` MUST run before scanning resumes.
- If positions exist in DB but not at IBKR after reconnect → they settled during downtime. Mark as SETTLED.

---

## Execution Errors

### Both legs submitted but only one fills
**Cause:** Thin order book on one side — second leg missed fill in 10s window.
- Phase 2 (chase) should kick in automatically.
- Check `fill_tracker.py` — is the unhedged timer starting correctly?
- If Phase 2 exhausted with no fill → Phase 3 (unwind) fires. Check Telegram for UNWIND alert.

### `FillError: Cannot sell on ForecastEx`
**Cause:** Code is attempting a sell order. ForecastEx has no sell orders.
- All closes must be buy orders on the opposing leg.
- Check `executor.py` close logic: YES position → buy NO. NO position → buy YES.

### Leg 1 filled but Leg 2 shows as open indefinitely
**Cause:** Fill callback not firing for Leg 2 — order may have been silently rejected.
```python
# In executor.py, always check order status explicitly after timeout:
# Don't rely solely on fill callbacks for IBKR ForecastEx contracts
```
- Check IBKR order status via `ib.openOrders()` after timeout.
- If order rejected silently → IBKR account permission issue for ForecastEx options.

### P&L showing negative when gap was confirmed positive
**Cause:** P&L calculated as `sell_price - buy_price` instead of `$1.00 - (yes_cost + no_cost)`.
```python
# WRONG:
pnl = sell_price - buy_price

# CORRECT:
pnl = 1.00 - (position.yes_cost + position.no_cost)
```
Check `positions/pnl.py`.

---

## Market Data Errors

### `yes_ask` returns 0 or None for a contract
**Cause 1:** Contract is deep ITM or OTM — no two-sided market.
- ATM filter should have excluded this. Check ATM filter logic.
- YES bid > 0.88 or < 0.12 → no NO or YES market respectively.

**Cause 2:** Using `last_price` instead of `ask`.
- IBKR tick types: `tick_type=1` = bid, `tick_type=2` = ask, `tick_type=4` = last
- Gate 1 in validator checks price age but assumes ask is populated.
- Verify `reqMktData()` is requesting tick type 2 (ask) not just last.

### Sum shows 0.91 but no EXECUTE signal fires
**Cause:** Sum calculated from bid prices, not ask. Web UI misleads — bid appears to show gap.
- Confirm `gap_detector.py` uses `yes_ask + no_ask`.
- Add explicit log: `log.debug("SUM_CALC", yes_ask=X, no_ask=Y, sum=Z)` and verify values.

### Prices seem stale (same value for > 5 minutes)
**Cause:** Market data subscription dropped silently.
- IBKR has a limit on concurrent market data subscriptions.
- If > ~100 contracts subscribed, some may be silently dropped.
- ATM filter should keep active subscriptions < 30 at any time.
- Check `universe/contract_universe.py` subscription count.

---

## Carry Harvest Errors

### Carry positions blocking arb capital
**Cause:** Carry unwind queue not prioritising by exit cost correctly.
- Carry positions should be sorted by `closing_leg_ask` ascending.
- Check `strategies/s2_carry.py` `prioritise_carry_unwind()` method.
- Carry positions must be in near-ATM contracts with DTE < 14 — verify entry filter.

### Carry position stuck (can't close)
**Cause:** Opposing leg has no ask (deep ITM/OTM — one-sided book).
- This should not happen if `S2_MIN_DTE = 7` and ATM filter applied at carry entry.
- If it happens: flag as ORPHAN, alert, close manually from IBKR portal.

---

## Database Errors

### `psycopg2.OperationalError: could not connect to server`
```bash
# Check PostgreSQL is running
sudo systemctl status postgresql

# Check connection string in .env
DATABASE_URL=postgresql://user:password@localhost:5432/forecastbot
```

### Position in DB but not in IBKR after restart
**Cause:** Position settled while bot was offline. Normal behaviour.
- Reconciler marks this as SETTLED automatically.
- If reconciler is not catching this → check `reconciler.py` portfolio query.

### Position in IBKR but not in DB after restart
**Cause:** Fill occurred but DB write failed (crash between fill and write).
- Reconciler flags this as ORPHAN.
- Manual review required: is this an open position or already closed?
- Check IBKR account statement for fill time and cost.

---

## Risk Engine Errors

### T2 triggered but bot won't reset after unhedged leg was fixed
**Cause:** T2 requires manual reset (by design — defensive posture).
```bash
python scripts/reset_risk_tier.py --tier 2
# This requires confirmation prompt — not silent
```

### T3 kill switch fired but positions still open
**Cause:** Close orders submitted but fills pending. T3 does not force-cancel — it submits closes.
- Check IBKR order status: are close orders pending fill?
- ForecastEx thin books may delay fills.
- If position still open after 10 minutes: close manually from IBKR portal.

---

## Catalyst Calendar Errors

### Bot not switching to CATALYST mode before NFP
**Cause 1:** Calendar not loaded correctly. Check startup log for "CATALYST_CALENDAR_LOADED".
**Cause 2:** Event date/time wrong timezone. All times in `catalyst_calendar.py` must be EST.
**Cause 3:** Pre-event window timing: T-30min check runs every 10s in NORMAL mode — may miss exact T-30 by up to 10s. Acceptable.

### Bot stuck in CATALYST mode long after event
**Cause:** Post-event timer not clearing. `CATALYST_POST_HOURS = 4` in config — bot stays hot for 4h after event. This is by design. If > 4h → check `catalyst_calendar.py` post-event timer.

---

## Telegram Errors

### Alerts not arriving
```bash
# Test Telegram connection
python scripts/test_telegram.py

# Check .env
TELEGRAM_BOT_TOKEN=<your token>
TELEGRAM_CHAT_ID=<your chat id>
```
- Bot must be started in the Telegram chat first (`/start`)
- Chat ID for group: negative number. For direct: positive number.

### Too many Telegram alerts (spam)
**Cause:** Alert throttling not implemented. Same gap event firing repeatedly.
- Add alert deduplication: same contract + same signal should not re-alert within 10 minutes.
- Check `risk/alerts.py` for dedup logic.

---

## Performance / CPU Errors

### High CPU in CATALYST mode
**Cause:** 500ms scan on 40+ contracts with DEBUG logging enabled.
- Disable DEBUG logs in production: `LOG_LEVEL=INFO` in config.
- If still high: check if `reqMktData()` tick callbacks are queueing faster than processing.
- Single-process asyncio should handle 40 contracts at 500ms easily. If not → profile `gap_detector.py`.

---

*Update this file whenever a new error is encountered and resolved.*
*Format: Error → Root Cause → Exact Fix*
