# ForecastBot — System Specification v2.0

**Classification:** Internal Development Spec  
**Version:** 2.0 (Revised from live market data — March 4, 2026)  
**Audience:** AI Coding Agent  
**Budget:** $30,000 CAD (~$22,000 USD deployable)  
**Max Concurrent Positions:** 3  

---

## CRITICAL CONTEXT — READ FIRST

This spec was revised after direct observation of live ForecastEx market data. The original spec assumed persistent liquid gaps. **That assumption was wrong.** The actual market structure is:

- **ForecastEx is illiquid most of the time.** Live observation on March 4, 2026 showed zero trades on S&P Above 6,600 — last trade was 2 days prior.
- **Prices shown in the IBKR web UI are last-traded prices, not live bid/ask.** The list view NO column showing 15% was the bid price. The actual ask was $0.21. Never trust the list view.
- **The current sum on S&P Above 6,600 is $1.01 — overpriced, no arb right now.**
- **Gaps are real but episodic.** The 1-month chart confirmed the NO price hit $0.14 in late February (sum ~$0.89, gap $0.11). That gap persisted for hours. Nobody captured it.
- **Competition is near-zero.** Gaps that open persist for hours because no automated system is watching. This is the entire edge.
- **Gaps open during catalyst events** — when the underlying (BTC, ES, NQ) makes a significant move and one side of the ForecastEx book gets hit while the other lags in repricing.

**The bot's job:** Watch 24/7, do nothing most of the time, execute with full conviction the moment a catalyst creates a gap. Miss the window = miss the month.

---

## 1. Strategy — One Strategy Only (Phase 1)

### S1 — Parity Arbitrage

**The law:** `YES_ask + NO_ask < $1.00` → buy both → guaranteed profit regardless of outcome.

```
Every ForecastEx contract pays exactly $1.00 at settlement.
You hold YES + NO on the same contract.
Exactly one pays $1.00. The other pays $0.00.
Total received: always $1.00.
Total paid: YES_ask + NO_ask (< $1.00 when gap exists).
Profit: $1.00 - (YES_ask + NO_ask). Locked at entry. Zero outcome risk.
```

**Entry condition:** `YES_ask + NO_ask < ENTRY_THRESHOLD` (config: default 0.93)  
**Do not enter:** if sum is between 0.93 and 1.00 — gap is too small after slippage  
**Never enter:** if sum ≥ 1.00 — this is a loss, not an arb  

### S2 — Carry Harvest (Runs in Background Always)

ForecastEx pays **3.14% APY** on all open positions regardless of outcome.

A fully hedged position (YES + NO held simultaneously) with combined cost ≤ $1.00 earns the daily coupon with zero directional risk. Capital deployed in carry harvest earns ~0.0086% per day.

**Purpose:** Capital is never idle. When no S1 opportunity exists (most of the time), all available capital sits in carry harvest positions, accruing the coupon. The moment an S1 opportunity fires, the Position Manager unwinds the cheapest carry positions first to free capital.

**S2 entry condition:** `YES_ask + NO_ask ≤ 1.00` AND `days_to_expiry ≥ 7`  
**S2 is NOT a primary alpha source.** It is a capital utilisation floor. On $22,000 USD deployed: ~$1.89/day, ~$690/year. Not the goal — just better than cash.

---

## 2. Contract Universe — Three Categories Only

Phase 1 monitors exactly three contract categories. All others are excluded.

### Category 1 — BTC Highest Price (PRIMARY — 24/7)

**Why BTC is the primary target:**
- BTC trades 168 hours per week. No market close. No weekends off.
- BTC moves 3–8% in a single day regularly.
- Every BTC move reprices ForecastEx BTC contracts — but slowly.
- Saturday 3 AM, Sunday 2 PM, Wednesday overnight — the bot is watching when humans are not.
- Gaps confirmed: $0.03–$0.06 on annual contracts. When BTC makes a large move, near-threshold contracts gap significantly.

**Contracts to monitor:**
```
BTC Highest Price 2026 (Dec 31'26):
  Above $85,000    Above $90,000    Above $95,000
  Above $100,000   Above $120,000   Above $150,000

Priority: Near-threshold contracts only.
  If BTC is at $87,000 → monitor Above $85K, $90K, $95K
  Deep ITM (Above $70K at BTC $87K) → skip, no NO market
  Deep OTM (Above $150K at BTC $87K) → skip, no YES market

Dynamic ATM filter: always monitor contracts where YES is between 15% and 85%.
```

