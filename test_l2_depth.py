"""Quick test: does ForecastEx support reqMktDepth (L2 market depth)?
Run with IB Gateway connected at 127.0.0.1:4001.
"""
import asyncio
from ib_async import IB, Contract
from dotenv import load_dotenv
import os

load_dotenv()

async def main():
    ib = IB()
    client_id = 59  # separate from condor bot (57) and other bots
    print(f"Connecting to IB Gateway (clientId={client_id})...")
    await ib.connectAsync("127.0.0.1", 4001, clientId=client_id)
    print("Connected.\n")

    # Discover an active UHAUS contract
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("US/Eastern")

    # Try today first
    for day_offset in range(0, 2):
        try_date = datetime.now(ET) + __import__('datetime').timedelta(days=day_offset)
        try_str = try_date.strftime("%Y%m%d")
        c = Contract()
        c.symbol = "UHAUS"
        c.secType = "OPT"
        c.exchange = "FORECASTX"
        c.currency = "USD"
        c.lastTradeDateOrContractMonth = try_str
        details = await ib.reqContractDetailsAsync(c)
        if not details:
            continue
        d0 = details[0]
        trading_hrs = getattr(d0, 'tradingHours', '') or ''
        if not trading_hrs:
            print(f"  UHAUS exp={try_str} — settled, skipping")
            continue
        print(f"  UHAUS exp={try_str} — active (tradingHours present)")

        # Pick a strike near the middle
        strikes = set()
        for d in details:
            strikes.add(float(d.contract.strike))
        strikes = sorted(strikes)
        mid_strike = strikes[len(strikes) // 2]
        print(f"  Strikes: {strikes}")
        print(f"  Testing L2 on strike {mid_strike}")

        # Find YES (Call) contract for that strike
        for d in details:
            if float(d.contract.strike) == mid_strike and d.contract.right == "C":
                test_contract = d.contract
                break

        print(f"\n  Contract: {test_contract}")
        print(f"  Trying reqMktDepth (L2)...")

        try:
            # Request market depth — numRows=10 gives up to 10 levels
            depth_ticker = ib.reqMktDepth(test_contract, numRows=10)
            print(f"  reqMktDepth returned: {type(depth_ticker)}")

            # Wait for data to arrive
            for i in range(10):
                await asyncio.sleep(1)
                dom = depth_ticker.domAsks
                dom_bids = depth_ticker.domBids
                if dom or dom_bids:
                    print(f"\n  L2 DATA RECEIVED after {i+1}s!")
                    print(f"  Ask levels ({len(dom)}):")
                    for level in dom:
                        print(f"    Price: ${level.price:.2f}  Size: {level.size}  MM: {level.marketMaker}")
                    print(f"  Bid levels ({len(dom_bids)}):")
                    for level in dom_bids:
                        print(f"    Price: ${level.price:.2f}  Size: {level.size}  MM: {level.marketMaker}")
                    break
                else:
                    print(f"  Waiting... ({i+1}s) asks={len(dom)} bids={len(dom_bids)}")

            if not depth_ticker.domAsks and not depth_ticker.domBids:
                print("\n  NO L2 DATA after 10s — ForecastEx may not support reqMktDepth")

            # Cancel depth subscription
            ib.cancelMktDepth(test_contract)

        except Exception as e:
            print(f"  reqMktDepth FAILED: {e}")
            print(f"  ForecastEx likely does not support L2 depth")

        # Also try L1 for comparison
        print(f"\n  L1 (reqMktData) for comparison:")
        ticker = ib.reqMktData(test_contract, snapshot=False)
        await asyncio.sleep(3)
        print(f"    Ask: ${ticker.ask}  AskSize: {ticker.askSize}")
        print(f"    Bid: ${ticker.bid}  BidSize: {ticker.bidSize}")
        ib.cancelMktData(test_contract)

        break

    ib.disconnect()
    print("\nDone.")

asyncio.run(main())
