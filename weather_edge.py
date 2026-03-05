"""
weather_edge.py — ForecastBot Weather Edge Scanner  v3.0
==========================================================
THESIS:
  Retail opens app, sees WU forecast, bets, walks away.
  They do NOT watch intraday trajectory or wind shifts.
  When actual temp diverges from forecast we catch that window.

  Edge is BIDIRECTIONAL:
    Market overprices YES  → BUY NO
    Market underprices YES → BUY YES  (e.g. Santa Ana spike incoming)

ARCHITECTURE:
  Fully async — single event loop, no threading.
  IB stays connected the whole session, tick data flows naturally.
  NWS polls run in executor (non-blocking).
  5-minute poll cycle reads cached IB ticker values instantly.

FIXES FROM v2:
  - Full async main() — IB event loop never blocked
  - No threading — was preventing tick callbacks from firing
  - Depth filter: skip strikes < MIN_DEPTH contracts on either leg
  - Safer ticker reads with hasattr + None guards
  - Bidirectional BUY_NO / BUY_YES alerts
  - Santa Ana filter: suppresses NWS signal when wind > 25mph offshore

RUN:
  python3 weather_edge.py
  Runs alongside kill_shot.py (uses clientId=45, different from kill_shot=40)

REQUIRES:
  pip install ib_async requests python-dotenv
"""

import asyncio
import csv
import logging
import math
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── LOGGING ───────────────────────────────────────────────────────────────────
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

