"""
weather_edge.py — ForecastBot Weather Edge Scanner  v4.0
==========================================================
THESIS (validated March 5, 2026):
  WU settlement = METAR ASOS data (rounded to integer °F).
  93% match rate over 30 days when accounting for UTC/PT offset.

  PWS stations (KCAELSEG23) read 2–5°F higher than WU published
  (sensor bias). Leading indicator in TIME — updates every 5 min
  vs METAR's hourly. PWS > strike+3°F predicts METAR will cross.

  Edge window = time between data source crossing a strike
  and IBKR market repricing:
    METAR updates hourly (~:53 past hour)
    → WU processes in ~7–10 min
    → IBKR market reprices in ~2–3 min after WU
    = ~10–13 min edge window per METAR update

ARCHITECTURE:
  Three data sources polled in parallel:
    1. METAR (aviationweather.gov) — hourly, settlement source
    2. WU current (api.weather.com) — ~10 min updates, confirmation
    3. PWS KCAELSEG23 (api.weather.com) — 5 min, early warning (bias +3°F)
  Plus IB market data streaming continuously.

  Golden hour: 12:00–14:30 PT (when daily peak occurs 83% of days)
  Poll rate: 300s normal, 60s golden hour, 30s approaching strike, 30s signal

SETTLEMENT SEMANTICS:
  "Exceed 75°F" means STRICTLY > 75°F.
  WU rounds to integer. Need ≥ 75.6°F actual to get WU=76 > 75.
  WU high of exactly 75°F does NOT pay K75 YES.

RUN:
  python3 weather_edge.py
  Runs alongside kill_shot.py (uses clientId=45, different from kill_shot=40)

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

load_dotenv()

# ─── LOGGING ──────────────────────────────────────────────────────────────────
LOG_DIR = os.getenv("LOG_DIR", "./data")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, "weather_edge.log"), mode="a"),
    ],
)
log = logging.getLogger("weather_edge")

PT = ZoneInfo("America/Los_Angeles")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
IBKR_HOST      = os.getenv("IBKR_HOST",                  "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT",              "4001"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID_WEATHER", "45"))

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")

# Poll rates
POLL_GOLDEN_SEC    = 60     # during golden hour (12–14:30 PT)
POLL_NORMAL_SEC    = 300    # outside golden hour
POLL_SIGNAL_SEC    = 30     # after a signal is detected
POLL_APPROACHING_SEC = 30   # when observed high is near a strike

# Strike proximity — accelerate polling when temp is approaching a strike
APPROACHING_THRESHOLD_F = 3  # within 3°F of any active strike
APPROACHING_BURST_SEC   = 600   # 10 min burst of fast polling
APPROACHING_COOLDOWN_SEC = 600  # 10 min cooldown before re-engaging
METAR_FETCH_INTERVAL_SEC = 3300  # 55 min — METAR updates hourly at ~:53

# Golden hour — when daily peak occurs (validated: 30 days of KCAELSEG23 data)
GOLDEN_START_HOUR  = 12     # 12:00 PM PT
GOLDEN_END_HOUR    = 15     # end at 3:00 PM PT (covers 14:30 + buffer)

# Edge thresholds
EDGE_ALERT_SCORE   = 0.15   # alert when |edge| ≥ this
ALERT_COOLDOWN_SEC = 900    # 15 min between same-strike alerts
IB_WARMUP_SEC      = 20     # seconds after subscribe before reading prices
MIN_DEPTH          = 10     # skip strikes with fewer contracts on either leg

# WU API key (public, scraped from WU website)
WU_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"

# Station IDs
WU_KLAX_GEOCODE  = "33.94,-118.41"    # LAX airport coordinates for WU current
PWS_STATION_ID   = "KCAELSEG23"       # WU's actual KLAX-linked PWS station
METAR_STATION    = "KLAX"             # Official ASOS station

# CSV paths
TICKS_CSV     = os.path.join(LOG_DIR, "weather_ticks_v4.csv")
SIGNAL_CSV    = os.path.join(LOG_DIR, "weather_signals_v4.csv")
SOURCE_CSV    = os.path.join(LOG_DIR, "weather_sources_v4.csv")
CROSSING_CSV  = os.path.join(LOG_DIR, "weather_crossings_v4.csv")


# ─── COLORS ───────────────────────────────────────────────────────────────────

class C:
    """ANSI color codes for terminal output (white/light macOS terminal)."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[38;5;242m"    # medium gray — \033[2m is invisible on white
    # Foreground — 256-color palette tuned for white backgrounds
    RED     = "\033[38;5;160m"     # strong red
    GREEN   = "\033[38;5;28m"     # forest green — readable on white
    YELLOW  = "\033[38;5;166m"    # orange — yellow is unreadable on white
    BLUE    = "\033[38;5;25m"     # deep blue
    MAGENTA = "\033[38;5;127m"    # dark magenta
    CYAN    = "\033[38;5;30m"     # dark teal — standard cyan washes out
    WHITE   = "\033[30m"          # black text on white background
    GRAY    = "\033[38;5;242m"    # medium gray — 90 is too faint on white
    # Combinations
    HEADER  = "\033[38;5;25m"     # deep blue — borders, headers
    VALUE   = "\033[1m"           # bold black — key values
    LABEL   = "\033[38;5;242m"    # medium gray — labels
    OK      = "\033[38;5;28m"     # forest green — confirmed
    WARN    = "\033[38;5;166m"    # orange — waiting, warnings
    ALERT   = "\033[38;5;160m\033[1m"  # bold red — signals
    EDGE    = "\033[38;5;127m"    # dark magenta — edge measurements
    SETTLE  = "\033[38;5;28m\033[1m"   # bold forest green — settlement


# ─── HELPERS ──────────────────────────────────────────────────────────────────

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


def c_to_f(c: float) -> float:
    return round(c * 9 / 5 + 32, 1)


def will_exceed_strike(wu_high_int: int, strike: float) -> bool:
    """WU integer high exceeds strike means wu_high > strike (strictly greater)."""
    return wu_high_int > strike


# ─── DATA STRUCTURES ─────────────────────────────────────────────────────────

@dataclass
class METARReading:
    temp_f: float           # decimal fahrenheit
    temp_rounded: int       # rounded to nearest integer (matches WU)
    obs_time_utc: str       # ISO timestamp of observation
    obs_time_pt: str        # formatted PT time
    wind_mph: float
    wind_dir: int           # degrees
    fetched_at: float       # time.time() when we fetched it
    raw_metar: str = ""     # raw METAR string


@dataclass
class WUReading:
    temp_f: int             # WU current temp (integer)
    high_f: int             # WU running daily high (integer) — THIS IS SETTLEMENT
    obs_time: str           # WU observation time
    fetched_at: float


@dataclass
class PWSReading:
    temp_f: float           # decimal fahrenheit from PWS
    obs_time: str           # local time of observation
    fetched_at: float


@dataclass
class StrikeCrossing:
    """Tracks when each data source first crossed a given strike.

    Timeline:
      METAR crosses strike (hourly)   ← EDGE STARTS here (we know the answer)
        → WU processes (~7–10 min)    ← settlement confirmation
        → Market reprices (~2–3 min)  ← EDGE CLOSES here

      PWS crossing is logged as early warning context only.
      PWS reads 2–5°F high so its crossing does NOT mean settlement will cross.
    """
    strike: float
    pws_crossed_at: float = 0.0       # early warning only (reads 2–5°F high)
    metar_crossed_at: float = 0.0     # EDGE STARTS — METAR is settlement source
    wu_crossed_at: float = 0.0        # settlement confirmed
    market_repriced_at: float = 0.0   # EDGE CLOSES — YES ask jumped > 0.80
    metar_historical: bool = False    # True if already exceeded at bot start
    wu_historical: bool = False       # True if already exceeded at bot start

    def metar_to_wu_lag(self) -> Optional[float]:
        """Minutes from METAR crossing to WU confirming. Hypothesis: 7–10 min."""
        if self.metar_crossed_at and self.wu_crossed_at:
            return (self.wu_crossed_at - self.metar_crossed_at) / 60
        return None

    def wu_to_market_lag(self) -> Optional[float]:
        """Minutes from WU confirming to market repricing. Hypothesis: 2–3 min."""
        if self.wu_crossed_at and self.market_repriced_at:
            return (self.market_repriced_at - self.wu_crossed_at) / 60
        return None

    def edge_window(self) -> Optional[float]:
        """Minutes from METAR crossing (we know) to market repricing (edge gone).
        This is THE number we need to validate. Hypothesis: 10–13 min."""
        if self.metar_crossed_at and self.market_repriced_at:
            return (self.market_repriced_at - self.metar_crossed_at) / 60
        return None

    def pws_early_warning(self) -> Optional[float]:
        """Minutes of advance notice PWS gave before METAR confirmed.
        Complementary info — how much earlier could we have positioned?"""
        if self.pws_crossed_at and self.metar_crossed_at:
            return (self.metar_crossed_at - self.pws_crossed_at) / 60
        return None


