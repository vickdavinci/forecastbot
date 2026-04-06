"""
kill_shot.py — ForecastBot Phase 0  v2.0
==========================================
PURPOSE:
  Streaming parity gap detector for YES+NO ForecastEx contracts.
  Fires on EVERY price tick — not on a timer.
  Catches gaps that open and close in 10-60 seconds.

UNIVERSE (7 contracts):
  Daily (refreshed every morning):
    CBBTC  — BTC daily close price         (OPT/FORECASTX)
    METLS  — Silver daily price            (OPT/FORECASTX)
    FES    — S&P 500 daily futures price   (FOP/FORECASTX)

  Monthly / Long-dated (persistent):
    FF     — Fed Decision                  (OPT/FORECASTX)
    YXHBT  — Bitcoin Highest Price 2026    (OPT/FORECASTX)
    PNFED  — Presidential Fed Chair       (OPT/FORECASTX)
    JPDEC  — Bank of Japan Decision       (OPT/FORECASTX)

ARCHITECTURE:
  - Event-driven streaming (not polling)
  - Daily contracts auto-refreshed at 09:31 ET every morning
  - Auto-reconnect on IB Gateway disconnect (up to 10 attempts)
  - Data quality validation before any gap is logged
  - Price must be CONFIRMED VALID (bid > 0 AND ask > 0 AND bid < ask)
  - Gap only logged when BOTH legs have valid confirmed prices
  - Minimum 3 consecutive ticks confirming gap before alert fires
    (prevents false positives from single bad tick)

GAP ECONOMICS:
  Exchange fee:   $0.01 per contract pair
  Breakeven sum:  YES ask + NO ask < $0.99
  Profitable sum: YES ask + NO ask < $0.93  (gap > $0.07)
  We log ALL gaps below $0.99, alert on gaps below $0.93

OUTPUT:
  data/all_ticks.csv       — every tick where both legs have valid data
  data/gap_events.csv      — every gap open/close (below $0.99 breakeven)
  data/gap_alerts.csv      — profitable gaps only (below $0.93)
  data/daily_summary.csv   — daily stats
  data/data_quality.csv    — per-pair data quality report
  Telegram alerts          — real-time on profitable gaps

DECISION MATRIX (30 days):
  ≥5 profitable gaps/week + depth ≥500 → build execution engine
  2-4 profitable gaps/week + depth ≥200 → build light execution engine
  <2 profitable gaps/week               → thesis not confirmed

RUN:
  python3 kill_shot.py

REQUIRES:
  pip install ib_async python-dotenv requests
  IB Gateway running, port 4001
  .env file configured (see .env.example)
"""

import asyncio
import csv
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional

import requests
from dotenv import load_dotenv
from ib_async import IB, Contract, Ticker

load_dotenv()

# ─── LOGGING SETUP ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("./data/kill_shot.log", mode="a"),
    ],
)
log = logging.getLogger("kill_shot")

ET = ZoneInfo("America/New_York")

# ─── CONFIG ────────────────────────────────────────────────────────────────────
IBKR_HOST       = os.getenv("IBKR_HOST",      "127.0.0.1")
IBKR_PORT       = int(os.getenv("IBKR_PORT",  "4001"))
IBKR_CLIENT_ID  = int(os.getenv("IBKR_CLIENT_ID", "40"))

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")

# Gap thresholds
BREAKEVEN_SUM    = 0.99   # Log all gaps where sum < this (after $0.01 fee)
ALERT_SUM        = 0.93   # Telegram alert only when sum < this (gap > $0.07)
CONFIRM_TICKS    = 3      # Consecutive ticks required before gap is confirmed
WARMUP_SECONDS   = 25     # Wait for initial prices after subscribing

# Reconnect settings
MAX_RECONNECT_ATTEMPTS = 10
RECONNECT_DELAY_SEC    = 30

# Data directories
LOG_DIR     = os.getenv("LOG_DIR", "./data")
TICKS_CSV   = os.path.join(LOG_DIR, "all_ticks.csv")
GAP_CSV     = os.path.join(LOG_DIR, "gap_events.csv")
ALERT_CSV   = os.path.join(LOG_DIR, "gap_alerts.csv")
DAILY_CSV   = os.path.join(LOG_DIR, "daily_summary.csv")
QUALITY_CSV = os.path.join(LOG_DIR, "data_quality.csv")


# ─── CONTRACT UNIVERSE ─────────────────────────────────────────────────────────

DAILY_SYMBOLS = [
    {
        "name":         "CBBTC",
        "label":        "BTC Daily Close",
        "symbol":       "CBBTC",
        "secType":      "OPT",
        "exchange":     "FORECASTX",
        "tradingClass": "CBBTC",
        "currency":     "USD",
        "daily":        True,
        "max_pairs":    10,
        "catalyst":     "BTC_MOVE",
    },
    {
        "name":         "METLS",
        "label":        "Silver Daily Price",
        "symbol":       "METLS",
        "secType":      "OPT",
        "exchange":     "FORECASTX",
        "tradingClass": "METLS",
        "currency":     "USD",
        "daily":        True,
        "max_pairs":    10,
        "catalyst":     "SILVER_MOVE",
    },
    {
        "name":         "FES",
        "label":        "S&P 500 Daily Price",
        "symbol":       "FES",
        "secType":      "FOP",
        "exchange":     "FORECASTX",
        "tradingClass": "FES",
        "currency":     "USD",
        "daily":        True,
        "max_pairs":    10,
        "catalyst":     "SP500_MOVE",
    },
    # UHLAX removed — handled by weather_edge.py with dedicated analysis
]

PERSISTENT_SYMBOLS = [
    {
        "name":         "FF",
        "label":        "Fed Decision",
        "symbol":       "FF",
        "secType":      "OPT",
        "exchange":     "FORECASTX",
        "tradingClass": "",
        "currency":     "USD",
        "daily":        False,
        "max_pairs":    5,
        "catalyst":     "FOMC",
    },
    {
        "name":         "YXHBT",
        "label":        "Bitcoin Highest Price 2026",
        "symbol":       "YXHBT",
        "secType":      "OPT",
        "exchange":     "FORECASTX",
        "tradingClass": "",
        "currency":     "USD",
        "daily":        False,
        "max_pairs":    5,
        "catalyst":     "BTC_MOVE",
    },
    {
        "name":         "PNFED",
        "label":        "Presidential Fed Chair",
        "symbol":       "PNFED",
        "secType":      "OPT",
        "exchange":     "FORECASTX",
        "tradingClass": "",
        "currency":     "USD",
        "daily":        False,
        "max_pairs":    5,
        "catalyst":     "POLITICAL",
    },
    {
        "name":         "JPDEC",
        "label":        "Bank of Japan Decision",
        "symbol":       "JPDEC",
        "secType":      "OPT",
        "exchange":     "FORECASTX",
        "tradingClass": "",
        "currency":     "USD",
        "daily":        False,
        "max_pairs":    5,
        "catalyst":     "BOJ",
    },
]