# ─── CONFIG ────────────────────────────────────────────────────────────────────
IBKR_HOST      = os.getenv("IBKR_HOST",                  "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT",              "4001"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID_WEATHER", "45"))

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")

POLL_INTERVAL_SEC  = 300    # every 5 minutes
EDGE_ALERT_SCORE   = 0.20   # alert when |edge| >= this
ALERT_COOLDOWN_SEC = 1800   # 30 min between same-direction alerts
IB_WARMUP_SEC      = 20     # seconds after subscribe before reading prices
MIN_DEPTH          = 50     # skip strikes with fewer contracts on either leg

# Santa Ana: hot dry offshore wind — NWS dramatically underforecasts when active
SANTA_ANA_MPH  = 25
SANTA_ANA_DIRS = {"N", "NNW", "NNE", "NE", "ENE"}

# NWS — free, no API key, KLAX = LAX airport station
NWS_HEADERS      = {"User-Agent": "forecastbot/1.0"}
NWS_LATEST_URL   = "https://api.weather.gov/stations/KLAX/observations/latest"
NWS_FORECAST_URL = "https://api.weather.gov/gridpoints/LOX/150,36/forecast/hourly"

TICKS_CSV = os.path.join(LOG_DIR, "weather_ticks.csv")
EDGE_CSV  = os.path.join(LOG_DIR, "weather_edge.csv")
DAILY_CSV = os.path.join(LOG_DIR, "weather_daily.csv")


# ─── HELPERS ───────────────────────────────────────────────────────────────────

def deg_to_compass(deg) -> str:
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    try:
        return dirs[round(float(deg) / 22.5) % 16]
    except Exception:
        return ""


def prob_exceed(forecast_f: float, threshold: float, sigma: float = 2.5) -> float:
    """P(actual > threshold) given NWS forecast using logistic approximation."""
    z = (threshold - forecast_f) / sigma
    return round(1.0 / (1.0 + math.exp(1.7 * z)), 3)


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
        log.warning(f"Telegram: {e}")


# ─── DATA STRUCTURES ───────────────────────────────────────────────────────────

@dataclass
class Obs:
    ts_utc:     str
    ts_pt:      str
    temp_f:     float
    conditions: str
    wind_mph:   float
    wind_dir:   str
    humidity:   float

    @property
    def santa_ana(self) -> bool:
        return self.wind_mph > SANTA_ANA_MPH and self.wind_dir in SANTA_ANA_DIRS

    @property
    def hour_pt(self) -> int:
        try:
            return datetime.fromisoformat(self.ts_utc).astimezone(PT).hour
        except Exception:
            return -1


@dataclass
class StrikeEdge:
    strike:      float
    yes_ask:     float
    no_ask:      float
    yes_depth:   int
    no_depth:    int
    market_prob: float
    nws_prob:    float
    traj_prob:   float  # -1 if not yet meaningful
    eff_prob:    float  # estimate used for edge calc
    edge_score:  float  # positive = buy NO, negative = buy YES
    direction:   str    # BUY_NO / BUY_YES / NONE
    max_profit:  float  # per contract


@dataclass
class DayState:
    date_pt:          str   = ""
    threshold:        float = 73.0
    opening_forecast: float = 0.0
    current_forecast: float = 0.0
    obs_history:      list  = field(default_factory=list)
    actual_high:      float = 0.0
    total_polls:      int   = 0
    edge_events:      int   = 0
    best_edge:        float = 0.0
    best_direction:   str   = ""

    def hours_falling(self) -> float:
        if len(self.obs_history) < 2:
            return 0.0
        peak_i = max(range(len(self.obs_history)),
                     key=lambda i: self.obs_history[i].temp_f)
        if peak_i == len(self.obs_history) - 1:
            return 0.0
        try:
            t0 = datetime.fromisoformat(self.obs_history[peak_i].ts_utc)
            t1 = datetime.fromisoformat(self.obs_history[-1].ts_utc)
            return (t1 - t0).total_seconds() / 3600
        except Exception:
            return 0.0

    def trajectory_prob(self, threshold: float) -> float:
        """Probability actual high > threshold based on observed trajectory."""
        if not self.obs_history:
            return -1.0
        hour = datetime.now(PT).hour
        if hour < 11:
            return -1.0

        high = self.actual_high

        if hour >= 17:
            if high > threshold:
                return 0.98
            if self.hours_falling() >= 2.0:
                return 0.02
            return 0.10

        if high > threshold:
            return 0.97

        gap           = threshold - high
        hours_to_peak = max(0.5, 14 - hour)
        rate_needed   = gap / hours_to_peak
        prob = max(0.03, min(0.95, 1.0 - (rate_needed / 4.5)))
        return round(prob, 3)


# ─── IB PRICE FEED ─────────────────────────────────────────────────────────────

class IBPriceFeed:
    """
    Async IB connection. Subscribes to all UHLAX YES+NO pairs at startup.
    Prices stream continuously — reads are instant cache lookups.
    Must be used within an already-running asyncio event loop.
    """

    def __init__(self):
        self.ib            = None
        self.pairs         = {}    # strike → (yes_ticker, no_ticker)
        self.connected     = False
        self.contract_date = ""    # YYYYMMDD of actively-trading contracts

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

            # UHLAX contracts are daily — today's contracts may have stopped
            # quoting if the temperature outcome is already determined.
            # Try today first, then tomorrow, to find actively-trading contracts.
            from datetime import timedelta

            for day_offset in range(0, 3):
                try_date = datetime.now(ET) + timedelta(days=day_offset)
                try_str = try_date.strftime("%Y%m%d")
                c = Contract()
                c.symbol   = "UHLAX"
                c.secType  = "OPT"
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
                no_map  = {d.contract.strike: d.contract
                           for d in details if d.contract.right == "P"}
                common  = sorted(set(yes_map) & set(no_map))

                if not common:
                    log.info(f"  UHLAX: no YES+NO pairs for {try_str}, trying next day...")
                    continue

                # Subscribe to market data
                self.pairs = {}
                for s in common:
                    yt = self.ib.reqMktData(yes_map[s], snapshot=False)
                    nt = self.ib.reqMktData(no_map[s],  snapshot=False)
                    self.pairs[s] = (yt, nt)

                log.info(f"  Subscribed {len(common)} UHLAX strikes (exp={try_str}). "
                         f"Warming up {IB_WARMUP_SEC}s...")
                await asyncio.sleep(IB_WARMUP_SEC)

                # Print initial price ladder and count live strikes
                log.info(f"\n  {'K':>6}  {'YES_ASK':>8}  {'NO_ASK':>8}  "
                         f"{'SUM':>8}  {'GAP':>8}  {'Y_DEPTH':>8}  {'N_DEPTH':>8}")
                log.info(f"  {'─'*66}")
                live_count = 0
                for s in common:
                    ya, na, yd, nd = self._read(s)
                    if ya > 0 and na > 0:
                        live_count += 1
                        log.info(f"  {s:>6.0f}  {ya:>8.4f}  {na:>8.4f}  "
                                 f"{ya+na:>8.4f}  {1-(ya+na):>+8.4f}  "
                                 f"{yd:>8}  {nd:>8}")
                    else:
                        log.info(f"  {s:>6.0f}  {'n/a':>8}  {'n/a':>8}")
                log.info(f"\n  Live prices on {live_count}/{len(common)} strikes")

                if live_count > 0:
                    self.contract_date = try_str
                    self.connected = True
                    return True

                # No live prices — cancel subscriptions and try next day
                log.warning(f"  No live prices for {try_str} — trying next day...")
                for yt, nt in self.pairs.values():
                    self.ib.cancelMktData(yt)
                    self.ib.cancelMktData(nt)
                self.pairs = {}

            log.warning("  UHLAX: no actively-trading contracts found in next 3 days")
            return False

        except Exception as e:
            log.warning(f"  IB start failed: {e}")
            return False

    def _read(self, strike: float) -> tuple[float, float, int, int]:
        """Returns (yes_ask, no_ask, yes_depth, no_depth)."""
        if strike not in self.pairs:
            return -1.0, -1.0, 0, 0
        yt, nt = self.pairs[strike]
        ya = float(yt.ask)    if hasattr(yt, 'ask')     and yt.ask     is not None and yt.ask     > 0 else -1.0
        na = float(nt.ask)    if hasattr(nt, 'ask')     and nt.ask     is not None and nt.ask     > 0 else -1.0
        yd = int(yt.askSize)  if hasattr(yt, 'askSize') and yt.askSize is not None else 0
        nd = int(nt.askSize)  if hasattr(nt, 'askSize') and nt.askSize is not None else 0
        return ya, na, yd, nd

    def read_all(self) -> dict:
        """
        Returns {strike: (yes_ask, no_ask, yes_depth, no_depth)}
        Only strikes with valid prices AND depth >= MIN_DEPTH on both legs.
        """
        result = {}
        for s in self.pairs:
            ya, na, yd, nd = self._read(s)
            if ya > 0 and na > 0 and yd >= MIN_DEPTH and nd >= MIN_DEPTH:
                result[s] = (ya, na, yd, nd)
        return result

    def read_all_raw(self) -> dict:
        """Same but no depth filter — for display/logging."""
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


# ─── NWS DATA (blocking — run in executor) ─────────────────────────────────────

def fetch_obs() -> Optional[Obs]:
    try:
        r = requests.get(NWS_LATEST_URL, headers=NWS_HEADERS, timeout=10)
        r.raise_for_status()
        p = r.json()["properties"]

        temp_c = p.get("temperature", {}).get("value")
        if temp_c is None:
            return None

        wind_ms  = p.get("windSpeed",        {}).get("value") or 0.0
        wind_deg = p.get("windDirection",    {}).get("value") or 0.0
        hum      = p.get("relativeHumidity", {}).get("value") or 0.0
        ts       = p.get("timestamp", "")
        desc     = p.get("textDescription", "")
        ts_pt    = (datetime.fromisoformat(ts).astimezone(PT)
                    .strftime("%Y-%m-%d %H:%M:%S") if ts else "")

        return Obs(
            ts_utc     = ts,
            ts_pt      = ts_pt,
            temp_f     = round(temp_c * 9/5 + 32, 1),
            conditions = desc,
            wind_mph   = round(wind_ms * 2.237, 1),
            wind_dir   = deg_to_compass(wind_deg),
            humidity   = round(hum, 1),
        )
    except Exception as e:
        log.warning(f"  NWS obs: {e}")
        return None


def fetch_forecast_high() -> Optional[float]:
    try:
        r = requests.get(NWS_FORECAST_URL, headers=NWS_HEADERS, timeout=10)
        r.raise_for_status()
        today_pt = datetime.now(PT).strftime("%Y-%m-%d")
        temps = []
        for p in r.json()["properties"]["periods"]:
            try:
                dt = datetime.fromisoformat(p["startTime"]).astimezone(PT)
                if dt.strftime("%Y-%m-%d") == today_pt:
                    temps.append(p["temperature"])
            except Exception:
                continue
        return float(max(temps)) if temps else None
    except Exception as e:
        log.warning(f"  NWS forecast: {e}")
        return None


# ─── EDGE CALCULATION ──────────────────────────────────────────────────────────

def compute_edges(prices: dict, day: DayState, obs: Obs) -> list[StrikeEdge]:
    results = []
    for strike, (ya, na, yd, nd) in prices.items():
        market_prob = ya
        nws_prob    = prob_exceed(day.current_forecast, strike)
        traj_prob   = day.trajectory_prob(strike)

        # Santa Ana active → NWS underforecasts, use trajectory only
        if obs.santa_ana:
            estimates = [traj_prob] if traj_prob >= 0 else []
        else:
            estimates = [p for p in [nws_prob, traj_prob] if p >= 0]

        if not estimates:
            results.append(StrikeEdge(
                strike=strike, yes_ask=ya, no_ask=na,
                yes_depth=yd, no_depth=nd,
                market_prob=market_prob, nws_prob=nws_prob,
                traj_prob=traj_prob, eff_prob=market_prob,
                edge_score=0.0, direction="NONE", max_profit=0.0,
            ))
            continue

        low  = min(estimates)
        high = max(estimates)
        edge_no  = round(market_prob - low,  3)   # positive → buy NO
        edge_yes = round(market_prob - high, 3)   # negative → buy YES

        if abs(edge_no) >= abs(edge_yes):
            eff, score = low, edge_no
        else:
            eff, score = high, edge_yes

        if score >= EDGE_ALERT_SCORE:
            direction  = "BUY_NO"
            max_profit = round(1.0 - na, 4)
        elif score <= -EDGE_ALERT_SCORE:
            direction  = "BUY_YES"
            max_profit = round(1.0 - ya, 4)
        else:
            direction  = "NONE"
            max_profit = 0.0

        results.append(StrikeEdge(
            strike=strike, yes_ask=ya, no_ask=na,
            yes_depth=yd, no_depth=nd,
            market_prob=market_prob, nws_prob=nws_prob,
            traj_prob=traj_prob, eff_prob=eff,
            edge_score=score, direction=direction,
            max_profit=max_profit,
        ))

    return sorted(results, key=lambda e: e.strike)


def get_edge_type(day: DayState, obs: Obs, se: StrikeEdge) -> str:
    hour = datetime.now(PT).hour
    if obs.santa_ana:
        return "SANTA_ANA"
    if hour >= 17 and se.traj_prob <= 0.10:
        return "TRAJECTORY_LOCK"
    if hour >= 11 and 0 <= se.traj_prob <= 0.30:
        return "TRAJECTORY_DIVERGE"
    if hour >= 11 and se.traj_prob >= 0.70:
        return "TRAJECTORY_BULLISH"
    if abs(day.current_forecast - day.opening_forecast) >= 2.0:
        return "FORECAST_REVISION"
    return "NORMAL"


# ─── CSV LOGGING ───────────────────────────────────────────────────────────────

def init_logs():
    if not os.path.exists(TICKS_CSV):
        with open(TICKS_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp_pt", "ts_utc", "date_pt", "hour_pt",
                "temp_f", "actual_high", "conditions",
                "wind_mph", "wind_dir", "santa_ana",
                "threshold", "forecast_opening", "forecast_current",
                "yes_ask", "no_ask", "yes_depth", "no_depth",
                "market_prob", "nws_prob", "traj_prob", "eff_prob",
                "edge_score", "edge_type", "direction", "max_profit",
                "hours_falling",
            ])
    if not os.path.exists(EDGE_CSV):
        with open(EDGE_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp_pt", "date_pt", "strike", "edge_type",
                "direction", "edge_score", "santa_ana",
                "temp_f", "actual_high", "forecast_current",
                "yes_ask", "no_ask", "yes_depth", "no_depth",
                "market_prob", "nws_prob", "traj_prob", "max_profit",
                "wind_mph", "wind_dir", "conditions", "hours_falling",
            ])
    if not os.path.exists(DAILY_CSV):
        with open(DAILY_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "date_pt", "threshold", "forecast_opening",
                "forecast_final", "actual_high", "outcome",
                "edge_events", "best_edge", "best_direction", "total_polls",
            ])