@dataclass
class DayState:
    date_pt: str = ""
    # WU day high — tracked from max WU current temp readings (not API 24h max)
    wu_high_f: int = 0
    wu_api_24h_high: int = 0        # API temperatureMax24Hour (rolling, may include yesterday)
    forecast_high_f: int = 0        # WU forecast high for today (from 5-day forecast API)
    # Bot-session highs — max each source reported since scanner started
    metar_high_f: float = 0.0       # max METAR reading since bot start (decimal)
    metar_high_rounded: int = 0     # rounded — predicts WU settlement
    wu_bot_high_f: int = 0          # max WU current temp since bot start
    pws_high_f: float = 0.0        # max PWS reading since bot start
    # Last readings
    last_metar: Optional[METARReading] = None
    last_wu: Optional[WUReading] = None
    last_pws: Optional[PWSReading] = None
    # Tracking
    total_polls: int = 0
    crossings_initialized: bool = False  # first crossing check seeds historical state
    signals_fired: int = 0
    signal_strikes: list = field(default_factory=list)
    # WU update tracking
    wu_last_obs_time: str = ""
    wu_update_count: int = 0
    wu_last_update_wallclock: float = 0.0
    # METAR update tracking
    metar_last_obs_time: str = ""
    metar_update_count: int = 0
    metar_last_update_wallclock: float = 0.0
    # Drift tracking (current readings, not highs)
    last_metar_wu_drift: float = 0.0     # METAR current − WU current
    last_pws_wu_drift: float = 0.0       # PWS current − WU current
    last_pws_metar_drift: float = 0.0    # PWS current − METAR current
    # WU update lag measurements (minutes)
    wu_lag_samples: list = field(default_factory=list)
    # Strike crossing timelines — the edge measurement
    crossings: dict = field(default_factory=dict)  # strike → StrikeCrossing
    # Market price snapshots for repricing detection
    last_market_prices: dict = field(default_factory=dict)  # strike → yes_ask

    def wu_settled_exceeds(self, strike: float) -> Optional[bool]:
        if self.wu_high_f == 0:
            return None
        return will_exceed_strike(self.wu_high_f, strike)

    def metar_predicts_exceeds(self, strike: float) -> Optional[bool]:
        if self.metar_high_rounded == 0:
            return None
        return will_exceed_strike(self.metar_high_rounded, strike)

    def update_drifts(self):
        """Compute current temperature drift between sources."""
        m = self.last_metar.temp_f if self.last_metar else None
        w = self.last_wu.temp_f if self.last_wu else None
        p = self.last_pws.temp_f if self.last_pws else None
        if m is not None and w is not None:
            self.last_metar_wu_drift = round(m - w, 1)
        if p is not None and w is not None:
            self.last_pws_wu_drift = round(p - w, 1)
        if p is not None and m is not None:
            self.last_pws_metar_drift = round(p - m, 1)

    def check_strike_crossings(self, strikes: list):
        """Update crossing timelines for all strikes based on current source highs.

        Order: METAR (edge start) → WU (confirmation) → Market (edge close)
        PWS logged as early warning context only (reads 2–5°F high, not reliable).

        First call seeds historical state — strikes already exceeded before
        the bot started are logged as historical, not live edge opportunities.
        """
        is_first = not self.crossings_initialized
        now = time.time()
        for strike in strikes:
            if strike not in self.crossings:
                self.crossings[strike] = StrikeCrossing(strike=strike)
            cx = self.crossings[strike]

            # PWS early warning (complementary — NOT the edge trigger)
            if self.pws_high_f > strike and cx.pws_crossed_at == 0:
                cx.pws_crossed_at = now
                if is_first:
                    log.info(f"  ℹ PWS already > K{strike:.0f}"
                             f" at bot start (PWS={self.pws_high_f:.1f}°F)")
                else:
                    log.info(f"  ⚠ PWS early warning: K{strike:.0f}"
                             f" (PWS={self.pws_high_f:.1f}°F, but reads 2–5°F high)")

            # METAR crossed = EDGE STARTS (METAR is the settlement source)
            if self.metar_high_rounded > strike and cx.metar_crossed_at == 0:
                cx.metar_crossed_at = now
                cx.metar_historical = is_first
                if is_first:
                    log.info(f"  ℹ METAR already > K{strike:.0f}"
                             f" at bot start (METAR={self.metar_high_rounded}°F)")
                else:
                    pws_note = ""
                    if cx.pws_crossed_at:
                        pws_note = (f"  (PWS warned {cx.pws_early_warning():.1f}min"
                                    f" earlier)")
                    log.info(f"  ⚡ EDGE START: METAR crossed K{strike:.0f}"
                             f" (METAR high={self.metar_high_rounded}°F)"
                             f" — WU should follow in ~10min{pws_note}")

            # WU crossed = settlement confirmed
            if self.wu_high_f > strike and cx.wu_crossed_at == 0:
                cx.wu_crossed_at = now
                cx.wu_historical = is_first
                if is_first:
                    log.info(f"  ℹ WU already > K{strike:.0f}"
                             f" at bot start (WU high={self.wu_high_f}°F)")
                else:
                    metar_lag = ""
                    if cx.metar_crossed_at:
                        metar_lag = (f"  METAR→WU took"
                                    f" {cx.metar_to_wu_lag():.1f}min")
                    log.info(f"  ✓ WU CONFIRMED: K{strike:.0f}"
                             f" (WU high={self.wu_high_f}°F){metar_lag}"
                             f" — market should reprice in ~2–3min")

        self.crossings_initialized = True

    def check_market_repricing(self, prices: dict):
        """Detect when market reprices after a source crossing.
        This closes the edge window measurement."""
        now = time.time()
        for strike, (ya, na, yb, nb, yad, nad, ybd, nbd) in prices.items():
            if strike not in self.crossings:
                continue
            cx = self.crossings[strike]

            # Market repriced = YES ask jumped above 0.80 (high confidence)
            prev_ya = self.last_market_prices.get(strike, 0)
            if (ya >= 0.80 and prev_ya < 0.80
                    and cx.market_repriced_at == 0
                    and (cx.metar_crossed_at > 0 or cx.wu_crossed_at > 0)):
                cx.market_repriced_at = now
                lags = []
                if cx.metar_to_wu_lag() is not None:
                    lags.append(f"METAR→WU={cx.metar_to_wu_lag():.1f}m")
                if cx.wu_to_market_lag() is not None:
                    lags.append(f"WU→MKT={cx.wu_to_market_lag():.1f}m")
                if cx.edge_window() is not None:
                    lags.append(f"EDGE={cx.edge_window():.1f}m")
                log.info(f"  ✗ EDGE CLOSED: K{strike:.0f}"
                         f" YES={prev_ya:.2f}→{ya:.2f}"
                         f"  {', '.join(lags)}")

            self.last_market_prices[strike] = ya


# ─── DATA FETCHERS ───────────────────────────────────────────────────────────