ALL_SYMBOLS = DAILY_SYMBOLS + PERSISTENT_SYMBOLS

# ─── CATALYST CALENDAR ─────────────────────────────────────────────────────────
# (date_str, hour_ET, minute_ET, label)
CATALYST_EVENTS = [
    ("2026-03-06",  8, 30, "NFP Feb 2026"),
    ("2026-03-11",  8, 30, "CPI Feb 2026"),
    ("2026-03-18", 14,  0, "BOJ Mar 2026"),
    ("2026-03-19", 14,  0, "FOMC Mar 2026"),
    ("2026-03-28",  8, 30, "PCE Feb 2026"),
    ("2026-04-03",  8, 30, "NFP Mar 2026"),
    ("2026-04-10",  8, 30, "CPI Mar 2026"),
    ("2026-04-16",  8, 30, "Retail Sales Mar 2026"),
    ("2026-04-29",  8, 30, "PCE Mar 2026"),
    ("2026-05-07", 14,  0, "FOMC May 2026"),
    ("2026-05-08",  8, 30, "NFP Apr 2026"),
]
CATALYST_WINDOW_HOURS = 2


# ─── COLORS (256-color palette — optimised for white/light macOS terminal) ────

class C:
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


# ─── DATA STRUCTURES ───────────────────────────────────────────────────────────

@dataclass
class PricePoint:
    """Single validated price snapshot for one contract."""
    bid:   float = -1.0
    ask:   float = -1.0
    size:  int   =  0
    ts:    float = field(default_factory=time.time)

    @property
    def valid(self) -> bool:
        return (
            self.bid > 0 and
            self.ask > 0 and
            self.ask >= self.bid and
            self.ask <= 1.00 and
            self.bid >= 0.01
        )


@dataclass
class Pair:
    """One YES+NO pair for a single strike."""
    symbol:      str
    label:       str
    catalyst:    str
    strike:      float
    expiry:      str
    yes_con:     Contract
    no_con:      Contract
    daily:       bool = False

    # Live tickers from IB
    yes_ticker:  Optional[Ticker] = None
    no_ticker:   Optional[Ticker] = None

    # Gap confirmation (require N consecutive ticks before alerting)
    _confirm_count:  int   = 0
    _gap_confirmed:  bool  = False

    # Gap state
    gap_open:        bool             = False
    gap_open_time:   Optional[float]  = None   # unix timestamp
    gap_open_sum:    float            = 0.0
    gap_open_leg:    str              = ""
    gap_peak_profit: float            = 0.0

    # Data quality tracking
    tick_total:      int   = 0
    tick_valid:      int   = 0   # both legs had valid prices
    tick_invalid:    int   = 0   # at least one leg had -1

    # Stats
    total_gaps:         int   = 0
    total_gap_seconds:  float = 0.0
    max_gap:            float = 0.0
    max_depth:          int   = 0

    @property
    def pair_id(self) -> str:
        return f"{self.symbol}_{self.expiry}_K{self.strike:.0f}"

    @property
    def pair_label(self) -> str:
        return f"{self.symbol} {self.expiry} K{self.strike:.0f}"

    def _get_price(self, ticker: Optional[Ticker]) -> PricePoint:
        if ticker is None:
            return PricePoint()
        bid  = float(ticker.bid)  if ticker.bid  and ticker.bid  > 0 else -1.0
        ask  = float(ticker.ask)  if ticker.ask  and ticker.ask  > 0 else -1.0
        size = int(ticker.askSize) if ticker.askSize else 0
        return PricePoint(bid=bid, ask=ask, size=size)

    def yes_price(self) -> PricePoint:
        return self._get_price(self.yes_ticker)

    def no_price(self) -> PricePoint:
        return self._get_price(self.no_ticker)

    def both_valid(self) -> bool:
        return self.yes_price().valid and self.no_price().valid

    def sum_ask(self) -> float:
        yp, np_ = self.yes_price(), self.no_price()
        if yp.valid and np_.valid:
            return round(yp.ask + np_.ask, 4)
        return -1.0

    def gap(self) -> float:
        s = self.sum_ask()
        return round(1.0 - s, 4) if s > 0 else -99.0

    def min_depth(self) -> int:
        yp, np_ = self.yes_price(), self.no_price()
        return min(yp.size, np_.size)

    def max_profit_at_gap(self) -> float:
        g = self.gap()
        d = self.min_depth()
        if g > 0 and d > 0:
            return round(g * d, 2)
        return 0.0

    def lagging_leg(self) -> str:
        """
        Which leg is stale?
        The stale leg is the one whose mid-price is closer to 0.50
        (hasn't repriced away from 50/50 while the other has moved).
        """
        yp, np_ = self.yes_price(), self.no_price()
        if not yp.valid or not np_.valid:
            return "UNKNOWN"
        yes_mid = (yp.bid + yp.ask) / 2
        no_mid  = (np_.bid + np_.ask) / 2
        if abs(yes_mid - 0.5) < abs(no_mid - 0.5):
            return "YES_LAGGING"
        return "NO_LAGGING"

    def data_quality_pct(self) -> float:
        if self.tick_total == 0:
            return 0.0
        return round(100.0 * self.tick_valid / self.tick_total, 1)


# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

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
        log.warning(f"Telegram failed: {e}")


# ─── CSV LOGGING ───────────────────────────────────────────────────────────────

