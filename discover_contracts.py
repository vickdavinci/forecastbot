"""
discover_contracts.py — Phase 0, Script 1
==========================================
PURPOSE:
  Find ALL active ForecastEx contracts across the full 3-tier universe.
  Test that reqMktData returns live bid/ask (not zero, not delayed).
  Print conIds for near-ATM strikes, ranked by OI descending.
  Output is used to populate WATCH_CONTRACTS in kill_shot.py.

RUN ORDER:
  1. Start IB Gateway (paper account, port 7497)
  2. python scripts/discover_contracts.py
  3. Copy near-ATM conIds into kill_shot.py WATCH_CONTRACTS

CONSTRAINTS:
  - NO order submission
  - reqContractDetails + reqMktData ONLY
  - Exits cleanly after printing results

OUTPUT FORMAT:
  TIER1  FF    3.875  20260319  YES  conId=111111  OI=107000  bid=0.94  ask=0.97  sum_pair=?
"""

import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from ib_async import IB, Contract, ContractDetails, Ticker

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────

IBKR_HOST      = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT", "7497"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "10"))

# Near-ATM filter: YES bid must be between these values
ATM_BID_LOW  = 0.15
ATM_BID_HIGH = 0.85

# Seconds to wait for market data to populate after subscribe
MKT_DATA_WAIT = 3.0

# ─── CONTRACT UNIVERSE ─────────────────────────────────────────────────────────
#
# Each entry: (tier, symbol, exchange, description)
# secType is always "OPT" for ForecastEx contracts
# IBKR models YES = Call (right="C"), NO = Put (right="P")
#
UNIVERSE = [
    # TIER 1 — Highest OI, continuous repricing, scan every 60s in kill_shot
    ("TIER1", "FF",    "FORECASTX", "Fed Funds Rate"),         # OI 107K confirmed
    ("TIER1", "BTC",   "FORECASTX", "Bitcoin annual"),         # 24/7 underlying
    ("TIER1", "ES",    "FORECASTX", "S&P 500 monthly"),        # liquid
    ("TIER1", "NQ",    "FORECASTX", "Nasdaq monthly"),         # correlated ES

    # TIER 2 — Medium OI, periodic repricing, scan every 5min in kill_shot
    ("TIER2", "CPI",   "FORECASTX", "US CPI monthly"),
    ("TIER2", "GC",    "FORECASTX", "Gold"),
    ("TIER2", "CL",    "FORECASTX", "Crude Oil"),
    ("TIER2", "METLS", "FORECASTX", "Silver Price"),           # confirmed visible in UI

    # TIER 3 — Lower priority, scan every 15min in kill_shot
    ("TIER3", "6E",    "FORECASTX", "EUR/USD"),
    ("TIER3", "UR",    "FORECASTX", "Unemployment Rate"),
    ("TIER3", "IC",    "FORECASTX", "Initial Jobless Claims"),
]

# ─── DATA CLASSES ──────────────────────────────────────────────────────────────

@dataclass
class ContractRecord:
    tier: str
    symbol: str
    description: str
    exchange: str
    strike: float
    expiry: str
    right: str          # "C" = YES, "P" = NO
    con_id: int
    oi: int
    bid: float
    ask: float
    is_atm: bool        # bid between ATM_BID_LOW and ATM_BID_HIGH

    @property
    def right_label(self):
        return "YES" if self.right == "C" else "NO"

    @property
    def expiry_fmt(self):
        try:
            return datetime.strptime(self.expiry, "%Y%m%d").strftime("%b%d'%y")
        except Exception:
            return self.expiry


# ─── HELPERS ───────────────────────────────────────────────────────────────────

def make_contract(symbol: str, exchange: str, right: str,
                  strike: float = 0, expiry: str = "") -> Contract:
    """Build a ForecastEx contract object for reqContractDetails."""
    c = Contract()
    c.symbol   = symbol
    c.secType  = "OPT"
    c.exchange = exchange
    c.currency = "USD"
    if right:
        c.right = right
    if strike:
        c.strike = strike
    if expiry:
        c.lastTradeDateOrContractMonth = expiry
    return c


def print_header():
    print("\n" + "="*90)
    print("  ForecastBot Phase 0 — Contract Discovery")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  "
          f"Host: {IBKR_HOST}:{IBKR_PORT}")
    print("="*90)


def print_section(title: str):
    print(f"\n{'─'*90}")
    print(f"  {title}")
    print(f"{'─'*90}")


