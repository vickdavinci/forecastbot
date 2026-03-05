"""
weather_edge.py — ForecastBot Weather Edge Scanner  v1.0
==========================================================
PURPOSE:
  Directional edge scanner for UHLAX (LA Daily Temperature High).
  Polls actual KLAX temperature observations every 5 minutes.
  Compares actual temperature trajectory to market-implied probability.
  Logs divergence events — when market is pricing wrong vs reality.

THESIS:
  Retail opens app, sees WU forecast (e.g. 77°F), bets YES, walks away.
  They do NOT watch intraday temperature trajectory.
  When actual temp diverges from forecast (wind shift, cloud cover,
  Santa Ana arrival/departure), market prices go stale.
  
  Edge = we poll actual KLAX obs every 5 minutes.
  Market reprices every 30-90 minutes at best (retail manual).
  That gap = directional opportunity.

DATA SOURCES:
  Primary:   NWS KLAX actual observations (free, no key, same station as WU)
  Forecast:  NWS hourly forecast for LAX grid (free, no key)
  Market:    IB API UHLAX prices (streaming via kill_shot connection)

TWO EDGE TYPES:
  Type A — Forecast Divergence (intraday):
    NWS/WU forecast revises significantly vs opening forecast
    Market hasn't repriced yet
    Signal: abs(current_forecast_high - opening_forecast_high) > 2°F
            AND market implied prob still reflects opening forecast

  Type B — Trajectory Lock (afternoon):
    Actual temp trajectory makes outcome near-certain
    e.g. peaked at 71°F and falling → K73 YES near worthless
    Market still has stale YES orders from morning
    Signal: actual_max_so_far + trajectory_ceiling < threshold
            with high confidence

OUTPUT:
  data/weather_ticks.csv    — every observation poll
  data/weather_edge.csv     — divergence events logged
  data/weather_daily.csv    — daily summary
  Telegram alerts           — when edge score > threshold

DECISION MATRIX (30 days):
  ≥10 edge events/month + avg edge > 0.20  → build execution
  5-9 edge events/month                    → promising, continue scanning
  <5 edge events/month                     → thesis weak on this contract

RUN:
  python3 weather_edge.py
  (run alongside kill_shot.py — different clientId, no conflict)

REQUIRES:
  pip install requests python-dotenv
  .env file with TELEGRAM config
  NO IB Gateway required for observation-only mode
  IB Gateway optional for live market price comparison
"""

import asyncio
import csv
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── LOGGING ───────────────────────────────────────────────────────────────────
os.makedirs("./data", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("./data/weather_edge.log", mode="a"),
    ],
)
log = logging.getLogger("weather_edge")

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")

