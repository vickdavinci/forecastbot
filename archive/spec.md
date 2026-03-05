# Prediction Market Trading Bot — System Specification

**Classification:** Internal Development Spec  
**Version:** 1.0  
**Audience:** Lead Developer  

---

## 1. Overview

This document outlines the architecture, strategy rationale, tech stack, and implementation requirements for an automated trading bot operating on prediction market event contracts via Interactive Brokers (IBKR). The system is designed to execute purely mathematical arbitrage strategies — strategies where profitability is derived from pricing law violations, not from predicting real-world outcomes.

---

## 2. What Are Prediction Markets

Prediction markets are regulated exchanges where participants trade contracts tied to the outcome of real-world events. Each contract resolves to either $1.00 (if the event occurs) or $0.00 (if it does not). Prices reflect the market's collective probability estimate of an event occurring, expressed as a value between $0.01 and $0.99.

### Key Platforms Accessible via IBKR Canada

**ForecastEx (IBKR subsidiary)**
- Contracts on economic indicators, Fed rate decisions, BTC/ETH price levels, climate data
- $1.00 payout per contract, quoted in $0.01 increments
- Zero commission
- 3.14% APY incentive coupon paid on open positions
- Critical constraint: positions cannot be sold directly — to exit, you buy the opposing leg (YES or NO), and IBKR nets them automatically
- Available to Interactive Brokers Canada Inc. clients
- Regulated by the US CFTC

**CME Group Event Contracts (accessible via IBKR)**
- Contracts on daily moves of S&P 500, NQ, Gold, Oil, FX futures
- $100 payout per contract, quoted in $1.00 increments
- Standard buy and sell mechanics (no opposing-leg constraint)
- Exchange fees apply (~$0.25–$0.40 per contract)

### Why Canada

Canadian regulations generally prohibit retail prediction market access, but IBKR Canada clients access ForecastEx through its US CFTC-regulated structure. This is currently operational and legal.

---

## 3. Strategy Philosophy — Why Tier 1 Only

The system will exclusively implement **Tier 1 Pure Mathematical Arbitrage strategies.** These are strategies where:

- The real-world outcome of the event is **completely irrelevant** to profitability
- Profit is **locked in at the moment of entry** through mathematical pricing relationships
- There is **zero directional market risk** — the bot takes no view on what will happen

This decision is deliberate. Prediction-based strategies (news trading, probability modelling, drift trading) require domain expertise, model maintenance, information edges, and carry genuine market risk. Tier 1 strategies carry only operational risk — the risk that the system fails to execute correctly, not that the market moves against us.

The mathematical laws being exploited are permanent. They cannot change because they are logical constraints, not market behaviours. This gives the strategy set a fundamentally durable edge profile.

---

## 4. Tier 1 Strategy Set

The following seven strategies form the complete scope of this system. Each is described at the conceptual level sufficient for implementation. Detailed pricing parameters, thresholds, and sizing logic will be provided separately.

---

### S1 — Parity Arbitrage

**Law:** `YES price + NO price = $1.00` at expiry, always.

When the sum of YES and NO prices for the same contract falls below $1.00, buying both legs guarantees a profit equal to the difference. The bot monitors all active contracts in real time, detects parity violations above a minimum threshold, and executes both legs simultaneously.

**Key mechanic:** Both legs must execute together. A partial fill on one leg without the other creates unhedged directional exposure.

---

### S2 — Incentive Coupon Carry Harvest

**Law:** ForecastEx pays 3.14% APY on all open positions regardless of outcome.

A fully hedged position (YES + NO on the same contract) has zero directional exposure. If the combined entry cost is at or below $1.00, the position is guaranteed to break even or better at settlement while accruing the daily coupon. This strategy runs continuously in the background, ensuring capital is never sitting idle in cash.

**Key mechanic:** This is a capital utilisation strategy, not a standalone alpha source. It runs underneath all other strategies, deployed on capital not currently committed to active arb trades.

---

### S3 — MUTEX Basket Arbitrage

**Law:** The sum of all YES prices across a mutually exclusive, exhaustive outcome set must equal exactly $1.00.

ForecastEx lists multiple outcome buckets for single events (e.g., Fed rate decision: cut 50bp+, cut 25bp, hold, raise 25bp, raise 50bp+). Exactly one bucket will resolve YES. Therefore all YES prices must sum to $1.00. When they sum to less, buying all buckets proportionally guarantees a profit.

**Key mechanic:** The bot must correctly identify complete, exhaustive sets. Buying an incomplete basket is not guaranteed to pay $1.00. Contract set definitions must be validated against ForecastEx rulebooks.

---

### S4 — Monotonic Probability Violation

**Law:** `P(event crosses lower threshold) ≥ P(event crosses higher threshold)` always.

For contracts on the same underlying with different strike levels (e.g., BTC above $90K, $95K, $100K), a higher threshold is a strict subset of a lower threshold. The probability of the lower threshold resolving YES must always be at least as large as the higher threshold. When this ordering is violated in market prices, a hedged position across the violated pair locks in a guaranteed profit on all resolution paths.

**Key mechanic:** The bot must sort contracts by threshold level, detect adjacent inversions, and construct the correct hedge pair. All three resolution paths must be verified profitable before execution.

---