**Best gap windows for BTC:**
- Large BTC price moves (±3% in < 4 hours)
- Weekend sessions (thin human monitoring)
- US overnight hours (2 AM – 8 AM EST)
- Post-macro events (CPI, NFP) that spill into crypto

### Category 2 — S&P 500 Futures Price (US Market Hours)

**Contracts to monitor:**
```
S&P 500 FES Mar31'26 (primary) → Apr30'26 after Mar31 settles:
  Near-ATM only: contracts where YES is between 20% and 80%
  
  Current ES at 6,932:
    Above 6,350  Above 6,600  Above 6,850  Above 7,050  Above 7,250
    (Skip Above 5,100 — no NO market. Skip Above 7,650 — no YES market.)
```

**Best gap windows for S&P:**
- NFP release (first Friday of month, 8:30 AM EST)
- CPI release (~10th-12th of month, 8:30 AM EST)
- FOMC decision days (2:00 PM EST)
- ES makes ±1.5% move intraday
- Open (9:30–11:00 AM EST) — highest volatility, most repricing

### Category 3 — Nasdaq Futures Price (US Market Hours)

**Contracts to monitor:**
```
NQ FES Mar31'26 → Apr30'26:
  Near-ATM only: YES between 20% and 80%
  NQ and ES move together — gaps often open simultaneously
  Confirmed gap: $0.09 on near-ATM NQ contracts
  OI thinner than S&P — size conservatively (50% of S&P sizing)
```

### Excluded Permanently from Phase 1

```
✗ Fed Decision contracts    — MUTEX complexity, capital locked, low frequency
✗ BOJ Decision contracts    — Too thin, too infrequent
✗ CPI contracts             — Captured via S&P/NQ moves anyway
✗ NFP contracts             — Same reason
✗ Any long-dated (>45 days) — Capital locked too long
✗ Deep ITM/OTM contracts   — No two-sided market
```

---

## 3. The Liquidity Problem and How the Bot Solves It

This is the most important section. Get this wrong and the bot loses.

### 3.1 The Market Structure Reality

```
Normal state (most of the time):
  Sum on all contracts: $0.97 – $1.03
  Trades happening: 0–2 per day per contract
  Action required: NONE — deploy in carry harvest, wait

Catalyst state (3–8 times per month):
  An underlying makes a significant move
  One side of the ForecastEx book gets hit by directional bettors
  The other side has NOT repriced yet (this is the lag)
  Sum drops to $0.85 – $0.93
  Gap: $0.07 – $0.15
  Duration: hours (nobody else is capturing it)
  Action required: EXECUTE IMMEDIATELY WITH FULL CONVICTION
```

### 3.2 The Catalyst Calendar

The bot maintains a pre-loaded calendar of events that historically cause the underlying to move significantly. These are the windows where maximum scan vigilance is required.

```python
CATALYST_CALENDAR = [
    # Format: (date, time_est, event_name, affected_categories, severity)
    ("2026-03-06", "08:30", "NFP",           ["SP", "NQ"],       "HIGH"),
    ("2026-03-12", "08:30", "CPI",           ["SP", "NQ", "BTC"],"HIGH"),
    ("2026-03-18", "14:00", "FOMC",          ["SP", "NQ", "BTC"],"CRITICAL"),
    ("2026-03-31", "16:00", "SP_EXPIRY",     ["SP"],             "CRITICAL"),
    ("2026-04-03", "08:30", "NFP",           ["SP", "NQ"],       "HIGH"),
    # ... generated programmatically for rolling 90-day window
]
```

**Severity levels:**
- `CRITICAL`: Scan frequency → every 500ms. Alert on any gap > $0.03. All capital on standby.
- `HIGH`: Scan frequency → every 2 seconds. Alert on any gap > $0.05.
- `NORMAL`: Scan frequency → every 10 seconds. Alert on gap > $0.07.

### 3.3 Scan Frequency — The Two-Speed Engine

The scanner runs at two speeds. Switching between them is automatic.

