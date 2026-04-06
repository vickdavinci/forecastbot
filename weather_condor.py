"""
weather_condor.py — ForecastBot Weather Condor Strategy  v1.0
==============================================================
THESIS:
  Buy YES at (forecast - offset) and NO at (forecast + offset) across
  multiple ForecastEx weather cities. Temperature can't be both below
  the YES strike AND above the NO strike, so at least one leg always
  pays $1.00 — guaranteeing profit when combined entry < $1.00.

  If temp lands between the two strikes (most likely), BOTH pay $1.00
  for a $2.00 return on < $1.00 entry.

ARCHITECTURE:
  Single-file daemon with a daily state machine:
    WAITING → SCANNING → ACTIVE → SETTLING → REPORTING → WAITING

  Phase         Trigger           Action
  WAITING       Startup/reset     Idle, check clock every 60s
  SCANNING      7:00 AM ET        Fetch WU forecasts + 4-source swing detection
  ACTIVE        Scan complete     Monitor prices; at 9:30 AM drift-check + entries
  SETTLING      7:00 PM ET        Fetch WU daily high, compute P&L per position
  REPORTING     Settlement done   Print summary, write CSVs, send Telegram, reset

  Two-stage morning:
    7:00 AM — Forecast scan + swing detection (morning models are in)
    9:30 AM — Re-fetch WU, drift check, then sweep into live order book

SETTLEMENT:
  "Exceed X°F" means STRICTLY > X°F.
  YES pays if wu_high > yes_strike.
  NO  pays if wu_high <= no_strike (temp does NOT exceed).

RUN:
  python3 weather_condor.py
  Uses clientId=56 (separate from kill_shot=10, weather_edge=45, depth=55)

REQUIRES:
  pip install ib_async requests python-dotenv
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
from ib_async import IB, Contract

load_dotenv()

# ─── LOGGING ──────────────────────────────────────────────────────────────────
LOG_DIR = os.getenv("LOG_DIR", "./data")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, "weather_condor.log"), mode="a"),
    ],
)
log = logging.getLogger("weather_condor")

# ─── TIMEZONES ────────────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
PT = ZoneInfo("America/Los_Angeles")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
IBKR_HOST      = os.getenv("IBKR_HOST",                   "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT",               "4001"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID_CONDOR",   "56"))

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")

# Portfolio
PORTFOLIO_USD       = 20000.0
MAX_CITY_BUDGET     = 2000.0    # hard cap per city (entries + SL hedges)
# Cross-strike condor: YES and NO are at different strikes, both can win.
# Max payout = $2.00 (both legs pay $1.00). Min payout = $1.00 (one leg always wins).
# Entry at $1.80 = $0.20 edge if both win. Below $1.65 = $0.35 edge (ideal).
MAX_ENTRY_SUM       = 1.80      # max combined cost — min $0.20 edge per contract
SWEEP_RANGE         = 0.03      # sweep up to +3 cents above best ask
MAX_LEG_PRICE       = 0.97      # never pay more than $0.97 per leg
TAKE_PROFIT         = 0.20      # exit both legs when sum rises $0.20 above entry


# Timing (all ET)
# Two-stage morning: scan forecasts at 7 AM (after overnight model updates),
# then wait for book liquidity before entering at 9:30 AM.
SCAN_HOUR           = 7         # 7:00 AM ET — fetch forecasts + swing detection
SCAN_MINUTE         = 0
ENTRY_HOUR          = 9         # 9:30 AM ET — earliest entry (book has liquidity)
ENTRY_MINUTE        = 30
SETTLE_HOUR         = 19        # 7:00 PM ET — settlement
YES_SL_LOCAL_HOUR   = 15        # 3:00 PM LOCAL time — YES stop-loss only after peak heating
WAITING_POLL_SEC    = 60        # check clock every 60s in WAITING

# Forecast drift: re-fetch WU at entry time — always use latest for strikes.
# Only skip if drift is extreme (>= 2x offset), meaning forecast is genuinely unstable.
FORECAST_DRIFT_EXTREME_F = 6    # skip only if drift >= this (2x typical offset)

# IB
IB_WARMUP_SEC       = 20
MAX_RECONNECT_ACTIVE = 10       # retries during ACTIVE phase (need data urgently)
MAX_RECONNECT_WAIT   = 0        # 0 = infinite retries during WAITING (never give up)
RECONNECT_DELAY_SEC  = 30       # base delay between retries
RECONNECT_MAX_DELAY  = 300      # max backoff delay (5 min)

# WU API
WU_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"

# NWS API (max 1 req/s — sequential with 1s delay)
NWS_DELAY_SEC = 1.1

# AFD skip keywords (case-insensitive)
AFD_SKIP_KEYWORDS = [
    "frontal passage", "wind shift", "temperature swing",
    "model disagreement", "uncertain", "tricky forecast",
]

# WU narrative skip keywords (case-insensitive)
WU_NARRATIVE_SKIP_KEYWORDS = [
    "storms", "gusty", "wind advisory", "record",
    "unusual", "well above normal", "well below normal",
]

# Hourly forecast swing threshold
HOURLY_SWING_THRESHOLD_F = 25

# Active phase poll intervals
# IB prices are streaming (cached reads, zero cost) — poll frequently for rich data.
# WU current temp updates every ~10 min — no point calling faster than every 5 min.
ACTIVE_PRICE_POLL_SEC = 15      # read IB prices + log to CSV every 15s
ACTIVE_WU_POLL_SEC    = 300     # fetch WU current temp every 5 min

# CSV paths
FORECAST_CSV  = os.path.join(LOG_DIR, "condor_forecasts.csv")
POSITION_CSV  = os.path.join(LOG_DIR, "condor_positions.csv")
DAILY_CSV     = os.path.join(LOG_DIR, "condor_daily.csv")
PRICES_CSV    = os.path.join(LOG_DIR, "condor_prices.csv")


# ─── CITY REGISTRY ───────────────────────────────────────────────────────────

CITY_REGISTRY = [
    {
        "name": "Chicago",      "symbol": "UHMDW", "metar": "KMDW",
        "geocode": "41.79,-87.75",  "tz": CT,  "offset": 2,
        "tier": 1,  "nws_office": "LOT",  "nws_gridpoint": "65,76",
    },
    {
        "name": "Los Angeles",  "symbol": "UHLAX", "metar": "KLAX",
        "geocode": "33.94,-118.41", "tz": PT,  "offset": 2,
        "tier": 1,  "nws_office": "LOX",  "nws_gridpoint": "149,48",
    },
    {
        "name": "San Francisco", "symbol": "UHSFO", "metar": "KSFO",
        "geocode": "37.62,-122.38", "tz": PT,  "offset": 2,
        "tier": 2,  "nws_office": "MTR",  "nws_gridpoint": "85,105",
    },
    {
        "name": "Austin",       "symbol": "UHAUS", "metar": "KAUS",
        "geocode": "30.19,-97.67",  "tz": CT,  "offset": 2,
        "tier": 2,  "nws_office": "EWX",  "nws_gridpoint": "156,91",
    },
    {
        "name": "Washington DC", "symbol": "UHDCA", "metar": "KDCA",
        "geocode": "38.85,-77.04",  "tz": ET,  "offset": 3,
        "tier": 2,  "nws_office": "LWX",  "nws_gridpoint": "97,71",
    },
    {
        "name": "Philadelphia", "symbol": "UHPHL", "metar": "KPHL",
        "geocode": "39.87,-75.23",  "tz": ET,  "offset": 3,
        "tier": 3,  "nws_office": "PHI",  "nws_gridpoint": "49,75",
    },
    {
        "name": "Seattle",      "symbol": "UHSEA", "metar": "KSEA",
        "geocode": "47.45,-122.31", "tz": PT,  "offset": 2,
        "tier": 3,  "nws_office": "SEW",  "nws_gridpoint": "124,67",
    },
    {
        "name": "New York",     "symbol": "UHLGA", "metar": "KLGA",
        "geocode": "40.77,-73.87",  "tz": ET,  "offset": 2,
        "tier": 0,  "nws_office": "OKX",  "nws_gridpoint": "33,37",
    },
    {
        "name": "Minneapolis",  "symbol": "UHMSP", "metar": "KMSP",
        "geocode": "44.88,-93.22",  "tz": CT,  "offset": 2,
        "tier": 2,  "nws_office": "MPX",  "nws_gridpoint": "110,68",
    },
    {
        "name": "Atlanta",      "symbol": "UHATL", "metar": "KATL",
        "geocode": "33.64,-84.43",  "tz": ET,  "offset": 2,
        "tier": 2,  "nws_office": "FFC",  "nws_gridpoint": "49,82",
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
class ForecastSnapshot:
    """Morning forecast capture for one city — the permanent record."""
    city: str
    symbol: str
    date: str               # YYYY-MM-DD
    capture_time_et: str    # HH:MM:SS ET
    forecast_high_f: int    # WU day 0 forecast high
    narrative: str          # WU day narrative (for logging)


@dataclass
class SwingCheckResult:
    """Result of 4-source swing detection for one city."""
    city: str
    nws_alerts: list        # list of active alert headlines
    afd_flags: list         # keywords found in AFD
    narrative_flags: list   # keywords found in WU narrative
    hourly_swing_f: int     # predicted high - predicted morning low
    eligible: bool = True   # all clear?
    skip_reason: str = ""   # if not eligible, why

    def compute_eligibility(self):
        flags = []
        if self.nws_alerts:
            flags.append(f"NWS_ALERT: {', '.join(self.nws_alerts[:2])}")
        if self.afd_flags:
            flags.append(f"AFD: {', '.join(self.afd_flags[:2])}")
        if self.narrative_flags:
            flags.append(f"WU_NARRATIVE: {', '.join(self.narrative_flags[:2])}")
        if self.hourly_swing_f > HOURLY_SWING_THRESHOLD_F:
            flags.append(f"HOURLY_SWING: {self.hourly_swing_f}F")
        if flags:
            self.eligible = False
            self.skip_reason = "; ".join(flags)
        else:
            self.eligible = True
            self.skip_reason = ""


@dataclass
class CondorPosition:
    """One simulated condor trade per city per day."""
    # Identity
    city: str
    symbol: str
    date: str
    forecast_high: int
    yes_strike: float
    no_strike: float
    # Entry
    yes_ask: float = 0.0        # best ask (display price)
    no_ask: float = 0.0         # best ask (display price)
    yes_blended: float = 0.0    # blended avg after sweep
    no_blended: float = 0.0     # blended avg after sweep
    entry_cost: float = 0.0     # yes_blended + no_blended
    num_contracts: int = 0
    total_cost: float = 0.0     # entry_cost * num_contracts
    entry_time: str = ""
    yes_depth: int = 0
    no_depth: int = 0
    sweep_levels: int = 0       # how many price levels swept
    # Exit (take-profit or stop-loss)
    exited: bool = False
    exit_reason: str = ""       # "TAKE_PROFIT" or "STOP_LOSS_YES" or "STOP_LOSS_NO"
    exit_time: str = ""
    exit_sum: float = 0.0       # market sum at exit
    exit_pnl: float = 0.0       # realized P&L at exit (hedged leg only for stop-loss)
    hedge_cost: float = 0.0     # opposing contract ask used to hedge losing leg
    # Settlement (filled later — only if not exited)
    wu_high: int = 0
    yes_won: bool = False
    no_won: bool = False
    payout: float = 0.0
    pnl: float = 0.0
    settled: bool = False


@dataclass
class CityDayState:
    """Per-city daily tracking — config + forecast + swing + position + monitoring."""
    # Config (from registry)
    city: str
    symbol: str
    metar: str
    geocode: str
    tz: ZoneInfo
    offset: int
    tier: int
    nws_office: str
    nws_gridpoint: str
    # Forecast
    forecast_high: int = 0
    early_forecast_high: int = 0    # morning scan forecast (for drift detection)
    narrative: str = ""
    forecast_captured: bool = False
    # Swing
    swing: Optional[SwingCheckResult] = None
    eligible: bool = False
    skip_reason: str = ""
    # Position
    position: Optional[CondorPosition] = None
    # Monitoring
    current_temp: int = 0
    wu_tracked_high: int = 0    # max of all WU current temp readings today
    yes_breach: bool = False    # temp dropped below yes_strike
    no_breach: bool = False     # temp exceeded no_strike
    hedge_logged: bool = False


@dataclass
class PortfolioState:
    """Global simulation state across all cities."""
    phase: str = "WAITING"
    date: str = ""
    portfolio_usd: float = PORTFOLIO_USD
    allocated_today: float = 0.0
    # Daily
    cities: list = field(default_factory=list)   # list[CityDayState]
    positions: list = field(default_factory=list) # list[CondorPosition]
    # Cumulative
    cumulative_pnl: float = 0.0
    trading_days: int = 0
    total_positions: int = 0
    both_won_count: int = 0
    one_won_count: int = 0
    # Internal tracking (not persisted)
    _last_wu_poll: float = 0.0
    _last_print: float = 0.0


# ─── WEATHER API FETCHERS ───────────────────────────────────────────────────

def fetch_wu_forecast(geocode: str) -> tuple:
    """Fetch WU 5-day forecast for a geocode.
    Returns (high_int, narrative_str) or (None, None) on failure."""
    try:
        url = (
            f"https://api.weather.com/v3/wx/forecast/daily/5day"
            f"?apiKey={WU_API_KEY}"
            f"&geocode={geocode}"
            f"&language=en-US&units=e&format=json"
        )
        r = requests.get(url, headers={"User-Agent": "forecastbot/condor-1.0"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        highs = data.get("calendarDayTemperatureMax", [])
        narratives = data.get("narrative", [])
        high = int(highs[0]) if highs and highs[0] is not None else None
        narr = str(narratives[0]) if narratives and narratives[0] else ""
        return high, narr
    except Exception as e:
        log.warning(f"  WU forecast failed ({geocode}): {e}")
        return None, None


def fetch_wu_current(geocode: str) -> Optional[int]:
    """Fetch WU current temperature for a geocode. Returns int F or None."""
    try:
        url = (
            f"https://api.weather.com/v3/wx/observations/current"
            f"?apiKey={WU_API_KEY}"
            f"&geocode={geocode}"
            f"&language=en-US&units=e&format=json"
        )
        r = requests.get(url, headers={"User-Agent": "forecastbot/condor-1.0"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        temp = data.get("temperature")
        return int(temp) if temp is not None else None
    except Exception as e:
        log.warning(f"  WU current failed ({geocode}): {e}")
        return None


def fetch_nws_alerts(geocode: str) -> list:
    """Fetch active NWS alerts for a geocode point.
    Returns list of alert headlines (empty = all clear)."""
    try:
        url = f"https://api.weather.gov/alerts/active?point={geocode}"
        r = requests.get(
            url,
            headers={"User-Agent": "forecastbot/condor-1.0 (contact: forecastbot@example.com)"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        features = data.get("features", [])
        headlines = []
        for f in features:
            props = f.get("properties", {})
            headline = props.get("headline", "")
            if headline:
                headlines.append(headline)
        return headlines
    except Exception as e:
        log.warning(f"  NWS alerts failed ({geocode}): {e}")
        return []


def fetch_nws_afd(office: str) -> str:
    """Fetch the latest NWS Area Forecast Discussion for a WFO office.
    Returns the text body or empty string on failure."""
    try:
        url = f"https://api.weather.gov/products/types/AFD/locations/{office}"
        r = requests.get(
            url,
            headers={"User-Agent": "forecastbot/condor-1.0 (contact: forecastbot@example.com)"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        products = data.get("@graph", [])
        if not products:
            return ""
        # Get the latest product
        latest_url = products[0].get("@id", "")
        if not latest_url:
            return ""
        time.sleep(NWS_DELAY_SEC)  # rate limit
        r2 = requests.get(
            latest_url,
            headers={"User-Agent": "forecastbot/condor-1.0 (contact: forecastbot@example.com)"},
            timeout=10,
        )
        r2.raise_for_status()
        return r2.json().get("productText", "")
    except Exception as e:
        log.warning(f"  NWS AFD failed ({office}): {e}")
        return ""


def fetch_nws_hourly_range(office: str, gridpoint: str, tz=None) -> int:
    """Fetch NWS hourly forecast and compute predicted intraday range.
    Returns predicted high - predicted low for today, or 0 on failure."""
    try:
        url = f"https://api.weather.gov/gridpoints/{office}/{gridpoint}/forecast/hourly"
        r = requests.get(
            url,
            headers={"User-Agent": "forecastbot/condor-1.0 (contact: forecastbot@example.com)"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        periods = data.get("properties", {}).get("periods", [])
        if not periods:
            return 0
        # Filter to today's periods only (use city's local timezone)
        today_str = datetime.now(tz or ET).strftime("%Y-%m-%d")
        today_temps = []
        for p in periods:
            start = p.get("startTime", "")
            if start.startswith(today_str):
                temp = p.get("temperature")
                if temp is not None:
                    today_temps.append(int(temp))
        if len(today_temps) < 2:
            return 0
        return max(today_temps) - min(today_temps)
    except Exception as e:
        log.warning(f"  NWS hourly failed ({office}/{gridpoint}): {e}")
        return 0


def fetch_wu_settlement(geocode: str) -> Optional[int]:
    """Fetch WU current conditions — use temperatureMax24Hour as settlement proxy.
    Late in the day (after peak), this reflects today's actual high."""
    try:
        url = (
            f"https://api.weather.com/v3/wx/observations/current"
            f"?apiKey={WU_API_KEY}"
            f"&geocode={geocode}"
            f"&language=en-US&units=e&format=json"
        )
        r = requests.get(url, headers={"User-Agent": "forecastbot/condor-1.0"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        high = data.get("temperatureMax24Hour")
        return int(high) if high is not None else None
    except Exception as e:
        log.warning(f"  WU settlement failed ({geocode}): {e}")
        return None


# ─── SWING DETECTION ────────────────────────────────────────────────────────

def check_swing_flags(city_state: CityDayState, loop) -> SwingCheckResult:
    """Run all 4 swing checks for a city. Sequential NWS calls with rate limiting.
    Returns SwingCheckResult with eligibility computed."""
    city = city_state.city
    geocode = city_state.geocode
    office = city_state.nws_office
    gridpoint = city_state.nws_gridpoint

    log.info(f"  Swing check: {city}…")

    # Check 1: NWS Alerts (blocking HTTP, run in executor from caller)
    alerts = fetch_nws_alerts(geocode)
    time.sleep(NWS_DELAY_SEC)

    # Check 2: NWS AFD
    afd_text = fetch_nws_afd(office)
    afd_lower = afd_text.lower()
    afd_flags = [kw for kw in AFD_SKIP_KEYWORDS if kw in afd_lower]
    time.sleep(NWS_DELAY_SEC)

    # Check 3: WU Narrative (already captured in forecast step)
    narr_lower = city_state.narrative.lower()
    narr_flags = [kw for kw in WU_NARRATIVE_SKIP_KEYWORDS if kw in narr_lower]

    # Check 4: NWS Hourly Range
    hourly_swing = fetch_nws_hourly_range(office, gridpoint, tz=city_state.tz)

    result = SwingCheckResult(
        city=city,
        nws_alerts=alerts,
        afd_flags=afd_flags,
        narrative_flags=narr_flags,
        hourly_swing_f=hourly_swing,
    )
    result.compute_eligibility()
    return result


# ─── IB PRICE FEED ──────────────────────────────────────────────────────────

class CondorPriceFeed:
    """
    Async IB connection for weather condor — discovers contracts for all
    8 weather symbols, subscribes only to target strikes per city.
    """

    def __init__(self):
        self.ib: Optional[IB] = None
        self.connected = False
        # Per-symbol: {symbol: {strike: (yes_contract, no_contract)}}
        self.contracts: dict = {}
        # Per-symbol expiry date
        self.expiries: dict = {}
        # Active tickers (L1): {(symbol, strike): (yes_ticker, no_ticker)}
        self.tickers: dict = {}
        # L2 depth tickers: {(symbol, strike): (yes_depth_ticker, no_depth_ticker)}
        self.depth_tickers: dict = {}
        # Requested → actual strike mapping (when snapped to nearest)
        # Key: (symbol, requested_strike), Value: actual_strike
        self.strike_map: dict = {}

    async def connect(self, max_retries: int = 0) -> bool:
        """Connect to IB Gateway with exponential backoff.

        Args:
            max_retries: max attempts. 0 = infinite (keeps trying forever).
        """
        if self.ib is None:
            self.ib = IB()
        attempt = 0
        delay = RECONNECT_DELAY_SEC
        while True:
            attempt += 1
            limit_str = f"/{max_retries}" if max_retries > 0 else ""
            try:
                log.info(f"  IB connecting (attempt {attempt}{limit_str})…")
                await self.ib.connectAsync(
                    IBKR_HOST, IBKR_PORT,
                    clientId=IBKR_CLIENT_ID, timeout=15,
                )
                log.info(f"  IB connected (clientId={IBKR_CLIENT_ID})")
                self.connected = True
                return True
            except Exception as e:
                log.warning(f"  IB connect attempt {attempt} failed: {e}")
                if max_retries > 0 and attempt >= max_retries:
                    log.critical("  Max IB reconnect attempts reached.")
                    return False
                log.info(f"  Retrying in {delay}s…")
                await asyncio.sleep(delay)
                # Exponential backoff: 30 → 60 → 120 → 300 (capped)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def discover_city(self, symbol: str) -> dict:
        """Discover today's actively-trading contracts for a weather symbol.

        Two expiry patterns across cities (all times US/Central):
          Pattern A (same-day): UHMDW, UHAUS, UHDCA, UHPHL — exp = today
          Pattern B (next-day): UHLAX, UHSFO, UHSEA — exp = tomorrow

        Strategy: try today first, then tomorrow. Covers both patterns in max 2 calls.
        Pick the first expiry with non-empty tradingHours (settled = empty).
        Targeted date search returns ~18 contracts in <1s vs broad returning 162+ in 60s."""
        for day_offset in range(0, 2):
            try_date = datetime.now(ET) + timedelta(days=day_offset)
            try_str = try_date.strftime("%Y%m%d")

            c = Contract()
            c.symbol = symbol
            c.secType = "OPT"
            c.exchange = "FORECASTX"
            c.currency = "USD"
            c.lastTradeDateOrContractMonth = try_str

            details = await self.ib.reqContractDetailsAsync(c)
            if not details:
                continue

            # Check tradingHours — settled contracts have empty string
            d0 = details[0]
            trading_hrs = getattr(d0, 'tradingHours', '') or ''
            if not trading_hrs:
                log.info(f"  {symbol}: exp={try_str} — settled (empty tradingHours), skipping")
                continue

            yes_map = {d.contract.strike: d.contract
                       for d in details if d.contract.right == "C"}
            no_map = {d.contract.strike: d.contract
                      for d in details if d.contract.right == "P"}
            common = sorted(set(yes_map) & set(no_map))

            if common:
                pairs = {s: (yes_map[s], no_map[s]) for s in common}
                self.contracts[symbol] = pairs
                self.expiries[symbol] = try_str

                tz_id = getattr(d0, 'timeZoneId', '?')
                liquid_hrs = getattr(d0, 'liquidHours', '?')
                log.info(f"  {symbol}: {len(common)} strikes (exp={try_str})"
                         f"  tz={tz_id}")
                log.info(f"  {symbol}: tradingHours={trading_hrs}")
                if liquid_hrs != trading_hrs:
                    log.info(f"  {symbol}: liquidHours={liquid_hrs}")

                return pairs

        log.warning(f"  {symbol}: no actively-trading contracts found")
        return {}

    async def subscribe_strikes(self, symbol: str, strikes: list) -> None:
        """Subscribe L1 market data for specific strikes of a symbol.
        L2 depth is NOT subscribed here — use subscribe_l2/cancel_l2 on demand
        (IB limits reqMktDepth to 3 concurrent streams).
        If exact strike unavailable, snaps to nearest within ±3 and records mapping."""
        if symbol not in self.contracts:
            return
        pairs = self.contracts[symbol]
        for requested in strikes:
            actual = requested
            if requested not in pairs:
                # Find nearest available strike
                available = sorted(pairs.keys())
                nearest = min(available, key=lambda s: abs(s - requested))
                if abs(nearest - requested) <= 3:
                    actual = nearest
                    log.info(f"  {symbol}: K{requested:.0f} → K{actual:.0f} (nearest)")
                else:
                    log.warning(f"  {symbol}: K{requested:.0f} not available,"
                                f" nearest K{nearest:.0f} too far")
                    continue
            # Record the mapping so read_condor_prices can resolve
            self.strike_map[(symbol, requested)] = actual
            key = (symbol, actual)
            if key in self.tickers:
                continue  # already subscribed
            yes_con, no_con = pairs[actual]
            # L1 only — best bid/ask + size (no limit on concurrent streams)
            yt = self.ib.reqMktData(yes_con, snapshot=False)
            nt = self.ib.reqMktData(no_con, snapshot=False)
            self.tickers[key] = (yt, nt)
            log.info(f"  {symbol} K{actual:.0f}: L1 subscribed")

    async def subscribe_l2(self, symbol: str, strikes: list) -> None:
        """Subscribe L2 depth for specific strikes — call before sweep,
        cancel_l2 after. IB allows max 3 concurrent reqMktDepth streams."""
        if symbol not in self.contracts:
            return
        pairs = self.contracts[symbol]
        for requested in strikes:
            actual = self.strike_map.get((symbol, requested), requested)
            key = (symbol, actual)
            if key in self.depth_tickers:
                continue  # already subscribed
            if actual not in pairs:
                continue
            yes_con, no_con = pairs[actual]
            try:
                ydt = self.ib.reqMktDepth(yes_con, numRows=10)
                ndt = self.ib.reqMktDepth(no_con, numRows=10)
                self.depth_tickers[key] = (ydt, ndt)
            except Exception as e:
                log.warning(f"  {symbol} K{actual:.0f}: L2 depth failed ({e})")
                self.depth_tickers[key] = (None, None)

    def cancel_l2(self, symbol: str, strikes: list) -> None:
        """Cancel L2 depth subscriptions to free slots for other cities."""
        if not self.ib:
            return
        pairs = self.contracts.get(symbol, {})
        for requested in strikes:
            actual = self.strike_map.get((symbol, requested), requested)
            key = (symbol, actual)
            if key not in self.depth_tickers:
                continue
            ydt, ndt = self.depth_tickers[key]
            if actual in pairs:
                yes_con, no_con = pairs[actual]
                if ydt is not None:
                    try:
                        self.ib.cancelMktDepth(yes_con)
                    except Exception:
                        pass
                if ndt is not None:
                    try:
                        self.ib.cancelMktDepth(no_con)
                    except Exception:
                        pass
            del self.depth_tickers[key]

    def read_condor_prices(self, symbol: str, yes_strike: float,
                           no_strike: float) -> tuple:
        """Read ask prices and depths for a condor pair.
        Resolves requested strikes through strike_map (handles snapped strikes).
        Returns (yes_ask, no_ask, yes_depth, no_depth, actual_yes_k, actual_no_k)."""
        ya, yad = -1.0, 0
        na, nad = -1.0, 0

        # Resolve requested → actual strikes
        actual_yes = self.strike_map.get((symbol, yes_strike), yes_strike)
        actual_no = self.strike_map.get((symbol, no_strike), no_strike)

        yes_key = (symbol, actual_yes)
        no_key = (symbol, actual_no)

        if yes_key in self.tickers:
            yt, _ = self.tickers[yes_key]
            ya = float(yt.ask) if yt.ask is not None and yt.ask > 0 else -1.0
            try:
                yad = int(yt.askSize) if yt.askSize is not None and yt.askSize == yt.askSize else 0
            except (ValueError, OverflowError):
                yad = 0  # nan or inf
        if no_key in self.tickers:
            _, nt = self.tickers[no_key]
            na = float(nt.ask) if nt.ask is not None and nt.ask > 0 else -1.0
            try:
                nad = int(nt.askSize) if nt.askSize is not None and nt.askSize == nt.askSize else 0
            except (ValueError, OverflowError):
                nad = 0  # nan or inf

        return ya, na, yad, nad, actual_yes, actual_no

    def read_hedge_ask(self, symbol: str, strike: float, losing_leg: str) -> float:
        """Read the opposing contract's ask at the SAME strike for stop-loss hedge.

        To close YES at strike K: buy NO at strike K → locks in $1.00 payout.
        To close NO at strike K:  buy YES at strike K → locks in $1.00 payout.

        Args:
            symbol: weather symbol (e.g. UHMSP)
            strike: the strike of the losing leg
            losing_leg: "YES" or "NO" — we read the OPPOSING side's ask

        Returns: opposing ask price, or -1.0 if unavailable.
        """
        actual = self.strike_map.get((symbol, strike), strike)
        key = (symbol, actual)
        if key not in self.tickers:
            return -1.0
        yes_ticker, no_ticker = self.tickers[key]
        if losing_leg == "YES":
            # Close YES by buying NO at same strike
            t = no_ticker
        else:
            # Close NO by buying YES at same strike
            t = yes_ticker
        ask = float(t.ask) if t.ask is not None and t.ask > 0 else -1.0
        return ask

    def read_l2_depth(self, symbol: str, yes_strike: float,
                      no_strike: float) -> tuple:
        """Read L2 order book depth for a condor pair.
        Returns (yes_ask_levels, no_ask_levels) where each is a list of
        (price, size) tuples sorted by price ascending.
        Falls back to L1 data if L2 is unavailable."""
        actual_yes = self.strike_map.get((symbol, yes_strike), yes_strike)
        actual_no = self.strike_map.get((symbol, no_strike), no_strike)

        yes_levels = []
        no_levels = []

        yes_key = (symbol, actual_yes)
        no_key = (symbol, actual_no)

        # YES leg L2 asks
        if yes_key in self.depth_tickers:
            ydt, _ = self.depth_tickers[yes_key]
            if ydt is not None and hasattr(ydt, 'domAsks') and ydt.domAsks:
                for level in ydt.domAsks:
                    p = float(level.price) if level.price is not None else 0
                    s = int(level.size) if level.size is not None and level.size == level.size else 0
                    if p > 0 and s > 0 and p <= MAX_LEG_PRICE:
                        yes_levels.append((p, s))
        # Fallback to L1 if no L2 data
        if not yes_levels and yes_key in self.tickers:
            yt, _ = self.tickers[yes_key]
            ya = float(yt.ask) if yt.ask is not None and yt.ask > 0 else 0
            try:
                yad = int(yt.askSize) if yt.askSize is not None and yt.askSize == yt.askSize else 0
            except (ValueError, OverflowError):
                yad = 0
            if ya > 0 and yad > 0:
                yes_levels.append((ya, yad))

        # NO leg L2 asks
        if no_key in self.depth_tickers:
            _, ndt = self.depth_tickers[no_key]
            if ndt is not None and hasattr(ndt, 'domAsks') and ndt.domAsks:
                for level in ndt.domAsks:
                    p = float(level.price) if level.price is not None else 0
                    s = int(level.size) if level.size is not None and level.size == level.size else 0
                    if p > 0 and s > 0 and p <= MAX_LEG_PRICE:
                        no_levels.append((p, s))
        # Fallback to L1
        if not no_levels and no_key in self.tickers:
            _, nt = self.tickers[no_key]
            na = float(nt.ask) if nt.ask is not None and nt.ask > 0 else 0
            try:
                nad = int(nt.askSize) if nt.askSize is not None and nt.askSize == nt.askSize else 0
            except (ValueError, OverflowError):
                nad = 0
            if na > 0 and nad > 0:
                no_levels.append((na, nad))

        # Sort by price ascending (cheapest first)
        yes_levels.sort(key=lambda x: x[0])
        no_levels.sort(key=lambda x: x[0])

        return yes_levels, no_levels

    async def refresh_daily(self):
        """Cancel all subscriptions and re-discover contracts on day rollover."""
        log.info("  Refreshing contracts for new day…")
        # Cancel L1 subscriptions — tickers are (yes_ticker, no_ticker)
        for (sym, strike), (yt, nt) in list(self.tickers.items()):
            actual = strike
            pairs = self.contracts.get(sym, {})
            if actual in pairs:
                yes_con, no_con = pairs[actual]
                try:
                    self.ib.cancelMktData(yes_con)
                except Exception:
                    pass
                try:
                    self.ib.cancelMktData(no_con)
                except Exception:
                    pass
        # Cancel L2 subscriptions — depth_tickers are (yes_depth, no_depth)
        for (sym, strike), (ydt, ndt) in list(self.depth_tickers.items()):
            actual = strike
            pairs = self.contracts.get(sym, {})
            if actual in pairs:
                yes_con, no_con = pairs[actual]
                if ydt is not None:
                    try:
                        self.ib.cancelMktDepth(yes_con)
                    except Exception:
                        pass
                if ndt is not None:
                    try:
                        self.ib.cancelMktDepth(no_con)
                    except Exception:
                        pass
        self.tickers = {}
        self.depth_tickers = {}
        self.contracts = {}
        self.expiries = {}
        self.strike_map = {}

    def disconnect(self):
        try:
            if self.ib:
                self.ib.disconnect()
                log.info("  IB disconnected.")
        except Exception:
            pass


# ─── CSV LOGGING ─────────────────────────────────────────────────────────────

def init_logs():
    """Initialize CSV files with headers if they don't exist."""
    if not os.path.exists(FORECAST_CSV):
        with open(FORECAST_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "date", "city", "symbol", "capture_time_et",
                "forecast_high_f", "narrative",
                "nws_alerts", "afd_flags", "narrative_flags",
                "hourly_swing_f", "eligible", "skip_reason",
            ])
    if not os.path.exists(POSITION_CSV):
        with open(POSITION_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "date", "city", "symbol",
                "forecast_high", "yes_strike", "no_strike",
                "yes_ask", "no_ask",
                "yes_blended", "no_blended",
                "entry_cost", "sweep_levels",
                "num_contracts", "total_cost", "entry_time",
                "yes_depth", "no_depth",
                "exited", "exit_reason", "exit_time", "exit_pnl",
                "wu_high", "yes_won", "no_won",
                "payout", "pnl", "settled",
            ])
    if not os.path.exists(DAILY_CSV):
        with open(DAILY_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "date", "phase",
                "cities_scanned", "cities_eligible", "cities_entered",
                "total_cost", "total_payout", "daily_pnl",
                "both_won", "one_won", "cumulative_pnl",
                "portfolio_usd",
            ])
    if not os.path.exists(PRICES_CSV):
        with open(PRICES_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "date", "time_et", "city", "symbol",
                "yes_strike", "no_strike",
                "yes_ask", "no_ask", "sum",
                "yes_depth", "no_depth",
                "entered", "current_temp", "wu_tracked_high",
            ])