### S5 — Synthetic Replication Arbitrage

**Law:** `YES = $1.00 − NO` in all cases.

YES and NO contracts on the same question trade in separate order books with independent market makers. When one book is hit heavily and the other lags in repricing, a transient gap opens between the actual price and the synthetic price implied by the opposing contract. The bot identifies when buying the actual contract is cheaper than constructing it synthetically, and executes accordingly alongside the opposing leg to lock in the spread.

**Key mechanic:** This overlaps with S1 mechanically but targets a different market microstructure pattern — single-sided book imbalance rather than symmetric parity gap.

---

### S6 — Conditional Probability Chain Arbitrage

**Law:** `P(superset event) ≥ P(subset event)` always.

When ForecastEx lists contracts across overlapping time windows for the same event (e.g., "Fed cuts in March" and "Fed cuts in Q1"), the broader window is a superset of the narrower. Its YES price must be at least as high. When the subset is priced higher than the superset, a hedged position across both contracts is profitable on every resolution path.

**Key mechanic:** The bot must map logical containment relationships between contracts — this requires parsing contract definitions, not just prices. Containment must be verified semantically, not assumed from labels.

---

### S7 — Calendar Spread Arbitrage

**Law:** `P(event by later date) ≥ P(event by earlier date)` always.

The same underlying question listed across multiple expiries (e.g., BTC above $100K by March 31, by June 30, by December 31) must be monotonically non-decreasing over time. A later expiry includes all earlier expiry possibilities plus additional time. When a later expiry is priced lower than an earlier one, a hedged position across the two expiries locks in profit on all paths.

**Key mechanic:** Contracts must be matched by identical underlying and strike, differing only in expiry. The bot must validate that the two contracts resolve against the same data source and strike definition before pairing.

---

## 5. System Architecture

### High-Level Components

```
┌─────────────────────────────────────────────────────────────┐
│                        SCANNING ENGINE                       │
│  Real-time price ingestion → Violation detection per strategy│
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                      OPPORTUNITY ROUTER                      │
│  Ranks violations by EV → Filters by capital availability   │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                     EXECUTION ENGINE                         │
│  Multi-leg order construction → Simultaneous submission      │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                   POSITION MANAGER                           │
│  Tracks open legs → Monitors fills → Manages carry harvest   │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                  RISK & MONITORING LAYER                     │
│  Operational safeguards → Alerts → Kill switch → Logging    │
└─────────────────────────────────────────────────────────────┘
```

---

## 6. Tech Stack

### Primary Language — Python 3.11+

**Why:** IBKR's official API has mature, well-documented Python bindings. The async ecosystem (asyncio) handles the concurrent scanning of hundreds of contracts without thread management complexity. NumPy and Pandas provide the numerical operations needed for basket sum calculations and monotonic checks. The quant trading ecosystem in Python is the broadest available.

### IBKR Connectivity — `ib_insync`

**Why:** `ib_insync` is the de facto standard async wrapper around IBKR's TWS API. It abstracts the low-level socket protocol into clean Python objects, handles reconnection, and supports streaming market data across hundreds of contracts simultaneously. The alternative (raw TWS API) is significantly more verbose with no performance benefit for this use case.

```
pip install ib_insync
```

Connects to Trader Workstation (TWS) or IB Gateway running locally or on a VPS.

### Market Data — IBKR WebSocket + TWS API

**Why:** IBKR provides real-time streaming quotes via TWS API for all ForecastEx and CME event contracts. No third-party data feed is required. Tick-level data is sufficient for Tier 1 strategies — these are not HFT latency-sensitive in the microsecond range, but they are time-sensitive in the seconds range.

### Database — PostgreSQL

**Why:** Opportunity logs, fill records, position state, and contract metadata need persistent storage with query capability. SQLite is insufficient for concurrent writes from the scanning engine. PostgreSQL handles concurrent reads/writes cleanly and is the standard for trading system record-keeping.

### Task Queue — Celery + Redis

**Why:** The scanning engine, execution engine, and position manager must run as independent processes. Celery manages task distribution; Redis serves as the message broker. This separation prevents a slow execution from blocking the scanner.

### Deployment — Linux VPS (Ubuntu 22.04)

**Why:** The bot must run 24/7 without interruption. A dedicated VPS co-located in a data centre provides consistent uptime, low-latency connectivity to IBKR servers, and isolation from local hardware issues. IB Gateway (headless TWS) runs as a daemon process.

Recommended providers: AWS EC2 (us-east-1 for proximity to IBKR servers), Vultr, or Hetzner.

### Monitoring — Prometheus + Grafana

**Why:** Real-time dashboards for opportunity capture rate, fill latency, position exposure, coupon accrual, and system health. Alerting via Prometheus alertmanager for operational failures (missed fills, API disconnects, anomalous position states).

### Logging — Structured JSON via `structlog`

**Why:** Every scan cycle, opportunity detected, order submitted, and fill received must be logged with timestamps and contract identifiers. Structured JSON enables querying logs programmatically for post-trade analysis.

### Contract Universe Management — Custom Module

**Why:** ForecastEx contract definitions, MUTEX set mappings, monotonic chains, and calendar spread pairings must be maintained in a structured registry. This module handles contract discovery via IBKR API, validates set completeness against ForecastEx rulebooks, and keeps the active universe current as new contracts are listed and expired ones are removed.