```
NORMAL MODE (default):
  Scan interval: 10 seconds
  Contracts monitored: all near-ATM across 3 categories
  Alert threshold: sum < 0.93
  CPU cost: minimal — bot can run indefinitely

PRE-CATALYST MODE (T-30 minutes before any HIGH/CRITICAL event):
  Scan interval: 500ms
  Contracts monitored: all near-ATM + expand to wider ATM range
  Alert threshold: sum < 0.97 (wider net — catch the gap forming)
  Telegram alert: "CATALYST WINDOW OPEN: [event]. Watching [N] contracts."
  CPU cost: elevated — acceptable for short windows

POST-CATALYST MODE (T+0 to T+4 hours after event):
  Same as PRE-CATALYST
  Most gaps open in the 30–120 minutes AFTER the event
  Do NOT drop back to NORMAL mode immediately after event time
  Stay in high-frequency mode for 4 hours post-event

TRANSITION back to NORMAL:
  4 hours post-event AND no open positions requiring monitoring
  OR manual override via kill switch
```

### 3.4 Gap Formation Detection

The scanner does not just check current sum. It detects gap formation in progress — catching the window as it opens, not after it has already been obvious for an hour.

```python
class GapFormationDetector:
    """
    Detects when a gap is actively forming, not just when it exists.
    This catches the opportunity 5-15 minutes earlier.
    """
    
    def __init__(self):
        self._sum_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
    
    def update(self, contract_id: str, yes_ask: float, no_ask: float) -> GapSignal:
        current_sum = yes_ask + no_ask
        history = self._sum_history[contract_id]
        history.append((time.time(), current_sum))
        
        if len(history) < 5:
            return GapSignal.NONE
        
        # Signal 1: Sum already below entry threshold
        if current_sum < config.ENTRY_THRESHOLD:
            return GapSignal.EXECUTE
        
        # Signal 2: Sum trending DOWN fast (gap forming)
        recent_sums = [s for _, s in list(history)[-5:]]
        sum_velocity = recent_sums[0] - recent_sums[-1]  # positive = falling
        
        if sum_velocity > 0.03 and current_sum < 0.97:
            # Sum dropped 3 cents in last 5 readings AND approaching threshold
            return GapSignal.ALERT  # Not yet executable, but log and heighten watch
        
        return GapSignal.NONE
```

### 3.5 What Happens When a Gap Opens — Exact Sequence

```
T+0:00  BTC drops 4.2% in 20 minutes
T+0:05  BTC Above $90K YES: $0.72 → $0.61 (repricing)
        BTC Above $90K NO:  $0.24 → still $0.24 (lagging)
        Sum: $0.61 + $0.24 = $0.85
        GapSignal: EXECUTE

T+0:05  Bot detects sum = $0.85, gap = $0.15
        Validation gates run (see Section 6)
        All gates pass
        Telegram alert: "EXECUTE SIGNAL: BTC>$90K Sum=$0.85 Gap=$0.15"

T+0:05  Execution Engine: submit YES limit at $0.62, NO limit at $0.25
        Both legs within 200ms of each other

T+0:06  Both legs fill
        Position locked: cost = $0.87, payout = $1.00, profit = $0.13/contract
        Position Manager: record position, start carry coupon accrual
        Carry harvest positions on BTC contracts: unwind cheapest first to free capital

T+0:06 to settlement:
        Bot continues monitoring OTHER contracts for additional gaps
        (BTC move may gap S&P contracts too if macro correlated)
        Max 3 positions total — if 3 already open, queue but do not execute
```

---

## 4. Capital Allocation — $30K CAD