# IBKR (optional — for live market price comparison)
IBKR_HOST      = os.getenv("IBKR_HOST",       "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT",   "4001"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID_WEATHER", "45"))  # different from kill_shot

# Scanner settings
POLL_INTERVAL_SEC   = 300    # poll every 5 minutes
EDGE_ALERT_SCORE    = 0.20   # alert when edge score >= 20%
TRAJECTORY_HOURS    = 2      # hours of falling temp to confirm peak
PEAK_CONFIDENCE_DEG = 2.0    # °F below forecast needed to flag trajectory edge

# NWS endpoints for KLAX
NWS_STATION     = "KLAX"
NWS_OBS_URL     = f"https://api.weather.gov/stations/{NWS_STATION}/observations"
NWS_LATEST_URL  = f"https://api.weather.gov/stations/{NWS_STATION}/observations/latest"
NWS_FORECAST_URL = "https://api.weather.gov/gridpoints/LOX/150,36/forecast/hourly"
NWS_HEADERS     = {"User-Agent": "forecastbot-weather-edge/1.0 (research)"}

# UHLAX contract
UHLAX_SYMBOL    = "UHLAX"

# Output files
LOG_DIR         = os.getenv("LOG_DIR", "./data")
TICKS_CSV       = os.path.join(LOG_DIR, "weather_ticks.csv")
EDGE_CSV        = os.path.join(LOG_DIR, "weather_edge.csv")
DAILY_CSV       = os.path.join(LOG_DIR, "weather_daily.csv")


# ─── DATA STRUCTURES ───────────────────────────────────────────────────────────

@dataclass
class TempObservation:
    timestamp_utc: str
    timestamp_pt:  str
    temp_f:        float
    conditions:    str
    wind_speed:    float   = 0.0
    wind_dir:      str     = ""
    humidity:      float   = 0.0

    @property
    def hour_pt(self) -> int:
        try:
            return datetime.fromisoformat(self.timestamp_utc).astimezone(PT).hour
        except Exception:
            return -1


@dataclass
class DayState:
    """Tracks everything for the current trading day."""
    date_pt:             str     = ""
    threshold:           float   = 0.0

    # Forecast tracking
    opening_forecast_high: float = 0.0   # what WU/NWS said at market open
    current_forecast_high: float = 0.0   # latest NWS forecast
    forecast_updated_at:   str   = ""

    # Actual observation tracking
    obs_history:           list  = field(default_factory=list)  # list of TempObservation
    actual_high_so_far:    float = 0.0
    actual_high_time_pt:   str   = ""
    peak_confirmed:        bool  = False   # True when temp has been falling 2h+ after peak

    # Market price tracking (from IB if available)
    market_yes_ask:        float = -1.0
    market_no_ask:         float = -1.0
    market_updated_at:     str   = ""

    # Edge events today
    edge_events:           int   = 0
    best_edge_score:       float = 0.0

    def market_implied_prob(self) -> float:
        """YES ask price = market's implied probability."""
        if self.market_yes_ask > 0:
            return self.market_yes_ask
        return -1.0

    def nws_implied_prob(self) -> float:
        """
        Estimate probability that actual high exceeds threshold
        based on current NWS forecast.
        Uses normal distribution ±3°F forecast error.
        """
        if self.current_forecast_high <= 0:
            return -1.0
        import math
        # NWS 6h forecast error for LA ≈ ±2.5°F std dev
        sigma = 2.5
        delta = self.threshold - self.current_forecast_high
        # P(actual > threshold) using normal CDF approximation
        z = delta / sigma
        # Approximation of 1 - Φ(z)
        prob = 1.0 / (1.0 + math.exp(1.7 * z))
        return round(prob, 3)

    def trajectory_implied_prob(self) -> float:
        """
        Probability based on actual trajectory so far.
        If peak is confirmed below threshold → near 0.
        If still rising toward threshold → depends on gap.
        """
        if not self.obs_history:
            return -1.0

        now_pt = datetime.now(PT)
        hour   = now_pt.hour

        # Before noon: trajectory not yet meaningful
        if hour < 11:
            return -1.0

        high = self.actual_high_so_far

        # After 5 PM PT: day's high is almost certainly locked in
        if hour >= 17:
            if high < self.threshold:
                # Already confirmed NO — near zero probability
                hours_falling = self._hours_falling_from_peak()
                if hours_falling >= 2:
                    return 0.02  # near-certain NO
                return 0.10
            else:
                return 0.98  # near-certain YES

        # 11 AM - 5 PM: use gap between actual high and threshold
        import math
        gap = self.threshold - high  # positive = threshold not reached yet
        if gap <= 0:
            return 0.95   # already exceeded threshold

        # Hours remaining until typical peak time (2 PM PT)
        hours_to_peak = max(0, 14 - hour)

        # Rate of temperature rise needed (°F per hour)
        # If we need 5°F more in 3 hours = 1.67°F/hr = plausible
        # If we need 5°F more in 0 hours = impossible
        if hours_to_peak == 0:
            return 0.05 if gap > 1 else 0.50

        rate_needed = gap / hours_to_peak
        # Typical LA temp rise rate 10AM-2PM ≈ 1-2°F/hour
        # Rate > 4°F/hour is very unlikely without Santa Ana
        prob = max(0.02, min(0.95, 1.0 - (rate_needed / 5.0)))
        return round(prob, 3)

    def _hours_falling_from_peak(self) -> float:
        """How many hours has temp been falling from today's peak?"""
        if len(self.obs_history) < 2:
            return 0.0
        # Find peak index
        peak_idx = max(range(len(self.obs_history)),
                       key=lambda i: self.obs_history[i].temp_f)
        if peak_idx == len(self.obs_history) - 1:
            return 0.0  # still at peak or not falling yet
        try:
            peak_time = datetime.fromisoformat(
                self.obs_history[peak_idx].timestamp_utc
            )
            latest_time = datetime.fromisoformat(
                self.obs_history[-1].timestamp_utc
            )
            return (latest_time - peak_time).total_seconds() / 3600
        except Exception:
            return 0.0

    def edge_score(self) -> float:
        """
        Edge score = max divergence between market prob and best estimate.
        Positive = market overpricing YES (buy NO opportunity).
        Negative = market underpricing YES (buy YES opportunity).
        """
        market = self.market_implied_prob()
        if market < 0:
            return 0.0

        # Use trajectory prob if past 11 AM and meaningful
        traj = self.trajectory_implied_prob()
        nws  = self.nws_implied_prob()

        # Take most conservative (most confident) estimate
        estimates = [p for p in [traj, nws] if p >= 0]
        if not estimates:
            return 0.0

        best_estimate = min(estimates)  # most bearish on YES
        return round(market - best_estimate, 3)

    def edge_type(self) -> str:
        traj = self.trajectory_implied_prob()
        nws  = self.nws_implied_prob()
        now_pt_hour = datetime.now(PT).hour

        if now_pt_hour >= 17 and traj <= 0.10:
            return "TRAJECTORY_LOCK"
        if now_pt_hour >= 11 and traj >= 0 and traj <= 0.30:
            return "TRAJECTORY_DIVERGE"
        if nws >= 0 and abs(self.current_forecast_high - self.opening_forecast_high) >= 2.0:
            return "FORECAST_REVISION"
        return "NORMAL"

    def max_profit_per_contract(self) -> float:
        """If buying NO: profit = 1.00 - NO_ask."""
        if self.market_no_ask > 0:
            return round(1.0 - self.market_no_ask, 4)
        if self.market_yes_ask > 0:
            return round(self.market_yes_ask, 4)  # approx: NO ≈ 1 - YES_ask
        return 0.0


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

    if not os.path.exists(TICKS_CSV):
        with open(TICKS_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp_pt", "timestamp_utc",
                "date_pt", "hour_pt",
                "temp_f", "actual_high_so_far",
                "conditions", "wind_speed", "wind_dir",
                "threshold",
                "forecast_high_opening", "forecast_high_current",
                "market_yes_ask", "market_no_ask",
                "market_implied_prob",
                "nws_implied_prob", "trajectory_implied_prob",
                "edge_score", "edge_type",
                "max_profit_per_contract",
                "hours_falling_from_peak",
            ])

    if not os.path.exists(EDGE_CSV):
        with open(EDGE_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp_pt", "date_pt", "threshold",
                "edge_type", "edge_score",
                "temp_f", "actual_high_so_far",
                "forecast_high", "market_yes_ask", "market_no_ask",
                "market_implied_prob", "nws_implied_prob",
                "trajectory_implied_prob",
                "max_profit_per_contract",
                "conditions", "wind_speed", "wind_dir",
                "hours_falling_from_peak",
            ])

    if not os.path.exists(DAILY_CSV):
        with open(DAILY_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "date_pt", "threshold",
                "opening_forecast_high", "final_forecast_high",
                "actual_high_recorded",
                "outcome",  # YES or NO vs threshold
                "edge_events_logged",
                "best_edge_score",
                "best_market_yes_ask",
                "best_max_profit_per_contract",
                "total_polls",
            ])