---

## 7. Scanning Logic — Core Loop

The scanner runs a continuous loop across all active contracts:

```
Every tick received:
  → Update internal price state for contract
  → Run S1 check: sum YES + NO, flag if < threshold
  → Run S2 check: flag all pairs where sum ≤ carry threshold
  → Run S3 check: sum all YES in each MUTEX set, flag if < threshold  
  → Run S4 check: sort by strike, check adjacent pairs for inversion
  → Run S5 check: compare actual vs synthetic price, flag gap
  → Run S6 check: compare subset vs superset contract prices
  → Run S7 check: compare same-strike contracts across expiries
  → All flags passed to Opportunity Router with EV and size estimate
```

The scanner must process ticks faster than they arrive. At current ForecastEx contract counts (~50–200 active), this is well within Python's async capabilities without native code optimisation.

---

## 8. Execution Requirements

### Atomicity

For S1, S3, and all multi-leg strategies, both (or all) legs must be submitted as close to simultaneously as possible. IBKR does not offer native combo orders for ForecastEx contracts the way it does for options spreads, so legs are submitted sequentially with sub-100ms gap. Partial fill handling must be implemented — if leg 1 fills but leg 2 fails, the position is unhedged and must be managed immediately.

### Order Types

- Limit orders only — never market orders on thin books
- Limit price set at or inside the ask for buys
- Fill-or-kill preferred for arb legs to avoid partial exposure

### ForecastEx No-Sell Constraint

ForecastEx does not permit sell orders. Exiting a YES position requires buying NO (and vice versa). The execution engine must account for this in all order construction. CME event contracts permit standard buy/sell mechanics.

---

## 9. Implementation Challenges

These are the known hard problems the developer will encounter. They are not blockers but require careful engineering decisions.

### 9.1 Leg Synchronisation

Multi-leg strategies (S1, S3, S4, S6, S7) require near-simultaneous execution of 2–6 orders. IBKR does not provide atomic multi-leg execution for these contract types. The gap between leg submissions creates a window where prices can move. If leg 1 fills at the expected price but leg 2 has repriced, the guaranteed profit shrinks or disappears. The execution engine must detect this and either complete the trade at the new price (if still profitable above minimum threshold) or cancel and unwind.

### 9.2 MUTEX Set Validation

Strategy S3 requires the bot to know with certainty that it has identified all contracts in a mutually exclusive exhaustive set. ForecastEx publishes contract rulebooks defining resolution criteria. These must be parsed and mapped — a basket that is missing one outcome bucket is not guaranteed to pay $1.00. This requires a contract registry that is updated as new contracts are listed and validated against official documentation.

### 9.3 Contract Discovery Latency

New ForecastEx contracts are listed periodically. The bot must discover new contracts quickly, classify them into the correct strategy sets, and begin scanning them. IBKR's contract search API has rate limits. The discovery module must be efficient and avoid hammering the API.

### 9.4 Carry Harvest Capital Allocation

Strategy S2 (carry harvest) runs on capital not used by active arb trades. The position manager must dynamically allocate and deallocate capital between carry harvest positions and arb opportunities. Carry positions need to be unwound quickly when arb capital is needed — this means maintaining a prioritised queue of carry positions by ease of exit (how close their YES+NO sum is to $1.00 on the NO side for quick exit).

### 9.5 Settlement Timing Mismatch

ForecastEx contracts settle at a defined time after the event occurs, based on an official data source. The bot must track each contract's resolution time and data source precisely. Holding a position past the last trade time without a fill on the opposing leg creates unintended directional exposure. The position manager must flag approaching expiries and force-manage any open legs.

### 9.6 API Rate Limits and Reconnection

IBKR's TWS API imposes rate limits on market data subscriptions and order submissions. At scale (100+ active contracts), the scanner may approach subscription limits. The system must implement subscription management — prioritising actively-scanning contracts, rotating subscriptions as opportunities emerge, and gracefully handling API disconnections with automatic reconnection and state reconciliation.

### 9.7 Monotonic and Chain Set Mapping

Strategies S4, S6, and S7 require the bot to understand logical relationships between contracts — not just their prices. A contract for "BTC above $95K by March 31" and "BTC above $90K by March 31" must be identified as a monotonic pair. "Fed cuts in March" and "Fed cuts in Q1" must be identified as a chain pair. These relationships must be maintained in a structured graph or lookup table, updated as the contract universe changes, and verified against contract definitions — not inferred from ticker symbols alone.

### 9.8 Minimum Viable Profit Threshold

After IBKR fees, the minimum gap that produces actual profit differs by strategy and contract type. ForecastEx contracts have zero commission but the incentive coupon mechanic must be factored. CME contracts have explicit fees. The Opportunity Router must apply correct fee models per contract type before flagging an opportunity as viable. Executing below the fee-adjusted threshold is a loss disguised as an arb.

### 9.9 IB Gateway Stability

IB Gateway (headless TWS) is the connection point for the bot. It requires periodic manual re-authentication (IBKR security requirement, typically every 24 hours unless two-factor is configured for auto-reauth). This must be handled via IBKR's IBKR Key or equivalent automated reauth mechanism. A Gateway outage means zero market data and zero execution — the system must detect this immediately and alert.