```
$30,000 CAD × 0.735 = $22,050 USD working capital

Reserve (never deployed):     $2,205  (10%)
Active arb capital:          $19,845  (90%)

Position sizing per arb trade:
  Max single position:        $8,000 USD
  Min position (below this, skip): $2,000 USD
  
  Sizing formula:
    position_size = min(
        available_capital × 0.45,    # Max 45% per trade
        config.MAX_SINGLE_TRADE_USD,  # Hard cap
        oi_based_limit                # Don't move the market
    )
  
  OI-based limit:
    max_contracts = OI × 0.01        # Never exceed 1% of open interest
    max_usd = max_contracts × avg_leg_price

Carry harvest baseline (when no arb):
  Deploy all $19,845 in carry harvest positions
  Earning: $19,845 × 3.14% / 365 = $1.71/day
  Monthly carry: ~$51 USD (not the goal — just better than idle)
  
  Carry positions must be instantly liquidatable:
    Prioritise contracts with < 14 days to expiry
    (closer to settlement = easier to unwind, less slippage)
    Keep carry positions in near-ATM contracts
    (most liquid, easiest to exit)

Capital state machine:
  CARRY_ONLY:     All capital in carry harvest. No arb opportunity.
  PARTIAL_ARB:    1-2 arb positions open. Remaining in carry.
  FULL_ARB:       3 positions open. Maximum deployment.
  UNWINDING:      Exiting carry positions to fund incoming arb.
                  Target: under 30 seconds to free $8,000 for arb.
```

---

## 5. Contract Universe Manager

The most complex module. Runs continuously, keeps the contract universe current.

```python
class ContractUniverseManager:
    """
    Discovers, classifies, and maintains all tradeable contracts.
    Runs a full refresh every 15 minutes.
    Runs an ATM recalculation every 60 seconds.
    """
    
    CATEGORIES = {
        "BTC": {
            "underlying": "BTC",
            "strikes": [85000, 90000, 95000, 100000, 120000, 150000],
            "expiry_range_days": (14, 365),  # BTC annual contracts OK
            "atm_filter": True,
        },
        "SP": {
            "underlying": "ES",
            "strikes": "dynamic",  # Pull from IBKR
            "expiry_range_days": (14, 45),   # Monthly only
            "atm_filter": True,
        },
        "NQ": {
            "underlying": "NQ",
            "strikes": "dynamic",
            "expiry_range_days": (14, 45),
            "atm_filter": True,
        }
    }
    
    ATM_WINDOW = (0.15, 0.85)  # Only monitor contracts where YES bid in this range
```

**Contract classification via IBKR API:**
```
ForecastEx contracts in TWS API:
  secType = "OPT"
  exchange = "FORECASTX"
  YES contracts = Call (right)
  NO contracts  = Put (right)
  
Discovery query:
  reqContractDetails(secType="OPT", exchange="FORECASTX", symbol="BTC")
  reqContractDetails(secType="OPT", exchange="FORECASTX", symbol="ES")
  reqContractDetails(secType="OPT", exchange="FORECASTX", symbol="NQ")
```

**ATM recalculation (every 60 seconds):**
```python
def recalculate_atm_contracts(self, underlying_prices: Dict[str, float]):
    """
    Drop contracts that have moved out of ATM window.
    Add contracts that have moved into ATM window.
    Update market data subscriptions accordingly.
    """
    for category, contracts in self._all_contracts.items():
        underlying = self._get_underlying_price(category)
        
        for contract in contracts:
            yes_bid = self._get_bid(contract.yes_leg)
            if yes_bid is None:
                continue
                
            in_atm_window = self.ATM_WINDOW[0] <= yes_bid <= self.ATM_WINDOW[1]
            
            if in_atm_window and contract not in self._active_contracts:
                self._subscribe(contract)
                self._active_contracts.add(contract)
                
            elif not in_atm_window and contract in self._active_contracts:
                self._unsubscribe(contract)
                self._active_contracts.discard(contract)
```

---

## 6. Execution Engine

### 6.1 Validation Gates (All Must Pass)

```python
def validate_opportunity(opp: ArbOpportunity) -> Optional[str]:
    
    # Gate 1: Both legs have live ask prices (not stale)
    if opp.yes_ask_age_seconds > 300:   return "STALE_YES_PRICE"
    if opp.no_ask_age_seconds > 300:    return "STALE_NO_PRICE"
    
    # Gate 2: Sum genuinely below threshold on ASK prices
    # (Never use bid prices or last traded for this calculation)
    if opp.yes_ask + opp.no_ask >= config.ENTRY_THRESHOLD:
        return "INSUFFICIENT_GAP"
    
    # Gate 3: Minimum net profit after slippage estimate
    estimated_slippage = 0.015  # $0.015 per leg conservative
    net_profit = (1.00 - opp.yes_ask - opp.no_ask) - (2 * estimated_slippage)
    if net_profit < config.MIN_NET_PROFIT:  return "BELOW_MIN_PROFIT"
    
    # Gate 4: OI depth — don't move the market
    max_contracts = min(opp.yes_oi, opp.no_oi) * 0.01
    if opp.target_contracts > max_contracts: return "OI_LIMIT"
    
    # Gate 5: Capital available
    if opp.required_capital > available_capital(): return "INSUFFICIENT_CAPITAL"
    
    # Gate 6: Position limit
    if open_position_count() >= config.MAX_CONCURRENT_POSITIONS: return "POSITION_LIMIT"
    
    # Gate 7: Not duplicate
    if is_duplicate(opp): return "DUPLICATE"
    
    # Gate 8: ATM check — both legs must be within ATM window
    if opp.yes_ask > 0.88 or opp.yes_ask < 0.12: return "NOT_ATM"
    
    return None  # All gates passed — execute
```

