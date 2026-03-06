"""
weather_edge.py -- ForecastBot Weather Edge Scanner  v4.0
==========================================================
THESIS (validated March 5, 2026):
  WU settlement = METAR ASOS data (rounded to integer F).
  93% match rate over 30 days when accounting for UTC/PT offset.

  PWS stations (KCAELSEG23) read 2-5F higher than WU published.
  They are NOT the settlement source, but ARE leading indicators.

  Edge window = time between data source crossing a strike
  and IBKR market repricing:
    METAR updates hourly (~:53 past hour)
    -> WU processes in ~7-10 min
    -> IBKR market reprices in ~2-3 min after WU
    = ~10-13 min edge window per METAR update

ARCHITECTURE:
  Three data sources polled in parallel:
    1. METAR (aviationweather.gov) - hourly, settlement source
    2. WU current (api.weather.com) - ~10 min updates, confirmation
    3. PWS KCAELSEG23 (api.weather.com) - 5 min, leading indicator
  Plus IB market data streaming continuously.

  Golden hour: 12:00-14:30 PT (when daily peak occurs 83% of days)
  Poll rate: 60s during golden hour, 300s outside

SETTLEMENT SEMANTICS:
  "Exceed 75F" means STRICTLY > 75F.
  WU rounds to integer. Need >= 75.6F actual to get WU=76 > 75.
  WU high of exactly 75F does NOT pay K75 YES.

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

# --- LOGGING -----------------------------------------------------------------
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
ET = ZoneInfo("America/New_York")

# --- CONFIG -------------------------------------------------------------------
IBKR_HOST      = os.getenv("IBKR_HOST",                  "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT",              "4001"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID_WEATHER", "45"))

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")

# Poll rates
POLL_GOLDEN_SEC    = 60     # during golden hour (12-14:30 PT)
POLL_NORMAL_SEC    = 300    # outside golden hour
POLL_SIGNAL_SEC    = 30     # after a signal is detected

# Golden hour: when daily peak occurs (validated from 30 days of KCAELSEG23 data)
GOLDEN_START_HOUR  = 12     # 12:00 PM PT
GOLDEN_END_HOUR    = 15     # end at 3:00 PM PT (covers 14:30 + buffer)

# Edge thresholds
EDGE_ALERT_SCORE   = 0.15   # alert when |edge| >= this
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


# --- HELPERS ------------------------------------------------------------------

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


# --- DATA STRUCTURES ---------------------------------------------------------

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
    high_f: int             # WU running daily high (integer) -- THIS IS SETTLEMENT
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
      METAR crosses strike (hourly)   <- EDGE STARTS here (we know the answer)
        -> WU processes (~7-10 min)   <- settlement confirmation
        -> Market reprices (~2-3 min) <- EDGE CLOSES here

      PWS crossing is logged as early warning context only.
      PWS reads 2-5F high so its crossing does NOT mean settlement will cross.
    """
    strike: float
    pws_crossed_at: float = 0.0       # early warning only (reads 2-5F high)
    metar_crossed_at: float = 0.0     # EDGE STARTS -- METAR is settlement source
    wu_crossed_at: float = 0.0        # settlement confirmed
    market_repriced_at: float = 0.0   # EDGE CLOSES -- YES ask jumped > 0.80

    def metar_to_wu_lag(self) -> Optional[float]:
        """Minutes from METAR crossing to WU confirming. Our hypothesis: 7-10 min."""
        if self.metar_crossed_at and self.wu_crossed_at:
            return (self.wu_crossed_at - self.metar_crossed_at) / 60
        return None

    def wu_to_market_lag(self) -> Optional[float]:
        """Minutes from WU confirming to market repricing. Our hypothesis: 2-3 min."""
        if self.wu_crossed_at and self.market_repriced_at:
            return (self.market_repriced_at - self.wu_crossed_at) / 60
        return None

    def edge_window(self) -> Optional[float]:
        """Minutes from METAR crossing (we know) to market repricing (edge gone).
        This is THE number we need to validate. Hypothesis: 10-13 min."""
        if self.metar_crossed_at and self.market_repriced_at:
            return (self.market_repriced_at - self.metar_crossed_at) / 60
        return None

    def pws_early_warning(self) -> Optional[float]:
        """Minutes of advance notice PWS gave before METAR confirmed.
        Complementary info -- how much earlier could we have positioned?"""
        if self.pws_crossed_at and self.metar_crossed_at:
            return (self.metar_crossed_at - self.pws_crossed_at) / 60
        return None