def write_tick(day: DayState, obs: Obs, se: StrikeEdge, etype: str):
    now_pt = datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S")
    with open(TICKS_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            now_pt, obs.ts_utc, day.date_pt, obs.hour_pt,
            obs.temp_f, day.actual_high, obs.conditions,
            obs.wind_mph, obs.wind_dir,
            "1" if obs.santa_ana else "0",
            day.threshold, day.opening_forecast, day.current_forecast,
            se.yes_ask, se.no_ask, se.yes_depth, se.no_depth,
            se.market_prob, se.nws_prob,
            f"{se.traj_prob:.4f}" if se.traj_prob >= 0 else "",
            se.eff_prob, f"{se.edge_score:+.4f}",
            etype, se.direction, se.max_profit,
            f"{day.hours_falling():.2f}",
        ])


def write_edge(day: DayState, obs: Obs, se: StrikeEdge, etype: str):
    now_pt = datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S")
    with open(EDGE_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            now_pt, day.date_pt, se.strike, etype,
            se.direction, f"{se.edge_score:+.4f}",
            "1" if obs.santa_ana else "0",
            obs.temp_f, day.actual_high, day.current_forecast,
            se.yes_ask, se.no_ask, se.yes_depth, se.no_depth,
            se.market_prob, se.nws_prob,
            f"{se.traj_prob:.4f}" if se.traj_prob >= 0 else "",
            se.max_profit, obs.wind_mph, obs.wind_dir,
            obs.conditions, f"{day.hours_falling():.2f}",
        ])
    day.edge_events += 1
    if abs(se.edge_score) > day.best_edge:
        day.best_edge      = abs(se.edge_score)
        day.best_direction = se.direction