---

## 10. What the Developer Does Not Need to Worry About

- Market prediction or outcome forecasting — the system takes no view on events
- Options pricing models (Black-Scholes, etc.) — not applicable to Tier 1
- External data feeds (Bloomberg, news APIs) — IBKR market data is sufficient
- Machine learning or AI components — pure rule-based logic only
- Portfolio hedging with underlying assets — all hedging is internal to contract pairs

---

## 11. Deliverables Expected

| Component | Description |
|---|---|
| Contract Registry | Module to discover, classify, and maintain active contract universe |
| Scanning Engine | Async loop implementing all 7 strategy checks per tick |
| Opportunity Router | EV calculation, fee adjustment, capital availability check |
| Execution Engine | Multi-leg order submission with partial fill handling |
| Position Manager | Open position tracking, carry harvest allocation, expiry management |
| Risk Layer | Kill switch, anomaly detection, position limits, alerting |
| Monitoring Dashboard | Grafana dashboard for key operational metrics |
| Logging Infrastructure | Structured logs for all system events |
| Test Suite | Unit tests for all mathematical checks, integration tests against IBKR paper trading |

---

## 12. Development Environment

- IBKR paper trading account for all development and testing
- IB Gateway running locally during development
- Python virtual environment with pinned dependencies
- Docker Compose for local PostgreSQL and Redis
- All credentials via environment variables — never hardcoded
- Git with branch protection on main — no direct commits

---

## 13. References

- IBKR Event Contracts API Documentation: https://www.interactivebrokers.com/campus/ibkr-api-page/event-contracts/
- IBKR TWS API Event Trading Guide: https://www.interactivebrokers.com/campus/ibkr-api-page/event-trading/
- ForecastEx Contract Universe: https://forecasttrader.interactivebrokers.com/eventtrader/#/markets
- ib_insync Documentation: https://ib-insync.readthedocs.io/
- IBKR Web API Staging Guide: https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-staging/

---

*Document prepared for internal development use. Do not distribute.*

---
---

# ADDENDUM A — Implementation Patterns & Spec Corrections

**Source:** Lessons from building Alpha NextGen V2 (multi-strategy trading system on IBKR via QuantConnect) — specifically the Iron Condor engine (4-leg option structure), VASS spread engine (2-leg debit/credit spreads), and OCO Manager (paired order lifecycle tracking). These systems solved many of the same problems ForecastBot will face.

**Purpose:** Give the implementing agent everything needed to build this correctly on the first pass.

---

## A1. Spec Corrections (MUST FIX)

### A1.1 — `ib_insync` Is Dead, Use `ib_async`

The original author of `ib_insync` (Ewald de Wit) passed away in early 2024. The library is unmaintained and has no new releases. The community fork is **`ib_async`** (maintained by Matt Stancliff under `ib-api-reloaded`).

```bash
# WRONG (spec §6)
pip install ib_insync

# CORRECT
pip install ib_async
```

- Migration is straightforward — `ib_async` is a rename with API improvements
- Minimum Python version raised from 3.6 to 3.10
- `ib_async` implements the full IBKR binary protocol internally (no external `ibapi` package needed)
- Docs: https://ib-api-reloaded.github.io/ib_async/
- Repo: https://github.com/ib-api-reloaded/ib_async

### A1.2 — ForecastEx Contracts Are Modeled as Options in the API

The spec doesn't mention this. In IBKR's TWS API:
- ForecastEx contracts use security type `"OPT"`
- YES contracts = `Call` (right), NO contracts = `Put` (right)
- Exchange is always `"FORECASTX"`
- Contract discovery uses standard `reqContractDetails()` with options-like flow

This is critical for the Contract Registry — it must query using options semantics, not custom contract types.

### A1.3 — Celery + Redis Is Overengineered for V1

The spec recommends Celery + Redis (§6) for task separation between scanner, executor, and position manager. For ~200 contracts, this adds complexity with no performance benefit.

**Replace with:** Single-process asyncio architecture using `ib_async`'s native event loop. The scanner, opportunity router, execution engine, and position manager run as coroutines in one process. This eliminates IPC overhead, simplifies deployment, and matches how `ib_async` is designed to work.

Add Celery + Redis only if contract universe grows past ~1,000 or if execution latency proves insufficient.

### A1.4 — Prometheus + Grafana Is Overengineered for V1

For a single-bot system, replace with:
- **Structured JSON logs** via `structlog` (spec §6 — keep this)
- **Simple alerting** via Telegram bot or email on critical events (unhedged exposure, API disconnect, kill switch)
- **Daily P&L summary** written to a dashboard file or sent via notification

Add Prometheus + Grafana in V2 when running multiple bot instances or when operational complexity justifies it.

### A1.5 — ForecastEx Now Trades Nearly 24/6

The spec doesn't mention trading hours. As of May 2025, IBKR expanded ForecastEx to nearly 24/6 trading. The bot must handle:
- Extended trading hours (not just US market hours)
- Low-liquidity periods where spreads widen
- Weekend shutdown handling

---

## A2. Architecture Patterns (From Alpha NextGen)

### A2.1 — Config-Driven Thresholds (MANDATORY)