@dataclass
class DayState:
    date_pt: str = ""
    # Tracked highs from each source
    metar_high_f: float = 0.0       # max METAR reading today (decimal)
    metar_high_rounded: int = 0     # rounded -- predicts WU settlement
    wu_high_f: int = 0              # WU published running high (integer) -- SETTLEMENT
    pws_high_f: float = 0.0        # PWS max today (leading indicator, reads high)
    # Last readings
    last_metar: Optional[METARReading] = None
    last_wu: Optional[WUReading] = None
    last_pws: Optional[PWSReading] = None
    # Tracking
    total_polls: int = 0
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
    last_metar_wu_drift: float = 0.0     # METAR current - WU current
    last_pws_wu_drift: float = 0.0       # PWS current - WU current
    last_pws_metar_drift: float = 0.0    # PWS current - METAR current
    # WU update lag measurements (minutes)
    wu_lag_samples: list = field(default_factory=list)  # list of (metar_update_ts, wu_follow_ts)
    # Strike crossing timelines -- the edge measurement
    crossings: dict = field(default_factory=dict)  # strike -> StrikeCrossing
    # Market price snapshots for repricing detection
    last_market_prices: dict = field(default_factory=dict)  # strike -> yes_ask

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

        Order: METAR (edge start) -> WU (confirmation) -> Market (edge close)
        PWS logged as early warning context only (reads 2-5F high, not reliable for crossing).
        """
        now = time.time()
        for strike in strikes:
            if strike not in self.crossings:
                self.crossings[strike] = StrikeCrossing(strike=strike)
            cx = self.crossings[strike]

            # PWS early warning (complementary -- NOT the edge trigger)
            # PWS reads 2-5F high, so crossing here doesn't mean settlement will cross
            if self.pws_high_f > strike and cx.pws_crossed_at == 0:
                cx.pws_crossed_at = now
                log.info(f"  PWS EARLY WARNING: K{strike:.0f}"
                         f" (PWS={self.pws_high_f:.1f}F, but reads 2-5F high)")

            # METAR crossed = EDGE STARTS (METAR is the settlement source)
            if self.metar_high_rounded > strike and cx.metar_crossed_at == 0:
                cx.metar_crossed_at = now
                pws_note = ""
                if cx.pws_crossed_at:
                    pws_note = (f"  (PWS warned {cx.pws_early_warning():.1f}min"
                                f" earlier)")
                log.info(f"  ** EDGE START: METAR crossed K{strike:.0f} **"
                         f" (METAR high={self.metar_high_rounded}F)"
                         f" -- WU should follow in ~10min{pws_note}")

            # WU crossed = settlement confirmed
            if self.wu_high_f > strike and cx.wu_crossed_at == 0:
                cx.wu_crossed_at = now
                metar_lag = ""
                if cx.metar_crossed_at:
                    metar_lag = (f"  METAR->WU took"
                                f" {cx.metar_to_wu_lag():.1f}min")
                log.info(f"  WU CONFIRMED: K{strike:.0f}"
                         f" (WU high={self.wu_high_f}F){metar_lag}"
                         f" -- market should reprice in ~2-3min")

    def check_market_repricing(self, prices: dict):
        """Detect when market reprices after a source crossing.
        This closes the edge window measurement."""
        now = time.time()
        for strike, (ya, na, yd, nd) in prices.items():
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
                    lags.append(f"METAR->WU={cx.metar_to_wu_lag():.1f}m")
                if cx.wu_to_market_lag() is not None:
                    lags.append(f"WU->MKT={cx.wu_to_market_lag():.1f}m")
                if cx.edge_window() is not None:
                    lags.append(f"EDGE WINDOW={cx.edge_window():.1f}m")
                log.info(f"  ** EDGE CLOSED: K{strike:.0f}"
                         f" YES={prev_ya:.2f}->{ya:.2f}"
                         f"  {', '.join(lags)} **")

            self.last_market_prices[strike] = ya


# --- DATA FETCHERS -----------------------------------------------------------

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


# --- IB PRICE FEED -----------------------------------------------------------

class IBPriceFeed:
    """Async IB connection for UHLAX YES+NO contract prices."""

    def __init__(self):
        self.ib = None
        self.pairs = {}          # strike -> (yes_ticker, no_ticker)
        self.connected = False
        self.contract_date = ""  # YYYYMMDD of actively-trading contracts
        self.strikes = []        # sorted list of active strikes

    async def start(self) -> bool:
        try:
            from ib_async import IB, Contract
        except ImportError:
            log.warning("  ib_async not installed -- running without IB prices")
            return False
        try:
            self.ib = IB()
            await self.ib.connectAsync(
                IBKR_HOST, IBKR_PORT,
                clientId=IBKR_CLIENT_ID, timeout=10,
            )
            log.info(f"  IB connected (clientId={IBKR_CLIENT_ID})")

            for day_offset in range(0, 3):
                try_date = datetime.now(ET) + timedelta(days=day_offset)
                try_str = try_date.strftime("%Y%m%d")
                c = Contract()
                c.symbol = "UHLAX"
                c.secType = "OPT"
                c.exchange = "FORECASTX"
                c.currency = "USD"
                c.lastTradeDateOrContractMonth = try_str

                details = await self.ib.reqContractDetailsAsync(c)
                if not details:
                    log.info(f"  UHLAX: no contracts for {try_str}, trying next day...")
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
                         f"Warming up {IB_WARMUP_SEC}s...")
                await asyncio.sleep(IB_WARMUP_SEC)

                # Check for live prices
                live_count = 0
                for s in common:
                    ya, na, _, _ = self._read(s)
                    if ya > 0 and na > 0:
                        live_count += 1

                if live_count > 0:
                    self.contract_date = try_str
                    self.strikes = common
                    self.connected = True
                    log.info(f"  Live prices on {live_count}/{len(common)} strikes")
                    return True

                log.warning(f"  No live prices for {try_str} -- trying next day...")
                for yt, nt in self.pairs.values():
                    self.ib.cancelMktData(yt)
                    self.ib.cancelMktData(nt)
                self.pairs = {}

            log.warning("  UHLAX: no actively-trading contracts found")
            return False

        except Exception as e:
            log.warning(f"  IB start failed: {e}")
            return False

    def _read(self, strike: float) -> tuple:
        """Returns (yes_ask, no_ask, yes_depth, no_depth)."""
        if strike not in self.pairs:
            return -1.0, -1.0, 0, 0
        yt, nt = self.pairs[strike]
        ya = float(yt.ask) if hasattr(yt, 'ask') and yt.ask is not None and yt.ask > 0 else -1.0
        na = float(nt.ask) if hasattr(nt, 'ask') and nt.ask is not None and nt.ask > 0 else -1.0
        yd = int(yt.askSize) if hasattr(yt, 'askSize') and yt.askSize is not None else 0
        nd = int(nt.askSize) if hasattr(nt, 'askSize') and nt.askSize is not None else 0
        return ya, na, yd, nd

    def read_all(self) -> dict:
        """Returns {strike: (yes_ask, no_ask, yes_depth, no_depth)}
        Only strikes with valid prices."""
        result = {}
        for s in self.pairs:
            ya, na, yd, nd = self._read(s)
            if ya > 0 and na > 0:
                result[s] = (ya, na, yd, nd)
        return result

    def stop(self):
        try:
            if self.ib:
                self.ib.disconnect()
                log.info("  IB disconnected.")
        except Exception:
            pass


# --- SIGNAL DETECTION ---------------------------------------------------------

@dataclass
class Signal:
    strike: float
    direction: str          # BUY_YES or BUY_NO
    reason: str             # what triggered the signal
    edge_score: float       # how mispriced the market is (0-1 scale)
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
    1. METAR crosses a strike -> WU will follow in ~10 min -> market will reprice
       If market hasn't moved yet, that's our edge.
    2. WU already updated past a strike but market still hasn't repriced.
    3. Temperature peaked and falling -> market still pricing YES too high.
    """
    signals = []

    metar_high = day.metar_high_rounded
    wu_high = day.wu_high_f
    pws_temp = day.last_pws.temp_f if day.last_pws else 0.0
    metar_temp = day.last_metar.temp_f if day.last_metar else 0.0

    now_pt = datetime.now(PT)
    hour = now_pt.hour

    for strike, (ya, na, yd, nd) in prices.items():
        market_yes_prob = ya  # YES ask price = implied probability

        # --- SIGNAL TYPE 1: METAR confirms exceed, market underprices YES ---
        # METAR rounded high > strike means WU will likely publish > strike
        if metar_high > strike:
            # Temperature has exceeded this strike per METAR
            # Fair value of YES is ~0.95+ (allowing for WU rounding uncertainty)
            fair_yes = 0.93
            edge = fair_yes - market_yes_prob
            if edge >= EDGE_ALERT_SCORE and ya < 0.90:
                signals.append(Signal(
                    strike=strike, direction="BUY_YES",
                    reason=f"METAR_CONFIRM: METAR high {metar_high}F > K{strike:.0f}",
                    edge_score=round(edge, 3),
                    yes_ask=ya, no_ask=na, yes_depth=yd, no_depth=nd,
                    metar_temp=metar_temp, wu_high=wu_high, pws_temp=pws_temp,
                    profit_per_contract=round(1.0 - ya, 4),
                ))

        # --- SIGNAL TYPE 2: WU already published high > strike, market lagging ---
        if wu_high > strike:
            # WU has already confirmed exceed -- this is near-certain
            fair_yes = 0.97
            edge = fair_yes - market_yes_prob
            if edge >= EDGE_ALERT_SCORE and ya < 0.93:
                signals.append(Signal(
                    strike=strike, direction="BUY_YES",
                    reason=f"WU_CONFIRM: WU high {wu_high}F > K{strike:.0f}",
                    edge_score=round(edge, 3),
                    yes_ask=ya, no_ask=na, yes_depth=yd, no_depth=nd,
                    metar_temp=metar_temp, wu_high=wu_high, pws_temp=pws_temp,
                    profit_per_contract=round(1.0 - ya, 4),
                ))

        # --- SIGNAL TYPE 3: Post-peak, temp falling, strike NOT exceeded ---
        # After golden hour, if neither METAR nor WU exceeded strike,
        # and temp is falling, YES is overpriced
        if hour >= 15 and metar_high <= strike and wu_high <= strike:
            # How far below strike is the current high?
            gap_to_strike = strike - max(metar_high, wu_high)
            if gap_to_strike >= 2:
                # Very unlikely to reach strike this late
                fair_yes = 0.05
                edge = market_yes_prob - fair_yes  # positive = market overprices YES
                if edge >= EDGE_ALERT_SCORE and na < 0.90:
                    signals.append(Signal(
                        strike=strike, direction="BUY_NO",
                        reason=f"POST_PEAK: high={max(metar_high, wu_high)}F, K{strike:.0f} gap={gap_to_strike}F",
                        edge_score=round(edge, 3),
                        yes_ask=ya, no_ask=na, yes_depth=yd, no_depth=nd,
                        metar_temp=metar_temp, wu_high=wu_high, pws_temp=pws_temp,
                        profit_per_contract=round(1.0 - na, 4),
                    ))

        # --- SIGNAL TYPE 4: PWS leading indicator during golden hour ---
        # PWS trending above strike but METAR hasn't confirmed yet
        # This is a softer signal -- "next METAR might cross"
        if (GOLDEN_START_HOUR <= hour < GOLDEN_END_HOUR
                and pws_temp > strike + 2  # PWS reads 2-5F high, so need margin
                and metar_high <= strike):
            # PWS suggests next METAR update may cross strike
            fair_yes = 0.60  # uncertain, just a leading indicator
            edge = fair_yes - market_yes_prob
            if edge >= EDGE_ALERT_SCORE and ya < 0.50:
                signals.append(Signal(
                    strike=strike, direction="BUY_YES",
                    reason=f"PWS_LEADING: PWS={pws_temp:.1f}F trending > K{strike:.0f}",
                    edge_score=round(edge, 3),
                    yes_ask=ya, no_ask=na, yes_depth=yd, no_depth=nd,
                    metar_temp=metar_temp, wu_high=wu_high, pws_temp=pws_temp,
                    profit_per_contract=round(1.0 - ya, 4),
                ))

    return signals


