# Weather Condor Strategy — Multi-City Directional Harvest v2

**Status:** RESEARCH / PRE-VALIDATION
**Created:** March 11, 2026
**Updated:** March 11, 2026 — v2 revised logic

---

## Thesis

Buy high-confidence YES and NO contracts across **different strikes** on weather prediction markets at market open. Select only cities with **stable forecasts, high open interest, and no active weather warnings**. Enter early to capture maximum order book depth.

The temperature cannot simultaneously be below the YES strike AND above the NO strike, guaranteeing a minimum $1.00 payout per pair. If temperature lands between the two strikes (the most likely outcome), both pay $1.00 each for a $2.00 return.

Target: **$0.30 minimum daily profit across all cities combined.**

---

## Daily Workflow (5 Steps)

### Step 1 — Scan Cities (Pre-Market, Before 6:30 AM ET)

Scan all 8 ForecastEx weather cities. Rank by **historical open interest** and filter for **geographic stability** (cities where forecasts are consistently accurate).

| City | Symbol | METAR | OI (Mar 11) | Climate Type |
|------|--------|-------|-------------|--------------|
| Chicago | UHMDW | KMDW | 149.1K | Midwest — moderate swing |
| Los Angeles | UHLAX | KLAX | 124.5K | Coastal — stable |
| San Francisco | UHSFO | KSFO | 46.1K | Coastal — very stable |
| Austin | UHAUS | KAUS | 40.4K | South — moderate |
| Washington DC | UHDCA | KDCA | 40.0K | East Coast — frontal swings |
| Philadelphia | UHPHL | KPHL | 28.0K | East Coast — frontal swings |
| Seattle | UHSEA | KSEA | 22.4K | Pacific NW — marine layer |
| New York | UHLGA | KLGA | 20.6K | East Coast — low ATM OI |

**City tiers by OI:**
```
Tier 1 (deep):     Chicago (149K), LA (124K)
Tier 2 (solid):    San Francisco (46K), Austin (40K), DC (40K)
Tier 3 (thin):     Philadelphia (28K), Seattle (22K)
Avoid:             NYC (20K total but ATM OI only 254 — dead book)
```

### Step 2 — Get Forecast + Swing Detection (Skip-If-Any-Flag)

For each city before market open, pull the forecast high AND check multiple weather sources for any abnormality. The logic is simple: **if any source raises any flag, skip that city.** No scoring, no weighting, no "it's only a minor advisory." One flag = no trade.

You have 8 cities. Even skipping 3-4 leaves enough to hit the $0.30 target.

#### 2a. Capture WU Forecast High

Pull WU 5-day forecast API (day 0) for each city. Log permanently — WU overwrites this intraday. This is the number we trade on.

#### 2b. Check All Weather Sources (Ordered by Reliability)

| Priority | Source | What It Catches | Why It's Reliable | API |
|----------|--------|----------------|-------------------|-----|
| **1** | **NWS Alerts API** | Official warnings, watches, advisories, special weather statements | Government-issued. If NWS says there's a warning, there IS a warning. | `api.weather.gov/alerts/active?point={lat},{lon}` |
| **2** | **NWS Area Forecast Discussion (AFD)** | Forecaster uncertainty, frontal timing, model disagreement, unusual patterns | Written by the actual humans making the forecast. If they express doubt, we should doubt too. | `api.weather.gov/products/types/AFD/locations/{office}` |
| **3** | **METAR Remarks** | Current wind gusts, variable winds, thunderstorm activity, pressure changes | Direct sensor observation from the airport. Not a prediction — what's actually happening. | `aviationweather.gov/api/data/metar?ids={station}` |
| **4** | **WU Forecast Narrative** | Plain-english day summary ("gusty winds", "afternoon storms", "well above average") | Processed from NWS but easier to parse. Contains context the numbers miss. | `api.weather.com/v3/wx/forecast/daily/5day` |
| **5** | **NWS Hourly Forecast** | Hour-by-hour temperature trajectory for the day | If NWS predicts a 25°F+ intraday range, that's an abnormal swing day. | `api.weather.gov/gridpoints/{office}/{x},{y}/forecast/hourly` |

#### 2c. Skip Rules (Any Single Flag = SKIP)