def write_forecast_row(snap: ForecastSnapshot, swing: SwingCheckResult):
    """Append one row to condor_forecasts.csv."""
    with open(FORECAST_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            snap.date, snap.city, snap.symbol, snap.capture_time_et,
            snap.forecast_high_f, snap.narrative[:200],
            "; ".join(swing.nws_alerts[:3]),
            "; ".join(swing.afd_flags),
            "; ".join(swing.narrative_flags),
            swing.hourly_swing_f, "1" if swing.eligible else "0",
            swing.skip_reason,
        ])


def write_position_row(pos: CondorPosition):
    """Append one row to condor_positions.csv."""
    with open(POSITION_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            pos.date, pos.city, pos.symbol,
            pos.forecast_high, pos.yes_strike, pos.no_strike,
            f"{pos.yes_ask:.2f}", f"{pos.no_ask:.2f}",
            f"{pos.yes_blended:.4f}", f"{pos.no_blended:.4f}",
            f"{pos.entry_cost:.4f}", pos.sweep_levels,
            pos.num_contracts, f"{pos.total_cost:.2f}", pos.entry_time,
            pos.yes_depth, pos.no_depth,
            "1" if pos.exited else "0",
            pos.exit_reason, pos.exit_time,
            f"{pos.exit_pnl:.2f}" if pos.exited else "",
            pos.wu_high,
            "1" if pos.yes_won else "0",
            "1" if pos.no_won else "0",
            f"{pos.payout:.2f}", f"{pos.pnl:.2f}",
            "1" if pos.settled else "0",
        ])