# --- CSV LOGGING --------------------------------------------------------------

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
                      yd: int, nd: int):
    now_pt = datetime.now(PT)
    is_golden = GOLDEN_START_HOUR <= now_pt.hour < GOLDEN_END_HOUR
    with open(TICKS_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            now_pt.strftime("%Y-%m-%d %H:%M:%S"), day.date_pt, strike,
            ya, na, yd, nd,
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
            continue  # no meaningful crossings
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


# --- CONSOLE OUTPUT -----------------------------------------------------------

def print_source_status(day: DayState):
    """Print current readings from all three data sources."""
    now_str = datetime.now(PT).strftime("%H:%M:%S PT")
    is_golden = GOLDEN_START_HOUR <= datetime.now(PT).hour < GOLDEN_END_HOUR
    mode = "GOLDEN HOUR" if is_golden else "normal"

    print(f"\n  === {now_str}  [{mode}]  poll #{day.total_polls} ===")

    # METAR
    if day.last_metar:
        m = day.last_metar
        age = int(time.time() - m.fetched_at)
        print(f"  METAR:  {m.temp_f:.1f}F (rounded={m.temp_rounded}F)"
              f"  obs={m.obs_time_pt}  wind={m.wind_mph:.0f}mph"
              f"  high_today={day.metar_high_rounded}F")
    else:
        print(f"  METAR:  no data")

    # WU
    if day.last_wu:
        w = day.last_wu
        print(f"  WU:     {w.temp_f}F  high={day.wu_high_f}F (SETTLEMENT)"
              f"  obs={w.obs_time}  updates={day.wu_update_count}")
    else:
        print(f"  WU:     no data")

    # PWS
    if day.last_pws:
        p = day.last_pws
        print(f"  PWS:    {p.temp_f:.1f}F (LEADING)"
              f"  obs={p.obs_time}  high_today={day.pws_high_f:.1f}F")
    else:
        print(f"  PWS:    no data")

    # Source drift (current temps, not highs)
    drifts = []
    if day.last_metar and day.last_wu:
        d = day.last_metar_wu_drift
        drifts.append(f"METAR-WU={d:+.1f}F")
    if day.last_pws and day.last_wu:
        d = day.last_pws_wu_drift
        drifts.append(f"PWS-WU={d:+.1f}F")
    if day.last_pws and day.last_metar:
        d = day.last_pws_metar_drift
        drifts.append(f"PWS-METAR={d:+.1f}F")
    if drifts:
        print(f"  Drift:  {', '.join(drifts)}")

    # Source highs
    sources = []
    if day.metar_high_rounded > 0:
        sources.append(f"METAR={day.metar_high_rounded}F")
    if day.wu_high_f > 0:
        sources.append(f"WU={day.wu_high_f}F")
    if day.pws_high_f > 0:
        sources.append(f"PWS={day.pws_high_f:.0f}F")
    if sources:
        high_drift = ""
        if day.metar_high_rounded > 0 and day.wu_high_f > 0:
            hd = day.metar_high_rounded - day.wu_high_f
            high_drift = f"  (METAR-WU high drift={hd:+d}F)"
        print(f"  Highs:  {', '.join(sources)}{high_drift}")

    # WU update timing
    if day.wu_last_update_wallclock > 0:
        wu_age = int(time.time() - day.wu_last_update_wallclock)
        print(f"  WU age: {wu_age}s since last update"
              f"  ({day.wu_update_count} updates today)")

    # Strike crossing timeline (the edge measurement)
    active_crossings = {k: v for k, v in day.crossings.items()
                        if v.metar_crossed_at or v.wu_crossed_at}
    if active_crossings:
        print(f"  --- Edge Timeline (METAR -> WU -> Market) ---")
        for strike in sorted(active_crossings.keys()):
            cx = active_crossings[strike]
            parts = []
            if cx.metar_crossed_at:
                parts.append(f"METAR={datetime.fromtimestamp(cx.metar_crossed_at, tz=PT).strftime('%H:%M')}")
            if cx.wu_crossed_at:
                parts.append(f"WU={datetime.fromtimestamp(cx.wu_crossed_at, tz=PT).strftime('%H:%M')}")
            else:
                parts.append("WU=waiting...")
            if cx.market_repriced_at:
                parts.append(f"MKT={datetime.fromtimestamp(cx.market_repriced_at, tz=PT).strftime('%H:%M')}")
            elif cx.wu_crossed_at:
                parts.append("MKT=waiting...")

            lags = []
            if cx.pws_early_warning() is not None:
                lags.append(f"PWS warned {cx.pws_early_warning():.0f}m early")
            if cx.metar_to_wu_lag() is not None:
                lags.append(f"METAR->WU={cx.metar_to_wu_lag():.1f}m")
            if cx.wu_to_market_lag() is not None:
                lags.append(f"WU->MKT={cx.wu_to_market_lag():.1f}m")
            if cx.edge_window() is not None:
                lags.append(f"EDGE={cx.edge_window():.1f}m")

            lag_str = f"  [{', '.join(lags)}]" if lags else ""
            print(f"    K{strike:.0f}: {' -> '.join(parts)}{lag_str}")


def print_market_prices(prices: dict, day: DayState):
    """Print current market prices for all strikes."""
    if not prices:
        print("  Market: no IB prices available")
        return

    print(f"\n  {'K':>6}  {'YES':>7}  {'NO':>7}  {'SUM':>7}  "
          f"{'YD':>5}  {'ND':>5}  {'METAR':>6}  {'WU':>4}  STATUS")
    print(f"  {'---':>6}  {'---':>7}  {'---':>7}  {'---':>7}  "
          f"{'---':>5}  {'---':>5}  {'---':>6}  {'---':>4}  ------")

    for strike in sorted(prices.keys()):
        ya, na, yd, nd = prices[strike]
        s = ya + na

        # Determine status based on data sources
        metar_exceeds = day.metar_predicts_exceeds(strike)
        wu_exceeds = day.wu_settled_exceeds(strike)

        if wu_exceeds:
            status = "WU CONFIRMED"
        elif metar_exceeds:
            status = "METAR > strike"
        elif wu_exceeds is False and metar_exceeds is False:
            status = "below"
        else:
            status = ""

        metar_str = f"{day.metar_high_rounded}" if day.metar_high_rounded > 0 else "?"
        wu_str = f"{day.wu_high_f}" if day.wu_high_f > 0 else "?"

        print(f"  {strike:>6.0f}  ${ya:>5.2f}  ${na:>5.2f}  ${s:>5.2f}  "
              f"{yd:>5}  {nd:>5}  {metar_str:>6}  {wu_str:>4}  {status}")


def print_signal(sig: Signal, day: DayState):
    """Print and send alert for a detected signal."""
    now_str = datetime.now(PT).strftime("%H:%M:%S PT")

    if sig.direction == "BUY_YES":
        action = f"BUY YES @ ${sig.yes_ask:.2f} -- pays $1.00 if temp > {sig.strike:.0f}F"
    else:
        action = f"BUY NO  @ ${sig.no_ask:.2f} -- pays $1.00 if temp <= {sig.strike:.0f}F"

    print(f"\n  {'=' * 60}")
    print(f"  SIGNAL: {sig.direction}  K{sig.strike:.0f}  [{sig.reason}]")
    print(f"  {'=' * 60}")
    print(f"  Edge score:       {sig.edge_score:+.3f}")
    print(f"  YES ask:          ${sig.yes_ask:.2f}  (depth={sig.yes_depth})")
    print(f"  NO ask:           ${sig.no_ask:.2f}  (depth={sig.no_depth})")
    print(f"  METAR temp:       {sig.metar_temp:.1f}F")
    print(f"  WU high:          {sig.wu_high}F")
    print(f"  PWS temp:         {sig.pws_temp:.1f}F")
    print(f"  Profit/contract:  ${sig.profit_per_contract:.4f}")
    print(f"  Action:           {action}")
    print(f"  Time:             {now_str}")
    print(f"  {'=' * 60}\n")

    send_telegram(
        f"*{sig.direction} -- K{sig.strike:.0f}*\n"
        f"[{sig.reason}]\n"
        f"Edge: `{sig.edge_score:+.3f}`\n"
        f"YES: `${sig.yes_ask:.2f}` NO: `${sig.no_ask:.2f}`\n"
        f"METAR: `{sig.metar_temp:.1f}F` WU: `{sig.wu_high}F` PWS: `{sig.pws_temp:.1f}F`\n"
        f"Profit: `${sig.profit_per_contract:.4f}/contract`\n"
        f"Action: `{action}`\n"
        f"Time: `{now_str}`"
    )


# --- MAIN (fully async) ------------------------------------------------------

async def main():
    print("\n" + "=" * 65)
    print("  ForecastBot -- Weather Edge Scanner v4.0")
    print(f"  Started: {datetime.now(PT).strftime('%Y-%m-%d %H:%M:%S PT')}")
    print(f"  Sources: METAR({METAR_STATION}) + WU + PWS({PWS_STATION_ID})")
    print(f"  Golden hour: {GOLDEN_START_HOUR}:00-{GOLDEN_END_HOUR}:00 PT")
    print(f"  Poll: {POLL_GOLDEN_SEC}s (golden) / {POLL_NORMAL_SEC}s (normal)")
    print(f"  Edge threshold: {EDGE_ALERT_SCORE:.0%}")
    print("  *** OBSERVATION ONLY -- NO ORDERS ***")
    print("=" * 65 + "\n")

    init_logs()
    loop = asyncio.get_event_loop()

    # -- IB connection
    ib_feed = IBPriceFeed()
    ib_connected = await ib_feed.start()
    if ib_connected:
        log.info(f"  IB price feed active -- {len(ib_feed.pairs)} strikes")
        log.info(f"  Contract date: {ib_feed.contract_date}")
    else:
        log.info("  IB unavailable -- data collection mode only")

    # -- Initialize day
    today_str = datetime.now(PT).strftime("%Y-%m-%d")
    day = DayState(date_pt=today_str)

    # -- Initial data fetch
    log.info("  Fetching initial data from all sources...")
    metar, wu, pws = await asyncio.gather(
        loop.run_in_executor(None, fetch_metar),
        loop.run_in_executor(None, fetch_wu_current),
        loop.run_in_executor(None, fetch_pws),
    )

    if metar:
        day.last_metar = metar
        day.metar_high_f = metar.temp_f
        day.metar_high_rounded = metar.temp_rounded
        log.info(f"  METAR: {metar.temp_f:.1f}F (rounded={metar.temp_rounded}F)"
                 f"  obs={metar.obs_time_pt}")

    if wu:
        day.last_wu = wu
        day.wu_high_f = wu.high_f
        day.wu_last_obs_time = wu.obs_time
        log.info(f"  WU: temp={wu.temp_f}F  high={wu.high_f}F  obs={wu.obs_time}")

    if pws:
        day.last_pws = pws
        day.pws_high_f = pws.temp_f
        log.info(f"  PWS: {pws.temp_f:.1f}F  obs={pws.obs_time}")

    send_telegram(
        f"*Weather Edge v4.0 Started*\n"
        f"Date: `{today_str}`\n"
        f"Sources: METAR + WU + PWS({PWS_STATION_ID})\n"
        f"METAR: `{day.metar_high_rounded}F`  WU: `{day.wu_high_f}F`\n"
        f"IB: `{'active -- ' + str(len(ib_feed.pairs)) + ' strikes' if ib_connected else 'unavailable'}`\n"
        f"Golden hour: `{GOLDEN_START_HOUR}:00-{GOLDEN_END_HOUR}:00 PT`"
    )

    log.info("\n  Polling... (Ctrl+C to stop)\n")

    last_date = today_str
    last_alert_ts = {}  # strike -> timestamp of last alert
    signal_mode_until = 0.0  # time.time() until which we poll at signal rate

    try:
        while True:
            now_pt = datetime.now(PT)
            today_str = now_pt.strftime("%Y-%m-%d")

            # -- Daily rollover
            if today_str != last_date:
                log.info(f"  Day rollover -> {today_str}")
                # Log daily summary
                log.info(f"  DAILY SUMMARY: METAR_high={day.metar_high_rounded}F"
                         f"  WU_high={day.wu_high_f}F  PWS_high={day.pws_high_f:.1f}F"
                         f"  signals={day.signals_fired}  polls={day.total_polls}"
                         f"  wu_updates={day.wu_update_count}")
                write_crossings(day)
                send_telegram(
                    f"*Daily Summary -- {day.date_pt}*\n"
                    f"METAR high: `{day.metar_high_rounded}F`\n"
                    f"WU high: `{day.wu_high_f}F` (SETTLEMENT)\n"
                    f"PWS high: `{day.pws_high_f:.1f}F`\n"
                    f"Signals: `{day.signals_fired}`\n"
                    f"WU updates: `{day.wu_update_count}`\n"
                    f"Polls: `{day.total_polls}`"
                )
                day = DayState(date_pt=today_str)
                last_date = today_str
                last_alert_ts = {}

            # -- Fetch all three data sources in parallel
            metar, wu, pws = await asyncio.gather(
                loop.run_in_executor(None, fetch_metar),
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
                             f" {metar.temp_f:.1f}F  obs={metar.obs_time_pt}")
                if metar.temp_f > day.metar_high_f:
                    old = day.metar_high_rounded
                    day.metar_high_f = metar.temp_f
                    day.metar_high_rounded = max(day.metar_high_rounded,
                                                 metar.temp_rounded)
                    if day.metar_high_rounded > old and old > 0:
                        log.info(f"  METAR NEW HIGH: {old}F -> {day.metar_high_rounded}F"
                                 f"  (raw={metar.temp_f:.1f}F)")

            # Update WU
            if wu:
                day.last_wu = wu
                if wu.high_f > day.wu_high_f:
                    old = day.wu_high_f
                    day.wu_high_f = wu.high_f
                    if old > 0:
                        log.info(f"  WU NEW HIGH: {old}F -> {wu.high_f}F (SETTLEMENT)")
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

            # -- Compute drift between sources
            day.update_drifts()

            # -- Check strike crossings (edge measurement)
            # Use IB strikes if available, otherwise generate from WU high range
            if ib_feed.connected and ib_feed.strikes:
                check_strikes = [float(s) for s in ib_feed.strikes]
            else:
                # Generate reasonable strike range around current temps
                center = max(day.metar_high_rounded, day.wu_high_f, 60)
                check_strikes = [float(center + i) for i in range(-3, 4)]
            day.check_strike_crossings(check_strikes)

            # -- Print source status
            print_source_status(day)

            # -- Log source data
            write_source_tick(day)

            # -- Read IB market prices
            prices = ib_feed.read_all() if ib_feed.connected else {}

            # -- Check market repricing (edge window measurement)
            if prices:
                day.check_market_repricing(prices)

            # -- Print market prices
            print_market_prices(prices, day)

            # -- Log market ticks
            for strike in sorted(prices.keys()):
                ya, na, yd, nd = prices[strike]
                write_market_tick(day, strike, ya, na, yd, nd)

            # -- Detect signals
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

            # -- Determine next poll interval
            now_ts = time.time()
            if now_ts < signal_mode_until:
                interval = POLL_SIGNAL_SEC
                mode_str = "signal"
            elif GOLDEN_START_HOUR <= now_pt.hour < GOLDEN_END_HOUR:
                interval = POLL_GOLDEN_SEC
                mode_str = "golden"
            else:
                interval = POLL_NORMAL_SEC
                mode_str = "normal"

            print(f"  Next poll in {interval}s ({mode_str})")

            await asyncio.sleep(interval)

    except KeyboardInterrupt:
        log.info("\n  Stopped by user.")
    except Exception as e:
        log.critical(f"\n  FATAL: {e}\n{traceback.format_exc()}")
        send_telegram(f"*Weather Edge CRASHED*\n`{str(e)[:200]}`")
    finally:
        write_crossings(day)
        ib_feed.stop()
        print("\n" + "=" * 65)
        print("  SESSION SUMMARY")
        print("=" * 65)
        print(f"  Date:             {day.date_pt}")
        print(f"  METAR high:       {day.metar_high_rounded}F (raw={day.metar_high_f:.1f}F)")
        print(f"  WU high:          {day.wu_high_f}F (SETTLEMENT)")
        print(f"  PWS high:         {day.pws_high_f:.1f}F")
        print(f"  WU updates:       {day.wu_update_count}")
        print(f"  Signals fired:    {day.signals_fired}")
        print(f"  Total polls:      {day.total_polls}")
        # Print crossing timeline summary
        active_cx = {k: v for k, v in day.crossings.items()
                     if v.metar_crossed_at or v.wu_crossed_at}
        if active_cx:
            print(f"\n  EDGE WINDOW MEASUREMENTS:")
            print(f"  (METAR crosses = edge starts, market reprices = edge closes)")
            for strike in sorted(active_cx.keys()):
                cx = active_cx[strike]
                parts = []
                if cx.pws_early_warning() is not None:
                    parts.append(f"PWS warned {cx.pws_early_warning():.0f}m early")
                if cx.metar_to_wu_lag() is not None:
                    parts.append(f"METAR->WU={cx.metar_to_wu_lag():.1f}m")
                if cx.wu_to_market_lag() is not None:
                    parts.append(f"WU->MKT={cx.wu_to_market_lag():.1f}m")
                if cx.edge_window() is not None:
                    parts.append(f"EDGE WINDOW={cx.edge_window():.1f}m")
                if parts:
                    print(f"    K{strike:.0f}: {', '.join(parts)}")
                else:
                    print(f"    K{strike:.0f}: METAR crossed, waiting for WU/market")

        print(f"\n  Data files:")
        print(f"    Sources:    {SOURCE_CSV}")
        print(f"    Market:     {TICKS_CSV}")
        print(f"    Signals:    {SIGNAL_CSV}")
        print(f"    Crossings:  {CROSSING_CSV}\n")


if __name__ == "__main__":
    asyncio.run(main())