### 6.2 Dual-Leg Execution With Retry

```
PHASE 1 — SIMULTANEOUS SUBMISSION (T+0)
  Submit YES limit order at yes_ask (or yes_ask + $0.01 if urgent)
  Submit NO limit order at no_ask (or no_ask + $0.01 if urgent)
  Both within 200ms of each other
  Wait for fills: timeout = 10 seconds

  IF both fill → SUCCESS → record position, start carry accrual
  IF neither fills → CANCEL both → log DROP:NO_FILL → continue scanning
  IF leg 1 fills, leg 2 does not → PHASE 2

PHASE 2 — CHASE LEG 2 (T+10s)
  Leg 1 is filled — we have unhedged exposure
  Re-submit leg 2 at ask + $0.01 (chase by 1 cent)
  Retry up to 3 times, each time chasing $0.01 further
  Re-validate after each retry: is trade still profitable at new price?
  
  IF leg 2 fills at any retry → SUCCESS (reduced profit, still hedged)
  IF still unfilled after 3 retries → PHASE 3

PHASE 3 — UNWIND (T+40s)
  Gap has closed or leg 2 is truly illiquid
  Cannot complete the hedge
  Unwind leg 1: buy opposing contract to close
  (ForecastEx no-sell constraint: buy NO to close YES, buy YES to close NO)
  Accept small known loss on unwind
  Log as FAILED_UNWIND with loss amount
  Alert: "UNWIND EVENT: [contract] cost=$X"
  
  Kill switch: if 2 unwind events in same session → TIER 2 (stop new trades)
```

### 6.3 ForecastEx No-Sell Constraint

ForecastEx does not permit sell orders. This affects everything:

```python
def close_position(pos: Position) -> Order:
    """To close a YES position, buy NO. To close NO, buy YES."""
    if pos.leg_type == "YES":
        return buy_order(contract=pos.contract, side="NO", qty=pos.qty)
    else:
        return buy_order(contract=pos.contract, side="YES", qty=pos.qty)

def calculate_pnl(pos: Position) -> float:
    """P&L = $1.00 - total_cost. NOT sell_price - buy_price."""
    return 1.00 - (pos.yes_cost + pos.no_cost)
```

### 6.4 Carry Position Unwind Priority

When arb opportunity fires, carry harvest positions must be unwound fast:

```python
def prioritise_carry_unwind(carry_positions: List[Position], 
                             capital_needed: float) -> List[Position]:
    """
    Sort carry positions by ease of exit.
    Easiest to exit = contract where the closing leg ask is cheapest.
    
    To close a carry YES: need to buy NO. Cheapest NO ask = easiest exit.
    Sort ascending by closing_leg_ask. Unwind top of list first.
    """
    def exit_cost(pos):
        if pos.leg_type == "YES":
            return get_ask(pos.contract, side="NO")
        return get_ask(pos.contract, side="YES")
    
    sorted_positions = sorted(carry_positions, key=exit_cost)
    
    # Return minimum set that frees enough capital
    to_unwind = []
    freed = 0
    for pos in sorted_positions:
        to_unwind.append(pos)
        freed += pos.capital_deployed
        if freed >= capital_needed:
            break
    
    return to_unwind
```

---

## 7. Risk Engine — Tiered Kill Switch

