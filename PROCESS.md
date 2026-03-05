# PROCESS.md — ForecastBot Workflow Contract

**This file governs how all coding work is done on ForecastBot.**
**Both Claude Code and any other agent must follow this contract.**

> **Current Phase: Phase 0 — Observation Only.**
> Full one-shot prompt workflow and gate process applies starting Gate 1.
> Phase 0 uses the simplified process described below.

---

## Phase 0 Process (Current)

Phase 0 is observation-only. The workflow is simpler than the full gate process:

1. **Run the scanners** — `kill_shot.py` (parity gaps) and `weather_edge.py` (weather edge)
2. **Observe output** — check `data/` CSV files and Telegram alerts
3. **Log issues** — document any errors or unexpected behaviour in `ERRORS.md`
4. **Debug as needed** — fix IB connection issues, price feed problems, data quality
5. **After 14-30 days** — evaluate the Decision Matrix in `WORKBOARD.md`
6. **If go** — proceed to Gate 1 with full one-shot prompt process

**Phase 0 testing is manual:**
- Run kill_shot.py — verify IB connects, contracts stream, gaps logged to CSV
- Run weather_edge.py — verify IB connects on clientId=45, UHLAX strikes subscribe, prices populate
- Check data/ directory for CSV output files

---

## Phase Gates — Sequential, No Skipping

```
Phase 0  ->  Gate 1  ->  Gate 2  ->  Gate 3  ->  Gate 4  ->  Gate 5
Observe     Infra      Monitor    Execution  Risk+Probe  Full Deploy
(current)
```

**Gate is passed when the explicit pass condition is met — not when the code "looks right".**

---

## The One-Shot Prompt Loop (Gate 1+)

ForecastBot is built gate by gate using one-shot prompts. Each prompt is self-contained: it specifies what to read, what to build, what not to touch, and exactly what "done" means.

**The loop:**
```
1. Fill the prompt template (CLAUDE.md -> One-Shot Prompt Template)
2. Agent reads specified files FIRST — no coding before reading
3. Agent implements exactly the specified change
4. Agent runs the specified test command
5. Agent commits if tests pass
6. You review the gate pass condition
7. Confirm or correct — then next prompt
```

**Never start coding without a filled prompt template.**

---

## Before Writing Any Code (Gate 1+)

The agent MUST:

1. Run wake-up protocol (CLAUDE.md top section)
2. Read the relevant spec section (SPECV2.md)
3. Read the exact files listed in the prompt template
4. Confirm understanding of blast radius (what NOT to touch)

**Skipping step 2 or 3 is the primary source of plumbing bugs.**

---

## Commit Contract

Every commit must follow this format:

```
<type>(<scope>): <what changed in one line>

Types:  feat | fix | test | docs | config | refactor
Scope:  kill-shot | weather-edge | gap-detector | executor | risk | carry | universe | position | connection

Examples:
  feat(kill-shot): add 3-tick confirmation before gap alert
  fix(weather-edge): fix async event loop blocking IB tick callbacks
  feat(gap-detector): add sum velocity tracking for early gap formation signal
  docs: update CLAUDE.md for Phase 0 current state
```

**Never commit without verifying the change works (manual test in Phase 0, pytest in Gate 1+).**

---

## Test Requirements Per Phase

### Phase 0 — Observation (Current)
No pytest tests. Manual verification only:
- kill_shot.py connects to IB, streams ticks, logs gaps to CSV
- weather_edge.py connects to IB on clientId=45, subscribes UHLAX, prices populate
- data/ directory contains expected CSV output files
- Telegram alerts fire on gap events (if configured)

### Gate 1 — Infrastructure
```bash
pytest tests/test_connection.py -v          # IBKR connects
pytest tests/test_contract_discovery.py -v  # Contracts found and classified
```
Manual: `python scripts/test_connection.py` shows live bid/ask on >= 5 contracts.

### Gate 2 — Catalyst Monitor
```bash
pytest tests/test_gap_detector.py -v       # GapSignal.EXECUTE, ALERT, NONE
pytest tests/test_catalyst_calendar.py -v  # Mode switching, timing
pytest tests/test_atm_filter.py -v         # Subscribe/unsubscribe on ATM moves
```