def log_tick(day: DayState, obs: TempObservation) -> None:
    now_pt = datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S")
    with open(TICKS_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            now_pt,
            obs.timestamp_utc,
            day.date_pt,
            obs.hour_pt,
            f"{obs.temp_f:.1f}",
            f"{day.actual_high_so_far:.1f}",
            obs.conditions,
            f"{obs.wind_speed:.1f}",
            obs.wind_dir,
            f"{day.threshold:.0f}",
            f"{day.opening_forecast_high:.1f}",
            f"{day.current_forecast_high:.1f}",
            f"{day.market_yes_ask:.4f}" if day.market_yes_ask > 0 else "",
            f"{day.market_no_ask:.4f}"  if day.market_no_ask  > 0 else "",
            f"{day.market_implied_prob():.4f}" if day.market_implied_prob() > 0 else "",
            f"{day.nws_implied_prob():.4f}"    if day.nws_implied_prob()    > 0 else "",
            f"{day.trajectory_implied_prob():.4f}" if day.trajectory_implied_prob() > 0 else "",
            f"{day.edge_score():.4f}",
            day.edge_type(),
            f"{day.max_profit_per_contract():.4f}",
            f"{day._hours_falling_from_peak():.2f}",
        ])


def log_edge_event(day: DayState, obs: TempObservation) -> None:
    now_pt = datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S")
    with open(EDGE_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            now_pt,
            day.date_pt,
            f"{day.threshold:.0f}",
            day.edge_type(),
            f"{day.edge_score():.4f}",
            f"{obs.temp_f:.1f}",
            f"{day.actual_high_so_far:.1f}",
            f"{day.current_forecast_high:.1f}",
            f"{day.market_yes_ask:.4f}" if day.market_yes_ask > 0 else "",
            f"{day.market_no_ask:.4f}"  if day.market_no_ask  > 0 else "",
            f"{day.market_implied_prob():.4f}" if day.market_implied_prob() > 0 else "",
            f"{day.nws_implied_prob():.4f}"    if day.nws_implied_prob()    > 0 else "",
            f"{day.trajectory_implied_prob():.4f}" if day.trajectory_implied_prob() > 0 else "",
            f"{day.max_profit_per_contract():.4f}",
            obs.conditions,
            f"{obs.wind_speed:.1f}",
            obs.wind_dir,
            f"{day._hours_falling_from_peak():.2f}",
        ])
    day.edge_events += 1
    if day.edge_score() > day.best_edge_score:
        day.best_edge_score = day.edge_score()


