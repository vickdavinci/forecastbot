"""
discover_contracts.py — Phase 0, Script 1
Broad scan — tries all known symbol candidates, finds every ForecastEx contract.
"""

import asyncio
import os
import sys
from datetime import datetime
from dataclasses import dataclass

from dotenv import load_dotenv
from ib_async import IB, Contract

load_dotenv()

IBKR_HOST      = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT", "4001"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "10"))
ATM_LOW        = 0.15
ATM_HIGH       = 0.85
MKT_DATA_WAIT  = 4.0

SYMBOL_CANDIDATES = [
    "FF", "FFER", "FEDFUNDS", "USIR",
    "BTC", "BTCUSD", "BITCOIN",
    "ES", "SPX", "SP500", "NQ", "NDX",
    "GC", "GOLD", "METLS", "SI", "SILVER",
    "CL", "OIL", "NG",
    "6E", "EURUSD",
    "CPI", "USCPI",
    "UR", "UNEMP",
    "IC", "JOBLESS", "NFP",
    "GDP", "PCE", "RS", "HS",
    "TEMP", "CO2",
]


@dataclass
class ContractRecord:
    symbol: str
    strike: float
    expiry: str
    right: str
    con_id: int
    bid: float
    ask: float

    @property
    def side(self): return "YES" if self.right == "C" else "NO"

    @property
    def expiry_short(self):
        try: return datetime.strptime(self.expiry[:8], "%Y%m%d").strftime("%b%d'%y")
        except: return self.expiry

    @property
    def is_atm(self): return ATM_LOW <= self.bid <= ATM_HIGH


async def scan_all_symbols(ib):
    all_details = []
    found = []

    print(f"\n  Scanning {len(SYMBOL_CANDIDATES)} symbol candidates on FORECASTX ...")
    print("  . = found  - = empty  x = error\n  ", end="")

    for symbol in SYMBOL_CANDIDATES:
        c = Contract()
        c.symbol = symbol; c.secType = "OPT"
        c.exchange = "FORECASTX"; c.currency = "USD"
        try:
            details = await ib.reqContractDetailsAsync(c)
            if details:
                all_details.extend(details)
                found.append(f"{symbol}({len(details)})")
                print(".", end="", flush=True)
            else:
                print("-", end="", flush=True)
        except:
            print("x", end="", flush=True)
        await asyncio.sleep(0.1)

    print(f"\n\n  Symbols with contracts: {', '.join(found) if found else 'NONE'}")

    if not all_details:
        return []

    seen = set()
    unique = [d for d in all_details if not (d.contract.conId in seen or seen.add(d.contract.conId))]
    print(f"  Unique contracts: {len(unique)} — fetching market data ...")

    tickers = []
    for d in unique:
        t = ib.reqMktData(d.contract, genericTickList="", snapshot=False)
        tickers.append((d, t))

    await asyncio.sleep(MKT_DATA_WAIT)

    records = []
    for d, t in tickers:
        c = d.contract
        bid = t.bid if t.bid and t.bid > 0 else 0.0
        ask = t.ask if t.ask and t.ask > 0 else 0.0
        records.append(ContractRecord(c.symbol, c.strike,
            c.lastTradeDateOrContractMonth, c.right, c.conId, bid, ask))
        ib.cancelMktData(c)

    return records


async def test_depth(ib, records):
    atm = [r for r in records if r.is_atm and r.right == "C"][:5]
    results = {}
    if not atm:
        print("  No near-ATM YES contracts to test depth on.")
        return results

    print(f"\n  Testing reqMktDepth on {len(atm)} near-ATM contracts ...")
    for r in atm:
        c = Contract(); c.conId = r.con_id
        c.exchange = "FORECASTX"; c.secType = "OPT"; c.symbol = r.symbol
        try:
            t = ib.reqMktDepth(c, numRows=5, isSmartDepth=False)
            await asyncio.sleep(2.0)
            depth = int(t.domAsks[0].size or 0) if hasattr(t, 'domAsks') and t.domAsks else 0
            results[r.con_id] = depth
            print(f"    {r.symbol:<8} strike={r.strike:<10} depth={depth}  "
                  f"{'✓ ORDER BOOK WORKS' if depth > 0 else '✗ no depth data'}")
            ib.cancelMktDepth(c, isSmartDepth=False)
        except Exception as e:
            print(f"    {r.symbol} error: {e}")
            results[r.con_id] = 0
        await asyncio.sleep(0.5)
    return results