def init_logs() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)

    # all_ticks.csv — every tick where both legs valid
    if not os.path.exists(TICKS_CSV):
        with open(TICKS_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp_et", "unix_ts", "pair_id", "symbol", "expiry",
                "strike", "catalyst", "mode",
                "yes_bid", "yes_ask", "yes_depth",
                "no_bid",  "no_ask",  "no_depth",
                "sum_ask", "gap", "below_breakeven", "below_alert",
                "lagging_leg", "max_profit",
            ])

    # gap_events.csv — every gap open/close (below breakeven $0.99)
    if not os.path.exists(GAP_CSV):
        with open(GAP_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "event_type", "timestamp_et", "unix_ts",
                "pair_id", "symbol", "expiry", "strike",
                "catalyst", "mode", "gap", "sum_ask",
                "yes_ask", "no_ask",
                "yes_depth", "no_depth", "min_depth",
                "lagging_leg", "max_profit",
                "duration_seconds",   # only on CLOSE
                "peak_profit",        # only on CLOSE
            ])

    # gap_alerts.csv — profitable gaps only (below alert threshold $0.93)
    if not os.path.exists(ALERT_CSV):
        with open(ALERT_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "event_type", "timestamp_et", "pair_id",
                "gap", "sum_ask", "min_depth", "max_profit",
                "lagging_leg", "duration_seconds",
            ])

    # daily_summary.csv
    if not os.path.exists(DAILY_CSV):
        with open(DAILY_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "date", "tick_count", "valid_ticks",
                "breakeven_gaps", "alert_gaps",
                "best_gap", "best_gap_symbol",
                "avg_gap_duration_sec", "max_depth",
                "max_profit",
            ])

    # data_quality.csv — written at shutdown
    if not os.path.exists(QUALITY_CSV):
        with open(QUALITY_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "pair_id", "symbol", "expiry", "strike",
                "tick_total", "tick_valid", "tick_invalid",
                "data_quality_pct", "total_gaps", "max_gap",
            ])


def log_tick(pair: Pair, mode: str) -> None:
    now_et   = datetime.now(ET)
    unix_ts  = time.time()
    yp       = pair.yes_price()
    np_      = pair.no_price()
    g        = pair.gap()
    s        = pair.sum_ask()
    mp       = pair.max_profit_at_gap()

    with open(TICKS_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            now_et.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            f"{unix_ts:.3f}",
            pair.pair_id,
            pair.symbol,
            pair.expiry,
            f"{pair.strike:.2f}",
            pair.catalyst,
            mode,
            f"{yp.bid:.4f}", f"{yp.ask:.4f}", yp.size,
            f"{np_.bid:.4f}", f"{np_.ask:.4f}", np_.size,
            f"{s:.4f}",
            f"{g:.4f}",
            "1" if s < BREAKEVEN_SUM else "0",
            "1" if s < ALERT_SUM    else "0",
            pair.lagging_leg(),
            f"{mp:.2f}",
        ])


def log_gap_event(
    event_type: str,
    pair: Pair,
    mode: str,
    duration_sec: float = 0.0,
) -> None:
    now_et  = datetime.now(ET)
    unix_ts = time.time()
    yp      = pair.yes_price()
    np_     = pair.no_price()
    g       = pair.gap()
    s       = pair.sum_ask()
    mp      = pair.max_profit_at_gap()
    lag     = pair.lagging_leg()

    with open(GAP_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            event_type,
            now_et.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            f"{unix_ts:.3f}",
            pair.pair_id,
            pair.symbol,
            pair.expiry,
            f"{pair.strike:.2f}",
            pair.catalyst,
            mode,
            f"{g:.4f}",
            f"{s:.4f}",
            f"{yp.ask:.4f}", f"{np_.ask:.4f}",
            yp.size, np_.size, pair.min_depth(),
            lag,
            f"{mp:.2f}",
            f"{duration_sec:.1f}",
            f"{pair.gap_peak_profit:.2f}",
        ])

    # Also write to alert log if profitable
    if s < ALERT_SUM:
        with open(ALERT_CSV, "a", newline="") as f:
            csv.writer(f).writerow([
                event_type,
                now_et.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                pair.pair_id,
                f"{g:.4f}",
                f"{s:.4f}",
                pair.min_depth(),
                f"{mp:.2f}",
                lag,
                f"{duration_sec:.1f}",
            ])


def write_daily_summary(date_str: str, stats: dict, all_pairs: list) -> None:
    total_b_gaps = stats.get("breakeven_gaps", 0)
    total_a_gaps = stats.get("alert_gaps", 0)
    total_gaps   = sum(p.total_gaps for p in all_pairs)
    total_sec    = sum(p.total_gap_seconds for p in all_pairs)
    avg_dur      = (total_sec / total_gaps) if total_gaps > 0 else 0.0

    with open(DAILY_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            date_str,
            stats.get("tick_count",   0),
            stats.get("valid_ticks",  0),
            total_b_gaps,
            total_a_gaps,
            f"{stats.get('best_gap', 0):.4f}",
            stats.get("best_gap_symbol", ""),
            f"{avg_dur:.1f}",
            stats.get("max_depth", 0),
            f"{stats.get('max_profit', 0):.2f}",
        ])

    send_telegram(
        f"📊 *Daily Summary — {date_str}*\n"
        f"Valid ticks: `{stats.get('valid_ticks', 0)}`\n"
        f"Breakeven gaps (sum<$0.99): `{total_b_gaps}`\n"
        f"Profitable gaps (sum<$0.93): `{total_a_gaps}`\n"
        f"Best gap: `${stats.get('best_gap', 0):.4f}` on `{stats.get('best_gap_symbol', '-')}`\n"
        f"Avg gap duration: `{avg_dur:.1f}s`\n"
        f"Max depth at gap: `{stats.get('max_depth', 0)}`\n"
        f"Max profit seen: `${stats.get('max_profit', 0):.2f}`"
    )


def write_quality_report(all_pairs: list) -> None:
    with open(QUALITY_CSV, "a", newline="") as f:
        w = csv.writer(f)
        for p in all_pairs:
            w.writerow([
                p.pair_id, p.symbol, p.expiry, f"{p.strike:.2f}",
                p.tick_total, p.tick_valid, p.tick_invalid,
                f"{p.data_quality_pct():.1f}",
                p.total_gaps, f"{p.max_gap:.4f}",
            ])


# ─── CATALYST MODE ─────────────────────────────────────────────────────────────

def current_mode() -> tuple[str, str]:
    now_et = datetime.now(ET)
    for date_str, hour, minute, label in CATALYST_EVENTS:
        event_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=hour, minute=minute, tzinfo=ET
        )
        window_start = event_dt - timedelta(hours=CATALYST_WINDOW_HOURS)
        window_end   = event_dt + timedelta(hours=CATALYST_WINDOW_HOURS)
        if window_start <= now_et <= window_end:
            return "CATALYST", label
    return "NORMAL", ""


# ─── CONTRACT DISCOVERY ────────────────────────────────────────────────────────

def get_today_et() -> str:
    """Return today's date in ET as YYYYMMDD string."""
    return datetime.now(ET).strftime("%Y%m%d")


def candidate_dates_et() -> list[str]:
    """Return candidate dates to try for daily contracts (today + next 3 days).
    Includes weekends — ForecastEx weather contracts can settle on Sat/Sun."""
    today = datetime.now(ET).date()
    return [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(4)]