def log_daily_summary(day: DayState, actual_high: float, total_polls: int) -> None:
    outcome = "YES" if actual_high > day.threshold else "NO"
    with open(DAILY_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            day.date_pt,
            f"{day.threshold:.0f}",
            f"{day.opening_forecast_high:.1f}",
            f"{day.current_forecast_high:.1f}",
            f"{actual_high:.1f}",
            outcome,
            day.edge_events,
            f"{day.best_edge_score:.4f}",
            f"{day.market_yes_ask:.4f}" if day.market_yes_ask > 0 else "",
            f"{day.max_profit_per_contract():.4f}",
            total_polls,
        ])

    send_telegram(
        f"📊 *Weather Edge Daily Summary — {day.date_pt}*\n"
        f"Threshold: `{day.threshold:.0f}°F`\n"
        f"Opening forecast: `{day.opening_forecast_high:.1f}°F`\n"
        f"Final forecast: `{day.current_forecast_high:.1f}°F`\n"
        f"Actual high: `{actual_high:.1f}°F`\n"
        f"Outcome: `{outcome}`\n"
        f"Edge events logged: `{day.edge_events}`\n"
        f"Best edge score: `{day.best_edge_score:.4f}`\n"
        f"Polls today: `{total_polls}`"
    )


# ─── DATA FETCHING ─────────────────────────────────────────────────────────────