```
TIER 1 — WARNING
  Trigger: Unhedged leg > 15 seconds
           OR any single position loss > $50
  Action:  Log warning. Prioritise filling opposing leg.
           No new arb trades until unhedged leg is resolved.

TIER 2 — DEFENSIVE
  Trigger: Unhedged leg > 40 seconds
           OR daily P&L loss > $200
           OR 2 unwind events in same session
  Action:  Stop all new arb execution.
           Manage and close existing positions only.
           Telegram: "TIER 2 DEFENSIVE — no new trades"

TIER 3 — KILL
  Trigger: Unhedged leg > 90 seconds
           OR daily P&L loss > $500
           OR IBKR API disconnect > 60 seconds
           OR manual kill command
  Action:  Attempt to close ALL positions immediately.
           Halt all execution.
           Telegram: "TIER 3 KILL SWITCH ENGAGED"
           Require manual restart.
```

---

## 8. Position Manager

```python
@dataclass
class Position:
    position_id: str          # UUID
    category: str             # "BTC" | "SP" | "NQ"
    contract_id: str          # IBKR conId
    contract_description: str # "BTC Above $90K Dec31'26"
    yes_cost: float           # Price paid for YES leg
    no_cost: float            # Price paid for NO leg
    total_cost: float         # yes_cost + no_cost
    locked_profit: float      # 1.00 - total_cost
    qty: int                  # Number of contract pairs
    capital_deployed: float   # total_cost × qty
    position_type: str        # "ARB" | "CARRY"
    entry_time: datetime
    expiry: datetime
    days_to_expiry: int
    fill_latency_ms: int      # Time between leg1 and leg2 fill
    catalyst_event: str       # What triggered the gap (if known)
    accrued_coupon: float     # 3.14% APY accrual to date
    status: str               # "OPEN" | "SETTLED" | "UNWOUND"

class PositionManager:
    
    MAX_POSITIONS = 3
    
    def total_arb_pnl_locked(self) -> float:
        """Sum of locked_profit × qty across all ARB positions."""
        return sum(p.locked_profit * p.qty 
                   for p in self._positions.values() 
                   if p.position_type == "ARB")
    
    def capital_available_for_arb(self) -> float:
        """Capital that can be immediately freed for arb."""
        carry_capital = sum(p.capital_deployed 
                           for p in self._positions.values()
                           if p.position_type == "CARRY")
        idle_capital = self._total_capital - self._deployed_capital
        return idle_capital + carry_capital  # Can unwind carry if needed
```

---

## 9. Alerting — Telegram Bot (Required)

The bot runs 24/7. You are not always watching. These alerts are mandatory:

```
IMMEDIATE ALERTS (fire within 5 seconds):
  ✓ GAP DETECTED: "[contract] sum=$X gap=$Y — executing"
  ✓ POSITION OPENED: "[contract] cost=$X locked_profit=$Y qty=Z"
  ✓ UNWIND EVENT: "[contract] unhedged leg unwound, loss=$X"
  ✓ TIER 2 or TIER 3 kill switch engaged
  ✓ IBKR API disconnect

DAILY SUMMARY (sent at 6 AM EST):
  ✓ Open positions with accrued P&L
  ✓ Locked arb profit to date
  ✓ Carry harvest accrued
  ✓ Opportunities detected vs executed (capture rate)
  ✓ Next catalyst events in 48 hours

CATALYST WINDOW ALERTS:
  ✓ "CATALYST WINDOW OPEN: NFP in 30 minutes. Watching 12 contracts."
  ✓ "POST-CATALYST: FOMC released. Heightened monitoring for 4 hours."
```

---

## 10. Tech Stack

```
Language:        Python 3.11+
IBKR Library:    ib_async (NOT ib_insync — unmaintained since 2024)
                 pip install ib_async
                 Docs: https://ib-api-reloaded.github.io/ib_async/

Database:        PostgreSQL (positions, trade log, opportunity log)
Architecture:    Single-process asyncio (NOT Celery+Redis — overkill for V1)
Deployment:      Linux VPS Ubuntu 22.04, AWS EC2 us-east-1
                 IB Gateway headless daemon
Alerting:        Telegram Bot API (python-telegram-bot)
Logging:         structlog (JSON structured logs)
Scheduling:      APScheduler (catalyst calendar, daily reports, registry refresh)
Config:          Single config.py — all tunable parameters, nothing hardcoded
```