async def discover_pairs_for_symbol(
    ib: IB,
    sym_cfg: dict,
    today_str: Optional[str] = None,
) -> list[Pair]:
    """
    Discover YES+NO pairs for one symbol.
    For daily contracts, pass today_str = YYYYMMDD.
    For persistent contracts, leave today_str = None.
    """
    c = Contract()
    c.symbol   = sym_cfg["symbol"]
    c.secType  = sym_cfg["secType"]
    c.exchange = sym_cfg["exchange"]
    c.currency = sym_cfg["currency"]
    if sym_cfg.get("tradingClass"):
        c.tradingClass = sym_cfg["tradingClass"]
    if today_str:
        c.lastTradeDateOrContractMonth = today_str

    try:
        details = await ib.reqContractDetailsAsync(c)
    except Exception as e:
        log.error(f"  Error discovering {sym_cfg['name']}: {e}")
        return []

    if not details:
        log.warning(f"  {sym_cfg['name']}: 0 contracts returned")
        return []

    # Separate YES (C=Call) and NO (P=Put)
    yes_map = {d.contract.strike: d.contract for d in details if d.contract.right == "C"}
    no_map  = {d.contract.strike: d.contract for d in details if d.contract.right == "P"}

    # Find matching pairs
    common_strikes = sorted(set(yes_map.keys()) & set(no_map.keys()))
    if not common_strikes:
        log.warning(f"  {sym_cfg['name']}: no matching YES+NO pairs found")
        return []

    # For daily contracts: select ATM strikes (middle of the available range)
    # For persistent: select all up to max_pairs
    max_p = sym_cfg.get("max_pairs", 10)
    if today_str and len(common_strikes) > max_p:
        # Pick strikes centered on median (ATM area)
        mid_idx = len(common_strikes) // 2
        half    = max_p // 2
        start   = max(0, mid_idx - half)
        end     = min(len(common_strikes), start + max_p)
        common_strikes = common_strikes[start:end]
    else:
        common_strikes = common_strikes[:max_p]

    pairs = []
    for strike in common_strikes:
        yc = yes_map[strike]
        nc = no_map[strike]
        expiry = yc.lastTradeDateOrContractMonth
        pairs.append(Pair(
            symbol   = sym_cfg["name"],
            label    = sym_cfg["label"],
            catalyst = sym_cfg["catalyst"],
            strike   = strike,
            expiry   = expiry,
            yes_con  = yc,
            no_con   = nc,
            daily    = sym_cfg.get("daily", False),
        ))

    log.info(
        f"  {sym_cfg['name']:<8} {len(pairs):>3} pairs  "
        f"expiry={pairs[0].expiry if pairs else 'N/A'}  "
        f"strikes=[{common_strikes[0]:.0f}..{common_strikes[-1]:.0f}]"
    )
    return pairs


async def _has_live_prices(ib: IB, pairs: list[Pair], warmup_sec: int = 10) -> bool:
    """Subscribe to pairs, wait for warmup, check if any have live prices."""
    subscribe_all(ib, pairs)
    await asyncio.sleep(warmup_sec)
    live = 0
    for p in pairs:
        yt, nt = p.yes_ticker, p.no_ticker
        has_activity = (
            (yt and hasattr(yt, 'bid') and yt.bid is not None and yt.bid > 0)
            or (yt and hasattr(yt, 'ask') and yt.ask is not None and yt.ask > 0)
            or (nt and hasattr(nt, 'bid') and nt.bid is not None and nt.bid > 0)
            or (nt and hasattr(nt, 'ask') and nt.ask is not None and nt.ask > 0)
        )
        if has_activity:
            live += 1
    return live > 0


async def discover_all_pairs(ib: IB, today_str: str) -> list[Pair]:
    """Discover all pairs for all symbols.
    For daily contracts, verifies live prices — tries today first,
    then next trading day if today's contracts are settled/stale."""
    all_pairs = []

    log.info("\n  ── Daily contracts ──────────────────────────────")
    for sym_cfg in DAILY_SYMBOLS:
        found = False
        for date_str in candidate_dates_et():
            pairs = await discover_pairs_for_symbol(ib, sym_cfg, date_str)
            if not pairs:
                continue
            # Check for live prices before committing
            if await _has_live_prices(ib, pairs):
                log.info(f"  {sym_cfg['name']}: live prices on {date_str}")
                all_pairs.extend(pairs)
                found = True
                break
            else:
                log.info(f"  {sym_cfg['name']}: no live prices for {date_str}, trying next…")
                unsubscribe_all(ib, pairs)
        if not found:
            log.warning(f"  {sym_cfg['name']}: no live contracts found")
        await asyncio.sleep(0.3)

    log.info("\n  ── Persistent contracts ─────────────────────────")
    for sym_cfg in PERSISTENT_SYMBOLS:
        pairs = await discover_pairs_for_symbol(ib, sym_cfg, None)
        all_pairs.extend(pairs)
        await asyncio.sleep(0.3)

    return all_pairs


# ─── SUBSCRIPTION ──────────────────────────────────────────────────────────────

def subscribe_pair(ib: IB, pair: Pair) -> None:
    pair.yes_ticker = ib.reqMktData(pair.yes_con, snapshot=False, regulatorySnapshot=False)
    pair.no_ticker  = ib.reqMktData(pair.no_con,  snapshot=False, regulatorySnapshot=False)


def unsubscribe_pair(ib: IB, pair: Pair) -> None:
    try:
        if pair.yes_ticker:
            ib.cancelMktData(pair.yes_con)
        if pair.no_ticker:
            ib.cancelMktData(pair.no_con)
    except Exception:
        pass


def subscribe_all(ib: IB, pairs: list[Pair]) -> None:
    for pair in pairs:
        subscribe_pair(ib, pair)


def unsubscribe_all(ib: IB, pairs: list[Pair]) -> None:
    for pair in pairs:
        unsubscribe_pair(ib, pair)


# ─── GAP PROCESSING ────────────────────────────────────────────────────────────