def fetch_latest_obs() -> Optional[TempObservation]:
    """Fetch latest KLAX observation from NWS."""
    try:
        r = requests.get(NWS_LATEST_URL, headers=NWS_HEADERS, timeout=10)
        r.raise_for_status()
        props = r.json()["properties"]

        temp_c = props.get("temperature", {}).get("value")
        if temp_c is None:
            return None
        temp_f = round(temp_c * 9 / 5 + 32, 1)

        wind_spd = props.get("windSpeed", {}).get("value") or 0.0
        # Convert m/s to mph
        wind_mph = round(wind_spd * 2.237, 1) if wind_spd else 0.0

        wind_dir_deg = props.get("windDirection", {}).get("value") or 0
        wind_dir = deg_to_compass(wind_dir_deg)

        humidity = props.get("relativeHumidity", {}).get("value") or 0.0
        desc     = props.get("textDescription", "")
        ts       = props.get("timestamp", "")
        ts_pt    = datetime.fromisoformat(ts).astimezone(PT).strftime(
                       "%Y-%m-%d %H:%M:%S") if ts else ""

        return TempObservation(
            timestamp_utc = ts,
            timestamp_pt  = ts_pt,
            temp_f        = temp_f,
            conditions    = desc,
            wind_speed    = wind_mph,
            wind_dir      = wind_dir,
            humidity      = humidity,
        )
    except Exception as e:
        log.warning(f"  NWS obs fetch failed: {e}")
        return None


def fetch_hourly_forecast() -> Optional[float]:
    """
    Fetch NWS hourly forecast for LAX grid.
    Returns predicted daily high for today.
    """
    try:
        r = requests.get(NWS_FORECAST_URL, headers=NWS_HEADERS, timeout=10)
        r.raise_for_status()
        periods = r.json()["properties"]["periods"]

        today_pt = datetime.now(PT).strftime("%Y-%m-%d")
        today_temps = []

        for p in periods:
            start = p.get("startTime", "")
            try:
                start_pt = datetime.fromisoformat(start).astimezone(PT)
                if start_pt.strftime("%Y-%m-%d") == today_pt:
                    today_temps.append(p["temperature"])
            except Exception:
                continue

        return float(max(today_temps)) if today_temps else None
    except Exception as e:
        log.warning(f"  NWS forecast fetch failed: {e}")
        return None


def fetch_uhlax_prices() -> tuple[float, float]:
    """
    Fetch UHLAX YES/NO prices from IB API.
    Returns (yes_ask, no_ask). Returns (-1, -1) if unavailable.
    Non-blocking — fails gracefully if IB not connected.
    """
    try:
        from ib_async import IB, Contract
        import asyncio

        async def _fetch():
            ib = IB()
            await ib.connectAsync(IBKR_HOST, IBKR_PORT,
                                  clientId=IBKR_CLIENT_ID, timeout=5)
            today_str = datetime.now(ET).strftime("%Y%m%d")

            c = Contract()
            c.symbol   = "UHLAX"
            c.secType  = "OPT"
            c.exchange = "FORECASTX"
            c.currency = "USD"
            c.lastTradeDateOrContractMonth = today_str

            details = await ib.reqContractDetailsAsync(c)
            if not details:
                ib.disconnect()
                return -1.0, -1.0

            # Find ATM pair (closest to 73°F default, or use threshold)
            yes_list = [d for d in details if d.contract.right == "C"]
            no_list  = {d.contract.strike: d for d in details
                        if d.contract.right == "P"}

            yes_ask = no_ask = -1.0

            for yd in sorted(yes_list, key=lambda d: d.contract.strike):
                s = yd.contract.strike
                if s in no_list:
                    yt = ib.reqMktData(yd.contract,       snapshot=False)
                    nt = ib.reqMktData(no_list[s].contract, snapshot=False)
                    await asyncio.sleep(6)
                    ya = float(yt.ask) if yt.ask and yt.ask > 0 else -1.0
                    na = float(nt.ask) if nt.ask and nt.ask > 0 else -1.0
                    if ya > 0 and na > 0:
                        yes_ask = ya
                        no_ask  = na
                        break
                    ib.cancelMktData(yd.contract)
                    ib.cancelMktData(no_list[s].contract)

            ib.disconnect()
            return yes_ask, no_ask

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_fetch())
        loop.close()
        return result

    except Exception as e:
        log.debug(f"  IB price fetch skipped: {e}")
        return -1.0, -1.0