**ForecastEx contracts in TWS API:**
```
secType = "OPT"
exchange = "FORECASTX"  
YES = Call, NO = Put
Discovery: reqContractDetails(secType="OPT", exchange="FORECASTX")
```

---

## 11. Config File — All Tunable Parameters

```python
# config.py — change here, nowhere else

# ── Strategy S1 ──
ENTRY_THRESHOLD = 0.93          # Max sum to enter arb (YES_ask + NO_ask)
MIN_NET_PROFIT = 0.02           # Min profit after slippage estimate
SLIPPAGE_ESTIMATE_PER_LEG = 0.01

# ── Strategy S2 Carry ──
S2_MAX_ENTRY_SUM = 1.00         # Only enter carry if sum ≤ $1.00
S2_MIN_DTE = 7                  # Min days to expiry for carry position

# ── Capital ──
TOTAL_CAPITAL_USD = 19845       # 90% of $22,050
RESERVE_USD = 2205              # 10% never deployed
MAX_SINGLE_TRADE_USD = 8000     # Max capital per arb trade
MIN_SINGLE_TRADE_USD = 2000     # Skip if position would be below this
MAX_CONCURRENT_POSITIONS = 3    # Hard limit
MAX_OI_PCT = 0.01               # Never exceed 1% of contract OI

# ── ATM Filter ──
ATM_YES_BID_MIN = 0.15          # Minimum YES bid to be "in range"
ATM_YES_BID_MAX = 0.85          # Maximum YES bid to be "in range"

# ── Scan Frequency ──
SCAN_INTERVAL_NORMAL_SEC = 10
SCAN_INTERVAL_CATALYST_MS = 500
CATALYST_WINDOW_PRE_MINUTES = 30
CATALYST_WINDOW_POST_HOURS = 4

# ── Execution ──
LEG_FILL_TIMEOUT_SEC = 10
LEG_CHASE_INCREMENT = 0.01      # Chase by $0.01 per retry
LEG_CHASE_MAX_RETRIES = 3
BOTH_LEGS_MAX_LATENCY_MS = 200

# ── Risk ──
KILL_T1_UNHEDGED_SEC = 15
KILL_T2_UNHEDGED_SEC = 40
KILL_T3_UNHEDGED_SEC = 90
KILL_T2_DAILY_LOSS_USD = 200
KILL_T3_DAILY_LOSS_USD = 500
KILL_T2_UNWIND_EVENTS_PER_SESSION = 2

# ── Price Staleness ──
MAX_PRICE_AGE_SECONDS = 300     # Reject prices older than 5 minutes

# ── Alerting ──
TELEGRAM_BOT_TOKEN = ""         # Set via env var
TELEGRAM_CHAT_ID = ""           # Set via env var
```

---

## 12. File Structure

```
forecastbot/
├── config.py                     # ALL tunable parameters
├── main.py                       # Async entry point
├── requirements.txt
├── .env.example
│
├── core/
│   ├── connection.py             # ib_async connection, auto-reconnect
│   ├── market_data.py            # Quote streaming, tick handling, staleness check
│   └── models.py                 # Contract, Position, ArbOpportunity dataclasses
│
├── universe/
│   ├── contract_universe.py      # Discovery, ATM filter, subscription management
│   ├── catalyst_calendar.py      # Event calendar, scan mode switching
│   └── gap_detector.py           # GapFormationDetector, sum velocity tracking
│
├── strategies/
│   ├── s1_parity.py              # Parity arb scanner, uses ASK prices only
│   └── s2_carry.py               # Carry harvest, unwind priority queue
│
├── execution/
│   ├── validator.py              # All 8 validation gates
│   ├── executor.py               # Dual-leg submission, Phase 1/2/3 retry
│   └── fill_tracker.py           # Fill callbacks, unhedged detection
│
├── positions/
│   ├── position_manager.py       # Open positions, capital state machine
│   ├── reconciler.py             # DB vs IBKR reconciliation on startup
│   └── pnl.py                    # ForecastEx P&L: $1.00 - (yes_cost + no_cost)
│
├── risk/
│   ├── risk_engine.py            # Tiered kill switch
│   └── alerts.py                 # Telegram alerts
│
├── persistence/
│   ├── database.py               # PostgreSQL
│   └── schema.sql                # Table definitions
│
└── tests/
    ├── test_s1_parity.py
    ├── test_execution.py
    ├── test_risk.py
    ├── test_carry_unwind.py
    └── test_gap_detector.py
```