**Never hardcode values.** Every tunable parameter must live in a single `config.py` file:

```python
# config.py — ALL tunable parameters in one place

# ── Parity Arbitrage (S1) ──
S1_MIN_PROFIT_THRESHOLD = 0.02      # Minimum YES+NO gap to trade ($0.02)
S1_MAX_POSITION_SIZE = 100          # Max contracts per parity trade

# ── Carry Harvest (S2) ──
S2_MAX_COST_ABOVE_PAR = 1.00       # Max combined cost (must be <= $1.00)
S2_MIN_DAYS_TO_EXPIRY = 7          # Don't enter carry with < 7 DTE

# ── MUTEX Basket (S3) ──
S3_MIN_BASKET_GAP = 0.03           # Min sum-below-1.00 to trade
S3_MAX_BASKET_SIZE = 8             # Max legs in a basket trade

# ── Monotonic (S4) ──
S4_MIN_INVERSION_GAP = 0.02       # Min price inversion to trade

# ── Risk ──
KILL_SWITCH_MAX_UNHEDGED_SEC = 30  # Max seconds with unhedged leg
KILL_SWITCH_MAX_DAILY_LOSS = 50.0  # Max daily loss before full shutdown ($)
MAX_TOTAL_POSITION_VALUE = 5000.0  # Max total capital deployed
MAX_SINGLE_TRADE_VALUE = 500.0     # Max capital per single arb trade

# ── Execution ──
RETRY_COOLDOWN_SEC = 5             # Seconds between retry attempts
RETRY_MAX_ATTEMPTS = 5             # Max retries before abandoning leg
ESCALATION_THRESHOLD = 2           # Sequential retries before alerting
```

**Why this matters:** When you discover that `S1_MIN_PROFIT_THRESHOLD = 0.02` catches too many false positives, you change one line — not hunt through 15 files.

### A2.2 — Tiered Risk Engine (MANDATORY)

The spec says "kill switch" (§5) but doesn't define graduated responses. From Alpha NextGen's tiered kill switch:

```
TIER 1 — WARNING (Soft Constraint)
  Trigger: Unhedged leg exposure > 10 seconds
  Action:  Log warning, continue scanning, prioritize filling opposing leg

TIER 2 — DEFENSIVE (Medium Constraint)
  Trigger: Unhedged leg > 30 seconds OR daily P&L loss > $25
  Action:  Stop new arb execution, only manage/close existing positions

TIER 3 — KILL (Hard Constraint)
  Trigger: Unhedged leg > 60 seconds OR daily P&L loss > $50 OR API disconnect > 30s
  Action:  Attempt to close all positions, alert immediately, halt all execution
```

### A2.3 — Multi-Leg Execution With Retry Escalation (CRITICAL)

This is the hardest engineering problem. Alpha NextGen's IC engine solved this for 4-leg iron condors. The pattern:

```
PHASE 1 — SIMULTANEOUS SUBMISSION
  Submit all legs as limit orders within 50ms
  Wait for fills (configurable timeout: 5 seconds)

  IF all legs fill → SUCCESS → record position
  IF partial fill → go to PHASE 2

PHASE 2 — RETRY WITH PRICE ADJUSTMENT
  Leg 1 filled, leg 2 didn't
  Re-submit leg 2 at slightly worse price (chase by $0.01)
  Retry up to RETRY_MAX_ATTEMPTS times

  IF leg 2 fills → SUCCESS (reduced profit but hedged)
  IF still unfilled → go to PHASE 3

PHASE 3 — UNWIND
  Leg 2 cannot be filled at profitable price
  Unwind leg 1 (buy opposing contract on ForecastEx, or sell on CME)
  Log the trade as FAILED with loss amount
  Alert if loss exceeds threshold
```

**Key insight from Alpha NextGen:** The OCO Manager tracks fill state per leg. Every order gets a unique `trace_id` linking it to the arb opportunity. When a fill comes in, the manager checks: "Is the opposing leg also filled? If not, start retry timer."

### A2.4 — Validation Gates Before Execution

Alpha NextGen's IC engine runs every condor candidate through a cascade of validation gates before committing capital. ForecastBot's Opportunity Router must do the same:

```python
def validate_opportunity(opp: ArbOpportunity) -> Optional[str]:
    """Return rejection reason or None if valid."""

    # Gate 1: Fee-adjusted EV
    net_profit = opp.gross_profit - opp.total_fees
    if net_profit < config.MIN_NET_PROFIT:
        return "BELOW_MIN_PROFIT"

    # Gate 2: Capital availability
    required = opp.total_cost
    if required > available_capital():
        return "INSUFFICIENT_CAPITAL"

    # Gate 3: Liquidity check (bid-ask spread on each leg)
    for leg in opp.legs:
        if leg.spread_pct > config.MAX_LEG_SPREAD_PCT:
            return "LEG_ILLIQUID"

    # Gate 4: Position limit
    if total_positions() >= config.MAX_CONCURRENT_POSITIONS:
        return "POSITION_LIMIT"

    # Gate 5: Duplicate check (not already in this trade)
    if is_duplicate_opportunity(opp):
        return "DUPLICATE"

    # Gate 6: Strategy-specific validation
    if opp.strategy == "S3_MUTEX":
        if not validate_basket_completeness(opp):
            return "INCOMPLETE_BASKET"

    return None  # All gates passed
```