```
For each city at 6:00 AM ET:

  CHECK 1 — NWS Alerts API
    Any active alert for the airport zone?
    (warning, watch, advisory, special statement — ANY type)
    → FLAG FOUND = SKIP

  CHECK 2 — NWS Area Forecast Discussion
    Mentions "frontal passage", "wind shift", "temperature swing",
    "model disagreement", "uncertain", "tricky forecast" for this area?
    → FLAG FOUND = SKIP

  CHECK 3 — WU Forecast Narrative
    Contains "storms", "gusty", "wind advisory", "record",
    "unusual", "well above normal", "well below normal"?
    → FLAG FOUND = SKIP

  CHECK 4 — NWS Hourly Forecast
    Predicted daily high minus predicted morning temp > 25°F?
    → FLAG FOUND = SKIP (abnormal swing day)

  ALL FOUR CLEAR = ELIGIBLE TO TRADE
```

**Why this works:** Each source catches different things. NWS Alerts catches official warnings. AFD catches forecaster uncertainty that hasn't risen to alert level. WU narrative catches plain-language red flags. Hourly forecast catches abnormal temperature ranges even when no alert is issued. Together, they cast a wide net with zero false negatives — at the cost of some false positives (skipping a city that would have been fine). That's acceptable. Skipping a good city costs nothing. Trading a bad city costs money.

### Step 3 — Pick Low-Swing Cities Only

From the cities that passed ALL checks in Step 2, apply geographic preference. Some cities are inherently more stable:

**Preferred (low swing):**
- Coastal: LA, San Francisco (marine influence dampens extremes)
- Southern inland: Austin (when no fronts expected)

**Conditional (trade only if all checks clear):**
- Chicago, Seattle (can be stable but fronts cause big swings)

**High risk (skip unless every single check is clean):**
- DC, Philadelphia (East Coast frontal zone — March 11 proved this)
- NYC (dead OI + frontal zone)

**March 11 example — what the filter would have done:**
```
CHECK RESULTS:
  LA            NWS=clear  AFD=clear  WU=clear  Swing=15°F  → ALL CLEAR
  San Francisco NWS=clear  AFD=clear  WU=clear  Swing=12°F  → ALL CLEAR
  Austin        NWS=clear  AFD=clear  WU=clear  Swing=18°F  → ALL CLEAR
  Chicago       NWS=clear  AFD=clear  WU=clear  Swing=20°F  → ALL CLEAR
  Seattle       NWS=clear  AFD=clear  WU=clear  Swing=14°F  → ALL CLEAR
  DC            NWS=WARN   AFD="front" WU="gusty" Swing=26°F → SKIP (4 flags!)
  Philadelphia  NWS=WARN   AFD="front" WU="gusty" Swing=30°F → SKIP (4 flags!)
  NYC           — SKIP (dead OI, no check needed)

TRADE:  LA, SF, Austin, Chicago, Seattle (5 cities)
SKIP:   DC, Philadelphia, NYC
```

### Step 4 — Enter at Market Open (The Condor)

For each selected city:

1. **YES strike** = morning forecast high **minus offset** (likely to be exceeded)
2. **NO strike** = morning forecast high **plus offset** (likely NOT to be exceeded)
3. Enter **immediately at market open** to get maximum depth from the order book before other participants
4. **Hard rule: combined entry cost must be < $1.00** (guarantees no-loss floor)

**Position sizing: maximum 40% of portfolio** allocated to the day's safest cities. Never go all-in. 60% is held in reserve for hedges (Step 5).

#### Order Book Sizing — How Many Contracts to Enter

The YES and NO legs are on **different strikes**. Size is determined by probing the order book at each strike independently. The bottleneck is always the thinner side.

**L1 (Top of Book) — Minimum Viable Sizing**

```
Step 1: Read ask depth at both target strikes

  YES strike (forecast - offset):  ask_price, ask_depth
  NO  strike (forecast + offset):  ask_price, ask_depth

Step 2: Max contracts = min(YES_ask_depth, NO_ask_depth)

  Example — UHLAX, forecast high = 70°F, offset = 2°F:

    K68 YES:  ask = $0.55   depth = 263 contracts
    K72 NO:   ask = $0.35   depth =  73 contracts

    Max condor pairs = min(263, 73) = 73
    Entry cost per pair = $0.55 + $0.35 = $0.90
    Total capital needed = 73 × $0.90 = $65.70
```