def write_price_snapshot(date: str, cs: 'CityDayState',
                         ya: float, na: float, yad: int, nad: int,
                         actual_yes: float = 0, actual_no: float = 0):
    """Append one price snapshot row — tracks sum trajectory throughout the day."""
    entry_sum = round(ya + na, 4) if ya > 0 and na > 0 else -1
    # Use actual IB strikes if available, else fall back to computed offset
    ys = actual_yes if actual_yes > 0 else cs.forecast_high - cs.offset
    ns = actual_no if actual_no > 0 else cs.forecast_high + cs.offset
    with open(PRICES_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            date, datetime.now(ET).strftime("%H:%M:%S"),
            cs.city, cs.symbol,
            ys, ns,
            f"{ya:.2f}" if ya > 0 else "",
            f"{na:.2f}" if na > 0 else "",
            f"{entry_sum:.4f}" if entry_sum > 0 else "",
            yad, nad,
            "1" if cs.position is not None else "0",
            cs.current_temp if cs.current_temp > 0 else "",
            cs.wu_tracked_high if cs.wu_tracked_high > 0 else "",
        ])


def write_daily_row(state: PortfolioState, daily_pnl: float,
                    both_won: int, one_won: int):
    """Append one row to condor_daily.csv."""
    entered = len(state.positions)
    eligible = sum(1 for c in state.cities if c.eligible)
    total_cost = sum(p.total_cost for p in state.positions)
    total_payout = sum(p.payout for p in state.positions)
    with open(DAILY_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            state.date, state.phase,
            len(state.cities), eligible, entered,
            f"{total_cost:.2f}", f"{total_payout:.2f}", f"{daily_pnl:.2f}",
            both_won, one_won, f"{state.cumulative_pnl:.2f}",
            f"{state.portfolio_usd:.2f}",
        ])


# ─── CONSOLE OUTPUT ─────────────────────────────────────────────────────────

def print_phase_banner(phase: str, date: str, extra: str = ""):
    """Print a phase transition banner."""
    W = 65
    now_str = datetime.now(ET).strftime("%H:%M:%S ET")
    print(f"\n  {C.HEADER}{'='*W}{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  {C.VALUE}{phase}{C.RESET}"
          f"   {C.DIM}{now_str}   {date}{C.RESET}"
          f"  {C.HEADER}|{C.RESET}")
    if extra:
        print(f"  {C.HEADER}|{C.RESET}  {C.DIM}{extra}{C.RESET}"
              f"  {C.HEADER}|{C.RESET}")
    print(f"  {C.HEADER}{'='*W}{C.RESET}")