def fetch_metar() -> Optional[METARReading]:
    """Fetch latest METAR observation for KLAX from aviationweather.gov."""
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={METAR_STATION}&format=json"
        r = requests.get(url, headers={"User-Agent": "forecastbot/4.0"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None

        obs = data[0]
        temp_c = obs.get("temp")
        if temp_c is None:
            return None

        temp_f = c_to_f(temp_c)
        wind_speed_kt = obs.get("wspd", 0) or 0
        wind_dir = obs.get("wdir", 0) or 0
        obs_time_epoch = obs.get("obsTime", 0)
        raw = obs.get("rawOb", "")

        # Convert epoch obs time to PT
        obs_time_utc = ""
        obs_pt = ""
        if obs_time_epoch:
            try:
                dt = datetime.fromtimestamp(obs_time_epoch, tz=PT)
                obs_pt = dt.strftime("%H:%M:%S")
                obs_time_utc = datetime.fromtimestamp(
                    obs_time_epoch).isoformat() + "Z"
            except Exception:
                obs_pt = str(obs_time_epoch)

        return METARReading(
            temp_f=temp_f,
            temp_rounded=round(temp_f),
            obs_time_utc=obs_time_utc,
            obs_time_pt=obs_pt,
            wind_mph=round(wind_speed_kt * 1.151, 1),
            wind_dir=wind_dir,
            fetched_at=time.time(),
            raw_metar=raw,
        )
    except Exception as e:
        log.warning(f"  METAR fetch failed: {e}")
        return None


def fetch_wu_current() -> Optional[WUReading]:
    """Fetch WU processed current conditions for KLAX area."""
    try:
        url = (
            f"https://api.weather.com/v3/wx/observations/current"
            f"?apiKey={WU_API_KEY}"
            f"&geocode={WU_KLAX_GEOCODE}"
            f"&language=en-US&units=e&format=json"
        )
        r = requests.get(url, headers={"User-Agent": "forecastbot/4.0"}, timeout=10)
        r.raise_for_status()
        data = r.json()

        temp = data.get("temperature")
        high = data.get("temperatureMax24Hour")
        obs_time = data.get("validTimeLocal", "")

        if temp is None:
            return None

        # Extract just time from ISO string
        obs_short = ""
        if obs_time:
            try:
                dt = datetime.fromisoformat(obs_time)
                obs_short = dt.strftime("%H:%M:%S")
            except Exception:
                obs_short = obs_time

        return WUReading(
            temp_f=int(temp) if temp is not None else 0,
            high_f=int(high) if high is not None else 0,
            obs_time=obs_short,
            fetched_at=time.time(),
        )
    except Exception as e:
        log.warning(f"  WU fetch failed: {e}")
        return None


def fetch_pws() -> Optional[PWSReading]:
    """Fetch latest PWS reading from KCAELSEG23 (WU's KLAX station)."""
    try:
        url = (
            f"https://api.weather.com/v2/pws/observations/current"
            f"?apiKey={WU_API_KEY}"
            f"&stationId={PWS_STATION_ID}"
            f"&units=e&format=json&numericPrecision=decimal"
        )
        r = requests.get(url, headers={"User-Agent": "forecastbot/4.0"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        obs_list = data.get("observations", [])
        if not obs_list:
            return None

        obs = obs_list[0]
        temp = obs.get("imperial", {}).get("temp")
        obs_time = obs.get("obsTimeLocal", "")

        if temp is None:
            return None

        obs_short = ""
        if obs_time:
            try:
                obs_short = obs_time.split(" ")[1] if " " in obs_time else obs_time
            except Exception:
                obs_short = obs_time

        return PWSReading(
            temp_f=float(temp),
            obs_time=obs_short,
            fetched_at=time.time(),
        )
    except Exception as e:
        log.warning(f"  PWS fetch failed: {e}")
        return None


def fetch_wu_forecast_high() -> Optional[int]:
    """Fetch today's forecast high temperature from WU 5-day forecast.
    Returns integer °F or None on failure."""
    try:
        url = (
            f"https://api.weather.com/v3/wx/forecast/daily/5day"
            f"?apiKey={WU_API_KEY}"
            f"&geocode={WU_KLAX_GEOCODE}"
            f"&language=en-US&units=e&format=json"
        )
        r = requests.get(url, headers={"User-Agent": "forecastbot/4.0"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        highs = data.get("calendarDayTemperatureMax", [])
        if highs and highs[0] is not None:
            return int(highs[0])
        return None
    except Exception as e:
        log.warning(f"  WU forecast fetch failed: {e}")
        return None


# ─── IB PRICE FEED ───────────────────────────────────────────────────────────

class IBPriceFeed:
    """
    Async IB connection for UHLAX YES+NO contract prices.
    Subscribes at startup, prices stream continuously — reads are instant.
    Must be used within an already-running asyncio event loop.
    """

    def __init__(self):
        self.ib = None
        self.pairs = {}          # strike → (yes_ticker, no_ticker)
        self.connected = False
        self.contract_date = ""  # YYYYMMDD of actively-trading contracts
        self.strikes = []        # sorted list of active strikes

    async def start(self) -> bool:
        try:
            from ib_async import IB, Contract
        except ImportError:
            log.warning("  ib_async not installed — running without IB prices")
            return False
        try:
            self.ib = IB()
            await self.ib.connectAsync(
                IBKR_HOST, IBKR_PORT,
                clientId=IBKR_CLIENT_ID, timeout=10,
            )
            log.info(f"  IB connected (clientId={IBKR_CLIENT_ID})")

            # UHLAX: measurement date = today, last trade date = tomorrow.
            # IB's lastTradeDateOrContractMonth = expiry/last-trade date.
            # Start from tomorrow (today's measurement), try up to +3 days.
            for day_offset in range(1, 4):
                try_date = datetime.now(PT) + timedelta(days=day_offset)
                try_str = try_date.strftime("%Y%m%d")
                c = Contract()
                c.symbol = "UHLAX"
                c.secType = "OPT"
                c.exchange = "FORECASTX"
                c.currency = "USD"
                c.lastTradeDateOrContractMonth = try_str

                details = await self.ib.reqContractDetailsAsync(c)
                if not details:
                    log.info(f"  UHLAX: no contracts for {try_str}, trying next day…")
                    continue

                log.info(f"  UHLAX: found {len(details)} contracts for {try_str}")

                yes_map = {d.contract.strike: d.contract
                           for d in details if d.contract.right == "C"}
                no_map = {d.contract.strike: d.contract
                          for d in details if d.contract.right == "P"}
                common = sorted(set(yes_map) & set(no_map))

                if not common:
                    continue

                self.pairs = {}
                for s in common:
                    yt = self.ib.reqMktData(yes_map[s], snapshot=False)
                    nt = self.ib.reqMktData(no_map[s], snapshot=False)
                    self.pairs[s] = (yt, nt)

                log.info(f"  Subscribed {len(common)} strikes (exp={try_str}). "
                         f"Warming up {IB_WARMUP_SEC}s…")
                await asyncio.sleep(IB_WARMUP_SEC)

                # Check for live prices — any sign of activity counts
                # (bid, ask, last, or volume on either leg)
                live_count = 0
                for s in common:
                    yt, nt = self.pairs[s]
                    has_activity = (
                        (hasattr(yt, 'bid') and yt.bid is not None and yt.bid > 0)
                        or (hasattr(yt, 'ask') and yt.ask is not None and yt.ask > 0)
                        or (hasattr(nt, 'bid') and nt.bid is not None and nt.bid > 0)
                        or (hasattr(nt, 'ask') and nt.ask is not None and nt.ask > 0)
                        or (hasattr(yt, 'volume') and yt.volume is not None and yt.volume > 0)
                    )
                    if has_activity:
                        live_count += 1

                if live_count > 0:
                    self.contract_date = try_str
                    self.strikes = common
                    self.connected = True
                    log.info(f"  Live prices on {live_count}/{len(common)} strikes")
                    return True

                log.info(f"  No live prices for {try_str} — trying next day…")
                # Don't cancel — ib_async logs noisy errors on reqId lookup.
                # Subscriptions are harmless and get cleaned up on disconnect.
                self.pairs = {}

            log.warning("  UHLAX: no actively-trading contracts found")
            return False

        except Exception as e:
            log.warning(f"  IB start failed: {e}")
            return False

    def _read(self, strike: float) -> tuple:
        """Returns (yes_ask, no_ask, yes_bid, no_bid,
                    yes_ask_depth, no_ask_depth, yes_bid_depth, no_bid_depth)."""
        if strike not in self.pairs:
            return -1.0, -1.0, -1.0, -1.0, 0, 0, 0, 0
        yt, nt = self.pairs[strike]
        ya = float(yt.ask) if hasattr(yt, 'ask') and yt.ask is not None and yt.ask > 0 else -1.0
        na = float(nt.ask) if hasattr(nt, 'ask') and nt.ask is not None and nt.ask > 0 else -1.0
        yb = float(yt.bid) if hasattr(yt, 'bid') and yt.bid is not None and yt.bid > 0 else -1.0
        nb = float(nt.bid) if hasattr(nt, 'bid') and nt.bid is not None and nt.bid > 0 else -1.0
        yad = int(yt.askSize) if hasattr(yt, 'askSize') and yt.askSize is not None else 0
        nad = int(nt.askSize) if hasattr(nt, 'askSize') and nt.askSize is not None else 0
        ybd = int(yt.bidSize) if hasattr(yt, 'bidSize') and yt.bidSize is not None else 0
        nbd = int(nt.bidSize) if hasattr(nt, 'bidSize') and nt.bidSize is not None else 0
        return ya, na, yb, nb, yad, nad, ybd, nbd

    def read_all(self) -> dict:
        """Returns {strike: (ya, na, yb, nb, yad, nad, ybd, nbd)}
        Includes any strike with market activity (bid or ask on either side)."""
        result = {}
        for s in self.pairs:
            vals = self._read(s)
            ya, na, yb, nb = vals[:4]
            if ya > 0 or na > 0 or yb > 0 or nb > 0:
                result[s] = vals
        return result

    def stop(self):
        try:
            if self.ib:
                self.ib.disconnect()
                log.info("  IB disconnected.")
        except Exception:
            pass


# ─── SIGNAL DETECTION ────────────────────────────────────────────────────────

@dataclass
class Signal:
    strike: float
    direction: str          # BUY_YES or BUY_NO
    reason: str             # what triggered the signal
    edge_score: float       # how mispriced the market is (0–1 scale)
    yes_ask: float
    no_ask: float
    yes_depth: int
    no_depth: int
    metar_temp: float       # METAR reading that triggered
    wu_high: int            # WU published high at signal time
    pws_temp: float         # PWS reading at signal time
    profit_per_contract: float  # max profit if correct


def detect_signals(day: DayState, prices: dict) -> list[Signal]:
    """
    Detect mispricing between data sources and market.

    Signal logic:
      1. METAR crosses strike → WU will follow in ~10 min → market will reprice.
         If market hasn't moved yet, that's our edge.
      2. WU already updated past strike but market still hasn't repriced.
      3. Temperature peaked and falling → market still pricing YES too high.
      4. PWS early warning — updates every 5 min vs METAR's hourly. When
         PWS > strike + 3°F (bias-adjusted), METAR likely crosses next update.

    Signals 1 & 2 only fire for LIVE crossings — strikes that a source crossed
    while the bot was running. Historical crossings (already exceeded at bot start)
    are not actionable since the market already repriced hours ago.
    """
    signals = []

    metar_high = day.metar_high_rounded
    wu_high = day.wu_high_f
    pws_temp = day.last_pws.temp_f if day.last_pws else 0.0
    metar_temp = day.last_metar.temp_f if day.last_metar else 0.0

    now_pt = datetime.now(PT)
    hour = now_pt.hour

    for strike, (ya, na, yb, nb, yad, nad, ybd, nbd) in prices.items():
        # Skip strikes with missing prices — can't evaluate edge
        if ya <= 0 or na <= 0:
            continue

        market_yes_prob = ya  # YES ask price = implied probability

        # Check if crossings for this strike are live (not historical)
        cx = day.crossings.get(strike)
        metar_is_live = (cx and cx.metar_crossed_at > 0
                         and not cx.metar_historical)
        wu_is_live = (cx and cx.wu_crossed_at > 0
                      and not cx.wu_historical)

        # ── SIGNAL 1: METAR confirms exceed, market underprices YES ──────
        # Only fire if METAR crossed this strike LIVE (not at bot start)
        if metar_high > strike and metar_is_live:
            fair_yes = 0.93
            edge = fair_yes - market_yes_prob
            if edge >= EDGE_ALERT_SCORE and ya < 0.90:
                signals.append(Signal(
                    strike=strike, direction="BUY_YES",
                    reason=f"METAR_CONFIRM: METAR high {metar_high}°F > K{strike:.0f}",
                    edge_score=round(edge, 3),
                    yes_ask=ya, no_ask=na, yes_depth=yad, no_depth=nad,
                    metar_temp=metar_temp, wu_high=wu_high, pws_temp=pws_temp,
                    profit_per_contract=round(1.0 - ya, 4),
                ))

        # ── SIGNAL 2: WU already published high > strike, market lagging ─
        # Only fire if WU crossed this strike LIVE
        if wu_high > strike and wu_is_live:
            fair_yes = 0.97
            edge = fair_yes - market_yes_prob
            if edge >= EDGE_ALERT_SCORE and ya < 0.93:
                signals.append(Signal(
                    strike=strike, direction="BUY_YES",
                    reason=f"WU_CONFIRM: WU high {wu_high}°F > K{strike:.0f}",
                    edge_score=round(edge, 3),
                    yes_ask=ya, no_ask=na, yes_depth=yad, no_depth=nad,
                    metar_temp=metar_temp, wu_high=wu_high, pws_temp=pws_temp,
                    profit_per_contract=round(1.0 - ya, 4),
                ))

        # ── SIGNAL 3: Post-peak, temp falling, strike NOT exceeded ───────
        # Uses wu_high (day high from API) — safe regardless of when bot started
        if hour >= 15 and metar_high <= strike and wu_high <= strike:
            gap_to_strike = strike - max(metar_high, wu_high)
            if gap_to_strike >= 2:
                fair_yes = 0.05
                edge = market_yes_prob - fair_yes
                if edge >= EDGE_ALERT_SCORE and na < 0.90:
                    signals.append(Signal(
                        strike=strike, direction="BUY_NO",
                        reason=f"POST_PEAK: high={max(metar_high, wu_high)}°F,"
                               f" K{strike:.0f} gap={gap_to_strike}°F",
                        edge_score=round(edge, 3),
                        yes_ask=ya, no_ask=na, yes_depth=yad, no_depth=nad,
                        metar_temp=metar_temp, wu_high=wu_high, pws_temp=pws_temp,
                        profit_per_contract=round(1.0 - na, 4),
                    ))

        # ── SIGNAL 4: PWS early warning during golden hour ────────────────
        # PWS reads 2–5°F higher (sensor bias), but updates every 5 min vs
        # METAR's hourly. When PWS > strike + 3°F (bias-adjusted), METAR
        # will likely cross the strike at its next hourly update.
        # Softer signal — only during golden hour when temps are climbing.
        if (GOLDEN_START_HOUR <= hour < GOLDEN_END_HOUR
                and pws_temp > strike + 3
                and metar_high <= strike):
            fair_yes = 0.60
            edge = fair_yes - market_yes_prob
            if edge >= EDGE_ALERT_SCORE and ya < 0.50:
                signals.append(Signal(
                    strike=strike, direction="BUY_YES",
                    reason=f"PWS_EARLY: PWS={pws_temp:.1f}°F > K{strike:.0f}+3"
                           f" (bias-adj), METAR likely next update",
                    edge_score=round(edge, 3),
                    yes_ask=ya, no_ask=na, yes_depth=yad, no_depth=nad,
                    metar_temp=metar_temp, wu_high=wu_high, pws_temp=pws_temp,
                    profit_per_contract=round(1.0 - ya, 4),
                ))

    return signals


# ─── CSV LOGGING ──────────────────────────────────────────────────────────────

def init_logs():
    if not os.path.exists(SOURCE_CSV):
        with open(SOURCE_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp_pt", "date_pt",
                "metar_temp_f", "metar_rounded", "metar_obs_time",
                "metar_high_f", "metar_high_rounded",
                "wu_temp_f", "wu_high_f", "wu_obs_time", "wu_update_count",
                "pws_temp_f", "pws_obs_time",
                "drift_metar_wu", "drift_pws_wu", "drift_pws_metar",
                "wu_age_sec", "is_golden_hour",
            ])
    if not os.path.exists(CROSSING_CSV):
        with open(CROSSING_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "date_pt", "strike",
                "metar_crossed_time", "wu_crossed_time",
                "market_repriced_time", "pws_early_warn_time",
                "metar_to_wu_min", "wu_to_market_min",
                "edge_window_min", "pws_early_warning_min",
            ])
    if not os.path.exists(TICKS_CSV):
        with open(TICKS_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp_pt", "date_pt", "strike",
                "yes_ask", "no_ask", "yes_depth", "no_depth",
                "metar_high_rounded", "wu_high_f",
                "pws_temp_f", "is_golden_hour",
            ])
    if not os.path.exists(SIGNAL_CSV):
        with open(SIGNAL_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp_pt", "date_pt", "strike",
                "direction", "reason", "edge_score",
                "yes_ask", "no_ask", "yes_depth", "no_depth",
                "metar_temp_f", "wu_high_f", "pws_temp_f",
                "profit_per_contract",
            ])


def write_source_tick(day: DayState):
    now_pt = datetime.now(PT)
    is_golden = GOLDEN_START_HOUR <= now_pt.hour < GOLDEN_END_HOUR
    wu_age = (int(time.time() - day.wu_last_update_wallclock)
              if day.wu_last_update_wallclock > 0 else "")
    with open(SOURCE_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            now_pt.strftime("%Y-%m-%d %H:%M:%S"), day.date_pt,
            day.last_metar.temp_f if day.last_metar else "",
            day.last_metar.temp_rounded if day.last_metar else "",
            day.last_metar.obs_time_pt if day.last_metar else "",
            day.metar_high_f, day.metar_high_rounded,
            day.last_wu.temp_f if day.last_wu else "",
            day.wu_high_f,
            day.last_wu.obs_time if day.last_wu else "",
            day.wu_update_count,
            day.last_pws.temp_f if day.last_pws else "",
            day.last_pws.obs_time if day.last_pws else "",
            day.last_metar_wu_drift if day.last_metar and day.last_wu else "",
            day.last_pws_wu_drift if day.last_pws and day.last_wu else "",
            day.last_pws_metar_drift if day.last_pws and day.last_metar else "",
            wu_age,
            "1" if is_golden else "0",
        ])


def write_market_tick(day: DayState, strike: float, ya: float, na: float,
                      yad: int, nad: int):
    now_pt = datetime.now(PT)
    is_golden = GOLDEN_START_HOUR <= now_pt.hour < GOLDEN_END_HOUR
    with open(TICKS_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            now_pt.strftime("%Y-%m-%d %H:%M:%S"), day.date_pt, strike,
            ya, na, yad, nad,
            day.metar_high_rounded, day.wu_high_f,
            day.last_pws.temp_f if day.last_pws else "",
            "1" if is_golden else "0",
        ])