### Gate 3 — Execution
```bash
pytest tests/test_s1_parity.py -v          # ASK-only detection
pytest tests/test_execution.py -v          # Phase 1/2/3 all paths
pytest tests/test_carry_unwind.py -v       # Priority queue ordering
pytest tests/test_reconciler.py -v         # DB vs broker reconciliation
```
Paper trade: 5 days. Both-leg fill rate > 80%. Zero unhedged legs > 30s.

### Gate 4 — Risk + Live Probe
```bash
pytest tests/test_risk.py -v               # All 3 tiers
pytest tests/test_execution.py::test_unwind_triggers_tier2 -v
```
Live probe: $2,000 CAD real capital. Both legs fill within $0.01 of detected gap.

### Gate 5 — Full Deployment
No new tests — operational validation:
- 7 days without manual intervention
- First catalyst event captured under live capital
- Daily Telegram P&L summary arriving correctly

---

## What Requires a Spec Update Before Coding (Gate 1+)

Any change to these areas requires updating SPECV2.md FIRST, then coding:

| Change | Update |
|--------|--------|
| New validation gate | SPECV2.md validation section |
| New config parameter | SPECV2.md + config.py comment |
| New contract category | SPECV2.md contract universe |
| New risk trigger | SPECV2.md risk section |
| New Telegram alert | SPECV2.md alerting section |
| Any change to P&L formula | SPECV2.md (critical) |

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

**Documentation updates are part of "done". Not optional.**

---

## Ownership Boundaries (Gate 1+ — Blast Radius Rules)

These boundaries prevent the most common plumbing failure: correct logic applied to the wrong component.

```
gap_detector.py      -> Emits GapSignal. Does NOT call validator or executor.
s1_parity.py         -> Consumes GapSignal. Does NOT execute. Does NOT validate.
validator.py         -> Runs gates. Does NOT execute. Does NOT call IBKR.
executor.py          -> Submits orders. ONLY component that calls ib.placeOrder().
position_manager.py  -> Tracks positions. Does NOT submit orders.
risk_engine.py       -> Evaluates tiers. Does NOT submit orders. ALWAYS before executor.
carry/s2_carry.py    -> Separate from s1_parity.py — no shared scanner state.
```

**If you find yourself calling `ib.placeOrder()` from anywhere other than `executor.py` -> stop, restructure.**

---

## How to Handle a Plumbing Bug (Gate 1+)

When the bot is not trading and you can't explain why:

1. Check drop code aggregate log first
2. Most common drop codes and what they mean: `ERRORS.md`
3. If no drop codes at all -> scanner is not reaching the validator
4. If drop codes but no execution -> validator passing but executor not firing (risk tier active?)

**Never assume. Trace the exact code path. Confirm vs Hypothesis.**

---

## IB Gateway Management

IB Gateway may require periodic re-authentication.

```bash
# kill_shot.py has auto-reconnect (up to 10 attempts)
# weather_edge.py reconnect TBD
# Port: 4001 (check .env)
```

**After any gateway restart in Gate 1+:** positions in DB must be verified against IBKR portfolio before scanner re-enables.

---

## Paper vs Live Checklist (Gate 3+)

Before switching to live capital ($2,000 CAD probe):

- [ ] Gate 3 fully passed (5 paper trading days clean)
- [ ] Both-leg fill rate confirmed > 80% on paper
- [ ] Zero unhedged legs > 30s in paper session
- [ ] T3 kill switch tested and confirmed halts correctly
- [ ] Telegram alerts confirmed arriving within 5 seconds
- [ ] Reconciler tested: restart mid-session, positions recovered correctly

Before scaling from $2,000 probe to full capital:

- [ ] Gate 4 fully passed
- [ ] Probe fills confirmed within $0.01 of detected gap
- [ ] At least one catalyst event observed and captured
- [ ] Daily P&L report arriving correctly

---

*ForecastBot PROCESS.md v1.1 — March 5, 2026*
