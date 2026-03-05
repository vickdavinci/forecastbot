# CLAUDE.md — ForecastBot AI Agent Instructions

## Analysis Rigor Rules (MANDATORY)

- NEVER state a config value from memory — always grep/read the actual line first
- NEVER claim a root cause without tracing the exact code path with line numbers
- NEVER propose a fix without first verifying current behaviour with data
- NEVER use bid prices or last-traded prices in gap calculations — ASK prices only, always
- If an earlier statement contradicts new evidence, flag it immediately as a correction
- Distinguish clearly: **CONFIRMED** (verified in code/data) vs **HYPOTHESIS** (needs verification)
- When analysing position state, cross-reference PostgreSQL DB + IBKR portfolio — never rely on one source alone

---

## 🚨 WAKE-UP PROTOCOL (READ FIRST AFTER COMPACTION OR NEW SESSION)

**Context Amnesia Warning:** If this session just started or was compacted, you have lost:
- Shell state (venv not active)
- Memory of what task you were working on
- Any files you previously read

**Before doing anything else, run these commands:**

```bash
# 1. Activate environment and verify Python version
source venv/bin/activate && python --version
# Expected: Python 3.11.x

# 2. Check current task state
head -60 WORKBOARD.md

# 3. Check git status for uncommitted work
git status && git branch

# 4. Verify IBKR paper account connection (if doing execution work)
python scripts/test_connection.py
# Expected: Connected to paper account, ForecastEx contracts discoverable
```

**Why this matters:**
- WORKBOARD.md tracks what task is in progress
- You may have uncommitted changes from before compaction
- IBKR connection state is not preserved between sessions

---

## Shared Process (Mandatory)

- Follow `PROCESS.md` for workflow, gates, commit contract, and test artifact process
- If any instruction here conflicts with `PROCESS.md`, run `PROCESS.md` checks first
- Never skip a phase gate — each gate has an explicit pass condition

---

## Build & Test Commands

```bash
# Setup (first time)
make setup                          # Create venv, install deps

# Run all tests
make test                           # or: pytest

# Run single test file
pytest tests/test_s1_parity.py -v

# Run single test function
pytest tests/test_s1_parity.py::test_gap_detected_on_ask_prices -v

# Run risk engine tests only
pytest tests/test_risk.py -v

# Run execution tests (partial fill, retry, unwind)
pytest tests/test_execution.py -v

# Run gap detector tests
pytest tests/test_gap_detector.py -v

# Lint
make lint                           # black + isort

# Validate config
make validate-config                # All required params present

# Test IBKR paper connection
python scripts/test_connection.py

# Discover live ForecastEx contracts (read-only, no orders)
python scripts/discover_contracts.py

# Simulate gap event (for scanner testing without live market)
python scripts/simulate_gap.py --contract BTC_90K --sum 0.89
```

---

## Project Overview

**ForecastBot** is a 24/7 parity arbitrage bot trading ForecastEx event contracts via Interactive Brokers. It monitors BTC, S&P 500, and Nasdaq prediction market contracts, detects YES+NO sum violations below $0.93, and executes dual-leg limit orders to lock in guaranteed profit.

**Phase 1 Strategy:**
- **S1 — Parity Arbitrage**: YES_ask + NO_ask < $0.93 → buy both → profit locked at entry
- **S2 — Carry Harvest**: Deploy idle capital in hedged pairs earning 3.14% APY (capital floor, not primary alpha)

**Key Market Reality (confirmed from live data March 4, 2026):**
- ForecastEx is illiquid most of the time (0 trades observed on primary contract)
- Gaps open episodically during catalyst events (NFP, CPI, FOMC, BTC large moves)
- When gaps open they persist for hours — no competition capturing them
- **Always use ASK prices.** Web UI shows last-traded prices which can be 2 days stale.

**Budget:** $30,000 CAD (~$22,050 USD active capital)
**Max positions:** 3 concurrent

---

## Repository Structure