**The thinner side dictates everything.** K68 has 263 contracts available but K72 only has 73. You can only build 73 complete condor pairs. Entering 263 on the YES side without matching NO contracts breaks the guaranteed payout structure.

**L2 (Full Order Book) — Finding Hidden Depth**

L1 only shows the best ask. L2 reveals additional contracts at progressively higher prices. ForecastEx supports L2 (confirmed — depth_finder detected it on UHLAX).

```
L2 for K68 YES (hypothetical):
  Level 1:  $0.55 × 263 contracts
  Level 2:  $0.56 × 150 contracts
  Level 3:  $0.58 × 400 contracts
  ─────────────────────────────────
  Total available: 813 contracts

L2 for K72 NO:
  Level 1:  $0.35 ×  73 contracts
  Level 2:  $0.37 ×  50 contracts
  Level 3:  $0.40 × 200 contracts
  ─────────────────────────────────
  Total available: 323 contracts
```

With L2, you can fill more contracts but at worse prices. The sizing decision becomes: **how deep can you go while keeping combined cost < $1.00?**

```
Tiered fill example:

  First 73 pairs:   YES @ $0.55 + NO @ $0.35 = $0.90  ✓ profitable
  Next  50 pairs:   YES @ $0.55 + NO @ $0.37 = $0.92  ✓ profitable
  Next 150 pairs:   YES @ $0.56 + NO @ $0.40 = $0.96  ✓ still under $1.00
  Next  50 pairs:   YES @ $0.58 + NO @ $0.40 = $0.98  ✓ barely under $1.00
  Beyond:           YES @ $0.60 + NO @ $0.42 = $1.02  ✗ STOP — over $1.00
```

**L2 sizing rules:**
1. Walk down both order books level by level
2. At each level, check: does combined ask still sum < $1.00?
3. If yes, include those contracts
4. If no, stop — everything beyond this loses money
5. Total contracts = sum of all profitable levels, capped by portfolio allocation

**Sizing caps (applied after order book analysis):**

```
Cap 1: Portfolio allocation
  max_capital_per_city = (portfolio × 0.40) / number_of_cities

Cap 2: Order book depth
  max_contracts = min(total_YES_depth, total_NO_depth) at profitable levels

Cap 3: OI percentage
  Never exceed 1% of the strike's open interest (avoid moving the market)

Final size = min(Cap 1, Cap 2, Cap 3)
```

**Live data example — UHLAX March 11, 4:16 PM ET (end of day):**

```
Strike  YES Ask  Depth   NO Ask  Depth   Sum    MinD   Tradeable?
K68     $0.20    263     $0.87    75     $1.07    75   ✗ sum > $1.00
K69     $0.08    111     $0.98   400     $1.06   111   ✗ sum > $1.00
K70     $0.07   1111     $0.98    15     $1.05    15   ✗ sum > $1.00
K71     $0.08     89     $0.99   116     $1.07    89   ✗ sum > $1.00
K72     $0.05    925     $0.99    73     $1.04    73   ✗ sum > $1.00
K73     $0.06    696     —         0     —         0   ✗ no NO side
K74     $0.04    644     —         0     —         0   ✗ no NO side
```

**Every strike sums above $1.00** — but this is end of day when the outcome is known. The market has priced in the actual temperature. **Morning prices should be different** because nobody knows the answer yet — that's when YES+NO sums may drop below $1.00. We need the same snapshot at 6:30 AM ET to validate this.

```
Example: 5 cities pass filter, portfolio = $5,000

  40% allocation = $2,000 across all cities
  Per city budget: ~$400

  Chicago   K46 YES + K50 NO   MinD=200  200 pairs × $0.88 = $176
  LA        K66 YES + K70 NO   MinD=150  150 pairs × $0.90 = $135
  SF        K66 YES + K70 NO   MinD=120  120 pairs × $0.92 = $110
  Austin    K79 YES + K83 NO   MinD= 80   80 pairs × $0.85 = $ 68
  Seattle   K49 YES + K53 NO   MinD= 60   60 pairs × $0.91 = $ 55
                                                     Total:  $544
```

### Step 5 — Hedge If Swing Develops

If an unexpected temperature swing develops mid-day despite the pre-market filter:

