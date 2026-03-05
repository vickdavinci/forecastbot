# PROCESS.md — ForecastBot Workflow Contract

**This file governs how all coding work is done on ForecastBot.**
**Both Claude Code and any other agent must follow this contract.**

---

## The One-Shot Prompt Loop

ForecastBot is built gate by gate using one-shot prompts. Each prompt is self-contained: it specifies what to read, what to build, what not to touch, and exactly what "done" means.

**The loop:**
```
1. Fill the prompt template (CLAUDE.md → One-Shot Prompt Template)
2. Agent reads specified files FIRST — no coding before reading
3. Agent implements exactly the specified change
4. Agent runs the specified test command
5. Agent commits if tests pass
6. You review the gate pass condition
7. Confirm or correct — then next prompt
```

**Never start coding without a filled prompt template.**

---

## Phase Gates — Sequential, No Skipping

Each gate must pass before the next begins. Gate pass conditions are in CLAUDE.md.

```
Gate 0  →  Gate 1  →  Gate 2  →  Gate 3  →  Gate 4
Infra      Monitor    Execution  Risk+Probe  Full Deploy
```

**Gate is passed when the explicit pass condition is met — not when the code "looks right".**

---

## Before Writing Any Code

The agent MUST:

1. Run wake-up protocol (CLAUDE.md top section)
2. Read the relevant spec section (SPEC.md — referenced in Component Map)
3. Read the exact files listed in the prompt template
4. Confirm understanding of blast radius (what NOT to touch)

**Skipping step 2 or 3 is the primary source of plumbing bugs.**

---

## Commit Contract

Every commit must follow this format:

```
<type>(<scope>): <what changed in one line>

Types:  feat | fix | test | docs | config | refactor
Scope:  gap-detector | executor | risk | carry | universe | position | connection

Examples:
  feat(gap-detector): add sum velocity tracking for early gap formation signal
  fix(executor): correct phase 2 chase price increment direction
  test(risk): add tier 3 kill switch trigger test
  config: raise ENTRY_THRESHOLD from 0.93 to 0.94 for slippage buffer
```

**Never commit without a passing test run.**
**Never commit config.py changes without updating the config table in CLAUDE.md.**

---

## Test Requirements Per Gate

### Gate 0 — Infrastructure
```bash
pytest tests/test_connection.py -v          # IBKR connects
pytest tests/test_contract_discovery.py -v # Contracts found and classified
```
Manual: `python scripts/test_connection.py` shows live bid/ask on ≥ 5 contracts.

### Gate 1 — Catalyst Monitor
```bash
pytest tests/test_gap_detector.py -v       # GapSignal.EXECUTE, ALERT, NONE
pytest tests/test_catalyst_calendar.py -v  # Mode switching, timing
pytest tests/test_atm_filter.py -v         # Subscribe/unsubscribe on ATM moves
```
Manual: `python scripts/simulate_gap.py --contract BTC_90K --sum 0.89` fires Telegram alert.

### Gate 2 — Execution
```bash
pytest tests/test_s1_parity.py -v          # ASK-only detection
pytest tests/test_execution.py -v          # Phase 1/2/3 all paths
pytest tests/test_carry_unwind.py -v       # Priority queue ordering
pytest tests/test_reconciler.py -v         # DB vs broker reconciliation
```
Paper trade: 5 days. Both-leg fill rate > 80%. Zero unhedged legs > 30s.

### Gate 3 — Risk + Live Probe
```bash
pytest tests/test_risk.py -v               # All 3 tiers
pytest tests/test_execution.py::test_unwind_triggers_tier2 -v
```
Manual: simulate T3 kill. Bot halts. Telegram fires. Requires manual restart to resume.
Live probe: $2,000 CAD real capital. Both legs fill within $0.01 of detected gap.

### Gate 4 — Full Deployment
No new tests — gate 4 is operational validation:
- 7 days without manual intervention
- First catalyst event captured under live capital
- Daily Telegram P&L summary arriving correctly

---

## What Requires a Spec Update Before Coding