---

## 13. Build Order — Phase Gates

### Gate 0 — Infrastructure (Week 1)
- [ ] `ib_async` connects to IBKR paper account
- [ ] Contract discovery: find all ForecastEx BTC/SP/NQ contracts
- [ ] Market data streaming: receive bid/ask ticks
- [ ] PostgreSQL schema created, writes working
- [ ] Basic Telegram alert fires on startup

**GATE 0 PASS:** Live bid/ask streaming on ≥ 5 contracts. Data writing to DB.

### Gate 1 — Catalyst Monitor (Week 1-2)
- [ ] Catalyst calendar loaded with rolling 90-day events
- [ ] Scan frequency auto-switches NORMAL → CATALYST mode
- [ ] GapFormationDetector: sum velocity tracking per contract
- [ ] Telegram alert fires when sum < 0.97 on any contract
- [ ] ATM filter active: only near-ATM contracts subscribed

**GATE 1 PASS:** Bot fires Telegram alert within 30 seconds of a simulated gap event.

### Gate 2 — S1 Parity + Carry (Week 2-3)
- [ ] S1 scanner: uses ASK prices only (never bid, never last traded)
- [ ] All 8 validation gates implemented and logged with drop codes
- [ ] Dual-leg execution with Phase 1/2/3 retry
- [ ] Carry harvest deployment and unwind priority queue
- [ ] Position manager with capital state machine
- [ ] Startup reconciliation

**GATE 2 PASS:** Paper trading for 5 days. Zero unhedged legs persisting > 30s. Carry deployed on idle capital.

### Gate 3 — Risk + Hardening (Week 3-4)
- [ ] Tiered kill switch all three tiers
- [ ] Price staleness rejection
- [ ] IB Gateway auto-reconnect with state recovery
- [ ] Daily P&L summary Telegram message
- [ ] Full test suite passing

**GATE 3 PASS:** Simulate TIER 3 kill. Bot halts, alerts, requires manual restart. Paper trade 10 days clean.

### Gate 4 — Live Deployment (Week 4-5)
- [ ] Deploy on VPS with IB Gateway daemon
- [ ] Live trading with $2,000 CAD probe capital
- [ ] Monitor for 2 weeks: capture rate, fill quality, slippage
- [ ] Scale to full $30K CAD if probe clean

**GATE 4 PASS:** Probe trades fill within $0.01 of detected gap. Both-leg fill rate > 80%.

---

## 14. Critical Rules — Never Violate

1. **Always use ASK prices for sum calculation.** Never bid. Never last traded. If no live ask exists → skip contract.
2. **Never enter if sum ≥ ENTRY_THRESHOLD.** The web UI showing a gap does not mean a gap exists. Pull the live ask via API.
3. **Both legs within 200ms.** Longer gap = price moves = no longer guaranteed.
4. **Never exceed 1% of contract OI per order.** On thin books, you will move the market against yourself.
5. **Always reconcile positions on startup.** The bot WILL restart. Unreconciled state = duplicate entries or lost positions.
6. **Never leave carry positions that cannot be unwound in < 30 seconds.** Carry is the floor, arb is the ceiling — capital must be able to pivot instantly.
7. **ForecastEx P&L = $1.00 - (yes_cost + no_cost).** Not sell_price - buy_price. No sell orders exist.
8. **Catalyst windows are sacred.** The bot must be in CATALYST mode before any HIGH/CRITICAL event. If the catalyst calendar fails to load → TIER 2 defensive mode until manually cleared.

---

## 15. References

- IBKR Event Contracts API: https://www.interactivebrokers.com/campus/ibkr-api-page/event-contracts/
- ib_async (maintained fork): https://github.com/ib-api-reloaded/ib_async
- ForecastEx Markets: https://forecasttrader.interactivebrokers.com/eventtrader/#/markets
- IBKR TWS Event Trading Guide: https://www.interactivebrokers.com/campus/ibkr-api-page/event-trading/

---

*ForecastBot SPEC v2.0 — Revised March 4, 2026 based on live market data observation.*  
*Key revision: Liquidity-aware architecture. Catalyst-triggered entry. ASK-price-only gap calculation.*