def process_pair_tick(pair: Pair, mode: str, stats: dict) -> None:
    """
    Called on every 0.5s loop iteration for each pair.
    Only processes when both legs have confirmed valid prices.
    """
    pair.tick_total += 1
    stats["tick_count"] = stats.get("tick_count", 0) + 1

    if not pair.both_valid():
        pair.tick_invalid += 1
        # If gap was open and prices disappeared, close it
        if pair.gap_open:
            _close_gap(pair, mode, stats, reason="DATA_LOSS")
        return

    pair.tick_valid += 1
    stats["valid_ticks"] = stats.get("valid_ticks", 0) + 1

    # Log every valid tick
    log_tick(pair, mode)

    s = pair.sum_ask()
    g = pair.gap()

    # Update stats
    if g > stats.get("best_gap", 0):
        stats["best_gap"]        = g
        stats["best_gap_symbol"] = pair.pair_label

    depth = pair.min_depth()
    if depth > pair.max_depth:
        pair.max_depth = depth
    if depth > stats.get("max_depth", 0):
        stats["max_depth"] = depth

    profit = pair.max_profit_at_gap()
    if profit > stats.get("max_profit", 0):
        stats["max_profit"] = profit

    # ── Gap state machine ──────────────────────────────────────────────────────
    if s < BREAKEVEN_SUM:
        # Potential gap — require CONFIRM_TICKS consecutive ticks
        pair._confirm_count += 1

        if not pair._gap_confirmed and pair._confirm_count >= CONFIRM_TICKS:
            pair._gap_confirmed = True

        if pair._gap_confirmed and not pair.gap_open:
            _open_gap(pair, mode, stats, s, g)

        elif pair.gap_open:
            # Gap is open — update peak
            if profit > pair.gap_peak_profit:
                pair.gap_peak_profit = profit
            if g > pair.max_gap:
                pair.max_gap = g

    else:
        # Sum is back above breakeven — gap closed
        pair._confirm_count = 0
        pair._gap_confirmed = False
        if pair.gap_open:
            _close_gap(pair, mode, stats)


def _open_gap(pair: Pair, mode: str, stats: dict, s: float, g: float) -> None:
    pair.gap_open      = True
    pair.gap_open_time = time.time()
    pair.gap_open_sum  = s
    pair.gap_open_leg  = pair.lagging_leg()
    pair.gap_peak_profit = pair.max_profit_at_gap()
    pair.total_gaps   += 1

    stats["breakeven_gaps"] = stats.get("breakeven_gaps", 0) + 1
    if s < ALERT_SUM:
        stats["alert_gaps"] = stats.get("alert_gaps", 0) + 1

    log_gap_event("OPEN", pair, mode)

    now_et = datetime.now(ET)
    now_str = now_et.strftime("%H:%M:%S.%f")[:-3]

    yp = pair.yes_price()
    np_ = pair.no_price()
    mp = pair.max_profit_at_gap()

    alert_clr = C.ALERT if s < ALERT_SUM else C.WARN
    print(f"\n  {alert_clr}┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓{C.RESET}")
    print(f"  {alert_clr}┃{C.RESET}  GAP OPEN   {C.VALUE}{pair.pair_label}{C.RESET}"
          f"   {C.DIM}{now_str} ET  [{mode}]{C.RESET}"
          f"  {alert_clr}┃{C.RESET}")
    print(f"  {alert_clr}┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛{C.RESET}")
    print(f"    {C.DIM}YES ask:{C.RESET}    {C.VALUE}${yp.ask:.4f}{C.RESET}  {C.DIM}depth={yp.size}{C.RESET}")
    print(f"    {C.DIM}NO ask:{C.RESET}     {C.VALUE}${np_.ask:.4f}{C.RESET}  {C.DIM}depth={np_.size}{C.RESET}")
    print(f"    {C.DIM}Sum:{C.RESET}        {C.GREEN}${s:.4f}{C.RESET}  {C.DIM}Gap:{C.RESET} {C.GREEN}${g:.4f}{C.RESET}")
    print(f"    {C.DIM}Lag leg:{C.RESET}    {pair.gap_open_leg}")
    print(f"    {C.DIM}Max profit:{C.RESET} {C.VALUE}${mp:.2f}{C.RESET}"
          f"  {C.DIM}({pair.min_depth()} contracts x ${g:.4f}){C.RESET}")

    if s < ALERT_SUM:
        send_telegram(
            f"⚡ *GAP OPEN — {pair.symbol}*\n"
            f"Pair: `{pair.pair_label}`\n"
            f"YES ask: `${yp.ask:.4f}` depth=`{yp.size}`\n"
            f"NO ask:  `${np_.ask:.4f}` depth=`{np_.size}`\n"
            f"Sum: `${s:.4f}`  Gap: `${g:.4f}`\n"
            f"Lagging: `{pair.gap_open_leg}`\n"
            f"Max profit: `${mp:.2f}`\n"
            f"Mode: `{mode}`\n"
            f"Time: `{now_str} ET`"
        )


def _close_gap(pair: Pair, mode: str, stats: dict, reason: str = "REPRICED") -> None:
    duration = time.time() - (pair.gap_open_time or time.time())
    pair.total_gap_seconds += duration
    pair.gap_open          = False
    pair._gap_confirmed    = False
    pair._confirm_count    = 0

    log_gap_event("CLOSE", pair, mode, duration_sec=duration)

    now_str = datetime.now(ET).strftime("%H:%M:%S.%f")[:-3]
    peak_p  = pair.gap_peak_profit

    print(f"\n  {C.OK}✓ GAP CLOSE{C.RESET}  {C.VALUE}{pair.pair_label}{C.RESET}"
          f"  {C.DIM}duration={C.RESET}{duration:.1f}s"
          f"  {C.DIM}peak=${C.RESET}{peak_p:.2f}"
          f"  {C.DIM}reason={C.RESET}{reason}"
          f"  {C.DIM}{now_str} ET{C.RESET}")

    if pair.gap_open_sum < ALERT_SUM and duration >= 5.0:
        send_telegram(
            f"✅ *GAP CLOSE — {pair.symbol}*\n"
            f"Pair: `{pair.pair_label}`\n"
            f"Duration: `{duration:.1f}s`\n"
            f"Peak profit: `${peak_p:.2f}`\n"
            f"Reason: `{reason}`"
        )

    pair.gap_peak_profit = 0.0


# ─── DAILY REFRESH ─────────────────────────────────────────────────────────────

async def refresh_daily_contracts(ib: IB, all_pairs: list[Pair]) -> list[Pair]:
    """
    Called at market open each morning.
    Unsubscribes old daily contracts, discovers new ones, resubscribes.
    Returns updated full pair list.
    """
    log.info("\n  ── Daily contract refresh ───────────────────────")
    today_str = get_today_et()

    # Separate daily from persistent
    daily_pairs      = [p for p in all_pairs if p.daily]
    persistent_pairs = [p for p in all_pairs if not p.daily]

    # Unsubscribe old daily pairs
    unsubscribe_all(ib, daily_pairs)
    await asyncio.sleep(1.0)

    # Discover new daily pairs — verify live prices
    new_daily = []
    for sym_cfg in DAILY_SYMBOLS:
        found = False
        for date_str in candidate_dates_et():
            pairs = await discover_pairs_for_symbol(ib, sym_cfg, date_str)
            if not pairs:
                continue
            if await _has_live_prices(ib, pairs):
                log.info(f"  {sym_cfg['name']}: live prices on {date_str}")
                new_daily.extend(pairs)
                found = True
                break
            else:
                log.info(f"  {sym_cfg['name']}: no live prices for {date_str}, trying next…")
                unsubscribe_all(ib, pairs)
        if not found:
            log.warning(f"  {sym_cfg['name']}: no live contracts found on refresh")
        await asyncio.sleep(0.3)

    # Subscribe new daily pairs
    subscribe_all(ib, new_daily)

    log.info(f"  Daily refresh complete: {len(new_daily)} new daily pairs")
    send_telegram(
        f"🔄 *Daily Contracts Refreshed*\n"
        f"Date: `{today_str}`\n"
        f"New daily pairs: `{len(new_daily)}`\n"
        f"Symbols: `{', '.join(s['name'] for s in DAILY_SYMBOLS)}`"
    )

    await asyncio.sleep(WARMUP_SECONDS)
    return persistent_pairs + new_daily