def print_results(records, depth_results):
    live = [r for r in records if r.bid > 0 or r.ask > 0]
    dead = [r for r in records if r.bid == 0 and r.ask == 0]

    print(f"\n{'='*80}")
    print(f"  RESULTS SUMMARY")
    print(f"  Total contracts found:  {len(records)}")
    print(f"  With live prices:       {len(live)}")
    print(f"  No price (bid=ask=0):   {len(dead)}")

    if not live:
        print("\n  ✗ CRITICAL: No live market data returned via API.")
        print("  Fixes to try:")
        print("  1. Uncheck 'Read-Only API' in Gateway → Configure → API → Settings")
        print("  2. Check Client Portal → Market Data Subscriptions for FORECASTX feed")
        print("  3. Call IBKR: 'Does ForecastEx need a market data subscription for TWS API?'")
        return

    # Build pairs
    yes_map = {(r.symbol, r.strike, r.expiry): r for r in live if r.right == "C"}
    no_map  = {(r.symbol, r.strike, r.expiry): r for r in live if r.right == "P"}
    pairs = []
    for key, yr in yes_map.items():
        if key in no_map:
            nr = no_map[key]
            if yr.ask > 0 and nr.ask > 0:
                pairs.append((yr, nr, yr.ask + nr.ask, 1.0 - (yr.ask + nr.ask)))
    pairs.sort(key=lambda x: abs(x[3]), reverse=True)

    print(f"\n  {'─'*78}")
    print(f"  ALL PAIRS — arb target: sum < $0.97  (gap > +$0.03)")
    print(f"  {'─'*78}")
    print(f"  {'SYM':<8} {'STRIKE':<10} {'EXPIRY':<12} "
          f"{'YES_ASK':>8} {'NO_ASK':>8} {'SUM':>8} {'GAP':>8}  NOTE")
    print(f"  {'─'*78}")
    for yr, nr, s, g in pairs:
        flag = "⚡ARB" if g >= 0.03 else ("→watch" if g >= 0.01 else "")
        atm  = "◀ATM" if yr.is_atm else ""
        print(f"  {yr.symbol:<8} {yr.strike:<10.4g} {yr.expiry_short:<12} "
              f"{yr.ask:>8.3f} {nr.ask:>8.3f} {s:>8.3f} {g:>+8.3f}  {flag} {atm}")

    atm_pairs = [(y, n, s, g) for y, n, s, g in pairs if y.is_atm]
    if atm_pairs:
        print(f"\n  ┌── PASTE INTO kill_shot.py → WATCH_CONTRACTS {'─'*32}┐")
        print(f"  WATCH_CONTRACTS = [")
        print(f"    # (tier, symbol, yes_conId, no_conId, strike, expiry, label)")
        for yr, nr, s, g in atm_pairs:
            print(f"    ('TIER1', '{yr.symbol}', {yr.con_id}, {nr.con_id}, "
                  f"{yr.strike}, '{yr.expiry}', '{yr.symbol} {yr.expiry_short} {yr.strike}'),")
        print(f"  ]")
        print(f"  └{'─'*78}┘")

    any_depth = any(v > 0 for v in depth_results.values()) if depth_results else False
    print(f"\n  reqMktDepth: {'✓ ORDER BOOK DATA AVAILABLE' if any_depth else '✗ no depth data — Level 2 may not be supported via API'}")

    print(f"\n  {'─'*78}")
    if not pairs:
        print("  VERDICT: ⚠  Connected + data flows BUT no complete YES+NO pairs found")
        print(f"           Symbols returning data: {sorted(set(r.symbol for r in live))}")
    elif not atm_pairs:
        print("  VERDICT: ⚠  Pairs found but none near-ATM right now (all deep ITM/OTM)")
        print("           Run again during market hours or check different strikes")
    else:
        print(f"  VERDICT: ✓  {len(atm_pairs)} near-ATM pairs ready → paste WATCH_CONTRACTS into kill_shot.py")
    print(f"  {'─'*78}\n")


async def main():
    print("\n" + "="*80)
    print("  ForecastBot Phase 0 — Broad Contract Discovery")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  {IBKR_HOST}:{IBKR_PORT}")
    print("="*80)

    ib = IB()
    print(f"\n  Connecting to {IBKR_HOST}:{IBKR_PORT} ...")
    try:
        await ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID)
    except Exception as e:
        print(f"\n  ✗ CONNECTION FAILED: {e}")
        print(f"  Port {IBKR_PORT} — try 4001 (live Gateway), 7497 (paper), 7496 (live TWS)")
        sys.exit(1)

    print("  ✓ Connected\n")
    try:
        records = await scan_all_symbols(ib)
        if not records:
            print("\n  ✗ Zero contracts found on FORECASTX.")
            print("  Client Portal → Settings → Trading Permissions → Event Contracts")
            return
        depth_results = await test_depth(ib, records)
        print_results(records, depth_results)
    finally:
        ib.disconnect()
        print("  Disconnected.")

if __name__ == "__main__":
    asyncio.run(main())
