"""
what_exists.py — Quick probe
Finds every ForecastEx AND CME Event Contract on this account.
Uses correct secType per contract type:
  ForecastEx → secType="OPT",  exchange="FORECASTX"
  CME Event  → secType="FOP",  exchange="CME"/"COMEX"/"CBOT"/"NYMEX"
"""
import asyncio
import os
import sys
from ib_async import IB, Contract
from dotenv import load_dotenv

load_dotenv()

IBKR_HOST      = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT", "4001"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "11"))

# ── ForecastEx contracts ───────────────────────────────────────────────
# secType="OPT", exchange="FORECASTX"
# Symbol = IBKR's internal product code
FORECASTX_CANDIDATES = [
    "FF",       # Fed Funds Rate        ← confirmed working
    "METLS",    # Silver                ← confirmed 368 contracts
    "CPI",      "USCPI",
    "UR",       "UNEMP",
    "IC",       "NFP",
    "GDP",      "PCE",
    "RS",       "HS",
    "TEMP",     "CO2",
    "PRES",     "ELECTION",
]

# ── CME Event Contracts ────────────────────────────────────────────────
# secType="FOP", exchange varies by product
# Trading class = "EC" + futures symbol (e.g. "ECES", "ECBT", "ECGC")
# Per IBKR docs: "Trading Class prefixed with 'EC' and followed by
# the symbol of the relevant futures product"
CME_CANDIDATES = [
    # (symbol, exchange, tradingClass, description)
    ("ES",   "CME",   "ECES",  "S&P 500"),
    ("NQ",   "CME",   "ECNQ",  "Nasdaq"),
    ("YM",   "CBOT",  "ECYM",  "Dow Jones"),
    ("BTC",  "CME",   "ECBT",  "Bitcoin"),
    ("ETH",  "CME",   "ECET",  "Ethereum"),
    ("GC",   "COMEX", "ECGC",  "Gold"),
    ("SI",   "COMEX", "ECSI",  "Silver"),
    ("CL",   "NYMEX", "ECCL",  "Crude Oil"),
    ("NG",   "NYMEX", "ECNG",  "Natural Gas"),
    ("6E",   "CME",   "EC6E",  "EUR/USD"),
    ("6J",   "CME",   "EC6J",  "JPY/USD"),
    ("6B",   "CME",   "EC6B",  "GBP/USD"),
]


async def try_contract(ib, symbol, sec_type, exchange, trading_class=""):
    c = Contract()
    c.symbol   = symbol
    c.secType  = sec_type
    c.exchange = exchange
    c.currency = "USD"
    if trading_class:
        c.tradingClass = trading_class
    try:
        details = await ib.reqContractDetailsAsync(c)
        return details
    except:
        return []


async def main():
    print(f"\n{'='*65}")
    print(f"  ForecastEx + CME Event Contracts — Full Universe Probe")
    print(f"{'='*65}\n")

    ib = IB()
    try:
        await ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID)
    except Exception as e:
        print(f"  ✗ Connection failed: {e}")
        sys.exit(1)
    print("  ✓ Connected\n")

    found = {}

    # ── Scan ForecastEx ───────────────────────────────────────────────
    print("  ── ForecastEx (secType=OPT, exchange=FORECASTX) ──")
    for symbol in FORECASTX_CANDIDATES:
        details = await try_contract(ib, symbol, "OPT", "FORECASTX")
        if details:
            expiries = sorted(set(d.contract.lastTradeDateOrContractMonth for d in details))
            strikes  = sorted(set(d.contract.strike for d in details))
            found[symbol] = {"type": "FORECASTX", "count": len(details),
                             "expiries": expiries, "strikes": strikes}
            print(f"  ✓ {symbol:<10} {len(details):>4} contracts  "
                  f"expiries: {expiries[0]}→{expiries[-1]}  "
                  f"strikes: {strikes[:3]}")
        else:
            print(f"  - {symbol}")
        await asyncio.sleep(0.1)

    # ── Scan CME Event Contracts ──────────────────────────────────────
    print(f"\n  ── CME Event Contracts (secType=FOP, exchange=CME/COMEX/etc) ──")
    for symbol, exchange, tclass, desc in CME_CANDIDATES:
        details = await try_contract(ib, symbol, "FOP", exchange, tclass)
        if details:
            expiries = sorted(set(d.contract.lastTradeDateOrContractMonth for d in details))
            strikes  = sorted(set(d.contract.strike for d in details))
            found[f"{symbol}@{exchange}"] = {
                "type": f"CME({exchange})", "count": len(details),
                "expiries": expiries, "strikes": strikes
            }
            print(f"  ✓ {symbol:<6} ({desc:<12}) {len(details):>4} contracts  "
                  f"expiries: {expiries[0]}→{expiries[-1]}  "
                  f"strikes: {strikes[:3]}")
        else:
            print(f"  - {symbol:<6} ({desc})")
        await asyncio.sleep(0.1)

    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"  SUMMARY — {len(found)} contract types accessible on this account")
    print(f"{'='*65}")
    for key, info in found.items():
        print(f"  {key:<15} {info['type']:<15} {info['count']:>4} contracts")

    if not found:
        print("  NONE found.")
    else:
        forecastx = [k for k,v in found.items() if v['type'] == 'FORECASTX']
        cme       = [k for k,v in found.items() if 'CME' in v['type']]
        print(f"\n  ForecastEx contracts: {forecastx}")
        print(f"  CME Event contracts:  {cme}")
        if cme:
            print(f"\n  ✓ CME contracts accessible — BTC/ES/Gold strategy may be viable!")
        else:
            print(f"\n  ✗ No CME contracts — Canadian CIRO block confirmed for CME products")

    ib.disconnect()
    print("\n  Done.")

if __name__ == "__main__":
    asyncio.run(main())