def print_scan_table(cities: list):
    """Print forecast + swing check results for all cities."""
    print(f"\n  {C.BOLD}{C.WHITE}MORNING SCAN{C.RESET}")
    print(f"  {C.HEADER}{'─'*72}{C.RESET}")
    print(f"  {C.WHITE}{'City':<16} {'Symbol':<8} {'Fcst':>5} {'NWS':>6}"
          f" {'AFD':>6} {'Narr':>6} {'Swing':>6} {'Result':>10}{C.RESET}")
    print(f"  {C.HEADER}{'─'*72}{C.RESET}")

    def _sc(text: str, width: int, color: str = "", align: str = ">") -> str:
        padded = f"{text:<{width}}" if align == "<" else f"{text:>{width}}"
        return f"{color}{padded}{C.RESET}" if color else padded

    for cs in cities:
        sw = cs.swing
        if sw is None:
            print(f"  {C.DIM}{cs.city:<16} {cs.symbol:<8} {'?':>5}"
                  f" {'?':>6} {'?':>6} {'?':>6} {'?':>6} {'NO DATA':>10}{C.RESET}")
            continue

        fcst = f"{cs.forecast_high}F" if cs.forecast_high > 0 else "?"
        nws = _sc("clear", 6, C.OK) if not sw.nws_alerts else _sc("WARN", 6, C.ALERT)
        afd = _sc("clear", 6, C.OK) if not sw.afd_flags else _sc("FLAG", 6, C.ALERT)
        narr = _sc("clear", 6, C.OK) if not sw.narrative_flags else _sc("FLAG", 6, C.ALERT)
        swing_txt = f"{sw.hourly_swing_f}F"
        swing = _sc(swing_txt, 6, C.ALERT if sw.hourly_swing_f > HOURLY_SWING_THRESHOLD_F else C.OK)

        if sw.eligible:
            result = _sc("ELIGIBLE", 10, f"{C.OK}{C.BOLD}")
        else:
            result = _sc("SKIP", 10, C.ALERT)

        print(f"  {_sc(cs.city, 16, C.VALUE, '<')} {cs.symbol:<8} {fcst:>5}"
              f" {nws} {afd} {narr} {swing} {result}")

    print(f"  {C.HEADER}{'─'*72}{C.RESET}")

    eligible = sum(1 for c in cities if c.eligible)
    print(f"\n  {C.WHITE}Eligible:{C.RESET} {C.VALUE}{eligible}{C.RESET}"
          f" / {len(cities)} cities")


def print_entry_table(positions: list):
    """Print simulated entries."""
    if not positions:
        print(f"\n  {C.DIM}No entries today (no eligible cities or all sums >= $1.00){C.RESET}")
        return

    print(f"\n  {C.BOLD}{C.WHITE}SIMULATED ENTRIES{C.RESET}")
    print(f"  {C.HEADER}{'─'*72}{C.RESET}")
    print(f"  {C.WHITE}{'City':<14} {'YES K':>6} {'NO K':>6} {'Y Ask':>6}"
          f" {'N Ask':>6} {'Sum':>6} {'Qty':>5} {'Cost':>8}{C.RESET}")
    print(f"  {C.HEADER}{'─'*72}{C.RESET}")

    total_cost = 0.0
    for p in positions:
        sum_clr = C.GREEN if p.entry_cost < 0.95 else C.YELLOW
        print(f"  {C.VALUE}{p.city:<14}{C.RESET}"
              f" K{p.yes_strike:>4.0f}  K{p.no_strike:>4.0f}"
              f" ${p.yes_ask:.2f} ${p.no_ask:.2f}"
              f" {sum_clr}${p.entry_cost:.2f}{C.RESET}"
              f" {p.num_contracts:>5} ${p.total_cost:>7.2f}")
        total_cost += p.total_cost

    print(f"  {C.HEADER}{'─'*72}{C.RESET}")
    print(f"  {C.WHITE}Total deployed:{C.RESET} {C.VALUE}${total_cost:.2f}{C.RESET}"
          f"  ({len(positions)} cities)")


def print_active_table(cities: list, prices: dict):
    """Print combined price scan + monitoring table for ACTIVE phase.
    Shows all eligible cities — entered or still watching."""
    eligible = [c for c in cities if c.eligible or c.position is not None]
    if not eligible:
        return

    now_str = datetime.now(ET).strftime("%H:%M:%S ET")
    entered = sum(1 for c in eligible if c.position is not None)
    watching = len(eligible) - entered

    print(f"\n  {C.HEADER}{'─'*106}{C.RESET}")
    print(f"  {C.BOLD}{C.WHITE}ACTIVE{C.RESET}  {C.DIM}{now_str}{C.RESET}"
          f"   {C.OK}{entered} entered{C.RESET}"
          f"   {C.CYAN}{watching} watching{C.RESET}")
    print(f"  {C.HEADER}{'─'*106}{C.RESET}")
    print(f"  {C.WHITE}{'City':<14} {'Fcst':>5} {'YES K':>6} {'NO K':>6}"
          f" {'Y Ask':>6} {'N Ask':>6} {'Sum':>6} {'Edge':>5}"
          f" {'YDp':>4} {'NDp':>4}"
          f" {'Temp':>5} {'High':>5} {'Unrl P&L':>10} {'Status':>12}{C.RESET}")
    print(f"  {C.HEADER}{'─'*106}{C.RESET}")

    # Helper: pad plain text to width, then wrap with ANSI color.
    # This prevents ANSI escape codes from breaking Python's format alignment.
    def _col(text: str, width: int, color: str = "", align: str = ">") -> str:
        padded = f"{text:<{width}}" if align == "<" else f"{text:>{width}}"
        return f"{color}{padded}{C.RESET}" if color else padded

    for cs in eligible:
        yes_strike = float(cs.forecast_high - cs.offset)
        no_strike = float(cs.forecast_high + cs.offset)
        key = cs.symbol

        ya, na, yad, nad, _, _ = prices.get(key, (-1.0, -1.0, 0, 0, 0.0, 0.0))
        entry_sum = round(ya + na, 4) if ya > 0 and na > 0 else -1

        ya_col = _col(f"${ya:.2f}", 6) if ya > 0 else _col("—", 6, C.DIM)
        na_col = _col(f"${na:.2f}", 6) if na > 0 else _col("—", 6, C.DIM)

        if entry_sum > 0:
            edge = round(2.00 - entry_sum, 2)
            sum_clr = C.GREEN if entry_sum < 1.50 else C.YELLOW if entry_sum < MAX_ENTRY_SUM else C.RED
            sum_col = _col(f"${entry_sum:.2f}", 6, sum_clr)
            edge_clr = C.GREEN if edge >= 0.10 else C.YELLOW if edge > 0 else C.RED
            edge_col = _col(f"${edge:.2f}", 5, edge_clr)
        else:
            sum_col = _col("—", 6, C.DIM)
            edge_col = _col("—", 5, C.DIM)

        yd_col = _col(str(yad), 4) if yad > 0 else _col("0", 4, C.DIM)
        nd_col = _col(str(nad), 4) if nad > 0 else _col("0", 4, C.DIM)

        temp_col = _col(f"{cs.current_temp}F" if cs.current_temp > 0 else "", 5)
        high_col = _col(f"{cs.wu_tracked_high}F" if cs.wu_tracked_high > 0 else "", 5)

        # P&L for entered positions
        if cs.position is not None:
            p = cs.position
            if p.exited:
                pnl_clr = C.GREEN if p.exit_pnl >= 0 else C.RED
                pnl_col = _col(f"${p.exit_pnl:+.2f}", 10, pnl_clr)
                status_col = _col(p.exit_reason, 12, pnl_clr)
            elif ya > 0 and na > 0:
                current_sum = round(ya + na, 4)
                unrl = round((current_sum - p.entry_cost) * p.num_contracts, 2)
                pnl_clr = C.GREEN if unrl >= 0 else C.RED
                pnl_col = _col(f"${unrl:+.2f}", 10, pnl_clr)

                if cs.no_breach:
                    status_col = _col("NO BREACH", 12, C.ALERT)
                elif cs.yes_breach:
                    status_col = _col("YES BREACH", 12, C.WARN)
                elif p.sweep_levels > 1:
                    status_col = _col(f"SWEPT x{p.num_contracts}", 12, C.OK)
                else:
                    status_col = _col(f"ENTERED x{p.num_contracts}", 12, C.OK)
            else:
                pnl_col = _col("—", 10, C.DIM)
                status_col = _col(f"ENTERED x{p.num_contracts}", 12, C.OK)
        else:
            pnl_col = _col("", 10)
            if entry_sum > 0 and entry_sum < MAX_ENTRY_SUM:
                status_col = _col("TRADEABLE", 12, C.GREEN)
            elif entry_sum > 0:
                status_col = _col("expensive", 12, C.DIM)
            else:
                status_col = _col("no price", 12, C.DIM)

        fcst_col = _col(f"{cs.forecast_high}F" if cs.forecast_high > 0 else "", 5)

        print(f"  {_col(cs.city, 14, C.VALUE, '<')}"
              f" {fcst_col}"
              f" K{yes_strike:>4.0f}  K{no_strike:>4.0f}"
              f" {ya_col} {na_col} {sum_col} {edge_col}"
              f" {yd_col} {nd_col}"
              f" {temp_col} {high_col} {pnl_col} {status_col}")

    # Portfolio P&L summary for entered positions
    total_pnl = 0.0
    total_cost = 0.0
    has_pnl = False
    all_exited = True
    for cs in eligible:
        if cs.position is not None:
            p = cs.position
            total_cost += p.total_cost
            if p.exited:
                total_pnl += p.exit_pnl
                has_pnl = True
            else:
                all_exited = False
                key = cs.symbol
                ya, na, _, _, _, _ = prices.get(key, (-1.0, -1.0, 0, 0, 0.0, 0.0))
                if ya > 0 and na > 0:
                    current_sum = round(ya + na, 4)
                    total_pnl += (current_sum - p.entry_cost) * p.num_contracts
                    has_pnl = True
    if has_pnl:
        total_pnl = round(total_pnl, 2)
        pnl_clr = C.GREEN if total_pnl >= 0 else C.RED
        pnl_label = "P&L" if all_exited else "Unrl P&L"
        print(f"  {C.HEADER}{'─'*106}{C.RESET}")
        print(f"  {C.BOLD}Portfolio:{C.RESET}"
              f"  Cost: ${total_cost:.2f}"
              f"  {pnl_label}: {pnl_clr}${total_pnl:>+.2f}{C.RESET}"
              f"  {C.DIM}({'realized' if all_exited else 'mark-to-market'}){C.RESET}")
    print(f"  {C.HEADER}{'─'*106}{C.RESET}")


def print_settlement_table(positions: list, daily_pnl: float):
    """Print settlement results."""
    if not positions:
        return

    print(f"\n  {C.BOLD}{C.WHITE}SETTLEMENT{C.RESET}")
    print(f"  {C.HEADER}{'─'*72}{C.RESET}")
    print(f"  {C.WHITE}{'City':<14} {'WU Hi':>6} {'YES K':>6} {'NO K':>6}"
          f" {'Y Won':>6} {'N Won':>6} {'Payout':>8} {'P&L':>8}{C.RESET}")
    print(f"  {C.HEADER}{'─'*72}{C.RESET}")

    def _stc(text: str, width: int, color: str = "", align: str = ">") -> str:
        padded = f"{text:<{width}}" if align == "<" else f"{text:>{width}}"
        return f"{color}{padded}{C.RESET}" if color else padded

    for p in positions:
        y_col = _stc("PAID", 6, C.OK) if p.yes_won else _stc("no", 6, C.RED)
        n_col = _stc("PAID", 6, C.OK) if p.no_won else _stc("no", 6, C.RED)
        pnl_clr = C.GREEN if p.pnl >= 0 else C.RED
        print(f"  {_stc(p.city, 14, C.VALUE, '<')}"
              f" {p.wu_high:>5}F K{p.yes_strike:>4.0f}  K{p.no_strike:>4.0f}"
              f" {y_col} {n_col}"
              f" ${p.payout:>7.2f} {_stc(f'${p.pnl:+.2f}', 8, pnl_clr)}")

    print(f"  {C.HEADER}{'─'*72}{C.RESET}")
    pnl_clr = C.GREEN if daily_pnl >= 0 else C.RED
    print(f"  {C.WHITE}Daily P&L:{C.RESET} {pnl_clr}{C.BOLD}${daily_pnl:+.2f}{C.RESET}")


# ─── PHASE HANDLERS ─────────────────────────────────────────────────────────