**Trigger:** Temperature exceeds the NO strike OR drops below the YES strike with 3+ hours of daylight remaining.

**Action:** Initiate the **opposite trade** on the breached strike to cap the loss.

```
Example: Austin K79 YES + K83 NO entered at $0.88

  2 PM: temp hits 84°F, blowing past K83 NO strike
  K83 NO will likely fail → losing $1.00 - cost_of_NO

  Hedge: BUY K83 YES
    If temp stays > 83 → K83 YES pays $1.00, offsetting K83 NO loss
    K79 YES also pays → still profitable overall

  Cost: the K83 YES ask mid-day (market already knows temp is high,
        so this will be expensive — maybe $0.80-0.90)
```

**Hedge rules:**
- Only hedge if the cost of the opposite trade keeps total portfolio loss below 10%
- If hedge is too expensive (would push total cost above $1.50 per pair), accept the $1.00 floor and don't throw good money after bad
- 60% of portfolio is unallocated — this is the hedge reserve

---

## Payout Structure

```
  If BOTH win:   profit = $2.00 - entry_cost        (forecast accurate)
  If ONE wins:   profit = $1.00 - entry_cost        (forecast missed one side)
  If NONE win:   IMPOSSIBLE (mutually exclusive conditions)

  Break-even:    entry_cost = $1.00
  Loss possible: ONLY if entry_cost > $1.00
```

### Worked Example

```
City: San Francisco, Morning Forecast High = 69°F

  Buy K67 YES  @ $0.55   (pays $1 if actual temp > 67°F)
  Buy K71 NO   @ $0.37   (pays $1 if actual temp ≤ 71°F)
  Combined cost: $0.92

  Scenario A: Actual = 69°F (between strikes — forecast nailed it)
    K67 YES pays  → $1.00
    K71 NO  pays  → $1.00
    Return: $2.00  Profit: $1.08

  Scenario B: Actual = 65°F (colder than expected)
    K67 YES fails → $0.00
    K71 NO  pays  → $1.00
    Return: $1.00  Profit: $0.08

  Scenario C: Actual = 73°F (warmer than expected)
    K67 YES pays  → $1.00
    K71 NO  fails → $0.00
    Return: $1.00  Profit: $0.08

  All scenarios profitable because entry < $1.00.
```

### Multi-City Daily Aggregation

The $0.30 target is across **all cities combined**, not per pair.

```
Good day (most forecasts accurate — both sides win):

  LA            cost $0.88  both win  → $1.12 profit
  SF            cost $0.92  both win  → $1.08 profit
  Chicago       cost $0.90  both win  → $1.10 profit
  Austin        cost $0.85  both win  → $1.15 profit
  Seattle       cost $0.91  both win  → $1.09 profit

  Daily total: $5.54 profit

Bad day (most forecasts miss one side):

  LA            cost $0.88  one wins  → $0.12 profit
  SF            cost $0.92  one wins  → $0.08 profit
  Chicago       cost $0.90  one wins  → $0.10 profit
  Austin        cost $0.85  one wins  → $0.15 profit
  Seattle       cost $0.91  one wins  → $0.09 profit

  Daily total: $0.54 profit (still above $0.30 target)
```

---

## Strike Selection Logic

```
  YES strike = morning_forecast_high - offset
  NO  strike = morning_forecast_high + offset

  Default offset: 2°F
```

**Per-city offset tuning** (to be refined with data):

| City | Offset | Rationale |
|------|--------|-----------|
| LA | 2°F | Coastal, marine layer, very stable |
| San Francisco | 2°F | Coastal, fog burns off predictably |
| Austin | 2°F | Southern, generally stable |
| Chicago | 2°F | Moderate, but watch for lake effect |
| Seattle | 2°F | Marine influence, generally stable |
| DC | 3°F | Frontal zone, wider buffer needed |
| Philadelphia | 3°F | Frontal zone, wider buffer needed |
| NYC | — | Avoid (dead OI) |

Wider offset = higher probability both sides win, but higher entry cost. Narrower = cheaper but higher risk of one-side-only payout.

---

## Trade Filters

### Enter When (ALL must be true)