```
forecastbot/
├── config.py                     # ALL tunable parameters — never hardcode
├── main.py                       # Async entry point, event loop
├── requirements.txt              # Pinned dependencies
├── .env.example                  # Environment variable template (never commit .env)
├── Makefile                      # Workflow automation
├── .python-version               # Python 3.11
│
├── CLAUDE.md                     # This file — AI agent instructions
├── README.md                     # Project overview
├── WORKBOARD.md                  # Current task tracking
├── SPEC.md                       # Full system specification (source of truth)
├── PROCESS.md                    # Workflow gates, commit contract
├── ERRORS.md                     # Known errors and solutions
│
├── core/
│   ├── connection.py             # ib_async connection, auto-reconnect
│   ├── market_data.py            # Quote streaming, tick handling, staleness check
│   └── models.py                 # Contract, Position, ArbOpportunity dataclasses
│
├── universe/
│   ├── contract_universe.py      # Discovery, ATM filter, subscription management
│   ├── catalyst_calendar.py      # Event calendar, scan mode switching (NORMAL/CATALYST)
│   └── gap_detector.py           # GapFormationDetector, sum velocity tracking
│
├── strategies/
│   ├── s1_parity.py              # Parity arb scanner — ASK prices only
│   └── s2_carry.py               # Carry harvest, unwind priority queue
│
├── execution/
│   ├── validator.py              # All 8 validation gates, drop code logging
│   ├── executor.py               # Dual-leg Phase 1/2/3 with retry and unwind
│   └── fill_tracker.py           # Fill callbacks, unhedged detection, timer
│
├── positions/
│   ├── position_manager.py       # Capital state machine, open position tracking
│   ├── reconciler.py             # DB vs IBKR reconciliation on startup
│   └── pnl.py                    # P&L: $1.00 - (yes_cost + no_cost)
│
├── risk/
│   ├── risk_engine.py            # Tiered kill switch (T1/T2/T3)
│   └── alerts.py                 # Telegram bot alerts
│
├── persistence/
│   ├── database.py               # PostgreSQL connection
│   └── schema.sql                # Table definitions
│
├── scripts/
│   ├── test_connection.py        # Verify IBKR paper account connects
│   ├── discover_contracts.py     # Dump live ForecastEx contract universe
│   ├── simulate_gap.py           # Inject fake gap event for scanner testing
│   └── daily_report.py           # Generate daily P&L summary
│
└── tests/
    ├── test_s1_parity.py         # Gap detection with ASK prices
    ├── test_execution.py         # Phase 1/2/3 retry, unwind logic
    ├── test_risk.py              # Kill switch tiers
    ├── test_carry_unwind.py      # Priority queue ordering
    ├── test_gap_detector.py      # Sum velocity, GapSignal enum
    └── test_reconciler.py        # DB vs broker reconciliation
```

---

## Component Map

**Single index for the entire system.** When debugging or modifying any component, read the spec section first.

### Core Components

| Component | File | Spec Section | Description |
|-----------|------|--------------|-------------|
| **Contract Universe** | `universe/contract_universe.py` | SPEC.md §5 | Discovers BTC/SP/NQ contracts, ATM filter, subscription mgmt |
| **Catalyst Calendar** | `universe/catalyst_calendar.py` | SPEC.md §4.2 | NFP/CPI/FOMC dates, NORMAL↔CATALYST mode switching |
| **Gap Detector** | `universe/gap_detector.py` | SPEC.md §4.3 | Sum velocity tracking, GapSignal.EXECUTE/ALERT/NONE |
| **S1 Parity Scanner** | `strategies/s1_parity.py` | SPEC.md §1 | ASK-only gap detection, opportunity events |
| **S2 Carry** | `strategies/s2_carry.py` | SPEC.md §1 | Idle capital deployment, unwind priority queue |
| **Validator** | `execution/validator.py` | SPEC.md §6.1 | 8 validation gates, drop code logging |
| **Executor** | `execution/executor.py` | SPEC.md §6.2 | Dual-leg Phase 1/2/3, retry, unwind |
| **Fill Tracker** | `execution/fill_tracker.py` | SPEC.md §6.2 | Fill callbacks, unhedged leg timer |
| **Position Manager** | `positions/position_manager.py` | SPEC.md §8 | Capital state machine, 3-position limit |
| **Reconciler** | `positions/reconciler.py` | SPEC.md §6.2 | DB vs IBKR on startup |
| **Risk Engine** | `risk/risk_engine.py` | SPEC.md §7 | T1 WARNING / T2 DEFENSIVE / T3 KILL |
| **Alerts** | `risk/alerts.py` | SPEC.md §9 | Telegram: gaps, fills, kills, daily summary |

### Ownership Boundaries (CRITICAL — Prevents Plumbing Bugs)

```
gap_detector.py         owns: sum calculation, velocity, GapSignal emission
s1_parity.py            owns: opportunity detection, does NOT execute
validator.py            owns: all 8 gates, does NOT execute
executor.py             owns: order submission, retry, unwind — ONLY component that submits orders
position_manager.py     owns: capital state machine, open position tracking
risk_engine.py          owns: kill switch tiers — evaluated BEFORE any execution
```