async def handle_waiting(state: PortfolioState) -> str:
    """WAITING phase — idle, check clock every 60s. Skip weekends.
    Never returns SCANNING — the main loop controls that transition
    via scanned_today to prevent double-scanning."""
    now_et = datetime.now(ET)

    # Before scan hour — show countdown
    if now_et.hour < SCAN_HOUR:
        scan_time = now_et.replace(hour=SCAN_HOUR, minute=SCAN_MINUTE, second=0)
        wait_min = (scan_time - now_et).total_seconds() / 60
        if int(wait_min) % 10 == 0 or wait_min < 5:
            log.info(f"  Waiting for scan time ({scan_time.strftime('%H:%M ET')})… "
                     f"{wait_min:.0f} min")

    await asyncio.sleep(WAITING_POLL_SEC)
    return "WAITING"


async def handle_scanning(state: PortfolioState, feed: CondorPriceFeed,
                          loop) -> str:
    """SCANNING phase — fetch WU forecasts + run swing detection for all cities.
    Returns next phase name."""
    print_phase_banner("SCANNING", state.date, "Fetching forecasts + swing detection")

    state.cities = []

    # Step 1: Fetch WU forecasts for all cities (parallel — WU has no rate limit)
    log.info("  Fetching WU forecasts for all cities…")
    forecast_tasks = []
    for reg in CITY_REGISTRY:
        if reg["tier"] == 0:
            continue  # skip NYC (dead OI)
        forecast_tasks.append(
            loop.run_in_executor(None, fetch_wu_forecast, reg["geocode"])
        )

    active_cities = [r for r in CITY_REGISTRY if r["tier"] > 0]
    results = await asyncio.gather(*forecast_tasks, return_exceptions=True)

    for reg, result in zip(active_cities, results):
        cs = CityDayState(
            city=reg["name"], symbol=reg["symbol"], metar=reg["metar"],
            geocode=reg["geocode"], tz=reg["tz"], offset=reg["offset"],
            tier=reg["tier"], nws_office=reg["nws_office"],
            nws_gridpoint=reg["nws_gridpoint"],
        )
        if isinstance(result, Exception):
            log.warning(f"  {reg['name']}: forecast error — {result}")
            cs.skip_reason = "FORECAST_FAILED"
        else:
            high, narrative = result
            if high is not None:
                cs.forecast_high = high
                cs.early_forecast_high = high  # save for drift check at entry time
                cs.narrative = narrative or ""
                cs.forecast_captured = True
                log.info(f"  {reg['name']}: forecast high = {high}F")

                # Save forecast snapshot
                snap = ForecastSnapshot(
                    city=reg["name"], symbol=reg["symbol"],
                    date=state.date,
                    capture_time_et=datetime.now(ET).strftime("%H:%M:%S"),
                    forecast_high_f=high, narrative=narrative or "",
                )
            else:
                cs.skip_reason = "NO_FORECAST"
                log.warning(f"  {reg['name']}: no forecast data")

        state.cities.append(cs)

    # Step 2: Swing detection (sequential for NWS rate limiting)
    log.info("  Running swing detection (NWS sequential — rate limited)…")
    for cs in state.cities:
        if not cs.forecast_captured:
            cs.eligible = False
            continue
        # Run swing check (blocking — NWS calls are sequential with delays)
        swing = await loop.run_in_executor(None, check_swing_flags, cs, loop)
        cs.swing = swing
        cs.eligible = swing.eligible
        cs.skip_reason = swing.skip_reason

        # Write forecast + swing row
        snap = ForecastSnapshot(
            city=cs.city, symbol=cs.symbol, date=state.date,
            capture_time_et=datetime.now(ET).strftime("%H:%M:%S"),
            forecast_high_f=cs.forecast_high, narrative=cs.narrative,
        )
        write_forecast_row(snap, swing)

        if swing.eligible:
            log.info(f"  {cs.city}: ELIGIBLE (all checks clear)")
        else:
            log.info(f"  {cs.city}: SKIP — {swing.skip_reason}")

    print_scan_table(state.cities)
    return "ACTIVE"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Order Book Sweep Simulation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _sweep_leg(ask_levels: list, max_contracts: int) -> tuple:
    """Sweep one leg through L2 ask levels within SWEEP_RANGE of best ask.
    ask_levels: [(price, size), ...] sorted by price ascending.
    Only takes levels where price <= best_ask + SWEEP_RANGE and <= MAX_LEG_PRICE.
    Returns (blended_price, contracts_filled, levels_used, detail_list).
    detail_list: [(price, qty_filled), ...] at each level."""
    if not ask_levels:
        return 0, 0, 0, []

    best_ask = ask_levels[0][0]
    sweep_cap = round(best_ask + SWEEP_RANGE, 4)  # e.g. $0.85 + $0.03 = $0.88
    price_cap = min(sweep_cap, MAX_LEG_PRICE)      # also respect $0.97 hard cap

    filled = 0
    cost = 0.0
    details = []
    levels_used = 0

    for price, size in ask_levels:
        if price > price_cap:
            break  # beyond sweep range — stop
        remaining = max_contracts - filled
        if remaining <= 0:
            break
        qty = min(int(size), remaining)
        if qty <= 0:
            continue
        cost += price * qty
        filled += qty
        details.append((price, qty))
        levels_used += 1

    blended = round(cost / filled, 4) if filled > 0 else 0
    return blended, filled, levels_used, details


def simulate_condor_sweep(yes_levels: list, no_levels: list,
                          per_city_budget: float) -> dict:
    """Sweep both legs using real L2 depth data.

    yes_levels / no_levels: [(price, size), ...] from L2 order book.
    Fill contracts level by level until budget is reached.
    Both legs must have equal contracts — tighter leg caps the size.

    Returns dict or None if skip.
    """
    if not yes_levels or not no_levels:
        return None

    # First pass: estimate max contracts from budget using best asks
    best_yes = yes_levels[0][0]
    best_no = no_levels[0][0]
    if best_yes <= 0 or best_no <= 0:
        return None
    best_sum = best_yes + best_no
    if best_sum >= MAX_ENTRY_SUM:
        return None

    # Max contracts we could want (budget / cheapest entry)
    budget_max = int(per_city_budget / best_sum) if best_sum > 0 else 0
    if budget_max <= 0:
        return None

    # Sweep each leg independently up to budget_max
    yes_blended, yes_filled, yes_lvls, yes_detail = _sweep_leg(yes_levels, budget_max)
    no_blended, no_filled, no_lvls, no_detail = _sweep_leg(no_levels, budget_max)

    if yes_filled <= 0 or no_filled <= 0:
        return None

    # Both legs must be equal — cap at the tighter leg
    num_contracts = min(yes_filled, no_filled)

    # Re-sweep at the capped size to get accurate blended prices
    if num_contracts < yes_filled:
        yes_blended, yes_filled, yes_lvls, yes_detail = _sweep_leg(yes_levels, num_contracts)
    if num_contracts < no_filled:
        no_blended, no_filled, no_lvls, no_detail = _sweep_leg(no_levels, num_contracts)

    blended_entry = round(yes_blended + no_blended, 4)
    if blended_entry >= MAX_ENTRY_SUM:
        return None

    # Further cap by budget at blended price
    budget_contracts = int(per_city_budget / blended_entry) if blended_entry > 0 else 0
    if budget_contracts < num_contracts:
        num_contracts = budget_contracts
        # Re-sweep again at final size
        yes_blended, yes_filled, yes_lvls, yes_detail = _sweep_leg(yes_levels, num_contracts)
        no_blended, no_filled, no_lvls, no_detail = _sweep_leg(no_levels, num_contracts)
        blended_entry = round(yes_blended + no_blended, 4)

    if num_contracts <= 0:
        return None

    total_cost = round(blended_entry * num_contracts, 2)

    # Range strings for logging
    yes_min = yes_detail[0][0] if yes_detail else 0
    yes_max = yes_detail[-1][0] if yes_detail else 0
    no_min = no_detail[0][0] if no_detail else 0
    no_max = no_detail[-1][0] if no_detail else 0
    yes_range = f"${yes_min:.2f}" if yes_lvls == 1 else f"${yes_min:.2f}-${yes_max:.2f}"
    no_range = f"${no_min:.2f}" if no_lvls == 1 else f"${no_min:.2f}-${no_max:.2f}"
    sweep_levels = max(yes_lvls, no_lvls)

    return {
        'yes_blended': round(yes_blended, 4),
        'no_blended': round(no_blended, 4),
        'entry_cost': blended_entry,
        'num_contracts': num_contracts,
        'total_cost': total_cost,
        'sweep_levels': sweep_levels,
        'yes_range': yes_range,
        'no_range': no_range,
        'yes_detail': yes_detail,
        'no_detail': no_detail,
    }