def deg_to_compass(deg: float) -> str:
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    try:
        idx = round(float(deg) / 22.5) % 16
        return dirs[idx]
    except Exception:
        return ""


def get_todays_threshold() -> float:
    """
    Get today's UHLAX threshold from IB.
    Fallback: return 73.0 (most liquid strike from data).
    In production this should be read from the active contract.
    """
    # For now use the ATM strike based on what we've seen
    # TODO: fetch dynamically from IB contract details
    return 73.0


# ─── MAIN LOOP ─────────────────────────────────────────────────────────────────

def print_status(day: DayState, obs: TempObservation) -> None:
    now_pt = datetime.now(PT).strftime("%H:%M:%S PT")
    edge   = day.edge_score()
    etype  = day.edge_type()
    flag   = "⚡" if edge >= EDGE_ALERT_SCORE else "  "

    print(
        f"\n  {flag} [{now_pt}]  "
        f"Temp={obs.temp_f:.1f}°F  "
        f"High={day.actual_high_so_far:.1f}°F  "
        f"Threshold={day.threshold:.0f}°F"
    )
    print(
        f"     Forecast={day.current_forecast_high:.1f}°F  "
        f"Wind={obs.wind_speed:.0f}mph {obs.wind_dir}  "
        f"Conditions={obs.conditions}"
    )

    mip = day.market_implied_prob()
    nip = day.nws_implied_prob()
    tip = day.trajectory_implied_prob()

    if mip > 0:
        print(
            f"     Market YES={mip:.2f}  "
            f"NWS_prob={nip:.2f}  "
            f"Traj_prob={tip:.2f}  "
            f"Edge={edge:+.3f}  [{etype}]"
        )
        if day.max_profit_per_contract() > 0:
            print(
                f"     Max profit/contract: ${day.max_profit_per_contract():.4f}  "
                f"{'← BUY NO' if edge > 0 else ''}"
            )
    else:
        print(
            f"     NWS_prob={nip:.2f}  "
            f"Traj_prob={tip:.2f}  "
            f"Edge={edge:+.3f}  [{etype}]  "
            f"(IB prices not available)"
        )