def write_signal(day: DayState, sig: Signal):
    now_pt = datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S")
    with open(SIGNAL_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            now_pt, day.date_pt, sig.strike,
            sig.direction, sig.reason, f"{sig.edge_score:+.3f}",
            sig.yes_ask, sig.no_ask, sig.yes_depth, sig.no_depth,
            sig.metar_temp, sig.wu_high, sig.pws_temp,
            sig.profit_per_contract,
        ])


def write_crossings(day: DayState):
    """Write all crossing timelines to CSV (called at end of day / shutdown)."""
    for strike, cx in day.crossings.items():
        if not (cx.metar_crossed_at or cx.wu_crossed_at):
            continue
        def fmt_ts(ts):
            return (datetime.fromtimestamp(ts, tz=PT).strftime("%H:%M:%S")
                    if ts > 0 else "")
        def fmt_lag(val):
            return f"{val:.1f}" if val is not None else ""
        with open(CROSSING_CSV, "a", newline="") as f:
            csv.writer(f).writerow([
                day.date_pt, strike,
                fmt_ts(cx.metar_crossed_at), fmt_ts(cx.wu_crossed_at),
                fmt_ts(cx.market_repriced_at), fmt_ts(cx.pws_crossed_at),
                fmt_lag(cx.metar_to_wu_lag()), fmt_lag(cx.wu_to_market_lag()),
                fmt_lag(cx.edge_window()), fmt_lag(cx.pws_early_warning()),
            ])


# ─── CONSOLE OUTPUT ──────────────────────────────────────────────────────────