**Record WHY opportunities are rejected** (Alpha NextGen calls these "drop codes"). This is invaluable for debugging: "Why isn't the bot trading?" becomes answerable by looking at drop code frequencies.

### A2.5 — Diagnostics / Drop Code Tracking

```python
# Track rejection reasons per strategy per hour
self._drop_codes: Dict[str, int] = defaultdict(int)

def _record_drop(self, code: str):
    self._drop_codes[code] += 1

# Periodically log summary
def log_diagnostics(self):
    for code, count in sorted(self._drop_codes.items()):
        log.info("DROP_SUMMARY", code=code, count=count)
    self._drop_codes.clear()
```

### A2.6 — State Persistence & Restart Recovery (MANDATORY)

Alpha NextGen's critical lesson: **the system WILL restart** (IB Gateway re-auth, VPS reboot, crash). Every position must survive a restart.

```
ON STARTUP:
  1. Connect to IBKR
  2. Load position state from PostgreSQL
  3. Query IBKR for actual positions (Portfolio.positions())
  4. RECONCILE: Compare DB state vs broker state
     - Position in DB but not at broker → mark as CLOSED (filled while offline)
     - Position at broker but not in DB → flag as ORPHAN (manual review)
     - Position in both → verify quantities match
  5. Resume normal scanning
```

**The reconciliation step is non-negotiable.** Without it, the bot will double-enter positions or lose track of fills that happened during downtime.

### A2.7 — Logging: Two-Tier Pattern

From Alpha NextGen's `trades_only` pattern. Scanning 200 contracts × 7 strategies generates massive log volume:

```python
# ALWAYS LOG (trade-level events)
log.info("FILL", strategy="S1", contract="FED_RATE_YES", price=0.45, qty=10)
log.info("OPPORTUNITY", strategy="S3", basket="FED_MARCH", gap=0.04, ev=3.20)
log.info("KILL_SWITCH", tier=2, reason="unhedged_exposure", duration_sec=35)

# DEBUG ONLY (scan-level events — disabled in production)
log.debug("SCAN_TICK", contract="BTC_90K_YES", bid=0.72, ask=0.74)
log.debug("SCAN_CHECK", strategy="S1", contract="FED_RATE", sum=0.998, gap=0.002)
```

Without this separation, logs will be >1GB/day and unusable.

---

## A3. ForecastEx-Specific Implementation Notes

### A3.1 — The No-Sell Constraint Changes Everything

The spec mentions this (§8) but underestimates its impact. In practice:

**Order Routing Must Translate "Close" to "Buy Opposing":**
```python
def close_position(position: Position):
    if position.exchange == "FORECASTX":
        # Cannot sell — buy the opposing contract
        if position.side == "YES":  # We hold YES (Call)
            # Buy NO (Put) to net out
            order = buy_contract(position.contract_id, side="NO", qty=position.qty)
        else:  # We hold NO (Put)
            order = buy_contract(position.contract_id, side="YES", qty=position.qty)
    else:  # CME — normal sell
        order = sell_contract(position.contract_id, qty=position.qty)
```

**P&L Calculation Is Different:**
- Entry: Buy YES at $0.45 (cost = $0.45/contract)
- Close: Buy NO at $0.50 (cost = $0.50/contract)
- Total cost = $0.95, guaranteed payout = $1.00, profit = $0.05
- This is NOT `sell_price - buy_price`. It's `$1.00 - (YES_cost + NO_cost)`.

**Position Sizing Must Account for Double Capital:**
- Holding a hedged position ties up capital on BOTH legs
- A $0.45 YES + $0.50 NO = $0.95 capital deployed for $0.05 profit
- Capital efficiency is much lower than traditional buy/sell markets

### A3.2 — Contract Registry: The Hardest Module

The spec lists this as a simple deliverable (§11). It's actually the most complex module in the system.

**What it must do:**
1. **Discovery**: Query IBKR via `reqContractDetails(secType="OPT", exchange="FORECASTX")` to find all active contracts
2. **Classification**: Parse contract descriptions to determine:
   - Which event does this belong to?
   - Is it YES or NO?
   - What's the strike/threshold?
   - What's the expiry?
3. **Relationship Mapping**: Build and maintain:
   - Parity pairs (YES ↔ NO for same question) — needed for S1, S2, S5
   - MUTEX sets (all outcomes for same event) — needed for S3
   - Monotonic chains (same event, different thresholds) — needed for S4
   - Calendar chains (same question, different expiries) — needed for S7
   - Containment pairs (subset ↔ superset events) — needed for S6
4. **Validation**: Verify completeness against ForecastEx rulebooks
5. **Lifecycle**: Detect new listings, expired contracts, and contract modifications