# ─── CONNECTION / RECONNECT ────────────────────────────────────────────────────

async def connect_with_retry() -> IB:
    ib = IB()
    for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
        try:
            log.info(f"  Connecting to IB Gateway (attempt {attempt}/{MAX_RECONNECT_ATTEMPTS})...")
            await ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID)
            # Request delayed data as fallback for contracts without
            # real-time subscription (e.g. FES/FOP). IB auto-upgrades
            # to live when the subscription exists.
            ib.reqMarketDataType(4)
            log.info("  ✓ Connected (market data type: delayed-frozen fallback)\n")
            return ib
        except Exception as e:
            log.error(f"  Connection attempt {attempt} failed: {e}")
            if attempt < MAX_RECONNECT_ATTEMPTS:
                log.info(f"  Retrying in {RECONNECT_DELAY_SEC}s...")
                await asyncio.sleep(RECONNECT_DELAY_SEC)
            else:
                log.critical("  Max reconnect attempts reached. Exiting.")
                sys.exit(1)
    return ib  # never reached


# ─── PRINT SNAPSHOT ────────────────────────────────────────────────────────────

def print_snapshot(all_pairs: list[Pair], title: str = "PRICE SNAPSHOT") -> None:
    now_str = datetime.now(ET).strftime("%H:%M:%S ET")
    mode, catalyst_label = current_mode()
    mode_str = (f"{C.WARN}CATALYST: {catalyst_label}{C.RESET}"
                if mode == "CATALYST" else f"{C.DIM}NORMAL{C.RESET}")

    print(f"\n  {C.HEADER}┌─────────────────────────────────────────────────────────────────┐{C.RESET}")
    print(f"  {C.HEADER}│{C.RESET}  {C.VALUE}{now_str}{C.RESET}   {mode_str}"
          f"  {C.HEADER}│{C.RESET}")
    print(f"  {C.HEADER}└─────────────────────────────────────────────────────────────────┘{C.RESET}")

    print(f"\n  {C.BOLD}{C.WHITE}{title}{C.RESET}")
    print(f"  {C.HEADER}┌──────────────────────────┬─────────┬─────────┬─────────┬────────┬───────┐{C.RESET}")
    print(f"  {C.HEADER}│{C.RESET} {C.WHITE}Pair{C.RESET}                     "
          f"{C.HEADER}│{C.RESET} {C.WHITE}YES ask{C.RESET} "
          f"{C.HEADER}│{C.RESET} {C.WHITE} NO ask{C.RESET} "
          f"{C.HEADER}│{C.RESET} {C.WHITE}   SUM{C.RESET}  "
          f"{C.HEADER}│{C.RESET} {C.WHITE}  GAP{C.RESET}  "
          f"{C.HEADER}│{C.RESET} {C.WHITE}Depth{C.RESET} {C.HEADER}│{C.RESET}")
    print(f"  {C.HEADER}├──────────────────────────┼─────────┼─────────┼─────────┼────────┼───────┤{C.RESET}")

    daily_pairs      = [p for p in all_pairs if p.daily]
    persistent_pairs = [p for p in all_pairs if not p.daily]

    for section, pairs in [("DAILY", daily_pairs), ("PERSISTENT", persistent_pairs)]:
        if not pairs:
            continue
        print(f"  {C.HEADER}│{C.RESET} {C.CYAN}{C.BOLD}{section}{C.RESET}"
              + " " * (24 - len(section))
              + f" {C.HEADER}│{C.RESET}         "
              f"{C.HEADER}│{C.RESET}         "
              f"{C.HEADER}│{C.RESET}         "
              f"{C.HEADER}│{C.RESET}        "
              f"{C.HEADER}│{C.RESET}       {C.HEADER}│{C.RESET}")
        for pair in pairs:
            yp  = pair.yes_price()
            np_ = pair.no_price()
            s   = pair.sum_ask()
            g   = pair.gap()
            d   = pair.min_depth()

            ya = f"${yp.ask:.4f}" if yp.ask > 0 else f"{C.DIM}  n/a {C.RESET}"
            na = f"${np_.ask:.4f}" if np_.ask > 0 else f"{C.DIM}  n/a {C.RESET}"

            if s > 0 and s < BREAKEVEN_SUM:
                sum_clr = C.GREEN
                gap_clr = C.GREEN
                flag = f"{C.GREEN}*{C.RESET}"
            elif s > 0 and s < 1.0:
                sum_clr = C.YELLOW
                gap_clr = C.YELLOW
                flag = " "
            elif s > 0:
                sum_clr = C.RED
                gap_clr = C.DIM
                flag = " "
            else:
                sum_clr = C.DIM
                gap_clr = C.DIM
                flag = " "

            sv = f"{sum_clr}${s:.4f}{C.RESET}" if s > 0 else f"{C.DIM}  n/a {C.RESET}"
            gv = f"{gap_clr}{g:+.4f}{C.RESET}" if g > -99 else f"{C.DIM}  n/a{C.RESET}"

            label = pair.pair_label[:24]
            print(f"  {C.HEADER}│{C.RESET}{flag}{C.VALUE}{label:<25}{C.RESET}"
                  f"{C.HEADER}│{C.RESET} {ya:>7} "
                  f"{C.HEADER}│{C.RESET} {na:>7} "
                  f"{C.HEADER}│{C.RESET} {sv:>7} "
                  f"{C.HEADER}│{C.RESET} {gv:>6} "
                  f"{C.HEADER}│{C.RESET} {d:>5} {C.HEADER}│{C.RESET}")

    print(f"  {C.HEADER}└──────────────────────────┴─────────┴─────────┴─────────┴────────┴───────┘{C.RESET}")


