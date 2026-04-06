"""
depth_finder.py — ForecastBot Depth Finder v1.0
=================================================
PURPOSE:
  Map the order book depth for UHLAX (LA daily high temperature) contracts.
  Answers the Phase 0 decision matrix question: "How deep is the book?"

  The decision matrix needs:
    avg_depth = average min(yes_depth, no_depth) across gap events
  This bot continuously logs depth data to build that picture.

APPROACH:
  1. Discover UHLAX contracts (weather prediction market)
  2. Subscribe to Level 1 data (reqMktData) — best bid/ask + sizes
  3. Attempt Level 2 data (reqMktDepth) — full order book if supported
  4. Log depth snapshots every poll cycle
  5. Aggregate stats: contracts with depth, average depth, max depth

CONTRACT UNIVERSE:
  UHLAX  — LA daily high temp    (OPT/FORECASTX)

OUTPUT:
  data/depth_snapshots.csv  — every depth reading per contract per poll
  data/depth_l2.csv         — Level 2 order book rows (if supported)
  data/depth_summary.csv    — daily aggregated depth stats

RUN:
  python3 depth_finder.py
  Runs alongside kill_shot.py and weather_edge.py (uses clientId=55)

REQUIRES:
  pip install ib_async python-dotenv requests
  IB Gateway running, port 4001
"""

import asyncio
import csv
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from ib_async import IB, Contract, Ticker

load_dotenv()