def write_daily(day: DayState):
    outcome = "YES" if day.actual_high > day.threshold else "NO"
    with open(DAILY_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            day.date_pt, day.threshold,
            day.opening_forecast, day.current_forecast,
            day.actual_high, outcome,
            day.edge_events, day.best_edge,
            day.best_direction, day.total_polls,
        ])
    send_telegram(
        f"📊 *Weather Edge Daily — {day.date_pt}*\n"
        f"Threshold: `{day.threshold:.0f}°F`\n"
        f"Opening forecast: `{day.opening_forecast:.1f}°F`\n"
        f"Actual high: `{day.actual_high:.1f}°F`\n"
        f"Outcome: `{outcome}`\n"
        f"Edge events: `{day.edge_events}`\n"
        f"Best edge: `{day.best_edge:.4f}` ({day.best_direction})\n"
        f"Polls: `{day.total_polls}`"
    )


# ─── CONSOLE OUTPUT ────────────────────────────────────────────────────────────

def print_poll(day: DayState, obs: Obs, edges: list[StrikeEdge]):
    now_str = datetime.now(PT).strftime("%H:%M:%S PT")
    sa_flag = "  🌬 SANTA ANA — NWS suppressed" if obs.santa_ana else ""

    print(f"\n  ── {now_str}{sa_flag}")
    print(f"  Temp={obs.temp_f:.1f}°F  High={day.actual_high:.1f}°F  "
          f"Threshold={day.threshold:.0f}°F  Forecast={day.current_forecast:.1f}°F")
    print(f"  Wind={obs.wind_mph:.0f}mph {obs.wind_dir}  "
          f"Cond={obs.conditions}  Falling={day.hours_falling():.1f}h")

    if not edges:
        print("  [IB prices not available — forecast-only mode]")
        nws  = prob_exceed(day.current_forecast, day.threshold)
        traj = day.trajectory_prob(day.threshold)
        traj_str = f"{traj:.3f}" if traj >= 0 else "n/a"
        print(f"  K{day.threshold:.0f}  NWS_prob={nws:.3f}  Traj_prob={traj_str}")
        return

    print(f"\n  {'K':>6}  {'YES':>6}  {'NO':>6}  "
          f"{'MKT%':>6}  {'NWS%':>6}  {'TRAJ%':>6}  "
          f"{'EDGE':>7}  {'YD':>6}  {'ND':>6}  {'SIGNAL':>10}")
    print(f"  {'─'*80}")

    for se in edges:
        flag     = "⚡" if se.direction != "NONE" else " "
        traj_str = f"{se.traj_prob:.3f}" if se.traj_prob >= 0 else "  n/a"
        print(
            f"  {flag}{se.strike:>5.0f}  "
            f"{se.yes_ask:>6.3f}  {se.no_ask:>6.3f}  "
            f"{se.market_prob:>6.3f}  {se.nws_prob:>6.3f}  {traj_str:>6}  "
            f"{se.edge_score:>+7.3f}  {se.yes_depth:>6}  {se.no_depth:>6}  "
            f"{se.direction:>10}"
        )

    best = max(edges, key=lambda e: abs(e.edge_score))
    if best.direction != "NONE":
        action = (
            f"BUY NO  @ ${best.no_ask:.3f}  profit=${best.max_profit:.3f}/contract"
            if best.direction == "BUY_NO" else
            f"BUY YES @ ${best.yes_ask:.3f}  profit=${best.max_profit:.3f}/contract"
        )
        print(f"\n  ⚡ BEST: K{best.strike:.0f}  {action}")