def format_record(r: ContractRecord, pair_sum: Optional[float] = None) -> str:
    atm_flag = "◀ ATM" if r.is_atm else ""
    sum_str  = f"  pair_sum={pair_sum:.3f}" if pair_sum is not None else ""
    return (
        f"  {r.tier:<6} {r.symbol:<6} {r.strike:<10.4g} {r.expiry_fmt:<10} "
        f"{r.right_label:<4}  conId={r.con_id:<10}  "
        f"OI={r.oi:<8}  bid={r.bid:.2f}  ask={r.ask:.2f}"
        f"{sum_str}  {atm_flag}"
    )


# ─── CORE LOGIC ────────────────────────────────────────────────────────────────

async def discover(ib: IB) -> list[ContractRecord]:
    """
    For each symbol in UNIVERSE:
      1. reqContractDetails to get all strikes/expiries
      2. reqMktData on near-ATM contracts to get live bid/ask
      3. Return list of ContractRecords
    """
    all_records: list[ContractRecord] = []
    failed_symbols: list[str] = []

    for tier, symbol, exchange, description in UNIVERSE:
        print(f"\n  Scanning {tier} — {symbol} ({description}) ...", end="", flush=True)

        # ── Step 1: Get contract details ──────────────────────────────────────
        query = make_contract(symbol, exchange, right="")
        try:
            details_list: list[ContractDetails] = await ib.reqContractDetailsAsync(query)
        except Exception as e:
            print(f" ERROR: {e}")
            failed_symbols.append(symbol)
            continue

        if not details_list:
            print(f" NO CONTRACTS FOUND — symbol may not exist on this account")
            failed_symbols.append(symbol)
            continue

        print(f" {len(details_list)} contracts found", end="", flush=True)

        # ── Step 2: Filter to YES (Call) contracts with sensible strikes ──────
        yes_details = [d for d in details_list if d.contract.right == "C"]
        print(f" ({len(yes_details)} YES legs)", end="", flush=True)

        if not yes_details:
            print(" — skipping (no YES contracts)")
            continue

        # ── Step 3: Subscribe to market data for each YES contract ─────────────
        tickers: dict[int, Ticker] = {}
        contracts_to_watch = []

        for d in yes_details:
            c = d.contract
            ticker = ib.reqMktData(c, genericTickList="", snapshot=False)
            tickers[c.conId] = ticker
            contracts_to_watch.append((d, ticker))

        # Wait for data to arrive
        await asyncio.sleep(MKT_DATA_WAIT)

        # ── Step 4: Read bid/ask, build records ────────────────────────────────
        symbol_records = []
        for d, ticker in contracts_to_watch:
            c = d.contract
            bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0.0
            ask = ticker.ask if ticker.ask and ticker.ask > 0 else 0.0
            oi  = int(d.longName) if d.longName and d.longName.isdigit() else 0

            # Try OI from contract details
            try:
                oi = ticker.openInterest or 0
            except AttributeError:
                oi = 0

            is_atm = ATM_BID_LOW <= bid <= ATM_BID_HIGH

            rec = ContractRecord(
                tier        = tier,
                symbol      = symbol,
                description = description,
                exchange    = exchange,
                strike      = c.strike,
                expiry      = c.lastTradeDateOrContractMonth,
                right       = c.right,
                con_id      = c.conId,
                oi          = oi,
                bid         = bid,
                ask         = ask,
                is_atm      = is_atm,
            )
            symbol_records.append(rec)

        # Cancel market data subscriptions to keep line count low
        for d, ticker in contracts_to_watch:
            ib.cancelMktData(d.contract)

        atm_count = sum(1 for r in symbol_records if r.is_atm)
        live_count = sum(1 for r in symbol_records if r.bid > 0)
        print(f" — {live_count} live prices, {atm_count} near-ATM")

        all_records.extend(symbol_records)

    return all_records, failed_symbols


async def test_depth(ib: IB, records: list[ContractRecord]) -> dict[int, int]:
    """
    For near-ATM YES contracts, call reqMktDepth to test if depth data is available.
    Returns {conId: depth_at_ask} for all tested contracts.
    """
    atm_yes = [r for r in records if r.is_atm and r.right == "C"][:10]  # test first 10
    depth_results: dict[int, int] = {}

    if not atm_yes:
        return depth_results

    print_section("DEPTH TEST — reqMktDepth on near-ATM YES contracts")
    print("  (This confirms whether order book data is accessible via API)\n")

    for r in atm_yes:
        c = make_contract(r.symbol, r.exchange, r.right, r.strike, r.expiry)
        c.conId = r.con_id

        ticker = ib.reqMktDepth(c, numRows=5, isSmartDepth=False)
        await asyncio.sleep(2.0)

        # Read depth
        ask_depth = 0
        if ticker.domAsks:
            ask_depth = int(ticker.domAsks[0].size) if ticker.domAsks[0].size else 0

        depth_results[r.con_id] = ask_depth

        status = "✓ DEPTH DATA" if ask_depth > 0 else "✗ NO DEPTH (API may not support or no orders)"
        print(f"  {r.symbol:<6} {r.expiry_fmt:<10} strike={r.strike:<10.4g} "
              f"ask_depth={ask_depth:<8} {status}")

        ib.cancelMktDepth(c, isSmartDepth=False)
        await asyncio.sleep(0.5)

    return depth_results