# ─── LOGGING ──────────────────────────────────────────────────────────────────
LOG_DIR = os.getenv("LOG_DIR", "./data")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, "depth_finder.log"), mode="a"),
    ],
)
log = logging.getLogger("depth_finder")

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
IBKR_HOST      = os.getenv("IBKR_HOST",              "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT",          "4001"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID_DEPTH", "55"))

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")

# Poll interval
POLL_INTERVAL_SEC = 30       # snapshot every 30s
WARMUP_SEC        = 20       # wait after subscribing for prices to arrive
L2_DEPTH_ROWS     = 10       # request up to 10 rows of Level 2 depth

# Reconnect
MAX_RECONNECT_ATTEMPTS = 10
RECONNECT_DELAY_SEC    = 30

# CSV paths
SNAPSHOT_CSV = os.path.join(LOG_DIR, "depth_snapshots.csv")
L2_CSV       = os.path.join(LOG_DIR, "depth_l2.csv")
SUMMARY_CSV  = os.path.join(LOG_DIR, "depth_summary.csv")


# ─── CONTRACT UNIVERSE ───────────────────────────────────────────────────────

ALL_SYMBOLS = [
    {
        "name": "UHLAX",  "label": "LA Daily High Temp",
        "symbol": "UHLAX", "secType": "OPT", "exchange": "FORECASTX",
        "tradingClass": "", "currency": "USD",
        "daily": True, "max_pairs": 20,
    },
]


# ─── COLORS ──────────────────────────────────────────────────────────────────

class C:
    """ANSI color codes for terminal output (white/light macOS terminal)."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[38;5;242m"
    RED     = "\033[38;5;160m"
    GREEN   = "\033[38;5;28m"
    YELLOW  = "\033[38;5;166m"
    BLUE    = "\033[38;5;25m"
    MAGENTA = "\033[38;5;127m"
    CYAN    = "\033[38;5;30m"
    WHITE   = "\033[30m"
    HEADER  = "\033[38;5;25m"
    VALUE   = "\033[1m"
    LABEL   = "\033[38;5;242m"
    OK      = "\033[38;5;28m"
    WARN    = "\033[38;5;166m"
    ALERT   = "\033[38;5;160m\033[1m"
    DEPTH   = "\033[38;5;127m"


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"  Telegram: {e}")


# ─── DATA STRUCTURES ────────────────────────────────────────────────────────

@dataclass
class DepthPair:
    """One YES+NO pair being tracked for depth."""
    symbol: str
    label: str
    strike: float
    expiry: str
    sec_type: str
    yes_con: Contract
    no_con: Contract
    daily: bool = False

    # Level 1 tickers (from reqMktData)
    yes_ticker: Optional[Ticker] = None
    no_ticker: Optional[Ticker] = None

    # Level 2 order book (from reqMktDepth)
    l2_yes: list = field(default_factory=list)  # [(price, size), ...]
    l2_no: list = field(default_factory=list)
    l2_supported: Optional[bool] = None  # None = untested

    @property
    def pair_id(self) -> str:
        return f"{self.symbol}_{self.expiry}_K{self.strike:.0f}"

    def read_l1(self):
        """Read Level 1 depth from tickers.
        Returns (yes_bid, yes_ask, yes_bid_size, yes_ask_size,
                 no_bid, no_ask, no_bid_size, no_ask_size)."""
        def _read_ticker(t):
            if t is None:
                return -1.0, -1.0, 0, 0
            bid = float(t.bid) if t.bid is not None and t.bid > 0 else -1.0
            ask = float(t.ask) if t.ask is not None and t.ask > 0 else -1.0
            bid_sz = int(t.bidSize) if t.bidSize is not None else 0
            ask_sz = int(t.askSize) if t.askSize is not None else 0
            return bid, ask, bid_sz, ask_sz

        yb, ya, ybs, yas = _read_ticker(self.yes_ticker)
        nb, na, nbs, nas = _read_ticker(self.no_ticker)
        return yb, ya, ybs, yas, nb, na, nbs, nas

    def has_activity(self) -> bool:
        yb, ya, ybs, yas, nb, na, nbs, nas = self.read_l1()
        return ya > 0 or na > 0 or yb > 0 or nb > 0

    def min_ask_depth(self) -> int:
        """min(yes_ask_size, no_ask_size) — the key metric for arb sizing."""
        _, _, _, yas, _, _, _, nas = self.read_l1()
        if yas > 0 and nas > 0:
            return min(yas, nas)
        return 0

    def sum_ask(self) -> float:
        _, ya, _, _, _, na, _, _ = self.read_l1()
        if ya > 0 and na > 0:
            return round(ya + na, 4)
        return -1.0


@dataclass
class DayStats:
    """Aggregated stats for the day."""
    date: str = ""
    total_polls: int = 0
    total_pairs_seen: int = 0
    pairs_with_depth: int = 0  # at least one side has ask depth > 0
    max_ask_depth_yes: int = 0
    max_ask_depth_no: int = 0
    max_min_depth: int = 0  # max of min(yes_ask_depth, no_ask_depth)
    depth_samples: list = field(default_factory=list)  # list of min_depth values > 0
    l2_supported_count: int = 0
    l2_unsupported_count: int = 0
    # Per-symbol tracking
    symbol_max_depth: dict = field(default_factory=dict)  # symbol → max min_depth


# ─── IB DEPTH SCANNER ───────────────────────────────────────────────────────

class DepthScanner:
    """
    Connects to IB, discovers all ForecastEx contracts,
    subscribes to Level 1 data, and attempts Level 2 depth.
    """

    def __init__(self):
        self.ib: Optional[IB] = None
        self.pairs: list[DepthPair] = []
        self.connected = False
        self.l2_tested = False

    async def connect(self) -> bool:
        try:
            self.ib = IB()
            await self.ib.connectAsync(
                IBKR_HOST, IBKR_PORT,
                clientId=IBKR_CLIENT_ID, timeout=15,
            )
            log.info(f"  IB connected (clientId={IBKR_CLIENT_ID})")
            self.connected = True
            return True
        except Exception as e:
            log.warning(f"  IB connect failed: {e}")
            return False

    async def discover_and_subscribe(self) -> int:
        """Discover all ForecastEx contracts and subscribe to market data.
        Returns total number of pairs subscribed."""
        total = 0

        for sym_def in ALL_SYMBOLS:
            try:
                pairs_found = await self._discover_symbol(sym_def)
                total += pairs_found
            except Exception as e:
                log.warning(f"  {sym_def['name']}: discovery failed — {e}")

        # Wait for prices to arrive
        if total > 0:
            log.info(f"  Warming up {WARMUP_SEC}s for {total} pairs…")
            await asyncio.sleep(WARMUP_SEC)

        return total

    async def _discover_symbol(self, sym_def: dict) -> int:
        """Discover UHLAX contracts and subscribe. Returns pair count."""
        name = sym_def["name"]
        max_pairs = sym_def["max_pairs"]

        # UHLAX: measurement date = today, last trade date = tomorrow.
        # Try tomorrow through +5 days to find actively-trading contracts.
        for day_offset in range(1, 6):
            try_date = datetime.now(PT) + timedelta(days=day_offset)
            try_str = try_date.strftime("%Y%m%d")

            c = Contract()
            c.symbol = sym_def["symbol"]
            c.secType = sym_def["secType"]
            c.exchange = sym_def["exchange"]
            c.currency = sym_def["currency"]
            if sym_def.get("tradingClass"):
                c.tradingClass = sym_def["tradingClass"]
            c.lastTradeDateOrContractMonth = try_str

            details = await self.ib.reqContractDetailsAsync(c)
            if not details:
                log.info(f"  {name}: no contracts for {try_str}, trying next day…")
                continue

            count = self._subscribe_from_details(
                details, name, sym_def["label"], try_str,
                sym_def["secType"], True, max_pairs,
            )
            if count > 0:
                log.info(f"  {name}: {count} pairs (exp={try_str})")
                return count

        log.info(f"  {name}: no contracts found")
        return 0

    def _subscribe_from_details(self, details, name, label, expiry,
                                 sec_type, daily, max_pairs) -> int:
        """Build YES+NO pairs from contract details and subscribe."""
        yes_map = {d.contract.strike: d.contract
                   for d in details if d.contract.right == "C"}
        no_map = {d.contract.strike: d.contract
                  for d in details if d.contract.right == "P"}
        common = sorted(set(yes_map) & set(no_map))

        if not common:
            return 0

        count = 0
        for strike in common:
            yt = self.ib.reqMktData(yes_map[strike], snapshot=False)
            nt = self.ib.reqMktData(no_map[strike], snapshot=False)

            pair = DepthPair(
                symbol=name, label=label, strike=strike,
                expiry=expiry, sec_type=sec_type,
                yes_con=yes_map[strike], no_con=no_map[strike],
                daily=daily,
                yes_ticker=yt, no_ticker=nt,
            )
            self.pairs.append(pair)
            count += 1

        return count

    async def try_l2_depth(self):
        """Attempt Level 2 market depth on a sample of active pairs.
        ForecastEx may or may not support this — we test and log the result."""
        if self.l2_tested:
            return

        self.l2_tested = True
        active = [p for p in self.pairs if p.has_activity()]
        if not active:
            log.info("  L2 depth: no active pairs to test")
            return

        # Test on first active pair
        test_pair = active[0]
        log.info(f"  L2 depth: testing on {test_pair.pair_id}…")

        try:
            depth_data = self.ib.reqMktDepth(
                test_pair.yes_con, numRows=L2_DEPTH_ROWS,
            )
            await asyncio.sleep(3)  # wait for depth data

            # Check if we got any depth updates
            if hasattr(depth_data, 'domBids') and depth_data.domBids:
                test_pair.l2_supported = True
                log.info(f"  L2 depth: SUPPORTED — got {len(depth_data.domBids)} bid rows")
            elif hasattr(depth_data, 'domAsks') and depth_data.domAsks:
                test_pair.l2_supported = True
                log.info(f"  L2 depth: SUPPORTED — got {len(depth_data.domAsks)} ask rows")
            else:
                test_pair.l2_supported = False
                log.info("  L2 depth: NOT SUPPORTED (no rows returned)")
                # Cancel depth subscription
                self.ib.cancelMktDepth(test_pair.yes_con)
                return

            # If supported, subscribe to L2 for all active pairs
            if test_pair.l2_supported:
                for pair in active[1:]:
                    try:
                        self.ib.reqMktDepth(pair.yes_con, numRows=L2_DEPTH_ROWS)
                        self.ib.reqMktDepth(pair.no_con, numRows=L2_DEPTH_ROWS)
                        pair.l2_supported = True
                    except Exception:
                        pair.l2_supported = False

                log.info(f"  L2 depth: subscribed to {len(active)} pairs")

        except Exception as e:
            test_pair.l2_supported = False
            log.info(f"  L2 depth: NOT SUPPORTED — {e}")

    def read_all_depth(self) -> list[dict]:
        """Read current depth snapshot from all pairs.
        Returns list of dicts with depth data."""
        snapshots = []
        now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")

        for pair in self.pairs:
            yb, ya, ybs, yas, nb, na, nbs, nas = pair.read_l1()
            sum_ask = pair.sum_ask()
            min_depth = pair.min_ask_depth()

            # Read L2 depth if available
            l2_yes_levels = 0
            l2_no_levels = 0
            l2_yes_total_size = 0
            l2_no_total_size = 0

            if pair.l2_supported and pair.yes_ticker:
                # L2 data is attached to the ticker via domBids/domAsks
                yt = pair.yes_ticker
                if hasattr(yt, 'domAsks') and yt.domAsks:
                    l2_yes_levels = len(yt.domAsks)
                    l2_yes_total_size = sum(d.size for d in yt.domAsks)
            if pair.l2_supported and pair.no_ticker:
                nt = pair.no_ticker
                if hasattr(nt, 'domAsks') and nt.domAsks:
                    l2_no_levels = len(nt.domAsks)
                    l2_no_total_size = sum(d.size for d in nt.domAsks)

            snapshots.append({
                "timestamp_et": now_et,
                "symbol": pair.symbol,
                "label": pair.label,
                "strike": pair.strike,
                "expiry": pair.expiry,
                "pair_id": pair.pair_id,
                # Level 1
                "yes_bid": yb, "yes_ask": ya,
                "yes_bid_size": ybs, "yes_ask_size": yas,
                "no_bid": nb, "no_ask": na,
                "no_bid_size": nbs, "no_ask_size": nas,
                "sum_ask": sum_ask,
                "min_ask_depth": min_depth,
                # Level 2 summary
                "l2_yes_levels": l2_yes_levels,
                "l2_no_levels": l2_no_levels,
                "l2_yes_total_size": l2_yes_total_size,
                "l2_no_total_size": l2_no_total_size,
                # Activity flag
                "has_activity": pair.has_activity(),
            })

        return snapshots

    def disconnect(self):
        try:
            if self.ib:
                self.ib.disconnect()
                log.info("  IB disconnected.")
        except Exception:
            pass


# ─── CSV LOGGING ─────────────────────────────────────────────────────────────

def init_logs():
    if not os.path.exists(SNAPSHOT_CSV):
        with open(SNAPSHOT_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp_et", "symbol", "label", "strike", "expiry",
                "pair_id",
                "yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size",
                "no_bid", "no_ask", "no_bid_size", "no_ask_size",
                "sum_ask", "min_ask_depth",
                "l2_yes_levels", "l2_no_levels",
                "l2_yes_total_size", "l2_no_total_size",
                "has_activity",
            ])
    if not os.path.exists(L2_CSV):
        with open(L2_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp_et", "pair_id", "side", "level",
                "price", "size",
            ])
    if not os.path.exists(SUMMARY_CSV):
        with open(SUMMARY_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "date", "polls", "total_pairs", "active_pairs",
                "pairs_with_ask_depth",
                "max_yes_ask_depth", "max_no_ask_depth",
                "max_min_depth",
                "avg_min_depth", "median_min_depth",
                "symbols_with_depth",
            ])


def write_snapshots(snapshots: list[dict]):
    with open(SNAPSHOT_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        for s in snapshots:
            writer.writerow([
                s["timestamp_et"], s["symbol"], s["label"],
                s["strike"], s["expiry"], s["pair_id"],
                s["yes_bid"], s["yes_ask"],
                s["yes_bid_size"], s["yes_ask_size"],
                s["no_bid"], s["no_ask"],
                s["no_bid_size"], s["no_ask_size"],
                s["sum_ask"], s["min_ask_depth"],
                s["l2_yes_levels"], s["l2_no_levels"],
                s["l2_yes_total_size"], s["l2_no_total_size"],
                "1" if s["has_activity"] else "0",
            ])


def write_daily_summary(stats: DayStats):
    import statistics
    avg_depth = (round(statistics.mean(stats.depth_samples), 1)
                 if stats.depth_samples else 0)
    med_depth = (int(statistics.median(stats.depth_samples))
                 if stats.depth_samples else 0)
    syms = ", ".join(f"{k}={v}" for k, v in sorted(stats.symbol_max_depth.items()))
    with open(SUMMARY_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            stats.date, stats.total_polls, stats.total_pairs_seen,
            stats.pairs_with_depth, stats.pairs_with_depth,
            stats.max_ask_depth_yes, stats.max_ask_depth_no,
            stats.max_min_depth,
            avg_depth, med_depth,
            syms,
        ])


# ─── CONSOLE OUTPUT ─────────────────────────────────────────────────────────

def print_depth_table(snapshots: list[dict], stats: DayStats):
    """Print current depth across all contracts."""
    now_str = datetime.now(ET).strftime("%H:%M:%S ET")

    print(f"\n  {C.HEADER}{'='*72}{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  {C.VALUE}DEPTH FINDER{C.RESET}"
          f"   {C.DIM}{now_str}   poll #{stats.total_polls}{C.RESET}"
          f"  {C.HEADER}|{C.RESET}")
    print(f"  {C.HEADER}{'='*72}{C.RESET}")

    # Group by symbol
    by_symbol = {}
    for s in snapshots:
        by_symbol.setdefault(s["symbol"], []).append(s)

    # Print header
    print(f"\n  {C.HEADER}{'─'*72}{C.RESET}")
    print(f"  {C.WHITE}{'Symbol':<8} {'Strike':>7} {'YES Ask':>8} {'YA Sz':>6}"
          f" {'NO Ask':>8} {'NA Sz':>6} {'Sum':>7} {'MinD':>5}{C.RESET}")
    print(f"  {C.HEADER}{'─'*72}{C.RESET}")

    active_count = 0
    depth_count = 0

    for symbol in ["UHLAX"]:
        if symbol not in by_symbol:
            print(f"  {C.DIM}{symbol:<8} — no contracts found{C.RESET}")
            continue

        pairs = by_symbol[symbol]
        printed_any = False
        for s in sorted(pairs, key=lambda x: x["strike"]):
            if not s["has_activity"]:
                continue

            printed_any = True
            active_count += 1

            ya = s["yes_ask"]
            na = s["no_ask"]
            yas = s["yes_ask_size"]
            nas = s["no_ask_size"]
            sum_ask = s["sum_ask"]
            min_d = s["min_ask_depth"]

            # Format prices
            ya_str = f"${ya:.2f}" if ya > 0 else f"{C.DIM}  —{C.RESET}  "
            na_str = f"${na:.2f}" if na > 0 else f"{C.DIM}  —{C.RESET}  "
            yas_str = f"{yas:>5}" if yas > 0 else f"{C.DIM}    0{C.RESET}"
            nas_str = f"{nas:>5}" if nas > 0 else f"{C.DIM}    0{C.RESET}"

            # Sum coloring
            if sum_ask > 0:
                sum_clr = C.GREEN if sum_ask < 0.95 else C.YELLOW if sum_ask < 1.0 else C.RED
                sum_str = f"{sum_clr}${sum_ask:.2f}{C.RESET}"
            else:
                sum_str = f"{C.DIM}   —{C.RESET}  "

            # Min depth coloring
            if min_d >= 500:
                d_clr = C.GREEN
            elif min_d >= 100:
                d_clr = C.YELLOW
            elif min_d > 0:
                d_clr = C.WARN
            else:
                d_clr = C.DIM
            d_str = f"{d_clr}{min_d:>5}{C.RESET}" if min_d > 0 else f"{C.DIM}    0{C.RESET}"

            if min_d > 0:
                depth_count += 1

            # Strike formatting
            strike = s["strike"]
            if strike >= 1000:
                strike_str = f"{strike:>7.0f}"
            else:
                strike_str = f"{strike:>7.1f}"

            print(f"  {C.VALUE}{symbol:<8}{C.RESET} {strike_str}"
                  f" {ya_str:>8} {yas_str}"
                  f" {na_str:>8} {nas_str}"
                  f" {sum_str:>7} {d_str}")

        if not printed_any:
            print(f"  {C.DIM}{symbol:<8} (subscribed but no live prices){C.RESET}")

    print(f"  {C.HEADER}{'─'*72}{C.RESET}")

    # Summary line
    total = len(snapshots)
    print(f"\n  {C.WHITE}Summary:{C.RESET}"
          f"  {total} pairs subscribed"
          f"  |  {C.VALUE}{active_count}{C.RESET} active"
          f"  |  {C.DEPTH}{depth_count}{C.RESET} with ask depth")

    if stats.depth_samples:
        import statistics
        avg = statistics.mean(stats.depth_samples)
        med = statistics.median(stats.depth_samples)
        print(f"  {C.WHITE}Depth (today):{C.RESET}"
              f"  max min_depth = {C.VALUE}{stats.max_min_depth}{C.RESET}"
              f"  |  avg = {C.VALUE}{avg:.0f}{C.RESET}"
              f"  |  median = {C.VALUE}{med:.0f}{C.RESET}"
              f"  |  samples = {len(stats.depth_samples)}")

    # Per-symbol max depth
    if stats.symbol_max_depth:
        parts = []
        for sym in ["UHLAX"]:
            if sym in stats.symbol_max_depth:
                v = stats.symbol_max_depth[sym]
                clr = C.GREEN if v >= 500 else C.YELLOW if v >= 100 else C.WARN
                parts.append(f"{sym}={clr}{v}{C.RESET}")
        if parts:
            print(f"  {C.WHITE}Max depth by symbol:{C.RESET}  {'  '.join(parts)}")

    # L2 status
    if any(s["l2_yes_levels"] > 0 for s in snapshots):
        l2_pairs = sum(1 for s in snapshots if s["l2_yes_levels"] > 0)
        print(f"  {C.OK}L2 depth available: {l2_pairs} pairs{C.RESET}")


# ─── MAIN ────────────────────────────────────────────────────────────────────

async def main():
    W = 65
    print(f"\n  {C.HEADER}{'='*W}{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  {C.BOLD}{C.WHITE}ForecastBot — Depth Finder v1.0{C.RESET}"
          + " " * (W - 34) + f"{C.HEADER}|{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  {C.DIM}Started: {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}{C.RESET}"
          + " " * (W - 39) + f"{C.HEADER}|{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  Universe: {C.CYAN}UHLAX (LA Daily High Temp){C.RESET}"
          + " " * (W - 33) + f"{C.HEADER}|{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  clientId: {C.VALUE}{IBKR_CLIENT_ID}{C.RESET}"
          f"  |  poll: {C.VALUE}{POLL_INTERVAL_SEC}s{C.RESET}"
          + " " * (W - 32) + f"{C.HEADER}|{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  {C.RED}{C.BOLD}*** OBSERVATION ONLY — NO ORDERS ***{C.RESET}"
          + " " * (W - 38) + f"{C.HEADER}|{C.RESET}")
    print(f"  {C.HEADER}{'='*W}{C.RESET}\n")

    init_logs()

    # ── Connect to IB ──────────────────────────────────────────────────
    scanner = DepthScanner()
    if not await scanner.connect():
        log.critical("  Cannot connect to IB Gateway. Exiting.")
        return

    # ── Discover and subscribe ─────────────────────────────────────────
    log.info("  Discovering ForecastEx contracts…")
    total_pairs = await scanner.discover_and_subscribe()

    if total_pairs == 0:
        log.critical("  No contracts found. Is IB Gateway running? Is market open?")
        scanner.disconnect()
        return

    log.info(f"  Total: {total_pairs} YES+NO pairs across {len(set(p.symbol for p in scanner.pairs))} symbols")

    # ── Test Level 2 depth ─────────────────────────────────────────────
    await scanner.try_l2_depth()

    # ── Send startup notification ──────────────────────────────────────
    symbols = sorted(set(p.symbol for p in scanner.pairs))
    send_telegram(
        f"*Depth Finder v1.0 Started*\n"
        f"Pairs: `{total_pairs}`\n"
        f"Symbols: `{', '.join(symbols)}`\n"
        f"Poll: `{POLL_INTERVAL_SEC}s`\n"
        f"clientId: `{IBKR_CLIENT_ID}`"
    )

    # ── Initialize day stats ───────────────────────────────────────────
    today = datetime.now(ET).strftime("%Y-%m-%d")
    stats = DayStats(date=today)

    log.info(f"\n  Polling every {POLL_INTERVAL_SEC}s… (Ctrl+C to stop)\n")

    try:
        while True:
            now_et = datetime.now(ET)
            today_str = now_et.strftime("%Y-%m-%d")

            # ── Daily rollover ─────────────────────────────────────────
            if today_str != stats.date:
                log.info(f"  Day rollover → {today_str}")
                write_daily_summary(stats)
                send_telegram(
                    f"*Depth Finder Daily Summary — {stats.date}*\n"
                    f"Polls: `{stats.total_polls}`\n"
                    f"Max min_depth: `{stats.max_min_depth}`\n"
                    f"Avg min_depth: `{round(sum(stats.depth_samples)/len(stats.depth_samples), 1) if stats.depth_samples else 0}`\n"
                    f"Depth samples: `{len(stats.depth_samples)}`"
                )
                stats = DayStats(date=today_str)

            # ── Read depth ─────────────────────────────────────────────
            stats.total_polls += 1
            snapshots = scanner.read_all_depth()
            stats.total_pairs_seen = len(snapshots)

            # ── Update stats ───────────────────────────────────────────
            for s in snapshots:
                yas = s["yes_ask_size"]
                nas = s["no_ask_size"]
                min_d = s["min_ask_depth"]
                sym = s["symbol"]

                if yas > 0 or nas > 0:
                    stats.pairs_with_depth += 1
                if yas > stats.max_ask_depth_yes:
                    stats.max_ask_depth_yes = yas
                if nas > stats.max_ask_depth_no:
                    stats.max_ask_depth_no = nas
                if min_d > stats.max_min_depth:
                    stats.max_min_depth = min_d
                if min_d > 0:
                    stats.depth_samples.append(min_d)
                if min_d > stats.symbol_max_depth.get(sym, 0):
                    stats.symbol_max_depth[sym] = min_d

            # ── Log to CSV ─────────────────────────────────────────────
            write_snapshots(snapshots)

            # ── Print console ──────────────────────────────────────────
            print_depth_table(snapshots, stats)

            print(f"\n  {C.DIM}Next poll in {POLL_INTERVAL_SEC}s…{C.RESET}")

            await asyncio.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        log.info("\n  Stopped by user.")
    except Exception as e:
        log.critical(f"\n  FATAL: {e}\n{traceback.format_exc()}")
        send_telegram(f"*Depth Finder CRASHED*\n`{str(e)[:200]}`")
    finally:
        write_daily_summary(stats)
        scanner.disconnect()

        print(f"\n  {C.HEADER}{'='*65}{C.RESET}")
        print(f"  {C.HEADER}|{C.RESET}  {C.BOLD}{C.WHITE}SESSION SUMMARY{C.RESET}"
              + " " * 48 + f"{C.HEADER}|{C.RESET}")
        print(f"  {C.HEADER}{'='*65}{C.RESET}")
        print(f"  {C.DIM}Date:{C.RESET}               {stats.date}")
        print(f"  {C.DIM}Polls:{C.RESET}              {stats.total_polls}")
        print(f"  {C.DIM}Pairs tracked:{C.RESET}      {stats.total_pairs_seen}")
        print(f"  {C.DIM}Max YES ask depth:{C.RESET}  {C.DEPTH}{stats.max_ask_depth_yes}{C.RESET}")
        print(f"  {C.DIM}Max NO ask depth:{C.RESET}   {C.DEPTH}{stats.max_ask_depth_no}{C.RESET}")
        print(f"  {C.DIM}Max min(Y,N) depth:{C.RESET} {C.VALUE}{stats.max_min_depth}{C.RESET}")
        if stats.depth_samples:
            import statistics
            print(f"  {C.DIM}Avg min depth:{C.RESET}     {statistics.mean(stats.depth_samples):.1f}")
            print(f"  {C.DIM}Median min depth:{C.RESET}  {statistics.median(stats.depth_samples):.0f}")
            print(f"  {C.DIM}Depth samples:{C.RESET}     {len(stats.depth_samples)}")
        if stats.symbol_max_depth:
            print(f"  {C.DIM}By symbol:{C.RESET}")
            for sym in ["UHLAX"]:
                if sym in stats.symbol_max_depth:
                    print(f"    {sym:<8} max min_depth = {stats.symbol_max_depth[sym]}")
        print(f"\n  {C.DIM}Data:{C.RESET} {SNAPSHOT_CSV}")
        print(f"  {C.DIM}Summary:{C.RESET} {SUMMARY_CSV}")
        print(f"  {C.HEADER}{'='*65}{C.RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