# ─── FINAL ANALYSIS ────────────────────────────────────────────────────────────

def print_final_analysis(all_pairs: list[Pair], run_days: float) -> None:
    total_gaps    = sum(p.total_gaps         for p in all_pairs)
    total_sec     = sum(p.total_gap_seconds  for p in all_pairs)
    avg_dur       = (total_sec / total_gaps) if total_gaps > 0 else 0
    max_gap       = max((p.max_gap           for p in all_pairs), default=0)
    max_depth     = max((p.max_depth         for p in all_pairs), default=0)
    total_valid   = sum(p.tick_valid         for p in all_pairs)
    total_ticks   = sum(p.tick_total         for p in all_pairs)
    quality_pct   = (100 * total_valid / total_ticks) if total_ticks > 0 else 0
    gaps_per_week = (total_gaps / run_days * 7) if run_days > 0 else 0

    W = 63
    print(f"\n  {C.HEADER}╔{'═'*W}╗{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.BOLD}{C.WHITE}PHASE 0 FINAL ANALYSIS{C.RESET}"
          + " " * (W - 24) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}╠{'═'*W}╣{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Run duration:{C.RESET}        {run_days:.1f} days"
          + " " * (W - 30) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Total ticks:{C.RESET}         {total_ticks:,}"
          + " " * max(0, W - 25 - len(f"{total_ticks:,}")) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Valid ticks:{C.RESET}         {total_valid:,}  ({quality_pct:.1f}%)"
          + " " * max(0, W - 32 - len(f"{total_valid:,}") - len(f"{quality_pct:.1f}")) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Gap events:{C.RESET}          {C.VALUE}{total_gaps}{C.RESET}  ({gaps_per_week:.1f}/week)"
          + " " * max(0, W - 35 - len(str(total_gaps)) - len(f"{gaps_per_week:.1f}")) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Avg gap duration:{C.RESET}    {avg_dur:.1f}s"
          + " " * max(0, W - 27 - len(f"{avg_dur:.1f}")) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Largest gap:{C.RESET}         ${max_gap:.4f}"
          + " " * (W - 28) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Max depth at gap:{C.RESET}    {max_depth}"
          + " " * max(0, W - 25 - len(str(max_depth))) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}╚{'═'*W}╝{C.RESET}")

    print(f"\n  {C.BOLD}{C.WHITE}PER-PAIR BREAKDOWN{C.RESET}")
    print(f"  {C.HEADER}┌──────────────────────────────────┬───────┬──────────┬──────────┬──────────┐{C.RESET}")
    print(f"  {C.HEADER}│{C.RESET} {C.WHITE}Pair{C.RESET}                             "
          f"{C.HEADER}│{C.RESET} {C.WHITE} Gaps{C.RESET}  "
          f"{C.HEADER}│{C.RESET} {C.WHITE} Max Gap{C.RESET}  "
          f"{C.HEADER}│{C.RESET} {C.WHITE} Avg Dur{C.RESET}  "
          f"{C.HEADER}│{C.RESET} {C.WHITE}Quality%{C.RESET}  {C.HEADER}│{C.RESET}")
    print(f"  {C.HEADER}├──────────────────────────────────┼───────┼──────────┼──────────┼──────────┤{C.RESET}")
    for p in sorted(all_pairs, key=lambda x: x.total_gaps, reverse=True)[:20]:
        avg_d = (p.total_gap_seconds / p.total_gaps) if p.total_gaps > 0 else 0
        g_clr = C.GREEN if p.total_gaps > 0 else C.DIM
        q_clr = C.GREEN if p.data_quality_pct() > 80 else C.YELLOW if p.data_quality_pct() > 40 else C.RED
        label = p.pair_label[:32]
        print(f"  {C.HEADER}│{C.RESET} {label:<32} "
              f"{C.HEADER}│{C.RESET} {g_clr}{p.total_gaps:>5}{C.RESET} "
              f"{C.HEADER}│{C.RESET} {p.max_gap:>8.4f} "
              f"{C.HEADER}│{C.RESET} {avg_d:>7.1f}s "
              f"{C.HEADER}│{C.RESET} {q_clr}{p.data_quality_pct():>7.1f}%{C.RESET} {C.HEADER}│{C.RESET}")
    print(f"  {C.HEADER}└──────────────────────────────────┴───────┴──────────┴──────────┴──────────┘{C.RESET}")

    if max_depth >= 500 and gaps_per_week >= 5:
        verdict_clr = C.OK
        verdict = "STRONG — BUILD EXECUTION ENGINE"
    elif max_depth >= 200 and gaps_per_week >= 2:
        verdict_clr = C.YELLOW
        verdict = "MODERATE — Build light execution engine"
    elif total_gaps >= 1:
        verdict_clr = C.WARN
        verdict = "WEAK — Gaps exist but thin"
    else:
        verdict_clr = C.RED
        verdict = "NO GAPS — Thesis not confirmed on this universe"

    print(f"\n  {C.DIM}Verdict:{C.RESET} {verdict_clr}{C.BOLD}{verdict}{C.RESET}\n")

    write_quality_report(all_pairs)

    send_telegram(
        f"🛑 *ForecastBot Stopped — Final Analysis*\n"
        f"Run: `{run_days:.1f}` days\n"
        f"Total gap events: `{total_gaps}` (`{gaps_per_week:.1f}/week`)\n"
        f"Avg duration: `{avg_dur:.1f}s`\n"
        f"Largest gap: `${max_gap:.4f}`\n"
        f"Max depth: `{max_depth}`\n"
        f"Data quality: `{quality_pct:.1f}%`\n"
        f"Verdict: {verdict}"
    )