**Implementation approach:**
```python
@dataclass
class ContractGroup:
    """A group of related contracts for a single event."""
    event_id: str                    # "FED_RATE_MAR_2026"
    event_description: str           # "Federal Reserve Rate Decision - March 2026"
    contracts: Dict[str, Contract]   # strike/outcome → contract
    group_type: str                  # "PARITY" | "MUTEX" | "MONOTONIC" | "CALENDAR"
    is_complete: bool                # All expected outcomes present?
    expiry: datetime

class ContractRegistry:
    def __init__(self):
        self._parity_pairs: Dict[str, Tuple[Contract, Contract]] = {}
        self._mutex_sets: Dict[str, ContractGroup] = {}
        self._monotonic_chains: Dict[str, List[Contract]] = {}
        self._calendar_chains: Dict[str, List[Contract]] = {}
        self._containment_pairs: Dict[str, Tuple[Contract, Contract]] = {}

    async def refresh(self, ib: IB):
        """Full contract universe refresh — run every 15 minutes."""
        ...

    def get_mutex_set(self, event_id: str) -> Optional[ContractGroup]:
        """Return complete MUTEX set or None if incomplete."""
        group = self._mutex_sets.get(event_id)
        if group and group.is_complete:
            return group
        return None  # NEVER trade an incomplete basket
```

### A3.3 — Coupon Accrual Tracking (S2)

The 3.14% APY coupon accrues daily on open positions. The Position Manager must:
- Track accrued coupon per position per day
- Factor coupon income into net P&L calculations
- Prioritize unwinding carry positions by "ease of exit" when arb capital is needed (positions where opposing leg ask is cheapest = easiest to unwind)

---

## A4. Build Order With Gate Criteria

Each phase has a **GATE** — criteria that must pass before moving to the next phase.

### Phase 0 — Infrastructure (Week 1)
- [ ] Project scaffold: `config.py`, async main loop, structured logging
- [ ] IBKR connection via `ib_async` to paper trading account
- [ ] Contract discovery: query ForecastEx contracts, parse responses
- [ ] Market data streaming: subscribe to quotes, handle ticks
- [ ] PostgreSQL: position table, trade log table, contract table
- [ ] Basic risk engine shell (kill switch on API disconnect)

**GATE 0:** Can connect to IBKR, discover contracts, stream live quotes, write to DB.

### Phase 1 — S1 Parity + S2 Carry (Week 2-3)
- [ ] Contract Registry: build parity pairs (YES ↔ NO)
- [ ] S1 Scanner: detect YES + NO < $1.00 violations
- [ ] Execution Engine: dual-leg limit order submission with retry
- [ ] Partial fill handling: detect, retry, unwind
- [ ] Position Manager: track open positions, persist to DB
- [ ] S2 Carry: deploy idle capital into hedged carry positions
- [ ] Restart recovery: reconcile DB vs broker on startup

**GATE 1:** Bot detects parity violations on paper account, executes both legs, tracks positions across restarts. Run for 5 trading days with zero unhedged exposure.

### Phase 2 — S4 Monotonic + S7 Calendar (Week 4-5)
- [ ] Contract Registry: build monotonic chains and calendar chains
- [ ] S4 Scanner: detect adjacent monotonic inversions
- [ ] S7 Scanner: detect calendar spread inversions
- [ ] Multi-path profit verification (all resolution scenarios)
- [ ] Capital allocation: prioritize arb over carry, unwind carry when needed

**GATE 2:** Bot detects monotonic/calendar violations, verifies all resolution paths profitable, executes. Run for 5 trading days.

### Phase 3 — S3 MUTEX + S5 Synthetic + S6 Chain (Week 6-8)
- [ ] Contract Registry: build MUTEX sets with completeness validation
- [ ] S3 Scanner: detect basket sum < $1.00 on complete sets only
- [ ] S5 Scanner: detect synthetic vs actual price gaps
- [ ] S6 Scanner: detect containment pair inversions
- [ ] Multi-leg basket execution (up to 6 legs for S3)
- [ ] Full risk engine with tiered kill switch

**GATE 3:** All 7 strategies active on paper for 10 trading days. Zero orphaned positions. P&L positive after fees.

### Phase 4 — Production Hardening (Week 9-10)
- [ ] IB Gateway auto-restart handling
- [ ] Alerting (Telegram/email on kill switch, unhedged, API issues)
- [ ] Daily P&L reports
- [ ] Position limits and capital allocation optimization
- [ ] Live deployment with minimal capital ($500-$1000)

**GATE 4:** Live for 20 trading days with minimal capital. No manual intervention needed.

---

## A5. Testing Strategy

### Unit Tests (No IBKR Connection)
```python
# Test S1: Parity detection
def test_parity_violation_detected():
    yes_price, no_price = 0.45, 0.50  # Sum = 0.95 < 1.00
    assert detect_parity_violation(yes_price, no_price) == 0.05

def test_no_parity_violation():
    yes_price, no_price = 0.45, 0.55  # Sum = 1.00
    assert detect_parity_violation(yes_price, no_price) is None

# Test S3: MUTEX basket
def test_mutex_incomplete_basket_rejected():
    basket = [0.30, 0.25, 0.20]  # 3 of 4 outcomes — INCOMPLETE
    assert validate_mutex_basket(basket, expected_count=4) is False

def test_mutex_complete_basket_detected():
    basket = [0.30, 0.25, 0.20, 0.15]  # Sum = 0.90, gap = 0.10
    assert detect_mutex_violation(basket) == 0.10

# Test S4: Monotonic
def test_monotonic_inversion_detected():
    # BTC > 90K at $0.60, BTC > 95K at $0.65 — INVERSION (higher threshold costs more)
    assert detect_monotonic_violation(lower_strike_price=0.60, higher_strike_price=0.65) == 0.05

# Test execution: partial fill handling
def test_partial_fill_triggers_retry():
    state = ExecutionState(leg1_filled=True, leg2_filled=False, elapsed_sec=3)
    assert get_next_action(state) == "RETRY_LEG2"

def test_unwind_after_max_retries():
    state = ExecutionState(leg1_filled=True, leg2_filled=False, retry_count=5)
    assert get_next_action(state) == "UNWIND_LEG1"

# Test risk: kill switch tiers
def test_tier1_warning():
    assert evaluate_risk(unhedged_sec=15, daily_loss=10) == RiskTier.WARNING

def test_tier3_kill():
    assert evaluate_risk(unhedged_sec=65, daily_loss=10) == RiskTier.KILL
```