**The golden rule:** `executor.py` is the ONLY component that submits orders to IBKR. Nothing else calls `ib.placeOrder()`.

---

## Blast Radius Control (MANDATORY — Include in All Prompts)

When modifying gap detection logic:
```
DO NOT TOUCH: executor.py, position_manager.py, risk_engine.py
```

When modifying execution retry/unwind:
```
DO NOT TOUCH: s1_parity.py, gap_detector.py, validator.py
```

When modifying risk engine:
```
DO NOT TOUCH: executor.py (risk is evaluated before execution, not inside it)
```

When modifying carry harvest:
```
DO NOT TOUCH: s1_parity.py (carry is separate strategy, does not share scanner)
```

When modifying position manager:
```
DO NOT TOUCH: executor.py internals (position_manager reads fills, does not submit orders)
```

---

## One-Shot Prompt Template

**Copy this template for every coding task. Fill in all fields. Do not skip any.**

```
## Task
[One sentence. E.g.: "Add sum velocity tracking to GapFormationDetector."]

## Context
[Version + what changed last. E.g.: "Gate 1 passed. Scanner detects static gaps. Now adding velocity."]

## Read First (before writing any code)
- [file path] lines [X–Y]
- [spec section]

## Exact Change Required
[2–5 sentences describing the logic. Reference config param names.]

## Do NOT Touch
- [file1.py]
- [file2.py]

## Test Command
pytest tests/[relevant_test].py -v

## Done When
[Exact pass condition. E.g.: "test_sum_velocity_alert passes. No other tests broken."]

## Commit Message
[Pre-written. E.g.: "feat(gap-detector): add sum velocity tracking for early gap formation signal"]
```

---

## Critical Rules — Never Violate

1. **ASK prices only.** `yes_ask + no_ask` — never bid, never last traded. Gate 1 rejects stale asks > 300 seconds old.

2. **executor.py is the only order submitter.** Nothing else calls `ib.placeOrder()`. Ever.

3. **Risk engine runs BEFORE execution.** Every opportunity evaluation: check risk tier first. If T2 or T3 → no new trades.

4. **Reconcile on every startup.** The bot WILL restart (IB Gateway 24h re-auth). Unreconciled state = duplicate positions or lost fills.

5. **Never enter above ENTRY_THRESHOLD.** `sum >= 0.93` → skip. The web UI showing a gap does not mean an executable gap exists.

6. **Carry must be instantly liquidatable.** Only near-ATM contracts with < 14 DTE. Capital must pivot to arb in < 30 seconds.

7. **ForecastEx P&L = $1.00 − (yes_cost + no_cost).** Not sell_price − buy_price. No sell orders exist on ForecastEx.

8. **Catalyst calendar must load on startup.** If calendar fails → enter TIER 2 defensive immediately.

9. **Log every rejected opportunity with a drop code.** "Why isn't the bot trading?" is answered by drop code frequencies, not guessing.

10. **Both legs within 200ms.** Longer gap = price may have moved = guarantee weakened.

---

## ForecastEx-Specific API Notes

```python
# ForecastEx contracts in IBKR TWS API:
secType  = "OPT"
exchange = "FORECASTX"
YES      = Call (right="C")
NO       = Put  (right="P")

# Discovery:
ib.reqContractDetails(Contract(secType="OPT", exchange="FORECASTX", symbol="BTC"))

# IBKR library — use ib_async (NOT ib_insync — unmaintained since 2024):
pip install ib_async
# Docs: https://ib-api-reloaded.github.io/ib_async/

# No sell orders on ForecastEx — to close a YES position, buy NO:
if position.leg_type == "YES":
    submit buy_order(side="NO", qty=position.qty)

# P&L formula:
pnl_per_contract = 1.00 - (yes_cost + no_cost)  # NOT sell - buy
```

---

## Logging Pattern (CRITICAL)

```python
# ALWAYS LOG (trade-level events — production):
log.info("GAP_DETECTED", contract="BTC_90K", yes_ask=0.61, no_ask=0.24, sum=0.85, gap=0.15)
log.info("FILL", strategy="S1", contract="BTC_90K", yes_cost=0.62, no_cost=0.25, qty=200)
log.info("KILL_SWITCH", tier=2, reason="unhedged_exposure", duration_sec=42)
log.info("DROP", code="STALE_YES_PRICE", contract="SP_6600", age_sec=340)

# DEBUG ONLY (scan-level — disable in production):
log.debug("SCAN_TICK", contract="BTC_90K", yes_ask=0.65, no_ask=0.28, sum=0.93)
log.debug("CATALYST_MODE", event="NFP", mode="CATALYST", scan_interval_ms=500)
```