def pair_contracts(records: list[ContractRecord]) -> list[tuple]:
    """
    Match YES (Call) and NO (Put) contracts by symbol + strike + expiry.
    Returns list of (yes_record, no_record, pair_sum).
    """
    yes_map = {}
    no_map  = {}

    for r in records:
        key = (r.symbol, r.strike, r.expiry)
        if r.right == "C":
            yes_map[key] = r
        else:
            no_map[key] = r

    pairs = []
    for key, yes_r in yes_map.items():
        if key in no_map:
            no_r = no_map[key]
            if yes_r.ask > 0 and no_r.ask > 0:
                pair_sum = yes_r.ask + no_r.ask
                pairs.append((yes_r, no_r, pair_sum))

    return pairs


def print_results(records: list[ContractRecord],
                  pairs: list[tuple],
                  depth_results: dict[int, int],
                  failed_symbols: list[str]):

    # ── All contracts by tier ──────────────────────────────────────────────────
    print_section("ALL CONTRACTS WITH LIVE PRICES (sorted by OI desc)")
    print(f"  {'TIER':<6} {'SYM':<6} {'STRIKE':<10} {'EXPIRY':<10} "
          f"{'SIDE':<4}  {'CON_ID':<12}  {'OI':<8}  {'BID':>6}  {'ASK':>6}")
    print(f"  {'─'*80}")

    live = [r for r in records if r.bid > 0]
    live.sort(key=lambda r: r.oi, reverse=True)

    for r in live:
        depth = depth_results.get(r.con_id, -1)
        depth_str = f"  depth={depth}" if depth >= 0 else ""
        print(format_record(r) + depth_str)

    # ── Near-ATM pairs (most important) ────────────────────────────────────────
    print_section("NEAR-ATM PAIRS — YES+NO ask sum (arb target: sum < $0.97)")
    print(f"  {'TIER':<6} {'SYM':<6} {'STRIKE':<10} {'EXPIRY':<10} "
          f"{'YES_ASK':>8}  {'NO_ASK':>7}  {'SUM':>7}  {'GAP':>7}  {'STATUS':<20}")
    print(f"  {'─'*85}")

    atm_pairs = [(y, n, s) for y, n, s in pairs if y.is_atm or n.is_atm]
    atm_pairs.sort(key=lambda x: x[0].oi, reverse=True)

    for yes_r, no_r, pair_sum in atm_pairs:
        gap    = 1.00 - pair_sum
        status = "⚡ ARB WINDOW" if gap >= 0.03 else ("≈ FAIR" if abs(gap) < 0.01 else "OVERPRICED")
        depth  = depth_results.get(yes_r.con_id, -1)
        depth_str = f"  depth={depth}" if depth >= 0 else ""
        print(
            f"  {yes_r.tier:<6} {yes_r.symbol:<6} {yes_r.strike:<10.4g} "
            f"{yes_r.expiry_fmt:<10}  "
            f"{yes_r.ask:>8.3f}  {no_r.ask:>7.3f}  {pair_sum:>7.3f}  "
            f"{gap:>+7.3f}  {status}{depth_str}"
        )

    # ── WATCH_CONTRACTS output for kill_shot.py ─────────────────────────────
    print_section("COPY THIS INTO kill_shot.py → WATCH_CONTRACTS")
    print("  # Format: (tier, symbol, yes_conId, no_conId, strike, expiry, description)")
    print("  WATCH_CONTRACTS = [")

    for yes_r, no_r, pair_sum in atm_pairs:
        print(
            f"    ('{yes_r.tier}', '{yes_r.symbol}', "
            f"{yes_r.con_id}, {no_r.con_id}, "
            f"{yes_r.strike}, '{yes_r.expiry}', "
            f"'{yes_r.description} {yes_r.expiry_fmt} {yes_r.strike}'),"
        )
    print("  ]")

    # ── Summary ────────────────────────────────────────────────────────────────
    print_section("SUMMARY")
    print(f"  Total contracts found:    {len(records)}")
    print(f"  Contracts with live data: {len(live)}")
    print(f"  Near-ATM pairs:           {len(atm_pairs)}")
    print(f"  Depth data available:     "
          f"{'YES ✓' if any(v > 0 for v in depth_results.values()) else 'NO ✗ — CHECK SUBSCRIPTION'}")

    if failed_symbols:
        print(f"\n  ⚠ Symbols that returned no contracts: {', '.join(failed_symbols)}")
        print("    These may use different symbol names on this account.")
        print("    Check ForecastTrader UI and compare symbol names.")

    if not live:
        print("\n  ✗ CRITICAL: No live market data returned via API.")
        print("    This means reqMktData is NOT working for ForecastEx contracts.")
        print("    Possible fixes:")
        print("    1. Subscribe to ForecastEx market data in Client Portal → Settings → Market Data")
        print("    2. Call IBKR support — ask if ForecastEx API data requires a subscription")
        print("    3. Try the REST API fallback (see README_PHASE0.md)")
        print("    DO NOT proceed to kill_shot.py until this is resolved.")
    elif not any(v > 0 for v in depth_results.values()):
        print("\n  ⚠ WARNING: reqMktDepth returned no data for any contract.")
        print("    Level 1 (bid/ask) works but Level 2 (order book depth) does not.")
        print("    kill_shot.py will still run but depth column will show 0.")
        print("    Gap frequency can still be measured. Depth measurement cannot.")
        print("    Phase 0 decision matrix will be depth-unknown — adjust thresholds accordingly.")
    else:
        print("\n  ✓ API ACCESS CONFIRMED — proceed to kill_shot.py")
        print("    Copy WATCH_CONTRACTS above into kill_shot.py and run.")