def print_alert(day: DayState, obs: Obs, se: StrikeEdge, etype: str):
    icon   = "📉" if se.direction == "BUY_NO" else "📈"
    action = (
        f"BUY NO  @ ${se.no_ask:.3f} — pays $1.00 if temp ≤ {se.strike:.0f}°F"
        if se.direction == "BUY_NO" else
        f"BUY YES @ ${se.yes_ask:.3f} — pays $1.00 if temp > {se.strike:.0f}°F"
    )
    traj_str = f"{se.traj_prob:.3f}" if se.traj_prob >= 0 else "n/a"

    print(f"\n  {'━'*60}")
    print(f"  {icon} {se.direction}  K{se.strike:.0f}  [{etype}]")
    print(f"  {'━'*60}")
    print(f"  Edge score:      {se.edge_score:+.4f}")
    print(f"  Market YES:      {se.market_prob:.3f}  ({se.market_prob*100:.0f}%)")
    print(f"  NWS prob:        {se.nws_prob:.3f}")
    print(f"  Trajectory prob: {traj_str}")
    print(f"  Depth YES/NO:    {se.yes_depth} / {se.no_depth}")
    print(f"  Profit/contract: ${se.max_profit:.4f}")
    print(f"  Action:          {action}")
    if obs.santa_ana:
        print(f"  ⚠ Santa Ana ({obs.wind_mph:.0f}mph {obs.wind_dir}) — NWS suppressed")
    print(f"  {'━'*60}\n")

    send_telegram(
        f"{icon} *{se.direction} — K{se.strike:.0f}* [{etype}]\n"
        f"Edge: `{se.edge_score:+.4f}`\n"
        f"Market YES: `{se.market_prob:.3f}` ({se.market_prob*100:.0f}%)\n"
        f"NWS prob: `{se.nws_prob:.3f}`\n"
        f"Traj prob: `{traj_str}`\n"
        f"Depth: `{se.yes_depth}Y / {se.no_depth}N`\n"
        f"Action: `{action}`\n"
        f"Temp: `{obs.temp_f:.1f}°F`  High: `{day.actual_high:.1f}°F`\n"
        f"Wind: `{obs.wind_mph:.0f}mph {obs.wind_dir}`\n"
        f"Time: `{datetime.now(PT).strftime('%H:%M PT')}`"
    )