Without this separation: logs will be >1GB/day from scanning 40+ contracts every 500ms.

---

## Drop Codes Reference

Every rejected opportunity must be logged with one of these codes:

| Drop Code | Gate | Meaning |
|-----------|------|---------|
| `STALE_YES_PRICE` | 1 | YES ask > 300 seconds old |
| `STALE_NO_PRICE` | 1 | NO ask > 300 seconds old |
| `INSUFFICIENT_GAP` | 2 | sum >= ENTRY_THRESHOLD (0.93) |
| `BELOW_MIN_PROFIT` | 3 | net profit after slippage < MIN_NET_PROFIT |
| `OI_LIMIT` | 4 | contracts > 1% of min(YES_OI, NO_OI) |
| `INSUFFICIENT_CAPITAL` | 5 | required > available (including unwindable carry) |
| `POSITION_LIMIT` | 6 | already at MAX_CONCURRENT_POSITIONS (3) |
| `DUPLICATE` | 7 | already in this contract pair |
| `NOT_ATM` | 8 | YES ask outside 0.12–0.88 window |
| `RISK_TIER_2` | — | T2 defensive — no new arb allowed |
| `RISK_TIER_3` | — | T3 kill — no execution |

**Aggregate drop code counts every hour.** When bot is not trading, drop code frequencies tell you exactly why.

---

## Phase Gates

| Gate | When | Pass Condition |
|------|------|----------------|
| **Gate 0** | Week 1 | ib_async connects. Contract discovery returns BTC/SP/NQ contracts. Live bid/ask streaming on ≥ 5 contracts. Telegram alert fires. PostgreSQL writes working. |
| **Gate 1** | Week 1–2 | Catalyst calendar loaded. Mode switches NORMAL→CATALYST. GapFormationDetector fires ALERT on simulated sum drop. ATM filter subscribes/unsubscribes correctly. |
| **Gate 2** | Week 2–3 | S1 scanner uses ASK only. All 8 gates implemented and logging drop codes. Dual-leg execution Phase 1/2/3 working on paper. Carry deployed on idle capital. Positions survive restart. |
| **Gate 3** | Week 3–4 | All 3 kill switch tiers working. TIER 3 halts and requires manual restart. IB Gateway reconnect recovers state. Live probe ($2,000 CAD) fills within $0.01 of detected gap. |
| **Gate 4** | Week 4–5 | Full $30K CAD deployed. Bot runs 7 days without manual intervention. First catalyst event captured under live capital. |

**Never skip a gate.** Each gate has one explicit pass condition. If it does not pass, do not proceed.

---

## Scan Mode Behaviour

```
NORMAL mode (default):
  Scan interval:   10 seconds
  Alert threshold: sum < 0.93
  Contracts:       near-ATM only (YES bid 15%–85%)

CATALYST mode (T-30min before HIGH/CRITICAL event, and T+4h after):
  Scan interval:   500ms
  Alert threshold: sum < 0.97 (wider net — catch gap forming)
  Contracts:       near-ATM + wider range
  Telegram:        "CATALYST WINDOW OPEN: [event]. Watching [N] contracts."

AUTO-TRIGGER to CATALYST (regardless of calendar):
  BTC moves ±3% in 4 hours → switch to CATALYST
  ES/NQ moves ±1.5% intraday → switch to CATALYST
```

**Post-catalyst: stay in 500ms mode for 4 full hours.** Most gaps open 30–120 min AFTER the event, not at release time.

---

## Execution Phase Reference

```
PHASE 1 — SUBMIT (T+0):
  Both legs as limit orders within 200ms.
  Wait 10 seconds for fills.
  Both fill → SUCCESS.
  Neither fills → CANCEL both, log DROP:NO_FILL.
  One fills → PHASE 2.

PHASE 2 — CHASE (T+10s):
  Re-submit unfilled leg at ask + $0.01.
  Max 3 retries. Re-validate profit after each.
  Still fills → SUCCESS (reduced profit, still hedged).
  Exhausted → PHASE 3.

PHASE 3 — UNWIND (T+40s):
  Buy opposing contract to close filled leg.
  (YES→buy NO, NO→buy YES — ForecastEx no-sell constraint.)
  Log FAILED_UNWIND with loss amount.
  Telegram alert.
  2 unwinds in session → TIER 2 defensive.
```

---

## Kill Switch Tiers