async def handle_active(state: PortfolioState, feed: CondorPriceFeed,
                        loop, first_tick: bool = False) -> str:
    """ACTIVE phase — two-speed polling:
      - IB prices every 15s (free cached reads, rich price data)
      - WU current temp every 5 min (HTTP calls, WU updates ~every 10 min)
    Before ENTRY_HOUR: monitors prices only (book is too thin to enter).
    At ENTRY_HOUR: drift-checks forecasts, then sweeps order book for entries.
    Monitors breaches + take-profit/stop-loss on entered positions throughout.
    Returns SETTLING when settlement hour arrives."""
    now_et = datetime.now(ET)
    if now_et.hour >= SETTLE_HOUR:
        return "SETTLING"

    eligible = [c for c in state.cities if c.eligible or c.position is not None]
    if not eligible:
        if not getattr(state, '_no_eligible_logged', False):
            log.info(f"  {C.DIM}No eligible cities — waiting for settlement{C.RESET}")
            state._no_eligible_logged = True
        await asyncio.sleep(ACTIVE_PRICE_POLL_SEC)
        return "ACTIVE"

    # On first tick: subscribe IB to target strikes for all eligible cities
    if first_tick and feed.connected:
        print_phase_banner("ACTIVE", state.date,
                           f"{len(eligible)} cities — prices every {ACTIVE_PRICE_POLL_SEC}s"
                           f", WU temp every {ACTIVE_WU_POLL_SEC}s"
                           f", entries at {ENTRY_HOUR}:{ENTRY_MINUTE:02d} ET")
        for cs in eligible:
            if cs.symbol not in feed.contracts:
                await feed.discover_city(cs.symbol)
            yes_strike = float(cs.forecast_high - cs.offset)
            no_strike = float(cs.forecast_high + cs.offset)
            await feed.subscribe_strikes(cs.symbol, [yes_strike, no_strike])
        log.info(f"  Waiting {IB_WARMUP_SEC}s for IB prices…")
        await asyncio.sleep(IB_WARMUP_SEC)
        # Initialize WU poll tracker
        state._last_wu_poll = 0.0

    # ── Read IB prices (every tick — free cached reads) ─────────────────
    prices = {}  # symbol → (ya, na, yad, nad, actual_yes, actual_no)
    if feed.connected:
        for cs in eligible:
            yes_strike = float(cs.forecast_high - cs.offset)
            no_strike = float(cs.forecast_high + cs.offset)
            ya, na, yad, nad, actual_yes, actual_no = feed.read_condor_prices(
                cs.symbol, yes_strike, no_strike)
            prices[cs.symbol] = (ya, na, yad, nad, actual_yes, actual_no)
            # Log price snapshot to CSV
            write_price_snapshot(state.date, cs, ya, na, yad, nad,
                                actual_yes, actual_no)

    # ── Poll WU current temp (only every ACTIVE_WU_POLL_SEC) ────────────
    now_ts = time.time()
    last_wu = getattr(state, '_last_wu_poll', 0.0)
    if now_ts - last_wu >= ACTIVE_WU_POLL_SEC:
        state._last_wu_poll = now_ts
        tasks = [loop.run_in_executor(None, fetch_wu_current, c.geocode)
                 for c in eligible]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        wu_updates = []
        for cs, result in zip(eligible, results):
            if isinstance(result, Exception) or result is None:
                continue
            old_high = cs.wu_tracked_high
            cs.current_temp = result
            if result > cs.wu_tracked_high:
                cs.wu_tracked_high = result
            wu_updates.append(f"{cs.city}={result}F(high={cs.wu_tracked_high}F)")
        if wu_updates:
            log.info(f"  WU temp poll: {', '.join(wu_updates)}")

    # ── Sweep entry — new positions + accumulate on partial fills ─────
    # Gate: don't attempt entries until ENTRY_HOUR:ENTRY_MINUTE ET.
    # Book is empty before ~9:30 AM — no point sweeping thin air.
    # Price reads, WU polls, and take-profit/stop-loss checks still run.
    entry_open = (now_et.hour > ENTRY_HOUR or
                  (now_et.hour == ENTRY_HOUR and now_et.minute >= ENTRY_MINUTE))
    if not entry_open:
        if not getattr(state, '_entry_wait_logged', False):
            log.info(f"  {C.DIM}Monitoring prices — entries open at"
                     f" {ENTRY_HOUR}:{ENTRY_MINUTE:02d} ET{C.RESET}")
            state._entry_wait_logged = True

    # ── Forecast refresh at entry time — always use latest strikes ──────
    # Re-fetch WU at 9:30 AM for better accuracy. Always update strikes.
    # Only skip on extreme drift (≥6°F) — means forecast is genuinely unstable.
    if entry_open and not getattr(state, '_drift_checked', False):
        state._drift_checked = True
        log.info("  Re-fetching WU forecasts (9:30 AM refresh)…")
        drift_tasks = [loop.run_in_executor(None, fetch_wu_forecast, c.geocode)
                       for c in eligible if c.forecast_captured]
        drift_cities = [c for c in eligible if c.forecast_captured]
        drift_results = await asyncio.gather(*drift_tasks, return_exceptions=True)
        for cs, result in zip(drift_cities, drift_results):
            if isinstance(result, Exception) or result is None:
                continue
            new_high, new_narr = result
            if new_high is None:
                continue
            old_high = cs.early_forecast_high
            drift = abs(new_high - old_high)
            if drift > 0:
                log.info(f"  {cs.city}: forecast updated {old_high}F → {new_high}F"
                         f" (Δ{drift}F)")
            if drift >= FORECAST_DRIFT_EXTREME_F:
                # Extreme drift — forecast is genuinely unstable, skip
                cs.eligible = False
                cs.skip_reason = f"FORECAST_DRIFT: {old_high}F→{new_high}F (Δ{drift}F)"
                log.info(f"  {C.WARN}{cs.city}: SKIP — extreme drift {drift}F"
                         f" (>={FORECAST_DRIFT_EXTREME_F}F){C.RESET}")
            else:
                # Normal drift — update to latest forecast for better strikes
                cs.forecast_high = new_high
                cs.narrative = new_narr or cs.narrative
        # Re-subscribe IB to updated strikes
        if feed.connected:
            for cs in eligible:
                if cs.eligible:
                    yes_strike = float(cs.forecast_high - cs.offset)
                    no_strike = float(cs.forecast_high + cs.offset)
                    await feed.subscribe_strikes(cs.symbol, [yes_strike, no_strike])

    # Cities eligible to accumulate: no position yet, or position exists
    # but budget not fully filled (and not exited).
    can_enter = [c for c in eligible if c.eligible and (
        c.position is None or
        (c.position is not None and not c.position.exited
         and not c.position.settled))]
    if entry_open and can_enter and feed.connected:
        per_city_budget = MAX_CITY_BUDGET

        for cs in can_enter:
            # Check total portfolio cap
            if state.allocated_today >= state.portfolio_usd:
                break  # portfolio fully deployed

            ya, na, yad, nad, actual_yes, actual_no = prices.get(
                cs.symbol, (-1.0, -1.0, 0, 0, 0.0, 0.0))
            if ya <= 0 or na <= 0:
                # Log once per city, not every 15s — suppress spam
                flag = f"_no_price_{cs.symbol}"
                if not getattr(state, flag, False):
                    log.info(f"  {C.DIM}{cs.city}: no price (Y=${ya:.2f} N=${na:.2f}){C.RESET}")
                    setattr(state, flag, True)
                continue

            # How much budget remains for this city?
            already_spent = cs.position.total_cost if cs.position else 0.0
            remaining_budget = min(per_city_budget - already_spent,
                                   state.portfolio_usd - state.allocated_today)
            if remaining_budget < 1.0:
                flag = f"_budget_full_{cs.symbol}"
                if not getattr(state, flag, False):
                    log.info(f"  {C.DIM}{cs.city}: budget full"
                             f" (${already_spent:.0f}/${per_city_budget:.0f}){C.RESET}")
                    setattr(state, flag, True)
                continue

            entry_sum = round(ya + na, 4)

            # Read L2 depth for real order book levels.
            # Subscribe L2 on-demand for this city only (IB max 3 concurrent),
            # wait briefly for data, then cancel after reading.
            yes_strike = float(cs.forecast_high - cs.offset)
            no_strike = float(cs.forecast_high + cs.offset)
            # Subscribe one strike at a time — IB max 3 concurrent reqMktDepth.
            # Each strike subscribes YES+NO = 2 streams. Two strikes = 4 = Error 309.
            # Pass 1: YES strike L2
            await feed.subscribe_l2(cs.symbol, [yes_strike])
            await asyncio.sleep(2)
            yes_levels, _ = feed.read_l2_depth(
                cs.symbol, yes_strike, no_strike)
            feed.cancel_l2(cs.symbol, [yes_strike])
            # Pass 2: NO strike L2
            await feed.subscribe_l2(cs.symbol, [no_strike])
            await asyncio.sleep(2)
            _, no_levels = feed.read_l2_depth(
                cs.symbol, yes_strike, no_strike)
            feed.cancel_l2(cs.symbol, [no_strike])

            sweep = simulate_condor_sweep(yes_levels, no_levels, remaining_budget)
            if sweep is None:
                # Diagnose why
                if not yes_levels and not no_levels:
                    reason = "no L2 depth"
                elif not yes_levels:
                    reason = "no YES depth"
                elif not no_levels:
                    reason = "no NO depth"
                elif entry_sum >= MAX_ENTRY_SUM:
                    reason = f"sum ${entry_sum:.2f} >= ${MAX_ENTRY_SUM:.2f}"
                else:
                    reason = f"blended too high after sweep"
                # Log skip reason once per city per reason — avoid spam
                flag = f"_skip_{cs.symbol}_{reason[:10]}"
                if not getattr(state, flag, False):
                    log.info(f"  {C.DIM}{cs.city}: skip — {reason}"
                             f"  (Y=${ya:.2f} N=${na:.2f}"
                             f"  L2: {len(yes_levels)}Y/{len(no_levels)}N lvls){C.RESET}")
                    setattr(state, flag, True)
                continue

            if cs.position is None:
                # ── First fill — create new position ──
                pos = CondorPosition(
                    city=cs.city, symbol=cs.symbol, date=state.date,
                    forecast_high=cs.forecast_high,
                    yes_strike=actual_yes, no_strike=actual_no,
                    yes_ask=ya, no_ask=na,
                    yes_blended=sweep['yes_blended'],
                    no_blended=sweep['no_blended'],
                    entry_cost=sweep['entry_cost'],
                    num_contracts=sweep['num_contracts'],
                    total_cost=sweep['total_cost'],
                    entry_time=datetime.now(ET).strftime("%H:%M:%S"),
                    yes_depth=yad, no_depth=nad,
                    sweep_levels=sweep['sweep_levels'],
                )
                cs.position = pos
                state.positions.append(pos)
                state.allocated_today += pos.total_cost

                log.info(f"  {C.OK}{C.BOLD}ENTRY (SWEEP){C.RESET} {cs.city}:"
                         f" K{actual_yes:.0f}Y+K{actual_no:.0f}N"
                         f"  YES {sweep['yes_range']}"
                         f"  NO {sweep['no_range']}"
                         f"  blended ${sweep['entry_cost']:.4f}"
                         f" x{sweep['num_contracts']}"
                         f" = ${sweep['total_cost']:.2f}"
                         f"  ({sweep['sweep_levels']} levels)")
                send_telegram(
                    f"*Condor Entry — {cs.city}*\n"
                    f"K{actual_yes:.0f}Y + K{actual_no:.0f}N\n"
                    f"YES: {sweep['yes_range']}  NO: {sweep['no_range']}\n"
                    f"Blended: `${sweep['entry_cost']:.4f}` x{sweep['num_contracts']}"
                    f" = `${sweep['total_cost']:.2f}`"
                )
            else:
                # ── Accumulate — add contracts to existing position ──
                p = cs.position
                old_qty = p.num_contracts
                new_qty = sweep['num_contracts']
                combined_qty = old_qty + new_qty

                # Recalculate blended averages across all fills
                p.yes_blended = round(
                    (p.yes_blended * old_qty + sweep['yes_blended'] * new_qty)
                    / combined_qty, 4)
                p.no_blended = round(
                    (p.no_blended * old_qty + sweep['no_blended'] * new_qty)
                    / combined_qty, 4)
                p.entry_cost = round(p.yes_blended + p.no_blended, 4)
                p.num_contracts = combined_qty
                p.total_cost = round(p.total_cost + sweep['total_cost'], 2)  # actual spend, not recomputed
                p.yes_ask = ya  # update to latest best ask
                p.no_ask = na
                p.sweep_levels += sweep['sweep_levels']
                state.allocated_today += sweep['total_cost']

                log.info(f"  {C.OK}{C.BOLD}ACCUMULATE{C.RESET} {cs.city}:"
                         f" +{new_qty} contracts"
                         f"  YES {sweep['yes_range']}"
                         f"  NO {sweep['no_range']}"
                         f"  this fill ${sweep['entry_cost']:.4f}"
                         f"  total now x{combined_qty}"
                         f" = ${p.total_cost:.2f}"
                         f"  (blended ${p.entry_cost:.4f})")
                send_telegram(
                    f"*Condor Accumulate — {cs.city}*\n"
                    f"+{new_qty} contracts (total: {combined_qty})\n"
                    f"This fill: `${sweep['entry_cost']:.4f}`\n"
                    f"Blended: `${p.entry_cost:.4f}` x{combined_qty}"
                    f" = `${p.total_cost:.2f}`"
                )

    # ── Monitor entered cities — take-profit + stop-loss ─────────────────
    for cs in eligible:
        if cs.position is None or cs.position.exited:
            continue
        p = cs.position
        temp = cs.current_temp

        # ── Take-profit: hedge cost low enough to lock in TAKE_PROFIT ──
        # To exit both legs on ForecastEx:
        #   Close YES K_yes: buy NO at K_yes → cost = no_k_yes_ask, locks $1.00
        #   Close NO  K_no:  buy YES at K_no → cost = yes_k_no_ask, locks $1.00
        #   Locked payout: $2.00. Locked P&L = $2.00 - entry - hedge_yes - hedge_no
        ya, na, _, _, _, _ = prices.get(cs.symbol, (-1.0, -1.0, 0, 0, 0.0, 0.0))
        if ya > 0 and na > 0:
            # Read hedge prices at each strike
            hedge_yes = feed.read_hedge_ask(cs.symbol, p.yes_strike, "YES")
            hedge_no = feed.read_hedge_ask(cs.symbol, p.no_strike, "NO")
            if hedge_yes > 0 and hedge_no > 0:
                total_hedge = round(hedge_yes + hedge_no, 4)
                profit_per = round(2.00 - p.entry_cost - total_hedge, 4)
                if profit_per >= TAKE_PROFIT:
                    exit_pnl = round(profit_per * p.num_contracts, 2)
                    p.exited = True
                    p.exit_reason = "TAKE_PROFIT"
                    p.exit_time = datetime.now(ET).strftime("%H:%M:%S")
                    p.exit_sum = total_hedge
                    p.exit_pnl = exit_pnl
                    p.hedge_cost = total_hedge
                    log.info(f"  {C.OK}{C.BOLD}TAKE PROFIT{C.RESET} {cs.city}:"
                             f" $2.00 - entry ${p.entry_cost:.2f}"
                             f" - hedge ${total_hedge:.2f}"
                             f" = ${profit_per:.2f}/ct × {p.num_contracts}"
                             f" = {C.OK}${exit_pnl:+.2f}{C.RESET}")
                    send_telegram(
                        f"*TAKE PROFIT — {cs.city}*\n"
                        f"$2.00 - entry `${p.entry_cost:.2f}`"
                        f" - hedge `${total_hedge:.2f}`"
                        f" = `${profit_per:.2f}`/ct\n"
                        f"P&L: `${exit_pnl:+.2f}`"
                    )
                    continue  # skip breach check — already exited

        # ── Stop-loss: temp crosses a strike ───────────────────────────
        if temp <= 0:
            continue

        # Track breaches
        # NO breach: fires immediately — temp already exceeded, NO is losing
        if temp > p.no_strike and not cs.no_breach:
            cs.no_breach = True
            log.info(f"  {cs.city}: NO BREACH — temp {temp}F > K{p.no_strike:.0f}")

        # YES breach: only after peak heating in the CITY'S LOCAL timezone —
        # before that, temp hasn't peaked yet and YES can still recover.
        # Peak heating is ~12-3 PM local; check at 3 PM local, not fixed ET.
        local_hour = datetime.now(cs.tz).hour
        if (local_hour >= YES_SL_LOCAL_HOUR
                and cs.wu_tracked_high <= p.yes_strike
                and not cs.yes_breach):
            cs.yes_breach = True
            local_tz_name = str(cs.tz).split("/")[-1]
            log.info(f"  {cs.city}: YES BREACH — tracked high {cs.wu_tracked_high}F"
                     f" <= K{p.yes_strike:.0f} (after {YES_SL_LOCAL_HOUR}:00 {local_tz_name})")

        # Stop-loss exit: temp crosses a strike → hedge the losing leg
        # by buying the opposing contract at the SAME strike.
        #
        # Example: NO K49 losing (temp > 49) → buy YES K49 to lock in $1.00
        #   Hedge cost = YES_K49_ask per contract
        #   NO leg P&L = $1.00 - NO_entry - YES_K49_ask  (locked, doesn't depend on settlement)
        #   YES leg continues to settlement normally.
        #
        # NO leg losing: temp > no_strike (temp exceeded, NO pays $0)
        if cs.no_breach and not p.exited:
            # Read hedge price: buy YES at the NO strike to close the NO leg
            hedge_ask = feed.read_hedge_ask(cs.symbol, p.no_strike, "NO")
            if hedge_ask > 0:
                # Hedged NO leg: locked payout $1.00, cost = no_entry + hedge_ask
                no_leg_pnl = round((1.00 - p.no_blended - hedge_ask) * p.num_contracts, 2)
                # YES leg continues to settlement — estimate using current ask
                # (will be recalculated at settlement with actual temp)
                yes_leg_est = round((1.00 - p.yes_blended) * p.num_contracts, 2) if ya > 0 else 0
                exit_pnl = round(no_leg_pnl + yes_leg_est, 2)

                p.exited = True
                p.exit_reason = "STOP_LOSS_NO"
                p.exit_time = datetime.now(ET).strftime("%H:%M:%S")
                p.exit_sum = round(hedge_ask + (ya if ya > 0 else 0), 4)
                p.exit_pnl = no_leg_pnl  # only the hedged leg's P&L is locked
                p.hedge_cost = hedge_ask
                log.info(f"  {C.ALERT}{C.BOLD}STOP LOSS (NO){C.RESET} {cs.city}:"
                         f" temp {temp}F > K{p.no_strike:.0f}"
                         f"  hedge: buy YES K{p.no_strike:.0f} @ ${hedge_ask:.2f}"
                         f"  NO leg locked: $1.00 - ${p.no_blended:.2f} - ${hedge_ask:.2f}"
                         f" = ${1.00 - p.no_blended - hedge_ask:+.2f}/ct"
                         f"  x{p.num_contracts} = {C.ALERT}${no_leg_pnl:+.2f}{C.RESET}"
                         f"  (YES leg continues to settlement)")
                send_telegram(
                    f"*STOP LOSS (NO) — {cs.city}*\n"
                    f"Temp `{temp}F` > K{p.no_strike:.0f} (NO losing)\n"
                    f"Hedge: buy YES K{p.no_strike:.0f} @ `${hedge_ask:.2f}`\n"
                    f"NO leg P&L: `${no_leg_pnl:+.2f}` (locked)\n"
                    f"YES leg continues to settlement"
                )
            else:
                log.warning(f"  {cs.city}: NO BREACH but no hedge price available"
                            f" (YES K{p.no_strike:.0f} ask unavailable)")

        # YES leg losing: tracked high <= yes_strike after peak heating
        if cs.yes_breach and not p.exited:
            # Read hedge price: buy NO at the YES strike to close the YES leg
            hedge_ask = feed.read_hedge_ask(cs.symbol, p.yes_strike, "YES")
            if hedge_ask > 0:
                # Hedged YES leg: locked payout $1.00, cost = yes_entry + hedge_ask
                yes_leg_pnl = round((1.00 - p.yes_blended - hedge_ask) * p.num_contracts, 2)
                # NO leg continues to settlement
                no_leg_est = round((1.00 - p.no_blended) * p.num_contracts, 2) if na > 0 else 0
                exit_pnl = round(yes_leg_pnl + no_leg_est, 2)

                p.exited = True
                p.exit_reason = "STOP_LOSS_YES"
                p.exit_time = datetime.now(ET).strftime("%H:%M:%S")
                p.exit_sum = round(hedge_ask + (na if na > 0 else 0), 4)
                p.exit_pnl = yes_leg_pnl  # only the hedged leg's P&L is locked
                p.hedge_cost = hedge_ask
                log.info(f"  {C.ALERT}{C.BOLD}STOP LOSS (YES){C.RESET} {cs.city}:"
                         f" tracked high {cs.wu_tracked_high}F <= K{p.yes_strike:.0f}"
                         f"  hedge: buy NO K{p.yes_strike:.0f} @ ${hedge_ask:.2f}"
                         f"  YES leg locked: $1.00 - ${p.yes_blended:.2f} - ${hedge_ask:.2f}"
                         f" = ${1.00 - p.yes_blended - hedge_ask:+.2f}/ct"
                         f"  x{p.num_contracts} = {C.ALERT}${yes_leg_pnl:+.2f}{C.RESET}"
                         f"  (NO leg continues to settlement)")
                send_telegram(
                    f"*STOP LOSS (YES) — {cs.city}*\n"
                    f"Tracked high `{cs.wu_tracked_high}F` <= K{p.yes_strike:.0f} (YES losing)\n"
                    f"Hedge: buy NO K{p.yes_strike:.0f} @ `${hedge_ask:.2f}`\n"
                    f"YES leg P&L: `${yes_leg_pnl:+.2f}` (locked)\n"
                    f"NO leg continues to settlement"
                )

    # ── Display (throttled — every 5 minutes, not every 15s) ─────────
    last_print = getattr(state, '_last_print', 0.0)
    # Table every 60s, dot heartbeat every 15s so user knows bot is alive
    if now_ts - last_print >= 60 or first_tick:
        state._last_print = now_ts
        print_active_table(state.cities, prices)
    else:
        # Per-position heartbeat table between full refreshes
        entered = sum(1 for c in eligible if c.position is not None)
        if entered > 0:
            total_pnl = 0.0
            rows = []
            for cs in eligible:
                if cs.position is None:
                    continue
                p = cs.position
                fcst = f"{cs.forecast_high}°F" if cs.forecast_high > 0 else "—"
                yk = f"K{p.yes_strike:.0f}" if p.yes_strike > 0 else "—"
                nk = f"K{p.no_strike:.0f}" if p.no_strike > 0 else "—"
                if p.exited:
                    total_pnl += p.exit_pnl
                    pclr = C.GREEN if p.exit_pnl >= 0 else C.RED
                    rows.append((cs.city, fcst, yk, nk,
                                 "—", "—", "—",     # entry YES/NO/sum
                                 "—", "—", "—",     # curr YES/NO/sum
                                 f"x{p.num_contracts}", "—",
                                 f"{p.exit_reason:<12}",
                                 f"{pclr}{f'${p.exit_pnl:+.2f}':>10}{C.RESET}"))
                else:
                    ya, na, _, _, _, _ = prices.get(
                        cs.symbol, (-1.0, -1.0, 0, 0, 0.0, 0.0))
                    if ya > 0 and na > 0:
                        curr_sum = round(ya + na, 4)
                        pos_pnl = round((curr_sum - p.entry_cost) * p.num_contracts, 2)
                        total_pnl += pos_pnl
                        pclr = C.GREEN if pos_pnl >= 0 else C.RED
                        temp_str = (f"{cs.current_temp}°F"
                                    if cs.current_temp > 0 else "—")
                        status = f"{'ACTIVE':<12}"
                        if cs.yes_breach:
                            status = f"{C.RED}{'Y-BREACH':<12}{C.RESET}"
                        elif cs.no_breach:
                            status = f"{C.RED}{'N-BREACH':<12}{C.RESET}"
                        rows.append((
                            cs.city, fcst, yk, nk,
                            f"{p.yes_blended:.2f}", f"{p.no_blended:.2f}",
                            f"{p.entry_cost:.2f}",
                            f"{ya:.2f}", f"{na:.2f}", f"{curr_sum:.2f}",
                            f"x{p.num_contracts}", temp_str,
                            status,
                            f"{pclr}{f'${pos_pnl:+.2f}':>10}{C.RESET}"))
                    else:
                        rows.append((cs.city, fcst, yk, nk,
                                     f"{p.yes_blended:.2f}", f"{p.no_blended:.2f}",
                                     f"{p.entry_cost:.2f}",
                                     "—", "—", "—",
                                     f"x{p.num_contracts}", "—",
                                     f"{'—':<12}", f"{'—':>10}"))
            total_pnl = round(total_pnl, 2)
            pnl_clr = C.GREEN if total_pnl >= 0 else C.RED
            now_str = datetime.now(ET).strftime('%H:%M:%S')
            HW = 132
            print(f"\n  {C.DIM}· {now_str}{C.RESET}"
                  f"  {pnl_clr}Total P&L: ${total_pnl:+.2f}{C.RESET}")
            print(f"  {C.HEADER}{'─'*HW}{C.RESET}")
            print(f"  {C.DIM}{'City':<14} {'Fcst':>5} {'YES°':>5} {'NO°':>5}"
                  f" {'':>1}{'── Entry ──':^18}"
                  f" {'':>1}{'── Current ──':^18}"
                  f" {'Qty':>7} {'Temp':>6} {'Status':<12} {'P&L':>10}{C.RESET}")
            print(f"  {C.DIM}{'':<14} {'':>5} {'':>5} {'':>5}"
                  f" {'YES':>6} {'NO':>6} {'Sum':>6}"
                  f" {'YES':>6} {'NO':>6} {'Sum':>6}"
                  f" {'':>7} {'':>6} {'':>12} {'':>10}{C.RESET}")
            print(f"  {C.HEADER}{'─'*HW}{C.RESET}")
            for (city, fcst, yk, nk, ey, en, es,
                 cy, cn, cs_sum, qty, temp, status, pnl) in rows:
                print(f"  {city:<14} {fcst:>5} {yk:>5} {nk:>5}"
                      f" {ey:>6} {en:>6} {es:>6}"
                      f" {cy:>6} {cn:>6} {cs_sum:>6}"
                      f" {qty:>7} {temp:>6} {status} {pnl}")
            print(f"  {C.HEADER}{'─'*HW}{C.RESET}", flush=True)
        else:
            print(f"  {C.DIM}· {datetime.now(ET).strftime('%H:%M:%S')}"
                  f"  {len(eligible)} watching — no entries yet{C.RESET}",
                  flush=True)
    await asyncio.sleep(ACTIVE_PRICE_POLL_SEC)
    return "ACTIVE"