def print_source_status(day: DayState):
    """Print current readings from all three data sources."""
    now_str = datetime.now(PT).strftime("%H:%M:%S PT")
    is_golden = GOLDEN_START_HOUR <= datetime.now(PT).hour < GOLDEN_END_HOUR

    if is_golden:
        mode_str = f"{C.YELLOW}☀ GOLDEN HOUR{C.RESET}"
    else:
        mode_str = f"{C.DIM}normal{C.RESET}"

    # ── Poll header ──
    print(f"\n  {C.HEADER}┌─────────────────────────────────────────────────────────────────┐{C.RESET}")
    print(f"  {C.HEADER}│{C.RESET}  {C.VALUE}{now_str}{C.RESET}   {mode_str}   "
          f"{C.DIM}poll #{day.total_polls}{C.RESET}"
          f"  {C.HEADER}│{C.RESET}")
    print(f"  {C.HEADER}└─────────────────────────────────────────────────────────────────┘{C.RESET}")

    # ── Forecast summary ──
    if day.forecast_high_f > 0:
        fh = day.forecast_high_f
        obs_high = max(day.metar_high_rounded, day.wu_high_f)
        if obs_high > 0:
            delta = obs_high - fh
            if delta > 0:
                trend = f"{C.GREEN}+{delta}°F above forecast{C.RESET}"
            elif delta < 0:
                trend = f"{C.DIM}{delta}°F below forecast{C.RESET}"
            else:
                trend = f"{C.YELLOW}at forecast{C.RESET}"
            print(f"\n  {C.BOLD}{C.WHITE}FORECAST HIGH: {C.EDGE}{fh}°F{C.RESET}"
                  f"  {C.DIM}│{C.RESET}  observed high so far: {C.VALUE}{obs_high}°F{C.RESET}"
                  f"  ({trend})")
        else:
            print(f"\n  {C.BOLD}{C.WHITE}FORECAST HIGH: {C.EDGE}{fh}°F{C.RESET}")

    # ── Data sources table ──
    print(f"\n  {C.BOLD}{C.WHITE}DATA SOURCES{C.RESET}")
    print(f"  {C.HEADER}┌──────────┬───────────┬──────────┬──────────┬──────────┬─────────────────┐{C.RESET}")
    print(f"  {C.HEADER}│{C.RESET} {C.WHITE}Source{C.RESET}   "
          f"{C.HEADER}│{C.RESET} {C.WHITE}Current{C.RESET}   "
          f"{C.HEADER}│{C.RESET} {C.WHITE}Day High{C.RESET} "
          f"{C.HEADER}│{C.RESET} {C.WHITE}Bot High{C.RESET} "
          f"{C.HEADER}│{C.RESET} {C.WHITE}Obs Time{C.RESET} "
          f"{C.HEADER}│{C.RESET} {C.WHITE}Notes{C.RESET}           {C.HEADER}│{C.RESET}")
    print(f"  {C.HEADER}├──────────┼───────────┼──────────┼──────────┼──────────┼─────────────────┤{C.RESET}")

    # Notes column is 17 chars wide (between │ markers)
    # Content: 1 leading space + up to 16 chars of text
    NOTES_W = 16

    # METAR row
    if day.last_metar:
        m = day.last_metar
        notes = f"wind={m.wind_mph:.0f}mph"
        print(f"  {C.HEADER}│{C.RESET} {C.CYAN}METAR{C.RESET}    "
              f"{C.HEADER}│{C.RESET} {C.VALUE}{m.temp_f:>6.1f}°F{C.RESET}  "
              f"{C.HEADER}│{C.RESET} {C.DIM}     —{C.RESET}   "
              f"{C.HEADER}│{C.RESET} {C.VALUE}{day.metar_high_rounded:>5}°F{C.RESET}  "
              f"{C.HEADER}│{C.RESET} {m.obs_time_pt:<8} "
              f"{C.HEADER}│{C.RESET} {C.DIM}{notes:<{NOTES_W}}{C.RESET}{C.HEADER}│{C.RESET}")
    else:
        print(f"  {C.HEADER}│{C.RESET} {C.CYAN}METAR{C.RESET}    "
              f"{C.HEADER}│{C.RESET} {C.DIM}  no data{C.RESET}  "
              f"{C.HEADER}│{C.RESET} {C.DIM}     —{C.RESET}   "
              f"{C.HEADER}│{C.RESET} {C.DIM}     —{C.RESET}   "
              f"{C.HEADER}│{C.RESET} {C.DIM}—{C.RESET}        "
              f"{C.HEADER}│{C.RESET} {'':<{NOTES_W}}{C.HEADER}│{C.RESET}")

    # WU row
    if day.last_wu:
        w = day.last_wu
        wu_age = int(time.time() - day.wu_last_update_wallclock) if day.wu_last_update_wallclock > 0 else 0
        notes = f"age={wu_age}s upd={day.wu_update_count}"
        print(f"  {C.HEADER}│{C.RESET} {C.SETTLE}WU{C.RESET}       "
              f"{C.HEADER}│{C.RESET} {C.VALUE}{w.temp_f:>6}°F{C.RESET}  "
              f"{C.HEADER}│{C.RESET} {C.SETTLE}{day.wu_high_f:>5}°F{C.RESET} ◀"
              f"{C.HEADER}│{C.RESET} {C.VALUE}{day.wu_bot_high_f:>5}°F{C.RESET}  "
              f"{C.HEADER}│{C.RESET} {w.obs_time:<8} "
              f"{C.HEADER}│{C.RESET} {C.DIM}{notes:<{NOTES_W}}{C.RESET}{C.HEADER}│{C.RESET}")
    else:
        print(f"  {C.HEADER}│{C.RESET} {C.SETTLE}WU{C.RESET}       "
              f"{C.HEADER}│{C.RESET} {C.DIM}  no data{C.RESET}  "
              f"{C.HEADER}│{C.RESET} {C.DIM}     —{C.RESET}   "
              f"{C.HEADER}│{C.RESET} {C.DIM}     —{C.RESET}   "
              f"{C.HEADER}│{C.RESET} {C.DIM}—{C.RESET}        "
              f"{C.HEADER}│{C.RESET} {'':<{NOTES_W}}{C.HEADER}│{C.RESET}")

    # PWS row
    if day.last_pws:
        p = day.last_pws
        notes = "reads 2-5°F hi"
        print(f"  {C.HEADER}│{C.RESET} {C.YELLOW}PWS{C.RESET}      "
              f"{C.HEADER}│{C.RESET} {C.VALUE}{p.temp_f:>6.1f}°F{C.RESET}  "
              f"{C.HEADER}│{C.RESET} {C.DIM}     —{C.RESET}   "
              f"{C.HEADER}│{C.RESET} {C.VALUE}{day.pws_high_f:>5.0f}°F{C.RESET}  "
              f"{C.HEADER}│{C.RESET} {p.obs_time:<8} "
              f"{C.HEADER}│{C.RESET} {C.DIM}{notes:<{NOTES_W}}{C.RESET}{C.HEADER}│{C.RESET}")
    else:
        print(f"  {C.HEADER}│{C.RESET} {C.YELLOW}PWS{C.RESET}      "
              f"{C.HEADER}│{C.RESET} {C.DIM}  no data{C.RESET}  "
              f"{C.HEADER}│{C.RESET} {C.DIM}     —{C.RESET}   "
              f"{C.HEADER}│{C.RESET} {C.DIM}     —{C.RESET}   "
              f"{C.HEADER}│{C.RESET} {C.DIM}—{C.RESET}        "
              f"{C.HEADER}│{C.RESET} {'':<{NOTES_W}}{C.HEADER}│{C.RESET}")

    print(f"  {C.HEADER}└──────────┴───────────┴──────────┴──────────┴──────────┴─────────────────┘{C.RESET}")

    # ── Drift ──
    drifts = []
    if day.last_metar and day.last_wu:
        d = day.last_metar_wu_drift
        clr = C.GREEN if abs(d) <= 1 else C.YELLOW if abs(d) <= 3 else C.RED
        drifts.append(f"METAR−WU = {clr}{d:+.1f}°F{C.RESET}")
    if day.last_pws and day.last_wu:
        d = day.last_pws_wu_drift
        clr = C.GREEN if abs(d) <= 2 else C.YELLOW if abs(d) <= 5 else C.RED
        drifts.append(f"PWS−WU = {clr}{d:+.1f}°F{C.RESET}")
    if day.last_pws and day.last_metar:
        d = day.last_pws_metar_drift
        clr = C.GREEN if abs(d) <= 2 else C.YELLOW if abs(d) <= 5 else C.RED
        drifts.append(f"PWS−METAR = {clr}{d:+.1f}°F{C.RESET}")
    if drifts:
        print(f"\n  {C.BOLD}{C.WHITE}DRIFT{C.RESET}  {C.DIM}(current temps){C.RESET}")
        print(f"    {'    '.join(drifts)}")

    # ── Edge timeline ──
    active_crossings = {k: v for k, v in day.crossings.items()
                        if v.metar_crossed_at or v.wu_crossed_at}
    if active_crossings:
        print(f"\n  {C.BOLD}{C.EDGE}EDGE TIMELINE{C.RESET}  {C.DIM}(METAR → WU → Market){C.RESET}")
        print(f"  {C.EDGE}╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌{C.RESET}")
        for strike in sorted(active_crossings.keys()):
            cx = active_crossings[strike]
            parts = []
            if cx.metar_crossed_at:
                parts.append(f"{C.CYAN}METAR={datetime.fromtimestamp(cx.metar_crossed_at, tz=PT).strftime('%H:%M')}{C.RESET}")
            if cx.wu_crossed_at:
                parts.append(f"{C.GREEN}WU={datetime.fromtimestamp(cx.wu_crossed_at, tz=PT).strftime('%H:%M')}{C.RESET}")
            else:
                parts.append(f"{C.WARN}WU=waiting…{C.RESET}")
            if cx.market_repriced_at:
                parts.append(f"{C.GREEN}MKT={datetime.fromtimestamp(cx.market_repriced_at, tz=PT).strftime('%H:%M')}{C.RESET}")
            elif cx.wu_crossed_at:
                parts.append(f"{C.WARN}MKT=waiting…{C.RESET}")

            lags = []
            if cx.pws_early_warning() is not None:
                lags.append(f"{C.YELLOW}PWS warned {cx.pws_early_warning():.0f}m early{C.RESET}")
            if cx.metar_to_wu_lag() is not None:
                lags.append(f"METAR→WU = {C.EDGE}{cx.metar_to_wu_lag():.1f}m{C.RESET}")
            if cx.wu_to_market_lag() is not None:
                lags.append(f"WU→MKT = {C.EDGE}{cx.wu_to_market_lag():.1f}m{C.RESET}")
            if cx.edge_window() is not None:
                lags.append(f"EDGE = {C.BOLD}{C.EDGE}{cx.edge_window():.1f}m{C.RESET}")

            print(f"    {C.VALUE}K{strike:.0f}{C.RESET}   {' → '.join(parts)}")
            if lags:
                print(f"          [{' │ '.join(lags)}]")