def main():
    print("\n" + "=" * 70)
    print("  ForecastBot — Weather Edge Scanner v1.0")
    print(f"  Started: {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"  Station: KLAX (Los Angeles International Airport)")
    print(f"  Contract: UHLAX — LA Daily Temperature High")
    print(f"  Poll interval: {POLL_INTERVAL_SEC}s (every 5 minutes)")
    print(f"  Edge alert threshold: {EDGE_ALERT_SCORE:.0%}")
    print("  *** OBSERVATION ONLY — NO ORDERS ***")
    print("=" * 70 + "\n")

    init_logs()

    # ── Initialize day state ───────────────────────────────────────────────────
    now_pt    = datetime.now(PT)
    today_str = now_pt.strftime("%Y-%m-%d")

    day = DayState(
        date_pt   = today_str,
        threshold = get_todays_threshold(),
    )

    # Get opening forecast
    log.info("  Fetching opening forecast...")
    forecast_high = fetch_hourly_forecast()
    if forecast_high:
        day.opening_forecast_high = forecast_high
        day.current_forecast_high = forecast_high
        log.info(f"  Opening forecast high: {forecast_high:.1f}°F")
    else:
        log.warning("  Could not fetch opening forecast. Will retry.")

    # Try IB prices
    log.info("  Fetching initial UHLAX market prices (optional)...")
    yes_ask, no_ask = fetch_uhlax_prices()
    if yes_ask > 0:
        day.market_yes_ask = yes_ask
        day.market_no_ask  = no_ask
        log.info(f"  UHLAX YES ask={yes_ask:.4f}  NO ask={no_ask:.4f}")
    else:
        log.info("  IB prices not available — running in forecast-only mode")

    send_telegram(
        f"🌤 *Weather Edge Scanner Started*\n"
        f"Date: `{today_str}`\n"
        f"Threshold: `{day.threshold:.0f}°F`\n"
        f"Opening forecast: `{day.opening_forecast_high:.1f}°F`\n"
        f"Market YES ask: `{yes_ask:.4f}`\n"
        f"Poll interval: every 5 minutes\n"
        f"Time: `{datetime.now(ET).strftime('%H:%M:%S ET')}`"
    )

    # ── Main poll loop ─────────────────────────────────────────────────────────
    total_polls  = 0
    last_date    = today_str
    last_edge_alert_time: float = 0
    ALERT_COOLDOWN = 1800  # 30 min between alerts for same edge type

    log.info("\n  Polling... (Ctrl+C to stop)\n")

    try:
        while True:
            now_pt    = datetime.now(PT)
            today_str = now_pt.strftime("%Y-%m-%d")

            # ── Daily rollover ─────────────────────────────────────────────────
            if today_str != last_date:
                log.info(f"\n  Day rollover: {last_date} → {today_str}")

                # Log yesterday's summary
                log_daily_summary(day, day.actual_high_so_far, total_polls)

                # Reset for new day
                day = DayState(
                    date_pt   = today_str,
                    threshold = get_todays_threshold(),
                )
                forecast_high = fetch_hourly_forecast()
                if forecast_high:
                    day.opening_forecast_high = forecast_high
                    day.current_forecast_high = forecast_high

                total_polls = 0
                last_date   = today_str

            # ── Fetch observation ──────────────────────────────────────────────
            obs = fetch_latest_obs()
            if obs is None:
                log.warning("  Observation fetch failed, retrying in 60s...")
                time.sleep(60)
                continue

            total_polls += 1

            # Update day's high
            if obs.temp_f > day.actual_high_so_far:
                day.actual_high_so_far = obs.temp_f
                day.actual_high_time_pt = obs.timestamp_pt

            # Add to history
            day.obs_history.append(obs)

            # ── Update forecast (every 30 min) ─────────────────────────────────
            if total_polls % 6 == 0:  # every 6 polls = 30 min
                new_forecast = fetch_hourly_forecast()
                if new_forecast:
                    if abs(new_forecast - day.current_forecast_high) >= 1.0:
                        log.info(
                            f"  Forecast updated: "
                            f"{day.current_forecast_high:.1f}°F → {new_forecast:.1f}°F"
                        )
                    day.current_forecast_high = new_forecast
                    day.forecast_updated_at   = obs.timestamp_pt

            # ── Update market prices (every 15 min) ────────────────────────────
            if total_polls % 3 == 0:  # every 3 polls = 15 min
                yes_ask, no_ask = fetch_uhlax_prices()
                if yes_ask > 0:
                    day.market_yes_ask = yes_ask
                    day.market_no_ask  = no_ask
                    day.market_updated_at = obs.timestamp_pt

            # ── Log tick ───────────────────────────────────────────────────────
            log_tick(day, obs)

            # ── Print status ───────────────────────────────────────────────────
            print_status(day, obs)

            # ── Check for edge ─────────────────────────────────────────────────
            edge  = day.edge_score()
            etype = day.edge_type()

            if edge >= EDGE_ALERT_SCORE:
                log_edge_event(day, obs)

                now_ts = time.time()
                if now_ts - last_edge_alert_time > ALERT_COOLDOWN:
                    last_edge_alert_time = now_ts
                    mip = day.market_implied_prob()
                    nip = day.nws_implied_prob()
                    tip = day.trajectory_implied_prob()
                    mp  = day.max_profit_per_contract()

                    print(
                        f"\n  ⚡ EDGE DETECTED [{etype}]\n"
                        f"     Edge score:  {edge:+.3f} (market overpricing YES)\n"
                        f"     Market YES:  {mip:.2f}\n"
                        f"     NWS prob:    {nip:.2f}\n"
                        f"     Traj prob:   {tip:.2f}\n"
                        f"     Forecast:    {day.current_forecast_high:.1f}°F\n"
                        f"     Actual high: {day.actual_high_so_far:.1f}°F\n"
                        f"     Threshold:   {day.threshold:.0f}°F\n"
                        f"     Profit/contract if buying NO: ${mp:.4f}\n"
                        f"     Wind: {obs.wind_speed:.0f}mph {obs.wind_dir}\n"
                        f"     Conditions: {obs.conditions}"
                    )

                    send_telegram(
                        f"⚡ *WEATHER EDGE — {etype}*\n"
                        f"Date: `{today_str}`\n"
                        f"Edge score: `{edge:+.3f}`\n"
                        f"Market YES ask: `{mip:.2f}` (implied {mip*100:.0f}%)\n"
                        f"NWS implied prob: `{nip:.2f}`\n"
                        f"Trajectory prob: `{tip:.2f}`\n"
                        f"Current temp: `{obs.temp_f:.1f}°F`\n"
                        f"Today's high so far: `{day.actual_high_so_far:.1f}°F`\n"
                        f"Threshold: `{day.threshold:.0f}°F`\n"
                        f"Forecast: `{day.current_forecast_high:.1f}°F`\n"
                        f"Profit/contract (buy NO): `${mp:.4f}`\n"
                        f"Wind: `{obs.wind_speed:.0f}mph {obs.wind_dir}`\n"
                        f"Conditions: `{obs.conditions}`\n"
                        f"Time: `{now_pt.strftime('%H:%M PT')}`"
                    )

            # ── Hourly summary to console ──────────────────────────────────────
            if now_pt.minute == 0:
                log.info(
                    f"  [HOURLY] {now_pt.strftime('%H:%M PT')}  "
                    f"Temp={obs.temp_f:.1f}°F  High={day.actual_high_so_far:.1f}°F  "
                    f"Forecast={day.current_forecast_high:.1f}°F  "
                    f"Threshold={day.threshold:.0f}°F  "
                    f"Edge={edge:+.3f}  Polls={total_polls}"
                )

            time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        log.info("\n  Stopped by user.")

    except Exception as e:
        log.critical(f"\n  FATAL: {e}\n{traceback.format_exc()}")
        send_telegram(f"🚨 *Weather Edge CRASHED*\n`{str(e)[:200]}`")

    finally:
        log_daily_summary(day, day.actual_high_so_far, total_polls)

        print("\n" + "=" * 70)
        print("  WEATHER EDGE FINAL SUMMARY")
        print("=" * 70)
        print(f"  Date:              {day.date_pt}")
        print(f"  Threshold:         {day.threshold:.0f}°F")
        print(f"  Opening forecast:  {day.opening_forecast_high:.1f}°F")
        print(f"  Final forecast:    {day.current_forecast_high:.1f}°F")
        print(f"  Actual high:       {day.actual_high_so_far:.1f}°F")
        print(f"  Edge events:       {day.edge_events}")
        print(f"  Best edge score:   {day.best_edge_score:.4f}")
        print(f"  Total polls:       {total_polls}")
        print()
        print(f"  Ticks log:    {TICKS_CSV}")
        print(f"  Edge events:  {EDGE_CSV}")
        print(f"  Daily log:    {DAILY_CSV}")
        print()


if __name__ == "__main__":
    main()