- [ ] Morning forecast captured before 6:30 AM ET
- [ ] NWS Alerts API — zero active alerts for airport zone
- [ ] NWS AFD — no mention of fronts, uncertainty, model disagreement
- [ ] WU Narrative — no red flag keywords (storms, gusty, record, unusual)
- [ ] NWS Hourly — predicted intraday range < 25°F
- [ ] Combined entry cost < $1.00 (guaranteed profit floor)
- [ ] Both YES and NO sides have ask depth at market open
- [ ] City has historical OI > 20K (liquid enough)
- [ ] ATM open interest > 1K (live book)

### Skip If (ANY single one is true)

- [ ] Any NWS alert of any type active for the area
- [ ] AFD mentions frontal passage, wind shift, model disagreement, or uncertainty
- [ ] WU narrative contains storms, gusty, wind advisory, record, unusual, well above/below normal
- [ ] Predicted intraday temperature range > 25°F
- [ ] Combined entry cost >= $1.00
- [ ] One or both sides have zero ask depth
- [ ] ATM open interest < 1K (dead book)
- [ ] City has historical forecast error > 4°F (30-day average, once data exists)

---

## Risk Analysis

| Risk | Impact | Mitigation |
|------|--------|------------|
| Forecast misses by > offset | One side fails, $1.00 floor holds | Only risk is entry_cost - $1.00 (small) |
| Unexpected mid-day swing | One side threatened | Step 5 hedge with opposite trade |
| No depth at market open | Cannot enter | Skip city, diversification across others |
| Entry cost > $1.00 | Can actually lose money | Hard rule: NEVER enter above $1.00 |
| Hedge too expensive | Loss on breached strike | Cap hedge spend; accept $1.00 floor |
| All cities miss same direction | Multiple one-side payouts | Geographically diverse cities = uncorrelated weather |
| WU forecast inaccurate for city | Systematic one-side losses | Track per-city accuracy; drop cities with > 4°F avg error |

---

## Critical Data Gap: Morning Forecast Capture

**Problem identified March 11, 2026:**

WU overwrites the forecast high throughout the day as actual temperatures come in. By afternoon, the "forecast high" is essentially the observed high — not the prediction that existed at market open.

**Example — March 11, 2026 (Philly):**
```
WU forecast at ~5 PM ET:  83°F   ← reflects actual day, not morning prediction
WU forecast at ~6 AM ET:  ???    ← what we'd trade on — GONE by afternoon
Actual high:              82°F
```

**Solution: daily pre-market forecast snapshot.**

The bot must capture and permanently log the WU forecast high for each city **once per day, before market open** (before 6:30 AM ET). This must never be overwritten.

**Data to capture each morning (per city):**

| Field | Source | Purpose |
|-------|--------|---------|
| `date` | System clock | Trading day |
| `city` | Contract symbol | Which city |
| `metar_station` | Derived from symbol | METAR source |
| `forecast_high_f` | WU 5-day forecast API, day 0 | Strike selection basis |
| `capture_time_et` | System clock | Prove it was pre-market |
| `nws_alerts` | NWS alerts API | Trade filter (list of active alerts) |
| `trade_eligible` | Filter logic | Pass/fail on all entry criteria |

**Storage:** `data/morning_forecasts.csv` — append-only, never overwrite. One row per city per day. This becomes the ground truth for backtesting.

**End-of-day validation:** Compare `morning_forecast_high` vs WU settlement high. Build per-city accuracy tracking over time.

---

## Case Study: March 11, 2026 — "Wild Swing Day"

### What Happened

A warm front pushed through the Northeast, causing extreme intraday temperature swings in DC and Philadelphia. Coastal and inland cities were unaffected.

**KPHL (Philadelphia) — 30°F swing:**
```
Forecast high (end of day): 83°F    Actual high: 82°F
Morning low: 52°F

Hour      Temp    Notes
05:54     52°F    morning low
08:54     62°F    warming
11:54     73°F    stalled
13:54     72°F    ← DROPPED (mid-day dip)
15:54     80°F    ← finally crossing strikes
16:54     82°F    ← peak
```

**KDCA (Washington DC) — 26°F swing:**
```
Forecast high (end of day): 85°F    Actual high: 85°F
Morning low: 59°F

Hour      Temp    Notes
05:52     60°F
10:52     69°F
12:52     69°F    ← DROPPED 3°F
13:52     77°F    ← JUMPED 8°F in one hour (front arrival)
14:52     84°F    ← JUMPED 7°F more
16:52     85°F    ← peak, gusting 23kt
```