### Integration Tests (Paper Trading)
- Connect to IBKR paper account
- Submit and cancel test orders
- Verify fill callbacks work
- Verify position reconciliation on reconnect

---

## A6. File Structure

```
forecast-bot/
├── config.py                    # ALL tunable parameters
├── main.py                      # Async entry point, event loop
├── SPEC.md                      # Original spec (this file)
├── requirements.txt             # Pinned dependencies
├── .env.example                 # Environment variable template
│
├── core/
│   ├── connection.py            # ib_async connection management, reconnection
│   ├── market_data.py           # Quote streaming, tick handling
│   └── models.py                # Contract, Position, Opportunity dataclasses
│
├── registry/
│   ├── contract_registry.py     # Contract discovery, classification, relationships
│   ├── mutex_validator.py       # MUTEX set completeness validation
│   └── chain_builder.py         # Monotonic, calendar, containment chain construction
│
├── strategies/
│   ├── base.py                  # BaseStrategy interface
│   ├── s1_parity.py             # Parity arbitrage scanner
│   ├── s2_carry.py              # Coupon carry harvest
│   ├── s3_mutex.py              # MUTEX basket arbitrage
│   ├── s4_monotonic.py          # Monotonic probability violation
│   ├── s5_synthetic.py          # Synthetic replication arbitrage
│   ├── s6_conditional.py        # Conditional probability chain
│   └── s7_calendar.py           # Calendar spread arbitrage
│
├── execution/
│   ├── router.py                # Opportunity validation gates, EV ranking
│   ├── executor.py              # Multi-leg order submission, retry, unwind
│   └── fill_tracker.py          # Fill monitoring, partial fill detection
│
├── positions/
│   ├── position_manager.py      # Open position tracking, carry allocation
│   ├── reconciler.py            # DB vs broker reconciliation on restart
│   └── pnl.py                   # P&L calculation (ForecastEx vs CME)
│
├── risk/
│   ├── risk_engine.py           # Tiered kill switch, position limits
│   └── alerts.py                # Telegram/email alerting
│
├── persistence/
│   ├── database.py              # PostgreSQL connection, migrations
│   └── models.py                # SQLAlchemy/raw SQL table definitions
│
├── tests/
│   ├── test_strategies.py       # Unit tests for all 7 strategy scanners
│   ├── test_execution.py        # Partial fill, retry, unwind tests
│   ├── test_risk.py             # Kill switch tier tests
│   ├── test_registry.py         # Contract classification tests
│   ├── test_pnl.py              # ForecastEx P&L calculation tests
│   └── integration/
│       └── test_ibkr_paper.py   # Paper trading integration tests
│
└── scripts/
    ├── discover_contracts.py    # One-shot contract universe dump
    └── daily_report.py          # Generate daily P&L summary
```

---

## A7. Critical Implementation Rules

1. **NEVER trade an incomplete MUTEX basket (S3).** If any outcome contract is missing from the set, the basket is not guaranteed to pay $1.00. Completeness validation is a hard gate.

2. **NEVER leave a leg unhedged for more than `KILL_SWITCH_MAX_UNHEDGED_SEC` seconds.** If the opposing leg can't be filled, unwind the filled leg at a loss. A small known loss is always better than unknown directional exposure.

3. **NEVER use market orders on ForecastEx.** Thin books mean market orders can fill at extreme prices. Limit orders only, with price adjusted on retry.

4. **ALWAYS reconcile positions on startup.** The bot WILL restart (IB Gateway re-auth is mandatory). Unreconciled state leads to duplicate entries or lost positions.

5. **ALWAYS log the reason an opportunity was rejected.** Drop code tracking is essential for debugging "why isn't the bot trading?" issues.

6. **ForecastEx P&L = $1.00 - (YES_cost + NO_cost)**, not `sell_price - buy_price`. The no-sell constraint means P&L math is fundamentally different from traditional markets.

7. **Capital deployed per hedged position = YES_cost + NO_cost.** A $0.45 YES + $0.53 NO = $0.98 deployed for $0.02 profit. Capital efficiency is low — factor this into position sizing.

8. **Coupon accrual (3.14% APY) is per-position, not per-account.** Track accrued coupon per position for accurate P&L attribution.

---

*Addendum prepared based on 3 months of Alpha NextGen V2 trading system development (Iron Condor engine, VASS spread engine, OCO Manager, tiered kill switch, multi-leg execution with orphan recovery).*