def print_market_prices(prices: dict, day: DayState):
    """Print current market prices for all strikes."""
    if not prices:
        print(f"\n  {C.DIM}[IB prices not available — data collection mode]{C.RESET}")
        return

    print(f"\n  {C.BOLD}{C.WHITE}MARKET PRICES{C.RESET}  {C.DIM}(ask prices — bid shown as [b] when no ask){C.RESET}")
    print(f"  {C.HEADER}┌────────┬─────────┬─────────┬─────────┬───────┬───────┬────────────────┐{C.RESET}")
    print(f"  {C.HEADER}│{C.RESET} {C.WHITE}Strike{C.RESET} "
          f"{C.HEADER}│{C.RESET} {C.WHITE}  YES{C.RESET}   "
          f"{C.HEADER}│{C.RESET} {C.WHITE}   NO{C.RESET}   "
          f"{C.HEADER}│{C.RESET} {C.WHITE}  SUM{C.RESET}   "
          f"{C.HEADER}│{C.RESET} {C.WHITE}  YD{C.RESET}  "
          f"{C.HEADER}│{C.RESET} {C.WHITE}  ND{C.RESET}  "
          f"{C.HEADER}│{C.RESET} {C.WHITE}Status{C.RESET}         {C.HEADER}│{C.RESET}")
    print(f"  {C.HEADER}├────────┼─────────┼─────────┼─────────┼───────┼───────┼────────────────┤{C.RESET}")

    for strike in sorted(prices.keys()):
        ya, na, yb, nb, yad, nad, ybd, nbd = prices[strike]

        # Format prices — show ask, fallback to bid (dimmed), else dash
        # All formats are exactly 6 visible chars to keep columns aligned
        if ya > 0:
            ya_str = f"${ya:>5.2f}"
        elif yb > 0:
            ya_str = f"{C.DIM}b{yb:>4.2f}{C.RESET} "
        else:
            ya_str = f"{'—':>6}"

        if na > 0:
            na_str = f"${na:>5.2f}"
        elif nb > 0:
            na_str = f"{C.DIM}b{nb:>4.2f}{C.RESET} "
        else:
            na_str = f"{'—':>6}"

        if ya > 0 and na > 0:
            s = ya + na
            sum_clr = C.GREEN if s < 0.95 else C.YELLOW if s < 1.0 else C.RED
            sum_str = f"{sum_clr}${s:>5.2f}{C.RESET}"
        else:
            sum_str = f"{C.DIM}{'—':>6}{C.RESET}"

        # Show ask depth, or bid depth (dimmed) when showing bid price
        yd_val = yad if ya > 0 else ybd
        nd_val = nad if na > 0 else nbd
        yd_str = f"{yd_val:>5}" if ya > 0 or yb <= 0 else f"{C.DIM}{yd_val:>5}{C.RESET}"
        nd_str = f"{nd_val:>5}" if na > 0 or nb <= 0 else f"{C.DIM}{nd_val:>5}{C.RESET}"

        metar_exceeds = day.metar_predicts_exceeds(strike)
        wu_exceeds = day.wu_settled_exceeds(strike)

        if wu_exceeds:
            status = f"{C.OK}✓ WU CONFIRMED{C.RESET}"
            status_pad = 14
        elif metar_exceeds:
            status = f"{C.YELLOW}⚡ METAR > K{C.RESET}"
            status_pad = 12
        elif wu_exceeds is False and metar_exceeds is False:
            status = f"{C.DIM}  below{C.RESET}"
            status_pad = 7
        else:
            status = ""
            status_pad = 0

        # Pad status to fill table cell (16 chars visible)
        pad = " " * max(0, 14 - status_pad)

        print(f"  {C.HEADER}│{C.RESET} {C.VALUE}K{strike:<5.0f}{C.RESET} "
              f"{C.HEADER}│{C.RESET} {C.WHITE}{ya_str}{C.RESET}  "
              f"{C.HEADER}│{C.RESET} {C.WHITE}{na_str}{C.RESET}  "
              f"{C.HEADER}│{C.RESET} {sum_str}  "
              f"{C.HEADER}│{C.RESET} {yd_str} "
              f"{C.HEADER}│{C.RESET} {nd_str} "
              f"{C.HEADER}│{C.RESET} {status}{pad} {C.HEADER}│{C.RESET}")

    print(f"  {C.HEADER}└────────┴─────────┴─────────┴─────────┴───────┴───────┴────────────────┘{C.RESET}")


def print_signal(sig: Signal, day: DayState):
    """Print and send alert for a detected signal."""
    now_str = datetime.now(PT).strftime("%H:%M:%S PT")
    icon = "📉" if sig.direction == "BUY_NO" else "📈"
    dir_clr = C.RED if sig.direction == "BUY_NO" else C.GREEN

    if sig.direction == "BUY_YES":
        action = f"BUY YES @ ${sig.yes_ask:.2f} — pays $1.00 if temp > {sig.strike:.0f}°F"
    else:
        action = f"BUY NO  @ ${sig.no_ask:.2f} — pays $1.00 if temp ≤ {sig.strike:.0f}°F"

    # Extract short reason (after the colon)
    short_reason = sig.reason.split(":")[0] if ":" in sig.reason else sig.reason

    print(f"\n  {C.ALERT}┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓{C.RESET}")
    print(f"  {C.ALERT}┃{C.RESET}  {icon} {dir_clr}{C.BOLD}{sig.direction}{C.RESET}"
          f"   {C.VALUE}K{sig.strike:.0f}{C.RESET}"
          f"   {C.DIM}{short_reason}{C.RESET}"
          f"  {C.ALERT}┃{C.RESET}")
    print(f"  {C.ALERT}┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛{C.RESET}")

    print(f"\n    {C.DIM}Reason:{C.RESET}           {sig.reason}")
    print(f"    {C.DIM}Edge score:{C.RESET}       {C.EDGE}{C.BOLD}{sig.edge_score:+.4f}{C.RESET}")
    print()
    print(f"    {C.DIM}YES ask:{C.RESET}          {C.VALUE}${sig.yes_ask:.2f}{C.RESET}"
          f"  {C.DIM}({sig.yes_ask*100:.0f}%){C.RESET}")
    print(f"    {C.DIM}NO ask:{C.RESET}           {C.VALUE}${sig.no_ask:.2f}{C.RESET}")
    print(f"    {C.DIM}Depth YES/NO:{C.RESET}     {sig.yes_depth} / {sig.no_depth}")
    print()
    print(f"    {C.DIM}METAR temp:{C.RESET}       {C.CYAN}{sig.metar_temp:.1f}°F{C.RESET}")
    print(f"    {C.DIM}WU high:{C.RESET}          {C.SETTLE}{sig.wu_high}°F{C.RESET}")
    print(f"    {C.DIM}PWS temp:{C.RESET}         {C.YELLOW}{sig.pws_temp:.1f}°F{C.RESET}")
    print()
    print(f"    {C.DIM}Profit/contract:{C.RESET}  {C.GREEN}{C.BOLD}${sig.profit_per_contract:.4f}{C.RESET}")
    print(f"    {C.DIM}Action:{C.RESET}           {dir_clr}{action}{C.RESET}")
    print()
    print(f"    {C.DIM}Time:{C.RESET}             {now_str}")
    print()

    send_telegram(
        f"{icon} *{sig.direction} — K{sig.strike:.0f}*\n"
        f"[{sig.reason}]\n"
        f"Edge: `{sig.edge_score:+.4f}`\n"
        f"YES: `${sig.yes_ask:.2f}` NO: `${sig.no_ask:.2f}`\n"
        f"METAR: `{sig.metar_temp:.1f}°F` WU: `{sig.wu_high}°F`"
        f" PWS: `{sig.pws_temp:.1f}°F`\n"
        f"Profit: `${sig.profit_per_contract:.4f}/contract`\n"
        f"Action: `{action}`\n"
        f"Time: `{now_str}`"
    )