async def handle_settling(state: PortfolioState, loop) -> str:
    """SETTLING phase — fetch WU settlement high, compute P&L.
    Returns next phase."""
    print_phase_banner("SETTLING", state.date, "Computing settlement")

    if not state.positions:
        log.info("  No positions to settle.")
        return "REPORTING"

    # For each position, use our tracked WU high (max of all current temp readings)
    # Also fetch WU settlement as cross-reference
    # Skip positions that were already exited (take-profit or stop-loss)
    active = [c for c in state.cities if c.position is not None and not c.position.exited]

    # Settle exited positions — stop-loss hedges only one leg,
    # the other leg still settles based on actual temperature.
    exited = [c for c in state.cities if c.position is not None and c.position.exited]
    for cs in exited:
        p = cs.position
        p.wu_high = cs.wu_tracked_high

        if p.exit_reason == "TAKE_PROFIT":
            # Take-profit exits both legs — P&L is fully locked at exit
            p.pnl = p.exit_pnl
            p.settled = True
            log.info(f"  {cs.city}: TAKE PROFIT at {p.exit_time}"
                     f"  P&L: ${p.exit_pnl:+.2f}  tracked high: {cs.wu_tracked_high}F")

        elif p.exit_reason.startswith("STOP_LOSS"):
            # Stop-loss hedged ONE leg; the other settles on actual temp.
            # exit_pnl = hedged leg P&L (locked at exit time)
            hedged_pnl = p.exit_pnl
            wu_high = cs.wu_tracked_high

            if p.exit_reason == "STOP_LOSS_NO":
                # NO leg was hedged (locked); YES leg settles normally
                yes_won = wu_high > p.yes_strike
                yes_payout = 1.00 * p.num_contracts if yes_won else 0
                yes_cost = round(p.yes_blended * p.num_contracts, 2)
                yes_pnl = round(yes_payout - yes_cost, 2)
                p.yes_won = yes_won
                p.no_won = False  # NO was losing, that's why we hedged
                p.pnl = round(hedged_pnl + yes_pnl, 2)
                log.info(f"  {cs.city}: STOP LOSS (NO) hedged at {p.exit_time}"
                         f"  NO leg locked: ${hedged_pnl:+.2f}"
                         f"  YES leg settled: wu_high={wu_high}F"
                         f" {'WON' if yes_won else 'lost'} ${yes_pnl:+.2f}"
                         f"  Total P&L: ${p.pnl:+.2f}")
            else:
                # YES leg was hedged (locked); NO leg settles normally
                no_won = wu_high <= p.no_strike
                no_payout = 1.00 * p.num_contracts if no_won else 0
                no_cost = round(p.no_blended * p.num_contracts, 2)
                no_pnl = round(no_payout - no_cost, 2)
                p.no_won = no_won
                p.yes_won = False  # YES was losing, that's why we hedged
                p.pnl = round(hedged_pnl + no_pnl, 2)
                log.info(f"  {cs.city}: STOP LOSS (YES) hedged at {p.exit_time}"
                         f"  YES leg locked: ${hedged_pnl:+.2f}"
                         f"  NO leg settled: wu_high={wu_high}F"
                         f" {'WON' if no_won else 'lost'} ${no_pnl:+.2f}"
                         f"  Total P&L: ${p.pnl:+.2f}")

            # Payout = hedged pair ($1.00 always) + open leg settlement
            hedged_pair_payout = 1.00 * p.num_contracts  # YES+NO at same strike always = $1.00
            open_leg_payout = (
                (1.00 * p.num_contracts if p.yes_won else 0)
                if p.exit_reason == "STOP_LOSS_NO"
                else (1.00 * p.num_contracts if p.no_won else 0)
            )
            p.payout = round(hedged_pair_payout + open_leg_payout, 2)
            p.settled = True
        else:
            # Unknown exit reason — use exit_pnl as-is
            p.pnl = p.exit_pnl
            p.settled = True
            log.info(f"  {cs.city}: exited ({p.exit_reason}) at {p.exit_time}"
                     f"  P&L: ${p.exit_pnl:+.2f}  tracked high: {cs.wu_tracked_high}F")

        write_position_row(p)

    if not active:
        log.info("  All positions exited during ACTIVE phase — no settlement needed.")
        return "REPORTING"

    # Parallel settlement fetch
    tasks = [loop.run_in_executor(None, fetch_wu_settlement, c.geocode)
             for c in active]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for cs, api_high in zip(active, results):
        p = cs.position
        # Primary: our tracked high from MONITORING polls (max of all WU current temp readings)
        # Fallback ONLY: WU API temperatureMax24Hour (rolling 24h — can bleed yesterday's high)
        wu_high = cs.wu_tracked_high
        if wu_high <= 0 and isinstance(api_high, int) and api_high is not None:
            log.info(f"  {cs.city}: no tracked high — using API fallback {api_high}F")
            wu_high = api_high
        elif isinstance(api_high, int) and api_high is not None:
            log.info(f"  {cs.city}: tracked={wu_high}F  API(24h)={api_high}F — using tracked")

        # Guard: if both sources returned 0/None, don't auto-settle with bad data
        if wu_high <= 0:
            log.critical(f"  {C.ALERT}{cs.city}: wu_high=0 — SKIPPING settlement"
                         f" (both WU sources failed){C.RESET}")
            continue

        p.wu_high = wu_high
        p.yes_won = wu_high > p.yes_strike    # "exceed X" = strictly > X
        p.no_won = wu_high <= p.no_strike      # NO pays if temp does NOT exceed strike
        wins = int(p.yes_won) + int(p.no_won)
        p.payout = round(wins * 1.00 * p.num_contracts, 2)
        p.pnl = round(p.payout - p.total_cost, 2)
        p.settled = True

        log.info(f"  {cs.city}: wu_high={wu_high}F  "
                 f"YES({'WON' if p.yes_won else 'lost'}) "
                 f"NO({'WON' if p.no_won else 'lost'})  "
                 f"payout=${p.payout:.2f}  pnl=${p.pnl:+.2f}")

        write_position_row(p)

    return "REPORTING"