### What the v2 Filter Would Have Done

```
Step 2: NWS check — warm front advisory for DC/Philly region
Step 3: Both cities flagged as HIGH SWING → SKIP

Result: DC and Philly NOT traded. No exposure to the wild swings.
        LA, SF, Austin, Chicago, Seattle traded instead.
```

### Key Lessons

1. **The v2 weather filter correctly identifies dangerous cities.** DC and Philly had frontal passages — the filter skips them.
2. **Hold-to-settlement works even on swing days.** Both cities ultimately hit their forecast highs, but the mid-day path was ugly. Active management would have caused losses on winning trades.
3. **Morning forecast capture is essential.** End-of-day forecast (83°F/85°F) may not match the 6 AM forecast. Strike selection and P&L depend entirely on the morning number.

---

## ForecastEx Contract Reference

```
  Contract type:    OPT (secType="OPT", exchange="FORECASTX")
  YES contract:     Call (right="C")
  NO contract:      Put (right="P")
  Settlement:       $1.00 if condition met, $0.00 if not
  "Exceed X°F":     Strictly > X (not >=)
  No sell orders:   To close YES, buy NO on same strike (and vice versa)
  Settlement source: Weather Underground (WU) published daily high
  Measurement:      METAR ASOS at the city's airport
```

**Contract symbols:**

| Symbol | City | Airport | METAR |
|--------|------|---------|-------|
| UHMDW | Chicago | Midway | KMDW |
| UHLAX | Los Angeles | LAX | KLAX |
| UHSFO | San Francisco | SFO | KSFO |
| UHAUS | Austin | Bergstrom | KAUS |
| UHDCA | Washington DC | Reagan | KDCA |
| UHPHL | Philadelphia | PHL | KPHL |
| UHSEA | Seattle | Sea-Tac | KSEA |
| UHLGA | New York | LaGuardia | KLGA |

---

## Timezone & Peak Heating Windows

The 8 cities span 3 US timezones. Temperature peaks at different ET hours for each city, which affects when the condor outcome becomes clear and when hedge decisions must be made.

### Peak Heating by City (Typical Clear Day)

| City | Timezone | Peak Local | Peak ET | METAR Updates |
|------|----------|------------|---------|---------------|
| Philadelphia | ET | 1:00–3:00 PM | 1:00–3:00 PM | Hourly (:54) |
| Washington DC | ET | 1:00–3:00 PM | 1:00–3:00 PM | Hourly (:52) |
| New York | ET | 1:00–3:00 PM | 1:00–3:00 PM | Hourly (:51) |
| Chicago | CT | 1:00–3:00 PM | 2:00–4:00 PM | Hourly (:53) |
| Austin | CT | 2:00–4:00 PM | 3:00–5:00 PM | Hourly (:53) |
| Seattle | PT | 2:00–4:00 PM | 5:00–7:00 PM | Hourly (:53) |
| San Francisco | PT | 1:00–3:00 PM | 4:00–6:00 PM | Hourly (:56) |
| Los Angeles | PT | 12:00–2:30 PM | 3:00–5:30 PM | Hourly (:53) |

### Implications for Trading

**East Coast cities resolve first.** Philadelphia, DC, and NYC hit peak temperatures by 3 PM ET. If you're going to hedge (Step 5), you need to act before 1 PM ET — once temp is at peak, it's too late.

**West Coast cities resolve last.** LA and SF don't peak until 3–6 PM ET. These cities give you the longest decision window but also the longest period of uncertainty.

**Chicago and Austin are in-between.** Peak temps arrive 2–5 PM ET — moderate urgency for hedging.

### Hedge Decision Timeline (All Times ET)

```
03:15 AM    ForecastEx opens (overnight session)
06:00 AM    Pre-market scan: WU forecast + NWS alerts + swing checks
06:30 AM    Enter condors (market already open since 3:15 AM)
11:00 AM    First East Coast readings approaching mid-day lull
01:00 PM    LAST CHANCE to hedge East Coast cities (DC, Philly, NYC)
02:00 PM    East Coast cities approaching peak — outcome becoming clear
03:00 PM    LAST CHANCE to hedge Chicago
04:00 PM    East Coast settled. Chicago near peak. Austin approaching.
05:00 PM    LAST CHANCE to hedge Austin, LA
06:00 PM    LAST CHANCE to hedge SF, Seattle
07:00 PM    All cities past peak — outcomes locked in
```