| Tier | Trigger | Action |
|------|---------|--------|
| **T1 WARNING** | Unhedged leg > 15s OR position loss > $50 | Log. Prioritise opposing leg fill. No new arb. |
| **T2 DEFENSIVE** | Unhedged > 40s OR daily loss > $200 OR 2 unwinds | Stop new arb. Manage existing only. Telegram alert. |
| **T3 KILL** | Unhedged > 90s OR daily loss > $500 OR API disconnect > 60s OR manual | Close ALL positions. Halt. Telegram. Manual restart required. |

---

## Config Quick Reference

All values in `config.py`. Never hardcode.

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `ENTRY_THRESHOLD` | 0.93 | Max sum to enter arb |
| `MIN_NET_PROFIT` | 0.02 | Min profit after slippage |
| `SLIPPAGE_ESTIMATE_PER_LEG` | 0.01 | Per-leg slippage estimate |
| `MAX_SINGLE_TRADE_USD` | 8000 | Hard cap per arb trade |
| `MIN_SINGLE_TRADE_USD` | 2000 | Skip below this size |
| `MAX_CONCURRENT_POSITIONS` | 3 | Hard limit |
| `MAX_OI_PCT` | 0.01 | Max 1% of contract OI |
| `ATM_YES_BID_MIN` | 0.15 | ATM window lower bound |
| `ATM_YES_BID_MAX` | 0.85 | ATM window upper bound |
| `SCAN_INTERVAL_NORMAL_SEC` | 10 | Normal mode scan rate |
| `SCAN_INTERVAL_CATALYST_MS` | 500 | Catalyst mode scan rate |
| `CATALYST_POST_HOURS` | 4 | Hours to stay hot after event |
| `LEG_FILL_TIMEOUT_SEC` | 10 | Phase 1 fill wait |
| `LEG_CHASE_MAX_RETRIES` | 3 | Phase 2 chase attempts |
| `LEG_CHASE_INCREMENT` | 0.01 | Phase 2 price chase step |
| `MAX_PRICE_AGE_SECONDS` | 300 | Staleness rejection threshold |
| `KILL_T1_UNHEDGED_SEC` | 15 | T1 unhedged trigger |
| `KILL_T2_UNHEDGED_SEC` | 40 | T2 unhedged trigger |
| `KILL_T3_UNHEDGED_SEC` | 90 | T3 unhedged trigger |
| `KILL_T2_DAILY_LOSS_USD` | 200 | T2 loss trigger |
| `KILL_T3_DAILY_LOSS_USD` | 500 | T3 loss trigger |
| `S2_MAX_ENTRY_SUM` | 1.00 | Carry: only enter if sum ≤ $1.00 |
| `S2_MIN_DTE` | 7 | Carry: min days to expiry |

---

## Common Pitfalls

See `ERRORS.md` for solutions. Key issues:

1. **Using last-traded price instead of ask** — Gate 1 must check `ask_age`, not just `ask` presence
2. **Sum calculated on bid not ask** — Always `yes_ask + no_ask`, never `yes_bid + no_bid`
3. **ib_insync imported instead of ib_async** — `ib_insync` is dead. Use `from ib_async import IB`
4. **Carry position blocking arb capital** — Carry unwind must complete before arb order submits
5. **Position state lost on restart** — Reconciler must run before any scanning begins
6. **ForecastEx sell order attempted** — No sell on ForecastEx. Always buy the opposing leg to close.
7. **Catalyst mode not entered before event** — Pre-event window is T-30min. Check calendar fires correctly.
8. **Drop codes not logged** — Every gate rejection must call `log_drop(code)` — never silently discard

---

## Custom Agents (defined in `.claude/agents/`)

| Agent | Purpose | Usage |
|-------|---------|-------|
| **gap-analyzer** | Analyse DB gap log — frequency, duration, size by category and time of day | `Use gap-analyzer to analyse last 14 days of gap_log table` |
| **position-auditor** | Cross-reference DB positions vs IBKR portfolio, flag orphans and mismatches | `Use position-auditor to validate current state` |
| **catalyst-reporter** | Generate weekly report: gaps detected vs executed, capture rate, best catalyst events | `Use catalyst-reporter to generate week ending March 14` |

---

## Recent Work Log

*(Update this section after every significant commit)*

### Current Version: Gate 0

**Status:** Infrastructure phase — not yet trading

**Next task:** See WORKBOARD.md

---

*ForecastBot CLAUDE.md v1.0 — March 4, 2026*
*Modelled on Alpha NextGen V2 CLAUDE.md patterns*