# ─── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    print_header()

    ib = IB()

    print(f"\n  Connecting to IB Gateway at {IBKR_HOST}:{IBKR_PORT} "
          f"(clientId={IBKR_CLIENT_ID}) ...")

    try:
        await ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID)
    except Exception as e:
        print(f"\n  ✗ CONNECTION FAILED: {e}")
        print("  Make sure IB Gateway is running and API is enabled.")
        print("  Configure → API → Settings → Enable ActiveX and Socket Clients")
        print(f"  Port should be 7497 (paper) or 7496 (live). Current: {IBKR_PORT}")
        sys.exit(1)

    print("  ✓ Connected")

    try:
        # Step 1: Discover all contracts
        print_section("SCANNING CONTRACT UNIVERSE (3 tiers, 11 symbols)")
        records, failed_symbols = await discover(ib)

        if not records:
            print("\n  ✗ No contracts found across any symbol.")
            print("  This account may not have ForecastEx permissions enabled.")
            print("  Go to Client Portal → Settings → Trading Permissions → Event Contracts")
            return

        # Step 2: Build YES/NO pairs
        all_records_with_no = []
        for tier, symbol, exchange, description in UNIVERSE:
            query_no = make_contract(symbol, exchange, right="P")
            try:
                no_details = await ib.reqContractDetailsAsync(query_no)
                tickers_no = []
                for d in no_details:
                    ticker = ib.reqMktData(d.contract, genericTickList="", snapshot=False)
                    tickers_no.append((d, ticker))
                await asyncio.sleep(MKT_DATA_WAIT)
                for d, ticker in tickers_no:
                    c = d.contract
                    bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0.0
                    ask = ticker.ask if ticker.ask and ticker.ask > 0 else 0.0
                    try:
                        oi = ticker.openInterest or 0
                    except AttributeError:
                        oi = 0
                    is_atm = ATM_BID_LOW <= bid <= ATM_BID_HIGH
                    rec = ContractRecord(
                        tier=tier, symbol=symbol, description=description,
                        exchange=exchange, strike=c.strike,
                        expiry=c.lastTradeDateOrContractMonth, right=c.right,
                        con_id=c.conId, oi=oi, bid=bid, ask=ask, is_atm=is_atm,
                    )
                    all_records_with_no.append(rec)
                    ib.cancelMktData(d.contract)
            except Exception:
                pass

        all_records = records + all_records_with_no

        # Step 3: Test depth on a sample of near-ATM YES contracts
        depth_results = await test_depth(ib, records)

        # Step 4: Pair YES + NO
        pairs = pair_contracts(all_records)

        # Step 5: Print results + WATCH_CONTRACTS template
        print_results(all_records, pairs, depth_results, failed_symbols)

    finally:
        ib.disconnect()
        print("\n  Disconnected. Done.\n")


if __name__ == "__main__":
    asyncio.run(main())