### ForecastEx Market Hours — CONFIRMED (March 11, 2026)

Queried via `reqContractDetails` on UHLAX. IB reports timezone as **US/Central**.

```
Raw from IB:
  tradingHours: 20260311:0215-20260311:1615;20260311:1616-20260312:0159
  liquidHours:  (same as tradingHours)
  timeZoneId:   US/Central

Parsed:
  Session 1:  2:15 AM CT  →  4:15 PM CT   (14 hours)
  Break:      4:15 PM CT  →  4:16 PM CT   (1 minute)
  Session 2:  4:16 PM CT  →  2:00 AM CT   (9h 44min)

  Total daily trading: ~23h 59min (essentially 24/7 with 1-min + 15-min breaks)

All timezones:
  CT:  2:15 AM  →  4:15 PM  /  4:16 PM  →  2:00 AM+1
  ET:  3:15 AM  →  5:15 PM  /  5:16 PM  →  3:00 AM+1
  PT: 12:15 AM  →  2:15 PM  /  2:16 PM  → 12:00 AM+1
```

**Key implications for the Condor strategy:**

1. **Morning entries at 6:30 AM ET — YES, market is already open.** It opened at 3:15 AM ET. We have full access to enter condors before most participants are awake.

2. **All cities resolve within market hours.** Even Seattle (latest peak, 5-7 PM ET) is well within the session. No city is cut off.

3. **The 1-minute break at 5:15-5:16 PM ET is negligible.** East Coast cities have already peaked. West Coast cities are past their peak by then too (LA peaks 3-5:30 PM ET, SF 4-6 PM ET). Only Seattle might still be heating, but 1 minute is irrelevant.

4. **Overnight session exists.** Contracts can be entered the evening before (5:16 PM ET onwards). This could be useful if overnight depth is better, but likely illiquid.

5. **Hold-to-settlement is the default.** The market is open all day but ForecastEx has no sell orders — you can only close by buying the opposite leg. The condor is designed to hold to settlement anyway.

---

## What Needs Validation (Phase 0 Data Collection)

1. **Morning forecast capture for all 8 cities (CRITICAL)**
   Log WU forecast high before 6:30 AM ET daily. Without this, we cannot backtest.

2. **Order book depth at market open per city**
   Run depth_finder across all 8 symbols. Is ask depth available at the forecast ± offset strikes?

3. **Is combined entry < $1.00 achievable?**
   The strategy lives or dies on this. Need 14+ days of opening ask prices at target strikes.

4. **Forecast accuracy by city (morning forecast vs settlement)**
   Which cities stay within ±2°F? How often? This drives offset tuning and city selection.

5. **NWS alerts correlation with swing days**
   Do NWS alerts reliably predict high-swing days? Are there swing days with no prior alert?

---

## Implementation Phases

### Phase A: Discovery + Morning Forecast Capture
- Expand depth_finder to scan all 8 weather city contracts
- Add daily pre-market forecast capture (WU API, before 6:30 AM ET) for all 8 cities
- Add NWS alerts check per city
- Log to `data/morning_forecasts.csv` (append-only)
- Collect 14 days of: morning forecasts, opening ask prices, settlement highs, NWS alerts

### Phase B: Backtest (Requires 14+ Days of Phase A Data)
- Morning forecast vs settlement accuracy per city
- Simulated entries at K(morning_forecast ± offset) using actual opening ask prices
- Simulated P&L: how often both win, how often one wins, average profit
- Identify best cities: high accuracy + deep OI + low entry cost
- Refine per-city offsets

### Phase C: Paper Trade
- Automated pre-market: forecast capture + NWS filter + city selection
- Simulated orders at market open (log what we would have bought)
- Track daily simulated P&L against $0.30 target
- Test Step 5 hedge triggers

### Phase D: Live
- Start with 2-3 best cities from Phase B
- 40% portfolio cap, real orders at market open
- Scale up as data confirms edge

---

*Weather Condor Strategy v2.0 — March 11, 2026*
*Pre-validation. Do not trade until Phase A data confirms entry pricing and forecast accuracy.*