Any change to these areas requires updating SPEC.md FIRST, then coding:

| Change | Update |
|--------|--------|
| New validation gate | SPEC.md §6.1 |
| New config parameter | SPEC.md §11 + config.py comment |
| New contract category | SPEC.md §3 |
| New risk trigger | SPEC.md §7 |
| New Telegram alert | SPEC.md §9 |
| Any change to P&L formula | SPEC.md §6.2 (critical — easy to get wrong) |

**If spec and code disagree, the spec wins. Fix the code, not the spec, unless the spec is provably wrong.**

---

## What Requires a CLAUDE.md Update After Coding

| Change | Update in CLAUDE.md |
|--------|---------------------|
| New config param | Config Quick Reference table |
| New drop code | Drop Codes Reference table |
| New component file | Component Map + Repository Structure |
| Gate passed | Phase Gates table — mark passed |
| New known bug | Common Pitfalls |
| New agent added | Custom Agents table |

**Documentation updates are part of "done". Not optional.**

---

## Ownership Boundaries (Blast Radius Rules)

These boundaries prevent the most common ForecastBot plumbing failure: correct logic applied to the wrong component.

```
gap_detector.py      → Emits GapSignal. Does NOT call validator or executor.
s1_parity.py         → Consumes GapSignal. Does NOT execute. Does NOT validate.
validator.py         → Runs gates. Does NOT execute. Does NOT call IBKR.
executor.py          → Submits orders. ONLY component that calls ib.placeOrder().
position_manager.py  → Tracks positions. Does NOT submit orders.
risk_engine.py       → Evaluates tiers. Does NOT submit orders. ALWAYS evaluated before executor.
carry/s2_carry.py    → Separate from s1_parity.py — no shared scanner state.
```

**If you find yourself calling `ib.placeOrder()` from anywhere other than `executor.py` → stop, restructure.**

---

## How to Handle a Plumbing Bug

When the bot is not trading and you can't explain why:

1. Check drop code aggregate log first:
   ```bash
   python scripts/drop_code_summary.py --hours 24
   ```
2. Most common drop codes and what they mean: `ERRORS.md`
3. If drop code is `INSUFFICIENT_CAPITAL` → check if carry positions are blocking
4. If drop code is `STALE_YES_PRICE` → check `market_data.py` ask age tracking
5. If no drop codes at all → scanner is not reaching the validator (gap_detector or s1_parity bug)
6. If drop codes but no execution → validator passing but executor not firing (risk tier active?)

**Never assume. Trace the exact code path. Confirm vs Hypothesis.**

---

## IB Gateway Management

IB Gateway requires re-authentication every 24 hours (IBKR security requirement).

```bash
# Check gateway status
python scripts/check_gateway.py

# Gateway auto-reconnect is handled by core/connection.py
# But on manual restart, always run reconciler before trading resumes:
python scripts/reconcile_positions.py
```

**After any gateway restart:** positions in DB must be verified against IBKR portfolio before the scanner is re-enabled.

---

## Paper vs Live Checklist

Before switching from paper to live ($2,000 CAD probe):

- [ ] Gate 2 fully passed (5 paper trading days clean)
- [ ] Both-leg fill rate confirmed > 80% on paper
- [ ] Zero unhedged legs > 30s in paper session
- [ ] T3 kill switch tested and confirmed halts correctly
- [ ] Telegram alerts confirmed arriving within 5 seconds
- [ ] Reconciler tested: restart mid-session, positions recovered correctly
- [ ] `ENTRY_THRESHOLD` reviewed — is 0.93 still the right level based on observed spreads?

Before scaling from $2,000 probe to full $30,000 CAD:

- [ ] Gate 3 fully passed
- [ ] Probe fills confirmed within $0.01 of detected gap
- [ ] Both legs confirmed filling (not just one)
- [ ] At least one catalyst event observed and captured (or missed and documented)
- [ ] Daily P&L report arriving correctly

---

*ForecastBot PROCESS.md v1.0 — March 4, 2026*