async def handle_reporting(state: PortfolioState) -> str:
    """REPORTING phase — print summary, write CSVs, Telegram, reset."""
    print_phase_banner("REPORTING", state.date, "Daily summary")

    # P&L: all positions now have p.pnl set correctly at settlement
    # (stop-loss positions include hedged leg + settled leg; take-profit = exit_pnl)
    daily_pnl = sum(p.pnl for p in state.positions)
    both_won = sum(1 for p in state.positions if p.yes_won and p.no_won)
    one_won = sum(1 for p in state.positions if (p.yes_won or p.no_won) and not (p.yes_won and p.no_won))
    exited_count = sum(1 for p in state.positions if p.exited)
    tp_count = sum(1 for p in state.positions if p.exit_reason == "TAKE_PROFIT")
    sl_count = sum(1 for p in state.positions if p.exit_reason.startswith("STOP_LOSS"))

    state.cumulative_pnl += daily_pnl
    state.portfolio_usd += daily_pnl
    state.trading_days += 1
    state.total_positions += len(state.positions)
    state.both_won_count += both_won
    state.one_won_count += one_won

    print_settlement_table(state.positions, daily_pnl)

    # Summary stats
    print(f"\n  {C.BOLD}{C.WHITE}CUMULATIVE{C.RESET}")
    print(f"  {C.DIM}Trading days:{C.RESET}    {C.VALUE}{state.trading_days}{C.RESET}")
    print(f"  {C.DIM}Total positions:{C.RESET} {C.VALUE}{state.total_positions}{C.RESET}")
    print(f"  {C.DIM}Both won:{C.RESET}        {C.OK}{state.both_won_count}{C.RESET}")
    print(f"  {C.DIM}One won:{C.RESET}         {C.YELLOW}{state.one_won_count}{C.RESET}")
    pnl_clr = C.GREEN if state.cumulative_pnl >= 0 else C.RED
    print(f"  {C.DIM}Cumulative P&L:{C.RESET}  {pnl_clr}{C.BOLD}${state.cumulative_pnl:+.2f}{C.RESET}")
    print(f"  {C.DIM}Portfolio:{C.RESET}        {C.VALUE}${state.portfolio_usd:.2f}{C.RESET}")

    write_daily_row(state, daily_pnl, both_won, one_won)

    send_telegram(
        f"*Condor Daily — {state.date}*\n"
        f"Positions: `{len(state.positions)}`\n"
        f"Both won: `{both_won}`  One won: `{one_won}`\n"
        f"Daily P&L: `${daily_pnl:+.2f}`\n"
        f"Cumulative: `${state.cumulative_pnl:+.2f}`\n"
        f"Portfolio: `${state.portfolio_usd:.2f}`"
    )

    return "WAITING"


# ─── MAIN ───────────────────────────────────────────────────────────────────

async def main():
    W = 65
    print(f"\n  {C.HEADER}{'='*W}{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  {C.BOLD}{C.WHITE}ForecastBot — Weather Condor v1.0{C.RESET}"
          + " " * (W - 35) + f"{C.HEADER}|{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  {C.DIM}Started: {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}{C.RESET}"
          + " " * (W - 39) + f"{C.HEADER}|{C.RESET}")
    cities_str = ", ".join(r["symbol"] for r in CITY_REGISTRY if r["tier"] > 0)
    print(f"  {C.HEADER}|{C.RESET}  Cities: {C.CYAN}{cities_str}{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  Portfolio: {C.VALUE}${PORTFOLIO_USD:.0f}{C.RESET}"
          f"  |  Per-city: {C.VALUE}${MAX_CITY_BUDGET:.0f}{C.RESET}"
          f"  |  clientId: {C.VALUE}{IBKR_CLIENT_ID}{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  Scan: {C.CYAN}{SCAN_HOUR}:00 ET{C.RESET}"
          f"  |  Settle: {C.CYAN}{SETTLE_HOUR}:00 ET{C.RESET}")
    print(f"  {C.HEADER}|{C.RESET}  {C.RED}{C.BOLD}*** SIMULATION ONLY — NO REAL ORDERS ***{C.RESET}"
          + " " * (W - 43) + f"{C.HEADER}|{C.RESET}")
    print(f"  {C.HEADER}{'='*W}{C.RESET}\n")

    init_logs()
    loop = asyncio.get_event_loop()

    # Connect to IB
    feed = CondorPriceFeed()
    ib_ok = await feed.connect()
    if ib_ok:
        log.info("  IB connected — discovering contracts for all cities…")
        for reg in CITY_REGISTRY:
            if reg["tier"] > 0:
                await feed.discover_city(reg["symbol"])
    else:
        log.warning("  IB unavailable — will run in forecast-capture-only mode")

    # Initialize state
    state = PortfolioState(
        phase="WAITING",
        date=datetime.now(ET).strftime("%Y-%m-%d"),
    )

    send_telegram(
        f"*Weather Condor v1.0 Started*\n"
        f"Date: `{state.date}`\n"
        f"Cities: `{len([r for r in CITY_REGISTRY if r['tier'] > 0])}`\n"
        f"Portfolio: `${PORTFOLIO_USD:.0f}`\n"
        f"IB: `{'connected' if ib_ok else 'unavailable'}`"
    )

    log.info(f"  Phase: WAITING — Ctrl+C to stop\n")

    last_date = state.date
    scanned_today = False
    active_first_tick = True

    try:
        while True:
            now_et = datetime.now(ET)
            today_str = now_et.strftime("%Y-%m-%d")

            # Daily rollover — delay until 3:15 AM ET (ForecastEx market open)
            if today_str != last_date and (now_et.hour > 3 or (now_et.hour == 3 and now_et.minute >= 15)):
                log.info(f"  Day rollover → {today_str} (ForecastEx open 3:15 AM ET)")
                state.date = today_str
                state.phase = "WAITING"
                state.cities = []
                state.positions = []
                state.allocated_today = 0.0
                scanned_today = False
                active_first_tick = True
                last_date = today_str
                if feed.connected:
                    await feed.refresh_daily()
                    for reg in CITY_REGISTRY:
                        if reg["tier"] > 0:
                            await feed.discover_city(reg["symbol"])

            # IB reconnect check — handles both mid-session drops and initial failures.
            # During WAITING: retry forever (Gateway restarts nightly, will come back).
            # During ACTIVE: limited retries (need data, can't wait hours).
            ib_lost = (feed.connected and feed.ib and not feed.ib.isConnected())
            ib_never = (not feed.connected)
            if ib_lost or ib_never:
                if ib_lost:
                    log.warning("  IB disconnected — attempting reconnect…")
                    feed.connected = False
                feed.tickers = {}
                feed.depth_tickers = {}
                feed.strike_map = {}
                # Choose retry strategy based on current phase
                if state.phase in ("ACTIVE", "SETTLING"):
                    retries = MAX_RECONNECT_ACTIVE  # limited — need data now
                else:
                    retries = MAX_RECONNECT_WAIT  # 0 = infinite — Gateway will restart
                ib_ok = await feed.connect(max_retries=retries)
                if ib_ok:
                    log.info("  IB connected — discovering contracts for all cities…")
                    for reg in CITY_REGISTRY:
                        if reg["tier"] > 0:
                            await feed.discover_city(reg["symbol"])
                    active_first_tick = True  # re-subscribe active strikes

            # Phase dispatch
            if state.phase == "WAITING":
                if not scanned_today and now_et.hour >= SCAN_HOUR and now_et.hour < SETTLE_HOUR:
                    state.phase = "SCANNING"
                else:
                    next_phase = await handle_waiting(state)
                    state.phase = next_phase

            elif state.phase == "SCANNING":
                next_phase = await handle_scanning(state, feed, loop)
                state.phase = next_phase
                scanned_today = True
                active_first_tick = True

            elif state.phase == "ACTIVE":
                next_phase = await handle_active(
                    state, feed, loop,
                    first_tick=active_first_tick)
                active_first_tick = False
                state.phase = next_phase

            elif state.phase == "SETTLING":
                next_phase = await handle_settling(state, loop)
                state.phase = next_phase

            elif state.phase == "REPORTING":
                next_phase = await handle_reporting(state)
                state.phase = next_phase

    except KeyboardInterrupt:
        log.info("\n  Stopped by user.")
    except Exception as e:
        log.critical(f"\n  FATAL: {e}\n{traceback.format_exc()}")
        send_telegram(f"*Weather Condor CRASHED*\n`{str(e)[:200]}`")
    finally:
        feed.disconnect()

        W = 65
        print(f"\n  {C.HEADER}{'='*W}{C.RESET}")
        print(f"  {C.HEADER}|{C.RESET}  {C.BOLD}{C.WHITE}SESSION SUMMARY{C.RESET}"
              + " " * (W - 17) + f"{C.HEADER}|{C.RESET}")
        print(f"  {C.HEADER}{'='*W}{C.RESET}")
        print(f"  {C.DIM}Date:{C.RESET}             {state.date}")
        print(f"  {C.DIM}Phase:{C.RESET}            {state.phase}")
        print(f"  {C.DIM}Trading days:{C.RESET}     {state.trading_days}")
        print(f"  {C.DIM}Positions today:{C.RESET}  {len(state.positions)}")
        pnl_clr = C.GREEN if state.cumulative_pnl >= 0 else C.RED
        print(f"  {C.DIM}Cumulative P&L:{C.RESET}  {pnl_clr}${state.cumulative_pnl:+.2f}{C.RESET}")
        print(f"  {C.DIM}Portfolio:{C.RESET}        ${state.portfolio_usd:.2f}")
        print(f"\n  {C.DIM}Data:{C.RESET}")
        print(f"    {FORECAST_CSV}")
        print(f"    {POSITION_CSV}")
        print(f"    {DAILY_CSV}")
        print(f"  {C.HEADER}{'='*W}{C.RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