# ─── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    W = 63
    now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    syms = ", ".join(s["name"] for s in ALL_SYMBOLS)

    print(f"\n  {C.HEADER}╔{'═'*W}╗{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.BOLD}{C.WHITE}ForecastBot — Kill-Shot Parity Scanner v2.0{C.RESET}"
          + " " * (W - 45) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Started:{C.RESET} {now_str}"
          + " " * (W - 31) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Universe:{C.RESET} {syms}"
          + " " * max(0, W - 12 - len(syms)) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Breakeven:{C.RESET} sum < ${BREAKEVEN_SUM:.2f}"
          f"  {C.DIM}│{C.RESET}  {C.DIM}Alert:{C.RESET} sum < ${ALERT_SUM:.2f}"
          + " " * (W - 48) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Confirm:{C.RESET} {CONFIRM_TICKS} ticks"
          f"  {C.DIM}│{C.RESET}  {C.DIM}Streaming:{C.RESET} 0.5s"
          + " " * (W - 40) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.RED}{C.BOLD}*** OBSERVATION ONLY — NO ORDERS ***{C.RESET}"
          + " " * (W - 38) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}╚{'═'*W}╝{C.RESET}\n")

    init_logs()
    run_start = time.time()

    # ── Connect ────────────────────────────────────────────────────────────────
    ib = await connect_with_retry()

    # ── Discover initial pairs ─────────────────────────────────────────────────
    today_str = get_today_et()
    log.info(f"  Discovering contracts for {today_str}...\n")
    all_pairs = await discover_all_pairs(ib, today_str)

    if not all_pairs:
        log.critical("  No contracts found. Check IB Gateway and permissions.")
        ib.disconnect()
        sys.exit(1)

    log.info(f"\n  Total pairs: {len(all_pairs)} "
             f"({sum(1 for p in all_pairs if p.daily)} daily, "
             f"{sum(1 for p in all_pairs if not p.daily)} persistent)\n")

    # ── Subscribe ──────────────────────────────────────────────────────────────
    log.info("  Subscribing to streaming market data...")
    subscribe_all(ib, all_pairs)
    log.info(f"  Warming up ({WARMUP_SECONDS}s)...")
    await asyncio.sleep(WARMUP_SECONDS)

    # Initial snapshot
    print_snapshot(all_pairs, "INITIAL SNAPSHOT")

    send_telegram(
        f"🚀 *ForecastBot v2.0 Started*\n"
        f"Pairs: `{len(all_pairs)}` "
        f"({sum(1 for p in all_pairs if p.daily)} daily + "
        f"{sum(1 for p in all_pairs if not p.daily)} persistent)\n"
        f"Daily symbols: `{', '.join(s['name'] for s in DAILY_SYMBOLS)}`\n"
        f"Persistent: `{', '.join(s['name'] for s in PERSISTENT_SYMBOLS)}`\n"
        f"Alert threshold: sum < `${ALERT_SUM:.2f}`\n"
        f"Confirm ticks: `{CONFIRM_TICKS}`\n"
        f"Time: `{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}`"
    )

    # ── Main loop ──────────────────────────────────────────────────────────────
    SNAPSHOT_INTERVAL_SEC = 300   # print snapshot every 5 minutes

    daily_stats:      dict     = {}
    last_date:        str      = datetime.now(ET).strftime("%Y-%m-%d")
    last_heartbeat:   datetime = datetime.now(ET)
    last_daily_refresh: str   = today_str
    last_snapshot_ts:  float  = 0.0

    log.info("  Streaming... (Ctrl+C to stop)\n")

    try:
        while True:
            await asyncio.sleep(0.5)

            now_et    = datetime.now(ET)
            today     = now_et.strftime("%Y-%m-%d")
            today_num = now_et.strftime("%Y%m%d")
            mode, catalyst_label = current_mode()

            # ── Check IB connection ────────────────────────────────────────────
            if not ib.isConnected():
                log.warning("  IB Gateway disconnected. Attempting reconnect...")
                unsubscribe_all(ib, all_pairs)
                try:
                    ib.disconnect()
                except Exception:
                    pass

                ib = await connect_with_retry()

                # Re-subscribe all
                subscribe_all(ib, all_pairs)
                await asyncio.sleep(WARMUP_SECONDS)
                log.info("  Reconnected and resubscribed.")
                send_telegram("🔁 *ForecastBot reconnected to IB Gateway*")
                continue

            # ── Daily contract refresh at 09:31 ET ────────────────────────────
            if (
                today_num != last_daily_refresh and
                now_et.hour == 9 and
                now_et.minute >= 31
            ):
                last_daily_refresh = today_num
                all_pairs = await refresh_daily_contracts(ib, all_pairs)
                print_snapshot(all_pairs, "POST-REFRESH SNAPSHOT")

            # ── Midnight rollover ──────────────────────────────────────────────
            if today != last_date:
                write_daily_summary(last_date, daily_stats, all_pairs)
                daily_stats = {}
                last_date   = today

            # ── Process each pair ──────────────────────────────────────────────
            for pair in all_pairs:
                process_pair_tick(pair, mode, daily_stats)

            # ── Periodic snapshot to console (every 5 min) ───────────────────
            now_ts = time.time()
            if now_ts - last_snapshot_ts >= SNAPSHOT_INTERVAL_SEC:
                last_snapshot_ts = now_ts
                print_snapshot(all_pairs, "PRICE SNAPSHOT")

            # ── Heartbeat every 30 minutes ─────────────────────────────────────
            if (now_et - last_heartbeat).total_seconds() >= 1800:
                open_gaps   = sum(1 for p in all_pairs if p.gap_open)
                total_g     = sum(p.total_gaps for p in all_pairs)
                total_valid = sum(p.tick_valid for p in all_pairs)
                total_ticks = sum(p.tick_total for p in all_pairs)
                qpct        = (100 * total_valid / total_ticks) if total_ticks > 0 else 0

                log.info(
                    f"  {C.DIM}[HEARTBEAT]{C.RESET} {now_et.strftime('%H:%M ET')}"
                    f"  {C.DIM}mode={C.RESET}{mode}"
                    f"  {C.DIM}gaps={C.RESET}{total_g}"
                    f"  {C.DIM}open={C.RESET}{open_gaps}"
                    f"  {C.DIM}quality={C.RESET}{qpct:.1f}%"
                )
                last_heartbeat = now_et

            # ── Catalyst mode banner ───────────────────────────────────────────
            if mode == "CATALYST" and now_et.second < 1:
                log.info(
                    f"  {C.WARN}{C.BOLD}CATALYST ACTIVE:{C.RESET}"
                    f" {C.VALUE}{catalyst_label}{C.RESET}"
                    f"  {C.DIM}{now_et.strftime('%H:%M:%S ET')}{C.RESET}"
                )

    except KeyboardInterrupt:
        log.info("\n  Stopped by user.")

    except Exception as e:
        log.critical(f"\n  FATAL ERROR: {e}\n{traceback.format_exc()}")
        send_telegram(f"🚨 *ForecastBot CRASHED*\n`{str(e)[:200]}`")

    finally:
        # Close any open gaps
        for pair in all_pairs:
            if pair.gap_open:
                _close_gap(pair, "SHUTDOWN", {}, reason="SHUTDOWN")

        unsubscribe_all(ib, all_pairs)
        write_daily_summary(last_date, daily_stats, all_pairs)

        run_days = (time.time() - run_start) / 86400
        print_final_analysis(all_pairs, run_days)

        try:
            ib.disconnect()
        except Exception:
            pass

        log.info("  Disconnected. Data saved.\n")


if __name__ == "__main__":
    asyncio.run(main())