# ─── MAIN (fully async) ──────────────────────────────────────────────────────

async def main():
    W = 65
    print(f"\n  {C.HEADER}╔{'═'*W}╗{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.BOLD}{C.WHITE}ForecastBot — Weather Edge Scanner v4.0{C.RESET}"
          + " " * (W - 40) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Started: {datetime.now(PT).strftime('%Y-%m-%d %H:%M:%S PT')}{C.RESET}"
          + " " * (W - 39) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  Station: {C.CYAN}KLAX{C.RESET}  │  Contract: {C.CYAN}UHLAX{C.RESET}"
          + " " * (W - 38) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  Sources: {C.CYAN}METAR{C.RESET} + {C.GREEN}WU{C.RESET} + {C.YELLOW}PWS({PWS_STATION_ID}){C.RESET}"
          + " " * (W - 41) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  Golden Hour: {C.YELLOW}{GOLDEN_START_HOUR}:00–{GOLDEN_END_HOUR}:00 PT{C.RESET}"
          + " " * (W - 33) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  Poll: {POLL_GOLDEN_SEC}s {C.YELLOW}golden{C.RESET}"
          f" │ {POLL_APPROACHING_SEC}s {C.CYAN}approaching{C.RESET}"
          f" │ {POLL_SIGNAL_SEC}s {C.RED}signal{C.RESET}"
          f" │ {POLL_NORMAL_SEC}s {C.DIM}normal{C.RESET}"
          + " " * (W - 62) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}║{C.RESET}  {C.RED}{C.BOLD}*** OBSERVATION ONLY — NO ORDERS ***{C.RESET}"
          + " " * (W - 38) + f"{C.HEADER}║{C.RESET}")
    print(f"  {C.HEADER}╚{'═'*W}╝{C.RESET}\n")

    init_logs()
    loop = asyncio.get_event_loop()

    # ── IB connection — async, stays alive for full session ────────────
    ib_feed = IBPriceFeed()
    ib_connected = await ib_feed.start()
    if ib_connected:
        log.info(f"  ✓ IB price feed active — {len(ib_feed.pairs)} strikes")
        log.info(f"  Contract date: {ib_feed.contract_date}")
    else:
        log.info("  IB unavailable — data collection mode only")

    # ── Initialize day ────────────────────────────────────────────────
    today_str = datetime.now(PT).strftime("%Y-%m-%d")
    day = DayState(date_pt=today_str)

    # ── Initial data fetch ────────────────────────────────────────────
    log.info("  Fetching initial data from all sources…")
    metar, wu, pws, forecast_high = await asyncio.gather(
        loop.run_in_executor(None, fetch_metar),
        loop.run_in_executor(None, fetch_wu_current),
        loop.run_in_executor(None, fetch_pws),
        loop.run_in_executor(None, fetch_wu_forecast_high),
    )

    if forecast_high is not None:
        day.forecast_high_f = forecast_high
        log.info(f"  WU Forecast High: {forecast_high}°F")
    else:
        log.warning("  WU forecast high unavailable")

    if metar:
        day.last_metar = metar
        day.metar_high_f = metar.temp_f
        day.metar_high_rounded = metar.temp_rounded
        log.info(f"  METAR: {metar.temp_f:.1f}°F (rounded={metar.temp_rounded}°F)"
                 f"  obs={metar.obs_time_pt}")

    if wu:
        day.last_wu = wu
        # Don't use wu.high_f (temperatureMax24Hour) — it's a rolling 24h max
        # that bleeds yesterday's high into the morning. Track day high from
        # current temp readings instead. wu.high_f only trustworthy after ~14:00 PT
        # when the 24h window is fully within today.
        day.wu_high_f = wu.temp_f  # seed with current temp, not rolling 24h max
        day.wu_api_24h_high = wu.high_f  # keep for reference/logging
        day.wu_bot_high_f = wu.temp_f
        day.wu_last_obs_time = wu.obs_time
        log.info(f"  WU: temp={wu.temp_f}°F  api_24h_high={wu.high_f}°F"
                 f"  (using current temp as day high seed)  obs={wu.obs_time}")

    if pws:
        day.last_pws = pws
        day.pws_high_f = pws.temp_f
        log.info(f"  PWS: {pws.temp_f:.1f}°F  obs={pws.obs_time}")

    send_telegram(
        f"🌤 *Weather Edge v4.0 Started*\n"
        f"Date: `{today_str}`\n"
        f"Forecast high: `{day.forecast_high_f}°F`\n"
        f"Sources: METAR + WU + PWS({PWS_STATION_ID})\n"
        f"METAR: `{day.metar_high_rounded}°F`  WU: `{day.wu_high_f}°F`\n"
        f"IB: `{'active — ' + str(len(ib_feed.pairs)) + ' strikes' if ib_connected else 'unavailable'}`\n"
        f"Golden hour: `{GOLDEN_START_HOUR}:00–{GOLDEN_END_HOUR}:00 PT`"
    )

    log.info("\n  Polling… (Ctrl+C to stop)\n")

    last_date = today_str
    last_alert_ts = {}  # strike → timestamp of last alert
    signal_mode_until = 0.0  # time.time() until which we poll at signal rate
    approaching_start = 0.0  # when approaching burst started
    approaching_cooldown_until = 0.0  # cooldown end timestamp
    last_metar_fetch_ts = time.time()  # initial fetch just happened

    try:
        while True:
            now_pt = datetime.now(PT)
            today_str = now_pt.strftime("%Y-%m-%d")

            # ── Daily rollover ────────────────────────────────────────
            if today_str != last_date:
                log.info(f"  Day rollover → {today_str}")
                log.info(f"  DAILY SUMMARY: METAR_high={day.metar_high_rounded}°F"
                         f"  WU_high={day.wu_high_f}°F  PWS_high={day.pws_high_f:.1f}°F"
                         f"  signals={day.signals_fired}  polls={day.total_polls}"
                         f"  wu_updates={day.wu_update_count}")
                write_crossings(day)
                send_telegram(
                    f"📊 *Daily Summary — {day.date_pt}*\n"
                    f"METAR high: `{day.metar_high_rounded}°F`\n"
                    f"WU high: `{day.wu_high_f}°F` (SETTLEMENT)\n"
                    f"PWS high: `{day.pws_high_f:.1f}°F`\n"
                    f"Signals: `{day.signals_fired}`\n"
                    f"WU updates: `{day.wu_update_count}`\n"
                    f"Polls: `{day.total_polls}`"
                )
                day = DayState(date_pt=today_str)
                last_date = today_str
                last_alert_ts = {}

            # ── Fetch data sources ───────────────────────────────────
            # METAR updates hourly — skip fetch if we already have a
            # recent reading to avoid wasting API calls during fast polling.
            fetch_ts = time.time()
            need_metar = (fetch_ts - last_metar_fetch_ts) >= METAR_FETCH_INTERVAL_SEC

            if need_metar:
                metar, wu, pws = await asyncio.gather(
                    loop.run_in_executor(None, fetch_metar),
                    loop.run_in_executor(None, fetch_wu_current),
                    loop.run_in_executor(None, fetch_pws),
                )
                last_metar_fetch_ts = fetch_ts
            else:
                metar = None  # reuse cached day.last_metar
                wu, pws = await asyncio.gather(
                    loop.run_in_executor(None, fetch_wu_current),
                    loop.run_in_executor(None, fetch_pws),
                )

            day.total_polls += 1

            # Update METAR
            if metar:
                day.last_metar = metar
                # Track METAR observation time changes
                if metar.obs_time_pt != day.metar_last_obs_time:
                    day.metar_last_obs_time = metar.obs_time_pt
                    day.metar_update_count += 1
                    day.metar_last_update_wallclock = time.time()
                    log.info(f"  METAR UPDATE #{day.metar_update_count}:"
                             f" {metar.temp_f:.1f}°F  obs={metar.obs_time_pt}")
                if metar.temp_f > day.metar_high_f:
                    old = day.metar_high_rounded
                    day.metar_high_f = metar.temp_f
                    day.metar_high_rounded = max(day.metar_high_rounded,
                                                 metar.temp_rounded)
                    if day.metar_high_rounded > old and old > 0:
                        log.info(f"  ⚡ METAR NEW HIGH: {old}°F → {day.metar_high_rounded}°F"
                                 f"  (raw={metar.temp_f:.1f}°F)")

            # Update WU
            if wu:
                day.last_wu = wu
                day.wu_api_24h_high = max(day.wu_api_24h_high, wu.high_f)
                # Track day high from current temp readings (not rolling 24h API)
                if wu.temp_f > day.wu_high_f:
                    old = day.wu_high_f
                    day.wu_high_f = wu.temp_f
                    if old > 0:
                        log.info(f"  ✓ WU NEW DAY HIGH: {old}°F → {wu.temp_f}°F (SETTLEMENT)")
                if wu.temp_f > day.wu_bot_high_f:
                    day.wu_bot_high_f = wu.temp_f
                # Track WU update cycles
                if wu.obs_time != day.wu_last_obs_time:
                    day.wu_last_obs_time = wu.obs_time
                    day.wu_update_count += 1
                    day.wu_last_update_wallclock = time.time()

            # Update PWS
            if pws:
                day.last_pws = pws
                if pws.temp_f > day.pws_high_f:
                    day.pws_high_f = pws.temp_f

            # ── Compute drift between sources ─────────────────────────
            day.update_drifts()

            # ── Check strike crossings (edge measurement) ────────────
            if ib_feed.connected and ib_feed.strikes:
                check_strikes = [float(s) for s in ib_feed.strikes]
            else:
                center = max(day.metar_high_rounded, day.wu_high_f, 60)
                check_strikes = [float(center + i) for i in range(-3, 4)]
            day.check_strike_crossings(check_strikes)

            # ── Print source status ───────────────────────────────────
            print_source_status(day)

            # ── Log source data ───────────────────────────────────────
            write_source_tick(day)

            # ── Read IB market prices — instant, already streaming ────
            prices = ib_feed.read_all() if ib_feed.connected else {}

            # ── Check market repricing (edge window measurement) ──────
            if prices:
                day.check_market_repricing(prices)

            # ── Print market prices ───────────────────────────────────
            print_market_prices(prices, day)

            # ── Log market ticks ──────────────────────────────────────
            for strike in sorted(prices.keys()):
                ya, na, yb, nb, yad, nad, ybd, nbd = prices[strike]
                write_market_tick(day, strike, ya, na, yad, nad)

            # ── Detect signals ────────────────────────────────────────
            if prices:
                signals = detect_signals(day, prices)

                for sig in signals:
                    now_ts = time.time()
                    strike_key = f"{sig.strike}_{sig.direction}"

                    # Check cooldown per strike+direction
                    if (now_ts - last_alert_ts.get(strike_key, 0)
                            > ALERT_COOLDOWN_SEC):
                        last_alert_ts[strike_key] = now_ts
                        day.signals_fired += 1
                        day.signal_strikes.append(sig.strike)
                        write_signal(day, sig)
                        print_signal(sig, day)
                        signal_mode_until = now_ts + 300  # 5 min of fast polling

            # ── Determine next poll interval ──────────────────────────
            now_ts = time.time()

            # Check if observed high is approaching any active strike
            obs_high = max(day.metar_high_rounded, day.wu_high_f)
            approaching_strike = None
            if obs_high > 0 and ib_feed.strikes:
                for s in ib_feed.strikes:
                    gap_to_strike = float(s) - obs_high
                    if 0 < gap_to_strike <= APPROACHING_THRESHOLD_F:
                        approaching_strike = int(s)
                        break  # nearest strike above is enough

            # Approaching mode with burst/cooldown to avoid API rate limits
            use_approaching = False
            if approaching_strike is not None and now_ts >= approaching_cooldown_until:
                if approaching_start == 0.0:
                    # Start a new burst
                    approaching_start = now_ts
                    use_approaching = True
                    log.info(f"  {C.CYAN}▶ APPROACHING MODE: K{approaching_strike}"
                             f" (obs={obs_high}°F, gap={approaching_strike - obs_high}°F)"
                             f" — burst {APPROACHING_BURST_SEC}s{C.RESET}")
                elif now_ts - approaching_start < APPROACHING_BURST_SEC:
                    # Still within burst window
                    use_approaching = True
                else:
                    # Burst expired → enter cooldown
                    approaching_cooldown_until = now_ts + APPROACHING_COOLDOWN_SEC
                    approaching_start = 0.0
                    log.info(f"  {C.DIM}⏸ Approaching cooldown"
                             f" ({APPROACHING_COOLDOWN_SEC}s){C.RESET}")
            elif approaching_strike is None:
                # Temp moved away or crossed — reset state
                approaching_start = 0.0

            if now_ts < signal_mode_until:
                interval = POLL_SIGNAL_SEC
                mode_str = "signal"
            elif use_approaching:
                interval = POLL_APPROACHING_SEC
                remaining = int(APPROACHING_BURST_SEC - (now_ts - approaching_start))
                mode_str = f"approaching K{approaching_strike} [{remaining}s left]"
            elif GOLDEN_START_HOUR <= now_pt.hour < GOLDEN_END_HOUR:
                interval = POLL_GOLDEN_SEC
                mode_str = "golden"
            else:
                interval = POLL_NORMAL_SEC
                mode_str = "normal"

            if "approaching" in mode_str:
                mode_clr = C.CYAN
            elif mode_str == "signal":
                mode_clr = C.RED
            elif mode_str == "golden":
                mode_clr = C.YELLOW
            else:
                mode_clr = C.DIM
            print(f"\n  {C.DIM}Next poll in{C.RESET} {mode_clr}{interval}s ({mode_str}){C.RESET}")

            await asyncio.sleep(interval)

    except KeyboardInterrupt:
        log.info("\n  Stopped by user.")
    except Exception as e:
        log.critical(f"\n  FATAL: {e}\n{traceback.format_exc()}")
        send_telegram(f"🚨 *Weather Edge CRASHED*\n`{str(e)[:200]}`")
    finally:
        write_crossings(day)
        ib_feed.stop()

        W = 65
        print(f"\n  {C.HEADER}╔{'═'*W}╗{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.BOLD}{C.WHITE}SESSION SUMMARY{C.RESET}"
              + " " * (W - 17) + f"{C.HEADER}║{C.RESET}")
        print(f"  {C.HEADER}╠{'═'*W}╣{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Date:{C.RESET}             {day.date_pt}"
              + " " * (W - 29) + f"{C.HEADER}║{C.RESET}")
        fh_str = f"{day.forecast_high_f}°F" if day.forecast_high_f > 0 else "n/a"
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Forecast high:{C.RESET}    "
              f"{C.EDGE}{fh_str}{C.RESET}"
              + " " * (W - 23 - len(fh_str)) + f"{C.HEADER}║{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}METAR high:{C.RESET}       "
              f"{C.CYAN}{day.metar_high_rounded}°F{C.RESET}"
              f"  {C.DIM}(raw = {day.metar_high_f:.1f}°F){C.RESET}"
              + " " * (W - 42) + f"{C.HEADER}║{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}WU high:{C.RESET}          "
              f"{C.SETTLE}{day.wu_high_f}°F{C.RESET}  ◀ SETTLEMENT"
              + " " * (W - 39) + f"{C.HEADER}║{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}PWS high:{C.RESET}         "
              f"{C.YELLOW}{day.pws_high_f:.1f}°F{C.RESET}"
              + " " * (W - 29) + f"{C.HEADER}║{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}WU updates:{C.RESET}       {day.wu_update_count}"
              + " " * (W - 23 - len(str(day.wu_update_count))) + f"{C.HEADER}║{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Signals fired:{C.RESET}    {day.signals_fired}"
              + " " * (W - 23 - len(str(day.signals_fired))) + f"{C.HEADER}║{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Total polls:{C.RESET}      {day.total_polls}"
              + " " * (W - 23 - len(str(day.total_polls))) + f"{C.HEADER}║{C.RESET}")

        # Crossing measurements
        active_cx = {k: v for k, v in day.crossings.items()
                     if v.metar_crossed_at or v.wu_crossed_at}
        if active_cx:
            print(f"  {C.HEADER}╠{'═'*W}╣{C.RESET}")
            print(f"  {C.HEADER}║{C.RESET}  {C.BOLD}{C.EDGE}EDGE WINDOW MEASUREMENTS{C.RESET}"
                  + " " * (W - 26) + f"{C.HEADER}║{C.RESET}")
            for strike in sorted(active_cx.keys()):
                cx = active_cx[strike]
                parts = []
                if cx.pws_early_warning() is not None:
                    parts.append(f"PWS warned {cx.pws_early_warning():.0f}m early")
                if cx.metar_to_wu_lag() is not None:
                    parts.append(f"METAR→WU = {cx.metar_to_wu_lag():.1f}m")
                if cx.wu_to_market_lag() is not None:
                    parts.append(f"WU→MKT = {cx.wu_to_market_lag():.1f}m")
                if cx.edge_window() is not None:
                    parts.append(f"EDGE = {cx.edge_window():.1f}m")
                detail = ' │ '.join(parts) if parts else "METAR crossed, waiting…"
                line = f"  K{strike:.0f}:  {detail}"
                pad = max(0, W - len(line) - 1)
                print(f"  {C.HEADER}║{C.RESET}{line}" + " " * pad + f"{C.HEADER}║{C.RESET}")

        # Data files
        print(f"  {C.HEADER}╠{'═'*W}╣{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Sources:    {SOURCE_CSV}{C.RESET}"
              + " " * max(0, W - 15 - len(SOURCE_CSV)) + f"{C.HEADER}║{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Market:     {TICKS_CSV}{C.RESET}"
              + " " * max(0, W - 15 - len(TICKS_CSV)) + f"{C.HEADER}║{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Signals:    {SIGNAL_CSV}{C.RESET}"
              + " " * max(0, W - 15 - len(SIGNAL_CSV)) + f"{C.HEADER}║{C.RESET}")
        print(f"  {C.HEADER}║{C.RESET}  {C.DIM}Crossings:  {CROSSING_CSV}{C.RESET}"
              + " " * max(0, W - 15 - len(CROSSING_CSV)) + f"{C.HEADER}║{C.RESET}")
        print(f"  {C.HEADER}╚{'═'*W}╝{C.RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