# ─── MAIN (fully async) ────────────────────────────────────────────────────────

async def main():
    print("\n" + "=" * 65)
    print("  ForecastBot — Weather Edge Scanner v3.0")
    print(f"  Started: {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"  Station: KLAX  |  Contract: UHLAX")
    print(f"  Poll: {POLL_INTERVAL_SEC}s  |  Alert: ±{EDGE_ALERT_SCORE:.0%}  |  "
          f"Cooldown: {ALERT_COOLDOWN_SEC//60}min")
    print(f"  Santa Ana filter: >{SANTA_ANA_MPH}mph {SANTA_ANA_DIRS}")
    print(f"  Min depth filter: {MIN_DEPTH} contracts per leg")
    print("  *** OBSERVATION ONLY — NO ORDERS ***")
    print("=" * 65 + "\n")

    init_logs()
    loop = asyncio.get_event_loop()

    # ── IB connection — async, stays alive for full session ───────────────────
    ib_feed = IBPriceFeed()
    ib_connected = await ib_feed.start()
    if ib_connected:
        log.info(f"  ✓ IB price feed active — {len(ib_feed.pairs)} strikes")
    else:
        log.info("  IB unavailable — forecast-only mode")

    # ── Initialize day ────────────────────────────────────────────────────────
    today_str = datetime.now(PT).strftime("%Y-%m-%d")
    day       = DayState(date_pt=today_str, threshold=73.0)

    log.info("  Fetching opening NWS forecast...")
    fh = await loop.run_in_executor(None, fetch_forecast_high)
    if fh:
        day.opening_forecast = fh
        day.current_forecast = fh
        log.info(f"  Opening NWS forecast high: {fh:.1f}°F")
    else:
        log.warning("  Forecast unavailable — will retry each poll")

    send_telegram(
        f"🌤 *Weather Edge v3.0 Started*\n"
        f"Date: `{today_str}`  Threshold: `{day.threshold:.0f}°F`\n"
        f"Opening NWS forecast: `{day.opening_forecast:.1f}°F`\n"
        f"IB feed: `{'active — ' + str(len(ib_feed.pairs)) + ' strikes' if ib_connected else 'unavailable'}`\n"
        f"Alert: `±{EDGE_ALERT_SCORE:.0%}`"
    )

    log.info("\n  Polling... (Ctrl+C to stop)\n")

    last_date     = today_str
    last_alert_ts = {"BUY_NO": 0.0, "BUY_YES": 0.0}
    poll_n        = 0

    try:
        while True:
            now_pt    = datetime.now(PT)
            today_str = now_pt.strftime("%Y-%m-%d")

            # ── Daily rollover ─────────────────────────────────────────────────
            if today_str != last_date:
                log.info(f"  Day rollover → {today_str}")
                write_daily(day)
                day       = DayState(date_pt=today_str, threshold=73.0)
                poll_n    = 0
                last_date = today_str
                fh = await loop.run_in_executor(None, fetch_forecast_high)
                if fh:
                    day.opening_forecast = fh
                    day.current_forecast = fh

            # ── Observation ───────────────────────────────────────────────────
            obs = await loop.run_in_executor(None, fetch_obs)
            if obs is None:
                log.warning("  Obs failed — retrying in 60s")
                await asyncio.sleep(60)
                continue

            poll_n          += 1
            day.total_polls += 1
            day.obs_history.append(obs)
            if obs.temp_f > day.actual_high:
                day.actual_high = obs.temp_f

            # ── Refresh forecast every 30 min ──────────────────────────────────
            if poll_n % 6 == 0:
                fh = await loop.run_in_executor(None, fetch_forecast_high)
                if fh:
                    if abs(fh - day.current_forecast) >= 1.0:
                        log.info(f"  Forecast revised: "
                                 f"{day.current_forecast:.1f}→{fh:.1f}°F")
                    day.current_forecast = fh

            # ── Read IB prices — instant, already streaming ────────────────────
            prices = ib_feed.read_all() if ib_feed.connected else {}
            edges  = compute_edges(prices, day, obs)

            # ── Log tick for threshold strike ──────────────────────────────────
            thr_edge = next(
                (e for e in edges if e.strike == day.threshold),
                edges[0] if edges else None,
            )
            if thr_edge:
                etype = get_edge_type(day, obs, thr_edge)
                write_tick(day, obs, thr_edge, etype)

            # ── Print status table ─────────────────────────────────────────────
            print_poll(day, obs, edges)

            # ── Check for alerts ───────────────────────────────────────────────
            alertable = [e for e in edges if e.direction != "NONE"]
            if alertable:
                best  = max(alertable, key=lambda e: abs(e.edge_score))
                etype = get_edge_type(day, obs, best)
                write_edge(day, obs, best, etype)

                now_ts = time.time()
                if now_ts - last_alert_ts.get(best.direction, 0) > ALERT_COOLDOWN_SEC:
                    last_alert_ts[best.direction] = now_ts
                    print_alert(day, obs, best, etype)

            await asyncio.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        log.info("\n  Stopped by user.")
    except Exception as e:
        log.critical(f"\n  FATAL: {e}\n{traceback.format_exc()}")
        send_telegram(f"🚨 *Weather Edge CRASHED*\n`{str(e)[:200]}`")
    finally:
        write_daily(day)
        ib_feed.stop()
        outcome = "YES" if day.actual_high > day.threshold else "NO"
        print("\n" + "=" * 65)
        print("  SESSION SUMMARY")
        print("=" * 65)
        print(f"  Date:             {day.date_pt}")
        print(f"  Threshold:        {day.threshold:.0f}°F")
        print(f"  Opening forecast: {day.opening_forecast:.1f}°F")
        print(f"  Final forecast:   {day.current_forecast:.1f}°F")
        print(f"  Actual high:      {day.actual_high:.1f}°F")
        print(f"  Outcome:          {outcome}")
        print(f"  Edge events:      {day.edge_events}")
        print(f"  Best edge:        {day.best_edge:.4f} ({day.best_direction})")
        print(f"  Total polls:      {day.total_polls}")
        print(f"\n  Ticks: {TICKS_CSV}")
        print(f"  Edges: {EDGE_CSV}")
        print(f"  Daily: {DAILY_CSV}\n")


if __name__ == "__main__":
    asyncio.run(main())
