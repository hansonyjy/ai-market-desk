#!/usr/bin/env python3
"""
AI Market Desk scanner: the rules engine and the run modes.

All frequent checking happens here with plain rules and costs nothing; Claude is
called only when a rule fires. Every run can start on a blank machine: state
(cooldowns, headlines already seen, the earnings cache) lives in local files
(DESK_HOME) or an optional Google Sheet, and every hit goes to the Signal Log.

Modes
  scan       SHORT + LONG timing rules on the whole universe
  vip        News and fast-move monitor for Priority = VIP names
  scorecard  Fill 5-day and 20-day returns in the Signal Log

Flags
  --dry-run  Print hits only. No log write, no state change, no hand-off.
  --force    Ignore market-hours gating.

Configuration comes from environment variables (see .env.example).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

try:  # optional: load .env next to this file
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(__file__).resolve().parent / "config"
# Everything a run remembers, when no Google Sheet is configured. Never committed.
HOME = Path(os.getenv("DESK_HOME") or ROOT / ".desk")
HOME.mkdir(parents=True, exist_ok=True)
STATE_FILE = HOME / "state.json"
LOCAL_LOG = HOME / "signal_log.csv"
VIP_CONFIG_FILE = CONFIG / "vip_config.json"
LOG_FILE = HOME / "desk.log"

LOG_COLUMNS = [
    "Timestamp", "Ticker", "Horizon", "Rule", "Price", "RVOL",
    "Catalyst", "Direction", "Confidence", "Pinged", "Ret5d", "Ret20d",
    # Added Oct 15, at the end so older rows keep their columns: what each side
    # said for emailed alerts, so the Scorecard can grade them against each other.
    "Call", "Chartist", "Strategist",
    # Added Oct 17: return against the market (SPY, or XIU.TO for TSX names) from
    # the alert day's close. In a week when everything falls, raw returns make every
    # "buying" read look wrong; excess return asks whether the name beat the market.
    "Ex5d", "Ex20d",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("scanner")


def env(name: str, default=None, cast=str):
    """Read a setting. Surrounding spaces and newlines are removed: values pasted
    into GitHub secrets often carry an invisible trailing newline."""
    v = os.getenv(name)
    v = v.strip() if v is not None else None
    return cast(v) if v not in (None, "") else default


# ---------------------------------------------------------------- thresholds
CFG = {
    "RVOL_MIN": env("RVOL_MIN", 2.5, float),
    "GAP_MIN_PCT": env("GAP_MIN_PCT", 3.0, float),
    "BREAKOUT_DAYS": env("BREAKOUT_DAYS", 20, int),
    "EARNINGS_DAYS": env("EARNINGS_DAYS", 5, int),          # trading days
    "NEAR_200DMA_PCT": env("NEAR_200DMA_PCT", 2.0, float),
    "PULLBACK_FROM_PCT": env("PULLBACK_FROM_PCT", 5.0, float),  # was this far above 200DMA in last 20d
    "MIN_MINUTES_AFTER_OPEN": env("MIN_MINUTES_AFTER_OPEN", 30, int),
    "SHORT_COOLDOWN_DAYS": env("SHORT_COOLDOWN_DAYS", 1, int),
    "LONG_COOLDOWN_DAYS": env("LONG_COOLDOWN_DAYS", 5, int),
    "VIP_MOVE_PCT": env("VIP_MOVE_PCT", 3.0, float),
    "VIP_MOVE_WINDOW_MIN": env("VIP_MOVE_WINDOW_MIN", 15, int),
    "VIP_MOVE_COOLDOWN_MIN": env("VIP_MOVE_COOLDOWN_MIN", 60, int),
    # Live alerts (Oct 13: every email came before 10:30 ET, then silence)
    "MOVE_PCT": env("MOVE_PCT", 3.0, float),                # fast move: this much ...
    "MOVE_WINDOW_MIN": env("MOVE_WINDOW_MIN", 30, int),     # ... within this many minutes
    "MOVE_COOLDOWN_MIN": env("MOVE_COOLDOWN_MIN", 120, int),
    "DIGEST_UNTIL_MIN": env("DIGEST_UNTIL_MIN", 45, int),   # SHORT hits before 10:15 ET go in one digest
    "DAY_MOVE_ATR": env("DAY_MOVE_ATR", 1.5, float),        # day move: at least MOVE_PCT and this many normal days
    # Triage: on 500 names a busy open can have dozens of hits. Only the strongest names get a
    # Tape Reader read, and only a read can become an email. The rest are logged.
    "MAX_READS_PER_SCAN": env("MAX_READS_PER_SCAN", 8, int),
}

# Approximate cumulative share of a US trading day's volume by minutes since
# the 9:30 ET open. Volume is front- and back-loaded (U shape), so a straight
# line would overstate RVOL in the morning. Tune from the Scorecard.
VOLUME_PROFILE = [
    (0, 0.0), (30, 0.13), (60, 0.21), (120, 0.33), (180, 0.43),
    (240, 0.53), (300, 0.64), (360, 0.80), (390, 1.0),
]

DEFAULT_VIP_CONFIG = {
    "keywords": {
        "SPCX": ["Starship", "Starlink", "Falcon", "launch", "FAA", "NASA",
                 "contract", "xAI", "SpaceXAI", "IPO lockup", "index"],
        "TSLA": ["deliveries", "production", "recall", "NHTSA", "robotaxi",
                 "FSD", "Optimus", "margin", "price cut", "Musk sale", "guidance"],
    },
    # Trusted X accounts per ticker (usernames, no @). Add reporters you trust.
    "x_accounts": {
        "SPCX": ["SpaceX", "elonmusk"],
        "TSLA": ["Tesla", "elonmusk"],
    },
}


# ---------------------------------------------------------------- data model
@dataclass
class Flag:
    ticker: str
    horizon: str          # SHORT | LONG | VIP
    rule: str
    detail: str
    price: float
    rvol: Optional[float] = None
    day_change_pct: Optional[float] = None
    bar_date: str = ""
    ref: Optional[float] = None   # the level the rule fired on (20-day high, 200DMA, gap %, ...)
    tags: list = field(default_factory=list)   # log-only labels; never change an email or the Call


# ---------------------------------------------------------------- state
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("state.json unreadable; starting fresh")
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str))
    tmp.replace(STATE_FILE)


def cooled_down(state: dict, key: str, today: date, days: int) -> bool:
    last = state.get("fired", {}).get(key)
    if not last:
        return True
    return (today - date.fromisoformat(last[:10])).days >= days


def cooled_down_min(state: dict, key: str, now: datetime, minutes: int) -> bool:
    """Cooldown in minutes, for intraday rules that can fire more than once a day."""
    last = state.get("fired", {}).get(key)
    if not last or len(last) <= 10:
        return True
    try:
        return (now - datetime.fromisoformat(last)).total_seconds() / 60 >= minutes
    except ValueError:
        return True


def mark_fired(state: dict, key: str, when: str) -> None:
    state.setdefault("fired", {})[key] = when


# ---------------------------------------------------------------- helpers
def to_float(x) -> Optional[float]:
    try:
        s = str(x).replace(",", "").replace("$", "").strip()
        return float(s) if s else None
    except ValueError:
        return None


def session_fraction(now_et: datetime) -> float:
    """Expected cumulative share of daily volume traded by now_et."""
    open_ = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    m = (now_et - open_).total_seconds() / 60
    if m <= 0:
        return 0.0
    if m >= 390:
        return 1.0
    xs, ys = zip(*VOLUME_PROFILE)
    return float(np.interp(m, xs, ys))


def minutes_since_open(now_et: datetime) -> float:
    open_ = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return (now_et - open_).total_seconds() / 60


def trading_days_between(a: date, b: date) -> int:
    return int(np.busday_count(a, b))


def exchange_for(ticker: str) -> str:
    return "XTSE" if ticker.upper().endswith((".TO", ".V")) else "XNYS"


def expected_session(ticker: str, now_et: datetime) -> date:
    """The session a fresh daily bar must come from: today's while the exchange
    is open, otherwise the last completed session. Holidays come from the
    exchange's own calendar (NYSE or TSX)."""
    import exchange_calendars as xc

    cal = xc.get_calendar(exchange_for(ticker))
    ts = pd.Timestamp(now_et).tz_convert("UTC").floor("min")
    if cal.is_open_on_minute(ts):
        return cal.minute_to_session(ts).date()
    return cal.minute_to_past_session(ts).date()


def split_stale(bars: dict[str, pd.DataFrame], now_et: datetime) -> tuple[dict, dict]:
    """Drop tickers whose latest bar is older than the session it should be.
    Returns (fresh bars, {ticker: (latest bar date, expected date)})."""
    fresh, stale = {}, {}
    cache: dict[str, date] = {}
    for t, df in bars.items():
        ex = exchange_for(t)
        if ex not in cache:
            cache[ex] = expected_session(t, now_et)
        last = pd.Timestamp(df.index[-1]).date()
        if last < cache[ex]:
            stale[t] = (last, cache[ex])
        else:
            fresh[t] = df
    return fresh, stale


# ---------------------------------------------------------------- rule engine
def _simple_ratio(r: float) -> bool:
    """A plain split (2-for-1, 3-for-2, 1-for-10) is a small whole-number fraction.
    A spin-off adjustment is not (Yahoo recorded Honeywell's Honeywell Aerospace spin as an odd ratio)."""
    return any(abs(r - p / q) < 0.01 for p in range(1, 21) for q in range(1, 21))


def corporate_action(bars: pd.DataFrame, within: int = 200) -> Optional[str]:
    """The date of a spin-off-like adjustment in the last `within` bars, else None.
    Oct 26: HON "pulled back to its 200-day average of $12.06" eighteen days after
    spinning off its seed business; the average was mostly the old company rescaled.
    A plain split keeps history comparable and is ignored."""
    recent = bars.iloc[-within:]
    if "Stock Splits" in recent:
        s = recent["Stock Splits"].fillna(0)
        for when, r in s[s > 0].items():
            if not _simple_ratio(float(r)):
                return pd.Timestamp(when).date().isoformat()
    if "Adj Close" in recent and "Close" in recent:
        ratio = (recent["Adj Close"] / recent["Close"]).dropna()
        jump = (ratio / ratio.shift(1) - 1).abs()
        big = jump[jump > 0.10]            # dividends move it by a few percent; a spin by much more
        if not big.empty:
            return pd.Timestamp(big.index[-1]).date().isoformat()
    return None


def evaluate(ticker: str, bars: pd.DataFrame, meta: dict, now_et: datetime,
             earnings_date: Optional[date] = None) -> list[Flag]:
    """Pure function: daily bars (ascending, last row may be today's partial bar)
    plus Universe row -> list of rule hits. No I/O, easy to test."""
    flags: list[Flag] = []
    bars = bars.dropna(subset=["Close"])
    n_look = CFG["BREAKOUT_DAYS"]
    if len(bars) < n_look + 2:
        return flags

    today = bars.iloc[-1]
    prev = bars.iloc[:-1]
    bar_date = pd.Timestamp(bars.index[-1]).date()
    live = bar_date == now_et.date()

    last = float(today["Close"])
    prev_close = float(prev["Close"].iloc[-1])
    day_chg = (last / prev_close - 1) * 100

    # Relative volume, time-of-day adjusted while the session is live
    avg20 = float(prev["Volume"].iloc[-20:].mean())
    frac = session_fraction(now_et) if live else 1.0
    rvol = None
    if avg20 > 0 and frac > 0:
        rvol = (float(today["Volume"]) / frac) / avg20
    # The first 30 minutes are opening noise, and RVOL projected from a few
    # minutes of volume is meaningless (24x at 9:35 on Oct 7). So no SHORT
    # flags then, and no RVOL figure on any flag.
    early = live and minutes_since_open(now_et) < CFG["MIN_MINUTES_AFTER_OPEN"]
    if early:
        rvol = None

    def add(h, rule, detail, ref=None):
        flags.append(Flag(ticker, h, rule, detail, round(last, 2),
                          round(rvol, 2) if rvol is not None else None,
                          round(day_chg, 2), bar_date.isoformat(),
                          round(ref, 2) if ref is not None else None))

    # ---- SHORT (not in the first 30 minutes)
    if early:
        pass
    elif rvol is not None and rvol >= CFG["RVOL_MIN"]:
        add("SHORT", "rvol_spike", f"RVOL {rvol:.1f}x, {day_chg:+.1f}% on day", rvol)

    gap = (float(today["Open"]) / prev_close - 1) * 100
    if not early and abs(gap) >= CFG["GAP_MIN_PCT"]:
        add("SHORT", "gap_up" if gap > 0 else "gap_down", f"Gap {gap:+.1f}% vs prior close", gap)
        # The reversal is the timing signal: a gap down bought back, or a gap up sold.
        if gap < 0 and last >= prev_close:
            add("SHORT", "gap_filled", f"Gap {gap:+.1f}% filled, back above prior close {prev_close:.2f}", gap)
        elif gap > 0 and last <= prev_close:
            add("SHORT", "gap_failed", f"Gap {gap:+.1f}% failed, back below prior close {prev_close:.2f}", gap)

    hi = float(prev["High"].iloc[-n_look:].max())
    lo = float(prev["Low"].iloc[-n_look:].min())
    if early:
        pass
    elif last > hi:
        add("SHORT", f"breakout_{n_look}d", f"Above {n_look}-day high {hi:.2f}", hi)
    elif last < lo:
        add("SHORT", f"breakdown_{n_look}d", f"Below {n_look}-day low {lo:.2f}", lo)

    if earnings_date and not early:
        d = trading_days_between(now_et.date(), earnings_date)
        if 0 <= d <= CFG["EARNINGS_DAYS"]:
            add("SHORT", "earnings_soon", f"Earnings {earnings_date.isoformat()} ({d} trading days)")

    # ---- LONG
    spun = corporate_action(bars)
    if spun:   # the 200-day average spans a different company; the team's own levels still apply
        log.info("%s: corporate action on %s, 200-day signals skipped", ticker, spun)
    if len(prev) >= 200 and not spun:
        sma200 = float(prev["Close"].iloc[-200:].mean())
        dist = (last / sma200 - 1) * 100
        came_from_above = float(prev["Close"].iloc[-20:].max()) > sma200 * (1 + CFG["PULLBACK_FROM_PCT"] / 100)
        if abs(dist) <= CFG["NEAR_200DMA_PCT"] and came_from_above:
            add("LONG", "pullback_200dma", f"{dist:+.1f}% from 200DMA {sma200:.2f}", sma200)

    target = to_float(meta.get("target entry level"))
    if target and last <= target:
        add("LONG", "target_entry_hit", f"At/below target entry {target:.2f}", target)

    trim = to_float(meta.get("trim level"))
    if trim and last >= trim:
        add("LONG", "trim_level_hit", f"At/above trim level {trim:.2f}", trim)

    # Hypothesis tag (Rule Lab, Oct 17): a volume surge on an up day while a DeMark
    # buy 9 or a fresh buy 13 is showing passed in the fit years but was no better
    # than luck overall. It is logged so the live Scorecard can test it on new days.
    # It changes nothing the team sees.
    spike_up = [f for f in flags if f.rule == "rvol_spike" and day_chg >= 0]
    if spike_up and demark_buy(bars):
        for f in spike_up:
            f.tags.append(HYPOTHESIS_TAG)

    return flags


HYPOTHESIS_TAG = "dm_buy_volume"


def demark_buy(bars: pd.DataFrame) -> bool:
    """True when a DeMark buy setup 9 is showing, or a buy 13 completed within the
    last 10 bars. Same definition as the Rule Lab's "buy 9 or 13" condition."""
    from desk import levels
    setup = levels.td_setup(bars["Close"])
    if setup["side"] == "buy" and setup["count"] == 9:
        return True
    cd = levels.td_countdown(bars, stale_bars=10)
    return cd.get("side") == "buy" and cd.get("count") == 13


# ---------------------------------------------------------------- Google Sheet I/O
STATE_HEADER = ["Key", "Value"]
FIRED_PREFIX = "fired:"
FIRED_KEEP_DAYS = 30   # cooldowns are at most a week; older entries are pruned
SEEN_KEEP = 500        # news ids remembered; Finnhub is queried one day back


def google_credentials(value: Optional[str]):
    """GOOGLE_CREDS_JSON may hold the JSON itself (GitHub secret) or a file path
    (local runs). Returns a dict, a path string, or None."""
    if not value:
        return None
    v = value.strip()
    if v.startswith("{"):
        return json.loads(v)
    return v


def state_to_rows(state: dict, today: date) -> list[list[str]]:
    """Flatten state into Key | Value rows. Each cooldown gets its own row so no
    single cell grows past Google's 50,000-character limit."""
    rows = [STATE_HEADER]
    for k, v in sorted(state.items()):
        if k == "fired":
            continue
        if k == "seen":
            v = list(v)[-SEEN_KEEP:]
        rows.append([k, json.dumps(v, default=str)])
    for k, when in sorted(state.get("fired", {}).items()):
        try:
            if (today - date.fromisoformat(str(when)[:10])).days > FIRED_KEEP_DAYS:
                continue
        except ValueError:
            pass
        rows.append([FIRED_PREFIX + k, str(when)])
    return rows


def rows_to_state(rows: list[list[str]]) -> dict:
    state: dict = {}
    for r in rows[1:] if rows and rows[0][:2] == STATE_HEADER else rows:
        if len(r) < 2 or not r[0]:
            continue
        k, v = r[0], r[1]
        if k.startswith(FIRED_PREFIX):
            state.setdefault("fired", {})[k[len(FIRED_PREFIX):]] = v
        else:
            try:
                state[k] = json.loads(v)
            except json.JSONDecodeError:
                log.warning("State row %s unreadable; ignored: %.80s", k, v)
    return state


def write_state_rows(ws, rows: list[list[str]], cols: str = "B") -> None:
    """Overwrite the State tab in place, then clear only the leftover rows below.
    Never clear first: if the write failed after a clear, the tab would be left
    empty and every cooldown and budget lost."""
    if ws.row_count < len(rows):
        ws.add_rows(len(rows) - ws.row_count)
    ws.update(values=rows, range_name="A1")
    if ws.row_count > len(rows):
        ws.batch_clear([f"A{len(rows) + 1}:{cols}{ws.row_count}"])


class Sheet:
    """Google Sheet access through a service account (gspread).

    In GitHub Actions the Sheet is required: a missing GOOGLE_CREDS_JSON fails
    the run loudly. Without credentials (local runs and tests only) the Universe
    comes from UNIVERSE_CSV, and the Signal Log and state go to local files."""

    def __init__(self, spreadsheet=None):
        self.sheet_id = env("SHEET_ID")
        self.universe_tab = env("UNIVERSE_TAB", "Universe")
        self.log_tab = env("LOG_TAB", "Signal Log")
        self.state_tab = env("STATE_TAB", "State")
        self.scorecard_tab = env("SCORECARD_TAB", "Scorecard")
        self.sh = spreadsheet
        self._ws: dict = {}
        if self.sh is None:
            creds = google_credentials(env("GOOGLE_CREDS_JSON"))
            if creds:
                import gspread  # noqa: WPS433

                if not self.sheet_id:
                    raise SystemExit("GOOGLE_CREDS_JSON is set but SHEET_ID is missing.")
                gc = (gspread.service_account_from_dict(creds) if isinstance(creds, dict)
                      else gspread.service_account(filename=creds))
                self.sh = gc.open_by_key(self.sheet_id)
            elif self.sheet_id:
                raise SystemExit("SHEET_ID is set but GOOGLE_CREDS_JSON is missing. Unset SHEET_ID to "
                                 "use local files, or add the service-account JSON.")

    # ---- worksheets
    def _tab(self, name: str, header: list[str]):
        """Return a worksheet, creating it if missing and writing the header
        if its first row is empty (tabs are created empty by hand)."""
        if name in self._ws:
            return self._ws[name]
        try:
            ws = self.sh.worksheet(name)
        except Exception:
            ws = self.sh.add_worksheet(name, rows=1000, cols=len(header))
        if not ws.row_values(1):
            ws.update(values=[header], range_name="A1")
        self._ws[name] = ws
        return ws

    # ---- universe
    def universe(self) -> list[dict]:
        if self.sh:
            rows = self.sh.worksheet(self.universe_tab).get_all_records()
        else:
            src = env("UNIVERSE_CSV") or str(CONFIG / "universe.csv")   # the S&P 500 by default
            if not src:
                raise SystemExit("No Sheet credentials. For a local run set GOOGLE_CREDS_JSON, "
                                 "or UNIVERSE_CSV to a CSV file of the Universe tab.")
            text = requests.get(src, timeout=30).text if src.startswith("http") else Path(src).read_text()
            rows = list(csv.DictReader(text.splitlines()))
        out = []
        for r in rows:
            r = {str(k).strip().lower(): (str(v).strip() if v is not None else "") for k, v in r.items()}
            if r.get("ticker"):
                r["ticker"] = r["ticker"].upper()
                out.append(r)
        return out

    # ---- state
    def load_state(self) -> dict:
        if self.sh:
            state = rows_to_state(self._tab(self.state_tab, STATE_HEADER).get_all_values())
            log.info("state loaded: %s, %d cooldowns",
                     ", ".join(sorted(k for k in state if k != "fired")) or "empty",
                     len(state.get("fired", {})))
            return state
        return load_state()

    def save_state(self, state: dict) -> None:
        if self.sh:
            rows = state_to_rows(state, datetime.now(ET).date())
            ws = self._tab(self.state_tab, STATE_HEADER)
            write_state_rows(ws, rows)
        else:
            save_state(state)

    # ---- signal log
    def append_log(self, flags: list[Flag], reads: Optional[dict] = None,
                   pinged: Optional[dict] = None) -> None:
        if not flags:
            return
        from desk import mailer
        reads, pinged = reads or {}, pinged or {}
        now = datetime.now(PT).strftime("%Y-%m-%d %H:%M")
        rows = []
        for f in flags:
            r = reads.get(f.ticker, {})
            catalyst = r.get("catalyst", "")
            if r.get("links"):
                catalyst += " | Sources: " + " ".join(r["links"])
            tag = " | sharp_down_day" if mailer.sharp_drop(asdict(f)) else ""
            tag += "".join(f" | {t}" for t in f.tags)
            rows.append([now, f.ticker, f.horizon, f"{f.rule} | {f.detail}{tag}", f.price,
                         f.rvol if f.rvol is not None else "",
                         catalyst, r.get("direction", ""), r.get("confidence", ""),
                         pinged.get(f.ticker, "no"), "", "",
                         *(r.get("votes", {}).get(k, "") for k in ("call", "chartist", "strategist"))])
        if self.sh:
            # RAW: store exactly what we send, so "CSU.TO" stays text, not a web link
            self._log_ws().append_rows(rows, value_input_option="RAW")
        else:
            new = not LOCAL_LOG.exists()
            with LOCAL_LOG.open("a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(LOG_COLUMNS)
                w.writerows(rows)

    def _log_ws(self):
        """The Signal Log tab, with any columns added since it was made appended to
        its header (new columns always go at the end, so old rows keep their places)."""
        ws = self._tab(self.log_tab, LOG_COLUMNS)
        head = ws.row_values(1)
        if head != LOG_COLUMNS and head == LOG_COLUMNS[:len(head)]:
            ws.update(values=[LOG_COLUMNS], range_name="A1")
        return ws

    def read_log(self) -> list[list[str]]:
        if self.sh:
            return self._tab(self.log_tab, LOG_COLUMNS).get_all_values()   # reading never writes
        if not LOCAL_LOG.exists():
            return []
        with LOCAL_LOG.open() as fh:
            return list(csv.reader(fh))

    def write_cells(self, updates: list[tuple[int, dict]]) -> None:
        """updates: (row_index_in_values, {column name: value}); row 0 is the header.
        Only the given cells change, so the desk's own appends are never overwritten."""
        if not updates:
            return
        col = {c: i for i, c in enumerate(LOG_COLUMNS)}
        if self.sh:
            import gspread.utils as gu

            ws = self._log_ws()
            data = [{"range": gu.rowcol_to_a1(i + 1, col[c] + 1), "values": [[v]]}
                    for i, cells in updates for c, v in cells.items() if v != ""]
            for k in range(0, len(data), 500):   # Google caps one request's size
                ws.batch_update(data[k:k + 500], value_input_option="USER_ENTERED")
        else:
            values = self.read_log()
            values[0] = LOG_COLUMNS
            for i, cells in updates:
                values[i] += [""] * (len(LOG_COLUMNS) - len(values[i]))
                for c, v in cells.items():
                    if v != "":
                        values[i][col[c]] = v
            with LOCAL_LOG.open("w", newline="") as fh:
                csv.writer(fh).writerows(values)

    def write_tab(self, name: str, rows: list[list]) -> None:
        """Overwrite a whole results tab in place (Rule Lab), leftovers blanked."""
        width = max(len(r) for r in rows)
        rows = [[str(x) for x in r] + [""] * (width - len(r)) for r in rows]
        if self.sh:
            import gspread.utils as gu

            ws = self._tab(name, rows[0])
            if ws.col_count < width:
                ws.add_cols(width - ws.col_count)
            last_col = gu.rowcol_to_a1(1, width).rstrip("1")
            write_state_rows(ws, rows, cols=last_col)
        else:
            with (LOCAL_LOG.parent / f"{name.lower().replace(' ', '_')}.csv").open("w", newline="") as fh:
                csv.writer(fh).writerows(rows)

    def write_summary(self, rows: list[list]) -> None:
        """The weekly Scorecard tab: overwritten in place, leftovers blanked."""
        if self.sh:
            ws = self._tab(self.scorecard_tab, rows[0])
            write_state_rows(ws, [[str(x) for x in r] + [""] * (6 - len(r)) for r in rows], cols="F")
        else:
            with (LOCAL_LOG.parent / "scorecard.csv").open("w", newline="") as fh:
                csv.writer(fh).writerows(rows)


# ---------------------------------------------------------------- market data
def fetch_daily(tickers: list[str], period: str = "14mo", adjust: bool = False) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    if not tickers:
        return {}
    # adjust=True (Rule Lab): prices adjusted for splits and dividends, so a
    # buy-and-hold return includes the dividends.
    # actions=True adds "Stock Splits" (and Dividends): a spin-off shows up there as
    # an odd ratio, which corporate_action() uses to distrust old levels.
    df = yf.download(tickers, period=period, interval="1d", group_by="ticker",
                     auto_adjust=adjust, actions=not adjust, progress=False, threads=True)
    out = {}
    for t in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if t not in df.columns.get_level_values(0):
                    log.warning("no data for %s (check the ticker format in the Universe tab)", t)
                    continue
                sub = df[t]
            else:
                sub = df
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                log.warning("no data for %s (check the ticker format in the Universe tab)", t)
            else:
                out[t] = sub
        except Exception as e:  # keep one bad ticker from killing the run
            log.warning("data error %s: %s", t, describe_error(e))
    return out


def earnings_dates(tickers: list[str], state: dict, get=None) -> dict[str, date]:
    """Next earnings date per US ticker from Finnhub, cached once per day.
    One calendar call per week of the window covers every company, so 500 names
    cost 3 calls instead of 500 (one per name at 60/min would take over 9 minutes)."""
    key = env("FINNHUB_KEY")
    today = datetime.now(ET).date().isoformat()
    cache = state.get("earnings", {})
    if cache.get("date") == today:
        return {t: date.fromisoformat(d) for t, d in cache.get("map", {}).items()}
    want = {t for t in tickers if "." not in t}   # Finnhub free tier: US symbols only
    spell = {t.replace("-", "."): t for t in want}   # Finnhub writes BRK.B, Yahoo BRK-B
    out: dict[str, str] = {}
    if key and want:
        get = get or requests.get
        start = datetime.now(ET).date()
        for w in range(3):   # 21 days, a week per call
            a, b = start + timedelta(days=7 * w), start + timedelta(days=7 * w + 6)
            try:
                r = get("https://finnhub.io/api/v1/calendar/earnings",
                        params={"from": a.isoformat(), "to": b.isoformat(), "token": key}, timeout=30)
                items = r.json().get("earningsCalendar", []) if r.ok else []
            except Exception as e:
                log.warning("earnings calendar %s..%s: %s", a, b, describe_error(e))
                continue
            for i in items:
                t = spell.get(str(i.get("symbol", "")).upper()) or (i.get("symbol") if i.get("symbol") in want else None)
                d = i.get("date")
                if t and d and (t not in out or d < out[t]):
                    out[t] = d
        log.info("earnings dates for %d of %d name(s) in the next 21 days", len(out), len(want))
    state["earnings"] = {"date": today, "map": out}
    return {t: date.fromisoformat(d) for t, d in out.items()}


# ---------------------------------------------------------------- hand-off
def fetch_intraday(tickers: list[str]) -> dict[str, pd.Series]:
    """Today's 1-minute closes per ticker, in one download."""
    import yfinance as yf

    out: dict[str, pd.Series] = {}
    if not tickers:
        return out
    df = yf.download(tickers, period="1d", interval="1m", group_by="ticker",
                     progress=False, auto_adjust=False, threads=True)
    for t in tickers:
        try:
            sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
            c = sub["Close"].dropna()
            if len(c):
                out[t] = c
        except KeyError:
            continue
    return out


def fast_moves(closes: dict[str, pd.Series], window: int, pct: float) -> dict[str, tuple[float, float]]:
    """Tickers that moved at least pct within the last `window` minutes:
    ticker -> (move %, last price). Pure function, easy to test."""
    out = {}
    for t, c in closes.items():
        if len(c) <= window:
            continue
        last = float(c.iloc[-1])
        lo, hi = float(c.iloc[-1 - window:].min()), float(c.iloc[-1 - window:].max())
        up, down = (last / lo - 1) * 100, (last / hi - 1) * 100   # from the window's low / high
        move = up if up >= abs(down) else down
        if abs(move) >= pct:
            out[t] = (round(move, 1), round(last, 2))
    return out


def day_stats(bars: pd.DataFrame, now_et: datetime) -> tuple[Optional[float], Optional[float]]:
    """(RVOL adjusted for time of day, day change %) for the latest bar."""
    b = bars.dropna(subset=["Close"])
    if len(b) < 22:
        return None, None
    today, prev = b.iloc[-1], b.iloc[:-1]
    live = pd.Timestamp(b.index[-1]).date() == now_et.date()
    chg = (float(today["Close"]) / float(prev["Close"].iloc[-1]) - 1) * 100
    avg20 = float(prev["Volume"].iloc[-20:].mean())
    frac = session_fraction(now_et) if live else 1.0
    rvol = (float(today["Volume"]) / frac) / avg20 if avg20 > 0 and frac > 0 else None
    return (round(rvol, 2) if rvol is not None else None), round(chg, 2)


def normal_day_pct(bars: pd.DataFrame) -> Optional[float]:
    """A normal day's range for this name: the 14-day average true range as a
    percent of price, from completed bars."""
    b = bars.dropna(subset=["Close"]).iloc[:-1]
    if len(b) < 15:
        return None
    c = b["Close"]
    tr = pd.concat([b["High"] - b["Low"], (b["High"] - c.shift()).abs(), (b["Low"] - c.shift()).abs()],
                   axis=1).max(axis=1)
    return round(float(tr.iloc[-14:].mean() / c.iloc[-1] * 100), 2)


def mentioned_today(state: dict, ticker: str, today: date) -> bool:
    """Any rule already fired for this name today (alert, digest or held)."""
    day = today.isoformat()
    return any(k.startswith(ticker + "|") and str(v).startswith(day) for k, v in state.get("fired", {}).items())


def describe_error(e: Exception) -> str:
    """Error type and its cause's type only. The message itself is not logged,
    because a bad-header error can echo the API key."""
    cause = e.__cause__ or e.__context__
    return type(e).__name__ + (f" (caused by {type(cause).__name__})" if cause else "")



def company_news(tickers: list[str], d_from: date, d_to: date, get=None) -> dict[str, list[dict]]:
    """Finnhub headlines per US ticker, newest first. Free and small: a few hundred
    tokens per name, against tens of thousands for a web search. Canadian names
    are not covered by the free tier and fall back to web search."""
    key = env("FINNHUB_KEY")
    get = get or requests.get
    out: dict[str, list[dict]] = {}
    if not key:
        return out
    for t in tickers:
        if "." in t:
            continue
        try:
            r = get("https://finnhub.io/api/v1/company-news",
                    params={"symbol": t, "from": d_from.isoformat(), "to": d_to.isoformat(),
                            "token": key}, timeout=15)
            items = r.json() if r.ok else []
        except Exception as e:
            log.warning("finnhub news %s: %s", t, describe_error(e))
            continue
        items = sorted((n for n in items if n.get("url") and n.get("headline")),
                       key=lambda n: n.get("datetime", 0), reverse=True)
        if items:
            out[t] = [{"headline": n["headline"], "source": n.get("source", ""), "url": n["url"],
                       "time": datetime.utcfromtimestamp(n.get("datetime", 0)).strftime("%m-%d %H:%M")}
                      for n in items[:8]]
    return out


def log_cost(state: dict, today: date, dry_run: bool) -> None:
    """This run's Claude spend in the log, and today's running total in the
    State tab (ai_budget.usd), so cost is visible without opening the Console."""
    from desk import ai
    u = ai.USAGE
    if not u["calls"]:
        return
    log.info("cost this run: %d calls, %d in, %d out, %d searches, $%.2f",
             u["calls"], u["in"], u["out"], u["searches"], u["usd"])
    if dry_run:
        return
    # Record every paid call, not only budgeted ones: the daily market read and the
    # "said" lookup run outside take_budget, so on a day where only they ran, the
    # row was never created and their cost went unrecorded (Oct 15).
    b = state.get("ai_budget") or {}
    if b.get("date") != today.isoformat():
        b = {"date": today.isoformat(), "calls": 0}
    b["usd"] = round(b.get("usd", 0) + u["usd"], 4)   # 4 places: a Haiku call is a fraction of a cent
    b["all_calls"] = b.get("all_calls", 0) + u["calls"]      # every call; "calls" counts only budgeted ones
    roles = b.setdefault("by_role", {})
    for r, usd in ai.USAGE_BY_ROLE.items():
        roles[r] = round(roles.get(r, 0) + usd, 4)
    state["ai_budget"] = b


def full_horizons(state: dict, today: date) -> set[str]:
    """Horizons whose email cap is already used today. Their new hits are logged
    without a Tape Reader read: the read could not lead to an email anyway."""
    from desk import mailer
    b = state.get("email_budget") or {}
    if b.get("date") != today.isoformat():
        return set()
    return {h for h, cap in mailer.caps().items() if b.get(h, 0) >= cap}


DRY_RUN_AI_CALLS = int(os.getenv("DRY_RUN_AI_CALLS") or 8)


def emailed_today(state: dict, today: date) -> set[str]:
    """Names already emailed today, in the digest or a live alert. Another SHORT
    email for one of them needs a new story (Oct 16: HACK emailed at 9:01 for its
    breakdown and again at 11:01 for its volume, the same selloff)."""
    out = set()
    for key in ("digest", "emailed"):
        d = state.get(key) or {}
        if d.get("date") == today.isoformat():
            out |= set(d.get("tickers", []))
    return out


def mark_emailed(state: dict, today: date, tickers) -> None:
    d = state.get("emailed") or {}
    earlier = d.get("tickers", []) if d.get("date") == today.isoformat() else []
    state["emailed"] = {"date": today.isoformat(), "tickers": sorted(set(earlier) | set(tickers))}


def worth_reading(flags: list[Flag], state: dict, today: date) -> list[Flag]:
    """Only hits that could become an email get a Tape Reader read. The same
    checks the email step applies later, done first, so no read is paid for and
    then thrown away (Oct 14: 34 calls, $3.12). All hits are still logged.
    - SHORT names below EMAIL_MIN_RVOL volume: never emailed, so not read.
    - Names already in today's digest: only a new story (fast move, gap
      reversal) is emailed, so only that is read."""
    from desk import mailer
    in_digest = emailed_today(state, today)
    best_rvol: dict[str, float] = {}
    for f in flags:
        if f.horizon == "SHORT":
            best_rvol[f.ticker] = max(best_rvol.get(f.ticker, 0), f.rvol or 0)
    keep, thin, repeat = [], set(), set()
    for f in flags:
        if f.horizon == "SHORT" and best_rvol.get(f.ticker, 0) < mailer.min_rvol():
            thin.add(f.ticker)
        elif f.horizon == "SHORT" and f.ticker in in_digest and f.rule not in NEW_STORY_RULES:
            repeat.add(f.ticker)
        else:
            keep.append(f)
    if thin:
        log.info("no read (volume under %.1fx, cannot be emailed): %s", mailer.min_rvol(), ",".join(sorted(thin)))
    if repeat:
        log.info("no read (already in today's digest): %s", ",".join(sorted(repeat)))
    return keep


def hit_score(fs: list[Flag]) -> float:
    """How strongly one name's hits ask for a read. Volume and the size of the
    move lead; a LONG setup or a new story (fast move, gap reversal) adds weight,
    and each extra rule on the same name adds a little. Plain arithmetic, so the
    ranking is easy to check in the log."""
    rvol = max((f.rvol or 0) for f in fs)
    move = max(abs(f.day_change_pct or 0) for f in fs)
    score = min(rvol, 5.0) + min(move, 10.0) / 2
    if any(f.horizon == "LONG" for f in fs):
        score += 1.5
    if any(f.rule in NEW_STORY_RULES for f in fs):
        score += 1.0
    score += 0.5 * (len({f.rule for f in fs}) - 1)
    return round(score, 2)


def triage(flags: list[Flag], limit: Optional[int] = None) -> list[Flag]:
    """Keep the hits of the `limit` highest-scoring names, strongest first. VIP
    hits are always kept. Cut hits stay in the Signal Log, tagged triage_cut."""
    limit = CFG["MAX_READS_PER_SCAN"] if limit is None else limit
    by: dict[str, list[Flag]] = {}
    for f in flags:
        by.setdefault(f.ticker, []).append(f)
    score = {t: hit_score(fs) for t, fs in by.items()}
    ranked = sorted(by, key=lambda t: (-score[t], t))
    vip = [t for t in ranked if any(f.horizon == "VIP" for f in by[t])]
    rest = [t for t in ranked if t not in vip]
    if limit <= 0 or len(rest) <= limit:
        return [f for t in vip + rest for f in by[t]]
    keep, cut = rest[:limit], rest[limit:]
    for t in cut:
        for f in by[t]:
            f.tags.append("triage_cut")
    log.info("triage: read top %d of %d name(s): %s | cut: %s", limit, len(rest),
             ", ".join(f"{t} {score[t]}" for t in keep), ", ".join(cut))
    return [f for t in vip + keep for f in by[t]]


def read_tape(flags: list[Flag], state: dict, today: date, client=None, dry_run: bool = False,
              news_fn=None) -> dict:
    """Ask the Tape Reader why the flagged names are moving, strongest names first
    (see triage). Skipped (hits are still logged) when no API key is set or today's
    AI budget is used up."""
    if not flags:
        return {}
    if not env("ANTHROPIC_API_KEY") and client is None:
        log.warning("ANTHROPIC_API_KEY not set; hits logged without a Tape Reader read")
        return {}
    from desk import ai
    full = full_horizons(state, today)
    if full:
        skipped = sorted({f.ticker for f in flags if f.horizon in full})
        flags = [f for f in flags if f.horizon not in full]
        if skipped:
            log.info("email cap used for %s today; no read for %s", "/".join(sorted(full)), ",".join(skipped))
    flags = triage(worth_reading(flags, state, today))
    if not flags:
        return {}
    if dry_run:
        # A dry run saves nothing, so its calls never reach the daily cap. Give it a
        # small cap of its own, so testing cannot spend money unseen.
        b = state.get("ai_budget") or {}
        used = b.get("calls", 0) if b.get("date") == today.isoformat() else 0
        state["ai_budget"] = {"date": today.isoformat(),
                              "calls": max(used, ai.DAILY_AI_CALLS - DRY_RUN_AI_CALLS)}
    tickers = list(dict.fromkeys(f.ticker for f in flags))   # strongest first
    news = (news_fn or company_news)(tickers, today - timedelta(days=1), today)
    log.info("finnhub headlines for %d of %d name(s)", len(news), len(tickers))
    try:
        return ai.tape_reader([asdict(f) for f in flags], today=today, client=client, news=news,
                              take_budget=lambda: ai.take_budget(state, today, reserve=4))
    except Exception as e:  # an AI outage must never lose the hits themselves
        log.error("Tape Reader failed: %s", describe_error(e))
        return {}


BENCHMARKS_FILE = CONFIG / "benchmarks.json"


def load_benchmarks() -> dict:
    try:
        return json.loads(BENCHMARKS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"market": {"US": "SPY", "CA": "XIU.TO"}, "sector": {}}


def benchmarks_for(ticker: str, bm: dict) -> tuple[str, str]:
    """(market ETF, sector ETF or '') for a ticker."""
    market = bm["market"]["CA" if exchange_for(ticker) == "XTSE" else "US"]
    sector = bm.get("sector", {}).get(ticker, "")
    return market, ("" if sector in (ticker, market) else sector)


def add_technicals(lv: dict, bars: dict, fetch_fn=None) -> None:
    """Chartist's facts (RSI, DeMark counts, relative strength) for each name.
    Benchmark ETFs not already in this run's bars are fetched once, together."""
    from desk import levels
    bm = load_benchmarks()
    pairs = {t: benchmarks_for(t, bm) for t in lv}
    need = sorted({e for m, sct in pairs.values() for e in (m, sct) if e and e not in bars})
    extra = {}
    if need:
        try:
            extra = (fetch_fn or fetch_daily)(need)
        except Exception as e:
            log.warning("benchmark fetch failed: %s", describe_error(e))
    allb = {**bars, **extra}
    for t, (m, sct) in pairs.items():
        lv[t]["tech"] = levels.technicals(bars[t], allb.get(m), m, allb.get(sct) if sct else None, sct)


SECTOR_WORDS = {
    "XLK": ["tech"], "SMH": ["semi", "chip", "tech"], "XSW": ["software", "tech"],
    "HACK": ["cyber", "software", "tech"], "XLC": ["communication", "internet", "media"],
    "XLY": ["consumer", "discretionary", "retail"], "XLP": ["staples", "consumer"],
    "XLF": ["financial", "bank", "insur", "payment", "crypto"], "VHT": ["health", "pharma", "medtech"], "XBI": ["biotech", "health"],
    "XLI": ["industrial", "aerospace", "transport"], "XLE": ["energy", "oil"], "XLB": ["material"],
    "VPU": ["utilit", "power"],
    "XLRE": ["real estate", "reit", "property"],
}


def comment_fits(c: dict, a: dict, bm: dict) -> bool:
    """A real comment is shown on an alert only when it is about that name, its
    sector, or its theme: the comment's own tickers or sectors must match."""
    base = a["ticker"].split(".")[0].split("-")[0].upper()
    if any(t.split(".")[0].upper() == base for t in c.get("tickers", [])):
        return True
    _, sector = benchmarks_for(a["ticker"], bm)
    words = set(SECTOR_WORDS.get(sector, []) + SECTOR_WORDS.get(a["ticker"], []))
    words |= {w for w in "".join(ch if ch.isalpha() else " " for ch in (a.get("theme") or "").lower()).split()
              if len(w) >= 4}
    text = " ".join(c.get("sectors", [])).lower()
    return any(w in text for w in words)


def macro_for(state: dict, today: date, bars: dict, fetch_fn=None) -> dict:
    """Today's market backdrop, computed once a day and cached in State."""
    from desk import macro
    cached = state.get("macro") or {}
    if cached.get("date") == today.isoformat():
        return cached.get("m") or {}
    want = list(macro.TICKERS.values())
    have = {t: bars[t] for t in want if t in bars}
    need = [t for t in want if t not in have]
    if need:
        try:
            have.update((fetch_fn or fetch_daily)(need))
        except Exception as e:
            log.warning("macro data failed: %s", describe_error(e))
    m = macro.snapshot(have)
    if m:
        state["macro"] = {"date": today.isoformat(), "m": m}
    return m


def strategist_market_for(state: dict, today: date, backdrop: str, client=None) -> str:
    """The day's Strategist (Lee-style) market read, once a day, cached in State."""
    cached = state.get("strategist_market") or {}
    if cached.get("date") == today.isoformat():
        return cached.get("view", "")
    if not backdrop or (not env("ANTHROPIC_API_KEY") and client is None):
        return ""
    from desk import ai
    budget = lambda: ai.take_budget(state, today)
    try:
        view = ai.strategist_market(backdrop, said_for(state, today, client, budget), client=client, take_budget=budget)
    except Exception as e:
        log.error("Strategist market read failed: %s", describe_error(e))
        return ""
    state["strategist_market"] = {"date": today.isoformat(), "view": view}
    return view


def said_for(state: dict, today: date, client, take_budget, fresh: bool = True) -> list[dict]:
    """Tom Lee's and Mark Newton's recent public comments, looked up once a day.
    fresh=False (Ask the desk) reuses the last lookup instead of paying for a new
    one. A lookup that finds nothing keeps the last one's comments that are still
    within 14 days, so one bad lookup does not blank the day."""
    from desk import ai
    cached = state.get("said") or {}
    if cached.get("date") == today.isoformat():
        return cached.get("items") or []
    earlier = ai.check_said(cached.get("items") or [], today)   # re-checked for age
    if not fresh and earlier:
        return earlier
    items = ai.what_they_said(today, client=client, take_budget=take_budget) or earlier
    state["said"] = {"date": today.isoformat(), "items": items}
    return items


def add_levels(send: list[dict], bars: dict, meta: dict, state: dict, today: date, client=None,
               fetch_fn=None) -> None:
    """Chartist lens levels and the Call line for the alerts about to go out.
    Any failure here only drops the Chartist line; the alert still goes."""
    from desk import ai
    from desk import levels
    from desk import mailer
    for a in send:
        row = meta.get(a["ticker"]) or {}
        a["call"] = mailer.call(a, row.get("status", ""))
        a["held"] = str(row.get("status", "")).strip().lower().startswith("hold")
        a["name"] = row.get("name", "")
    lv = {a["ticker"]: levels.compute_levels(bars[a["ticker"]]) for a in send if a["ticker"] in bars}
    lv = {t: v for t, v in lv.items() if v}
    if not lv:
        log.warning("Chartist lens skipped: no price history for %s", ",".join(a["ticker"] for a in send))
        return
    if not env("ANTHROPIC_API_KEY") and client is None:
        return
    add_technicals(lv, bars, fetch_fn)
    budget = lambda: ai.take_budget(state, today)
    try:
        got = ai.chartist_lens(send, lv, client=client, take_budget=budget,
                             breadth=ai.breadth_line(levels.breadth(bars)))
    except Exception as e:
        log.error("Chartist lens failed: %s", describe_error(e))
        got = {}
    for a in send:
        if a["ticker"] in got:
            a["chartist"] = got[a["ticker"]]
        if a["ticker"] in lv:
            a["tech"] = lv[a["ticker"]].get("tech") or {}
    # Strategist lens: the day's market backdrop (code) and the two strategists'
    # real recent comments (one lookup a day), then one call for all alerts.
    from desk import macro
    backdrop = macro.backdrop_line(macro_for(state, today, bars, fetch_fn))
    if not backdrop:
        log.warning("Strategist lens skipped: no market backdrop data")
        return
    try:
        said = said_for(state, today, client, budget)
        market_read = strategist_market_for(state, today, backdrop, client)   # cached once a day
        strategist = ai.strategist_lens(send, backdrop, said, client=client, take_budget=budget, market_read=market_read)
    except Exception as e:
        log.error("Strategist lens failed: %s", describe_error(e))
        strategist = {}
    shown = state.get("said_shown") or {}
    shown_urls = set(shown.get("urls", [])) if shown.get("date") == today.isoformat() else set()
    bm = load_benchmarks()
    for a in send:
        if a["ticker"] not in strategist:
            continue
        a["theme"] = (meta.get(a["ticker"]) or {}).get("theme", "")
        keep = []
        for c in strategist[a["ticker"]].get("said") or []:
            # Checked in code, not left to the model (Oct 14: a Tom Lee interview about
            # jobs and crypto was attached to IAU), and each comment shown once a day.
            if c["url"] in shown_urls or not comment_fits(c, a, bm):
                continue
            keep.append(c)
            shown_urls.add(c["url"])
        a["strategist"] = dict(strategist[a["ticker"]], said=keep)
    state["said_shown"] = {"date": today.isoformat(), "urls": sorted(shown_urls)}


def resolve_links(links: list[str], get=None) -> list[str]:
    """Finnhub gives links like finnhub.io/api/news?id=<hash> that redirect to the
    article. The reader should see where a link goes, so emailed links are
    resolved to the article itself. Only for emailed alerts (a few a day), never
    for every headline. Any failure keeps the original link."""
    from desk import ai
    get = get or requests.get
    out = []
    for u in links:
        if "finnhub.io/api/news" in u:
            try:
                r = get(u, allow_redirects=False, timeout=10)
                loc = r.headers.get("Location", "") if 300 <= r.status_code < 400 else ""
                if loc.startswith("http") and ai.is_real_source(loc):
                    u = loc
            except Exception as e:
                log.warning("link not resolved: %s", describe_error(e))
        if u not in out:
            out.append(u)
    return out


NEW_STORY_RULES = {"fast_move", "gap_filled", "gap_failed"}


def asdict_list(flags: list) -> list[dict]:
    return [f if isinstance(f, dict) else asdict(f) for f in flags]


def email_alerts(flags: list[Flag], reads: dict, state: dict, today: date, args,
                 bars: Optional[dict] = None, meta: Optional[dict] = None, client=None) -> dict:
    """Email the sourced, non-NOISE reads, within today's caps, with Chartist levels
    and the Call line. A dry run only previews the email.
    Returns ticker -> yes / held / failed for the Signal Log."""
    from desk import mailer
    fl = [asdict(f) for f in flags]
    pinged: dict[str, str] = {}
    if getattr(args, "opening", False):
        digest = mailer.pick_digest(fl, reads)
        if digest:
            for a in digest:
                a["links"] = resolve_links(a["links"])
                a["name"] = (meta or {}).get(a["ticker"], {}).get("name", "")
            themes = {t: r.get("theme", "") for t, r in (meta or {}).items()}
            from desk import macro
            backdrop = macro.backdrop_line(macro_for(state, today, bars or {}, getattr(args, "fetch_fn", None)))
            read = strategist_market_for(state, today, backdrop, client)
            subject, body, body_html = mailer.compose_digest(digest, themes, backdrop, read)
            if args.dry_run:
                args.digest = {"subject": subject, "body": body}
            else:
                ok = mailer.send(subject, body, html_body=body_html)
                if not ok:
                    args.email_failed = True
            done = {a["ticker"] for a in digest}
            for t in done:
                pinged[t] = "digest" if (args.dry_run or ok) else "failed"
            fl = [f for f in fl if not (f["horizon"] == "SHORT" and f["ticker"] in done)]
            prev = state.get("digest") or {}
            earlier = prev.get("tickers", []) if prev.get("date") == today.isoformat() else []
            state["digest"] = {"date": today.isoformat(), "tickers": sorted(set(earlier) | done)}
    # A name already in today's digest is not alerted again for the same story
    # (Oct 14: NEM in the 10:06 digest, then alone at 10:16 for its volume).
    # Only something new about it gets through: a fast move or a gap reversal.
    in_digest = emailed_today(state, today)
    if in_digest and not getattr(args, "opening", False):
        before = len(fl)
        fl = [f for f in fl if not (f["horizon"] == "SHORT" and f["ticker"] in in_digest
                                    and f["rule"] not in NEW_STORY_RULES)]
        for f in [f for f in asdict_list(flags) if f["ticker"] in in_digest and f["horizon"] == "SHORT"]:
            pinged.setdefault(f["ticker"], "in digest")
        if len(fl) < before:
            log.info("%d hit(s) on names already in today's digest not emailed again", before - len(fl))
    send, held = mailer.pick_alerts(fl, reads, state, today)
    pinged.update({a["ticker"]: "held" for a in held})
    if not send:
        return pinged
    for a in send:
        a["links"] = resolve_links(a["links"])
    add_levels(send, bars or {}, meta or {}, state, today, client)
    for a in send:   # each side's vote, for the Signal Log
        reads.setdefault(a["ticker"], {})["votes"] = {
            "call": a.get("call", ""), "chartist": (a.get("chartist") or {}).get("action", ""),
            "strategist": (a.get("strategist") or {}).get("action", "")}
    subject, body = mailer.compose(send)
    if args.dry_run:
        args.email = {"subject": subject, "body": body, "held": sorted(pinged)}
        return pinged
    ok = mailer.send(subject, body, html_body=mailer.compose_html(send))
    for a in send:
        pinged[a["ticker"]] = "yes" if ok else "failed"
    if ok:
        mark_emailed(state, today, [a["ticker"] for a in send if a["horizon"] == "SHORT"])
    if not ok:
        mailer.refund(state, send)   # failed alerts do not use up today's cap
        args.email_failed = True
    return pinged


def vip_recent(state: dict, today: str) -> list[str]:
    """Headlines emailed or held today and on the previous run day."""
    v = state.get("vip_sent") or {}
    if v.get("date") == today:
        return v.get("headlines", []) + v.get("before", [])
    return v.get("headlines", [])          # the last run day's become "before" on the next save


EARNINGS_WINDOW_DAYS = 3   # trading days after a report, including the report day


def earnings_window(state: dict, today: date) -> set[str]:
    """Names that reported in the last EARNINGS_WINDOW_DAYS trading days (or report
    today). Their VIP news is not held by the daily cap.
    Oct 20: SPCX reported with the cap used by 10:16 PT on repeats.
    Oct 22: BofA's SpaceX target raise was held: the report was two days old, and the
    earnings map (next dates only) had already dropped SPCX. Report dates are now
    remembered in state["reported"] once seen."""
    m = (state.get("earnings") or {}).get("map", {})
    rep = dict(state.get("reported") or {})
    for t, d in m.items():
        if d <= today.isoformat():
            rep[t] = d
    days, d = [], today
    while len(days) < EARNINGS_WINDOW_DAYS:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= timedelta(days=1)
    state["reported"] = {t: x for t, x in rep.items() if x >= days[-1]}
    return {t for t, x in state["reported"].items() if x in days}


def send_vip(items: list[dict], state: dict, names: dict, args) -> str:
    """Email VIP news and fast moves within the daily VIP cap. Returns the Pinged value."""
    from desk import mailer
    hot = earnings_window(state, datetime.now(ET).date())
    force = any(i["ticker"] in hot for i in items)   # other items ride along in the same email
    if force:
        log.info("VIP: earnings window for %s, daily cap not applied", ",".join(sorted(hot)))
    k = mailer.take_vip(state, datetime.now(ET).date(), len(items), force=force)
    if k < len(items):
        log.info("VIP: %d item(s) logged, not emailed (%s)", len(items) - k,
                 "daily VIP email cap reached" if not k else f"at most {k} per email")
    if not args.dry_run:
        # What went out, or was held, today: the filter drops repeats of either, so a
        # held story is not held again from another outlet (Oct 19: "Starlink a
        # must-have for airlines" was held at 9:31 and again at 13:30).
        today = datetime.now(ET).date().isoformat()
        prev = state.get("vip_sent") or {}
        same_day = prev.get("date") == today
        heads = prev.get("headlines", []) if same_day else []
        before = prev.get("before", []) if same_day else prev.get("headlines", [])
        heads += [f"{i['ticker']}: {i.get('headline', '')}" for i in items if i.get("type") == "news"]
        state["vip_sent"] = {"date": today, "headlines": heads[-40:], "before": before[-40:]}
    if not k:
        return "held"
    subject, body, body_html = mailer.compose_vip(items[:k], names)
    if args.dry_run:
        print("\n===== VIP EMAIL PREVIEW (dry run, not sent) =====\nSubject: " + subject + "\n\n" + body)
        return "yes"
    if mailer.send(subject, body, html_body=body_html):
        return "yes"
    args.email_failed = True
    return "failed"


# ---------------------------------------------------------------- modes
def run_scan(args) -> list[Flag]:
    now_et = datetime.now(ET)
    if not args.force and (now_et.weekday() >= 5 or not
                           (now_et.replace(hour=9, minute=30) <= now_et <= now_et.replace(hour=16, minute=5))):
        log.info("scan: outside US session, exiting")
        return []

    sheet = Sheet()
    universe = sheet.universe()
    tickers = [r["ticker"] for r in universe]
    meta = {r["ticker"]: r for r in universe}
    state = sheet.load_state()

    bars = fetch_daily(tickers)
    bars, stale = split_stale(bars, now_et)
    if stale:
        sample = ", ".join(f"{t} ({a} < {b})" for t, (a, b) in list(stale.items())[:5])
        log.warning("stale data skipped for %d ticker(s): %s", len(stale), sample)
    if stale and len(stale) >= len(bars):
        # Most of the market data is old: alerting on it would announce old moves as new.
        raise SystemExit(f"Market data is stale for {len(stale)} of {len(stale) + len(bars)} tickers "
                         f"(e.g. {sample}). No hits evaluated, nothing sent.")
    earn = earnings_dates(tickers, state)

    # Preview: a dry run that pretends nothing has fired today, to see the full email.
    preview = args.dry_run and getattr(args, "ignore_cooldowns", False)
    if preview:
        state.pop("email_budget", None)
        log.info("preview: cooldowns and today's email cap ignored (dry run, nothing written)")

    new: list[Flag] = []
    for t in tickers:
        if t not in bars:
            continue
        for f in evaluate(t, bars[t], meta[t], now_et, earn.get(t)):
            cd = CFG["LONG_COOLDOWN_DAYS"] if f.horizon == "LONG" else CFG["SHORT_COOLDOWN_DAYS"]
            if f.rule == "earnings_soon":
                cd = CFG["EARNINGS_DAYS"] + 2
            k = f"{t}|{f.rule}"
            if preview or cooled_down(state, k, date.fromisoformat(f.bar_date), cd):
                new.append(f)
                if not args.dry_run:
                    mark_fired(state, k, f.bar_date)

    # Fast moves: something just happened. Price is the trigger, the Tape Reader
    # finds the why. VIP names have their own 15-minute check, so they are skipped.
    live = now_et.weekday() < 5 and minutes_since_open(now_et) >= CFG["MIN_MINUTES_AFTER_OPEN"] \
        and now_et <= now_et.replace(hour=16, minute=5)
    if live:
        vips = {t for t, r in meta.items() if r.get("priority", "").upper() == "VIP"}
        try:
            intraday = (getattr(args, "intraday_fn", None) or fetch_intraday)(
                [t for t in bars if t not in vips])
        except Exception as e:
            log.warning("intraday data failed: %s", describe_error(e))
            intraday = {}
        for t, (move, last) in fast_moves(intraday, CFG["MOVE_WINDOW_MIN"], CFG["MOVE_PCT"]).items():
            k = f"{t}|fast_move"
            if preview or cooled_down_min(state, k, datetime.now(PT), CFG["MOVE_COOLDOWN_MIN"]):
                rvol, chg = day_stats(bars[t], now_et)
                new.append(Flag(t, "SHORT", "fast_move",
                                f"{move:+.1f}% in the last {CFG['MOVE_WINDOW_MIN']} min", last, rvol, chg,
                                now_et.date().isoformat(), move))
                if not args.dry_run:
                    mark_fired(state, k, datetime.now(PT).isoformat())
        # Day move: a name drifting well beyond its normal range that nothing else has
        # mentioned today. Scaled to the name, so high-beta names need a bigger move.
        flagged = {f.ticker for f in new}
        for t in bars:
            if t in flagged or t in vips or (not preview and mentioned_today(state, t, now_et.date())):
                continue
            rvol, chg = day_stats(bars[t], now_et)
            normal = normal_day_pct(bars[t])
            if chg is None or normal is None:
                continue
            need = max(CFG["MOVE_PCT"], CFG["DAY_MOVE_ATR"] * normal)
            if abs(chg) >= need:
                new.append(Flag(t, "SHORT", "day_move",
                                f"{chg:+.1f}% on the day, normal range {normal:.1f}%",
                                round(float(bars[t]["Close"].dropna().iloc[-1]), 2), rvol, chg,
                                now_et.date().isoformat(), round(abs(chg) / normal, 1)))
                if not args.dry_run:
                    mark_fired(state, f"{t}|day_move", now_et.date().isoformat())
    # Before 10:15 ET, SHORT hits are the open's gaps and breaks: one digest email,
    # outside the daily caps, so the caps are kept for live moves later in the day.
    args.opening = live and minutes_since_open(now_et) < CFG["DIGEST_UNTIL_MIN"]

    log.info("scan: %d names, %d new hits", len(bars), len(new))
    reads = read_tape(new, state, now_et.date(), dry_run=args.dry_run) if new else {}
    args.reads = reads
    pinged = email_alerts(new, reads, state, now_et.date(), args, bars, meta)
    if new and not args.dry_run:
        sheet.append_log(new, reads, pinged)
    log_cost(state, now_et.date(), args.dry_run)
    if not args.dry_run:
        sheet.save_state(state)
    if getattr(args, "email_failed", False):
        # Fail loudly: GitHub emails the owner. Hits and reads are already in the Signal Log.
        raise SystemExit("Alert email failed to send; see the 'email' line above. "
                         "Hits are logged with Pinged = failed.")
    return new


def load_vip_config() -> dict:
    if VIP_CONFIG_FILE.exists():
        return json.loads(VIP_CONFIG_FILE.read_text())
    return DEFAULT_VIP_CONFIG


def vip_words(cfg: dict, ticker: str) -> list[str]:
    """The name's own topics plus words for material events at any company (Oct 21:
    "Tesla and SpaceX form special committees to weigh a merger" matched no keyword,
    so the day's biggest story reached the team only through the opening digest).
    The VIP filter still drops what is not material."""
    return cfg.get("keywords", {}).get(ticker, []) + cfg.get("material_all", [])


def vip_reject(cfg: dict, ticker: str, headline: str, url: str) -> str:
    """Why a VIP headline is not about the company, or is opinion; '' to keep it.
    Oct 28: 10 VIP items reached the team and 3 were worth reading. A market wrap
    ("Nasdaq 100 climbs ... Boeing sinks") and a Google earnings preview matched
    on their summaries, an Amazon/Globalstar story named only Musk, and two were
    opinion columns. The headline must now name the company or one of its products,
    and known opinion sources are skipped; the VIP filter still judges the rest."""
    names = cfg.get("subject", {}).get(ticker)
    if names and not keyword_hit(headline, names):
        return "headline does not name the company"
    if any(s in (url or "").lower() for s in cfg.get("opinion_sources", [])):
        return "opinion source"
    return ""


def keyword_hit(text: str, words: list[str]) -> list[str]:
    """A keyword must start a word ('board' does not match 'onboard'); endings are
    allowed ('launch' matches 'launches'). Short words like SEC, CEO and Q3 must be
    whole words, so 'SEC' does not match 'second' or 'sector'."""
    low = text.lower()
    def pat(w):
        end = r"(?![a-z0-9])" if len(w) <= 3 else ""
        return rf"(?<![a-z0-9]){re.escape(w.lower())}{end}"
    return [w for w in words if re.search(pat(w), low)]


def run_vip(args) -> list[dict]:
    now_pt = datetime.now(PT)
    if not args.force and (now_pt.weekday() >= 5 or not (5 <= now_pt.hour < 17)):
        log.info("vip: outside 5:00-17:00 PT weekdays, exiting")
        return []

    sheet = Sheet()
    vips = [r["ticker"] for r in sheet.universe() if r.get("priority", "").upper() == "VIP"]
    if not vips:
        log.info("vip: no VIP names in Universe")
        return []
    cfg = load_vip_config()
    state = sheet.load_state()
    seen = set(state.get("seen", []))
    primed = state.get("vip_primed", False)
    items: list[dict] = []

    # 1) Finnhub company news
    key = env("FINNHUB_KEY")
    if key:
        d_to = datetime.now(ET).date()
        d_from = d_to - timedelta(days=1)
        for t in vips:
            if "." in t:
                continue
            try:
                r = requests.get("https://finnhub.io/api/v1/company-news",
                                 params={"symbol": t, "from": d_from.isoformat(),
                                         "to": d_to.isoformat(), "token": key}, timeout=15)
                for n in (r.json() if r.ok else []):
                    nid = f"fh:{n.get('id')}"
                    if nid in seen:
                        continue
                    seen.add(nid)
                    hits = keyword_hit(f"{n.get('headline', '')} {n.get('summary', '')}",
                                       vip_words(cfg, t))
                    why = vip_reject(cfg, t, n.get("headline", ""), n.get("url", ""))
                    if hits and why:
                        log.info("vip: %s skipped (%s): %s", t, why, str(n.get("headline", ""))[:90])
                    if hits and not why:
                        items.append({"ticker": t, "type": "news", "headline": n.get("headline"),
                                      "source": n.get("source"), "url": n.get("url"),
                                      "time_utc": datetime.utcfromtimestamp(n.get("datetime", 0)).isoformat(),
                                      "keywords": hits})
            except Exception as e:
                log.warning("finnhub news %s: %s", t, describe_error(e))
    else:
        log.warning("FINNHUB_KEY not set; news check skipped")

    # 2) X recent search (optional; needs an X API bearer token)
    bearer = env("X_BEARER_TOKEN")
    if bearer:
        since = state.setdefault("x_since_id", {})
        for t in vips:
            accts = cfg["x_accounts"].get(t, [])
            words = cfg["keywords"].get(t, [])
            if not accts or not words:
                continue
            q_from = " OR ".join(f"from:{a}" for a in accts)
            q_kw = " OR ".join(f'"{w}"' if " " in w else w for w in words)
            query = f"({q_from}) ({q_kw}) -is:retweet"[:512]
            params = {"query": query, "max_results": 20,
                      "tweet.fields": "created_at,author_id", "expansions": "author_id",
                      "user.fields": "username"}
            if since.get(t):
                params["since_id"] = since[t]
            try:
                r = requests.get("https://api.x.com/2/tweets/search/recent", params=params,
                                 headers={"Authorization": f"Bearer {bearer}"}, timeout=15)
                if not r.ok:
                    log.warning("x search %s: %s %s", t, r.status_code, r.text[:200])
                    continue
                js = r.json()
                users = {u["id"]: u["username"] for u in js.get("includes", {}).get("users", [])}
                for tw in js.get("data", []):
                    tid = f"x:{tw['id']}"
                    if tid in seen:
                        continue
                    seen.add(tid)
                    user = users.get(tw.get("author_id"), "")
                    items.append({"ticker": t, "type": "x_post", "headline": tw.get("text", "")[:280],
                                  "source": f"@{user}", "url": f"https://x.com/{user}/status/{tw['id']}",
                                  "time_utc": tw.get("created_at"),
                                  "keywords": keyword_hit(tw.get("text", ""), words)})
                if js.get("meta", {}).get("newest_id"):
                    since[t] = js["meta"]["newest_id"]
            except Exception as e:
                log.warning("x search %s: %s", t, e)

    # 3) Fast price move
    now_et = datetime.now(ET)
    if now_et.weekday() < 5 and now_et.replace(hour=9, minute=30) <= now_et <= now_et.replace(hour=16):
        try:
            import yfinance as yf

            df = yf.download(vips, period="1d", interval="1m", group_by="ticker",
                             progress=False, auto_adjust=False)
            for t in vips:
                sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                closes = sub["Close"].dropna()
                w = CFG["VIP_MOVE_WINDOW_MIN"]
                if len(closes) <= w:
                    continue
                move = (float(closes.iloc[-1]) / float(closes.iloc[-1 - w]) - 1) * 100
                k = f"{t}|vip_move"
                last = state.get("fired", {}).get(k)
                ok = (not last or (datetime.now(PT) - datetime.fromisoformat(last)).total_seconds()
                      / 60 >= CFG["VIP_MOVE_COOLDOWN_MIN"])
                if abs(move) >= CFG["VIP_MOVE_PCT"] and ok:
                    items.append({"ticker": t, "type": "price_move",
                                  "headline": f"{move:+.1f}% in {w} min, last {float(closes.iloc[-1]):.2f}",
                                  "source": "yfinance 1m", "url": "", "time_utc": datetime.utcnow().isoformat()})
                    if not args.dry_run:
                        mark_fired(state, k, datetime.now(PT).isoformat())
        except Exception as e:
            log.warning("vip price check: %s", e)

    if not primed:
        # First run: remember what already exists, alert on nothing.
        log.info("vip: first run, priming seen items (%d), no alerts sent", len(seen))
        items = [i for i in items if i["type"] == "price_move"]
        state["vip_primed"] = True

    log.info("vip: %d new material-candidate items", len(items))
    if items and (env("ANTHROPIC_API_KEY") or getattr(args, "client", None)):
        from desk import ai
        items = ai.vip_filter(items, client=getattr(args, "client", None),
                              take_budget=lambda: ai.take_budget(state, datetime.now(ET).date()),
                              sent_today=vip_recent(state, datetime.now(ET).date().isoformat()),
                              names={r["ticker"]: r.get("name", "") for r in sheet.universe()})
    for i in items:
        if i.get("url"):
            i["url"] = resolve_links([i["url"]])[0]
    pinged = send_vip(items, state, {r["ticker"]: r.get("name", "") for r in sheet.universe()}, args) \
        if items else ""
    log_cost(state, datetime.now(ET).date(), args.dry_run)   # the VIP filter's calls count too
    if not args.dry_run:
        state["seen"] = list(seen)[-3000:]
        if items:
            sheet.append_log([Flag(i["ticker"], "VIP", i["type"], (i["headline"] or "")[:200], 0.0)
                              for i in items], {}, {i["ticker"]: pinged for i in items})
        sheet.save_state(state)
    if getattr(args, "email_failed", False):
        raise SystemExit("VIP email failed to send; see the 'email' line above.")
    return items


def market_of(ticker: str, bm: dict) -> str:
    return bm.get("market", {}).get("CA" if ticker.upper().endswith((".TO", ".V", ".NE")) else "US",
                                    "XIU.TO" if ticker.upper().endswith(".TO") else "SPY")


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def grade_rows(values: list[list[str]], bars: dict, bm: dict) -> list[tuple[int, dict]]:
    """Fill Ret5d/Ret20d (from the logged price: what acting on the alert earned) and
    Ex5d/Ex20d (alert day's close to N sessions later, minus the market over the same
    sessions). Cells already filled are left alone. VIP rows have no price."""
    header = values[0]
    ix = {c: header.index(c) for c in header}
    out = []
    for i, row in enumerate(values[1:], start=1):
        row = row + [""] * (len(LOG_COLUMNS) - len(row))
        get = lambda c: row[ix[c]] if c in ix else ""
        if get("Horizon") == "VIP":
            continue
        t, base = get("Ticker"), to_float(get("Price"))
        if t not in bars or not base:
            continue
        closes = bars[t]["Close"].dropna()
        pos = closes.index.searchsorted(pd.Timestamp(get("Timestamp")[:10]))
        if pos >= len(closes):
            continue
        mk = bars.get(market_of(t, bm))
        cells = {}
        for n in (5, 20):
            if not get(f"Ret{n}d") and pos + n < len(closes):
                cells[f"Ret{n}d"] = _pct(float(closes.iloc[pos + n]) / base - 1)
            if not get(f"Ex{n}d") and pos + n < len(closes) and mk is not None:
                m = mk["Close"].dropna()
                d0, dn = closes.index[pos], closes.index[pos + n]
                if d0 in m.index and dn in m.index:
                    own = float(closes.iloc[pos + n]) / float(closes.iloc[pos]) - 1
                    cells[f"Ex{n}d"] = _pct(own - (float(m[dn]) / float(m[d0]) - 1))
        if cells:
            out.append((i, cells))
    return out


CALL_SIDE = {"Consider adding": "add", "Consider trimming": "trim"}


def summarize(values: list[list[str]], today: date) -> list[list]:
    """The Scorecard tab: how each side's calls did against the market over 5 days.
    One row per name per day (a name logged several times in a day counts once).
    'Right way' means: buying/add beat the market, selling/trim lagged it."""
    header = values[0]
    ix = {c: header.index(c) for c in header}
    seen, groups = set(), {}

    def add(group: str, side: str, ex: float):
        g = groups.setdefault(group, [])
        g.append((side, ex))

    for row in values[1:]:
        row = row + [""] * (len(LOG_COLUMNS) - len(row))
        get = lambda c: row[ix[c]] if c in ix else ""
        ex = to_float(get("Ex5d").rstrip("%"))
        if ex is None or get("Horizon") == "VIP":
            continue
        key = (get("Ticker"), get("Timestamp")[:10])
        if key in seen:
            continue
        seen.add(key)
        emailed = get("Pinged") in ("yes", "digest")
        if f"| {HYPOTHESIS_TAG}" in get("Rule"):
            add("Watching: DeMark buy + volume surge, up day", "add", ex)
        d = get("Direction") or "no read"
        side = {"ACCUMULATION": "add", "DISTRIBUTION": "trim"}.get(d, "")
        add(f"Tape Reader: {d.lower()}, {'emailed' if emailed else 'not emailed'}", side, ex)
        call = get("Call")
        if call:
            add(f"Call: {call}", CALL_SIDE.get(call, ""), ex)
        for lens in ("Chartist", "Strategist"):
            if get(lens):
                add(f"{lens}: {get(lens)}", get(lens) if get(lens) in ("add", "trim") else "", ex)
    rows = [["Group", "Names", "Avg vs market, 5d", "Right way", "Hit rate", f"Updated {today.isoformat()}"]]
    for name in sorted(groups):
        g = groups[name]
        avg = sum(e for _, e in g) / len(g)
        graded = [(s, e) for s, e in g if s]
        right = sum(1 for s, e in graded if (e > 0) == (s == "add"))
        rows.append([name, len(g), f"{avg:+.2f}%",
                     f"{right} of {len(graded)}" if graded else "",
                     f"{right / len(graded) * 100:.0f}%" if graded else "", ""])
    rows.append([])
    rows.append(["Right way: buying or add beat the market over 5 trading days; selling or trim lagged it. "
                 "Watch calls are shown, not graded. Below about 20 names, treat a rate as noise."])
    return rows


# ---------------------------------------------------------------- Ask the desk
def ask_facts(t: str, state: dict, today: date, client=None, fetch_fn=None) -> str:
    """What the answer may use for one name, all computed here: levels, trend,
    momentum and leadership, plus the market backdrop and the dated real comments."""
    from desk import ai
    from desk import levels
    from desk import macro
    lines = []
    if t:
        bars = (fetch_fn or fetch_daily)([t])
        lv = levels.compute_levels(bars[t]) if t in bars else {}
        if lv:
            add_technicals({t: lv}, bars, fetch_fn)
            lvl = "; ".join(f"{k} {v}" for k, v in lv["levels"].items())
            lines.append(f"{t}: last {lv['last']} | daily trend {lv['trend_daily']} | weekly trend "
                         f"{lv['trend_weekly']} | ATR14 {lv.get('atr14')}\n  levels: {lvl}")
            tech = ai.tech_line(lv.get("tech") or {})
            if tech:
                lines.append(f"  momentum and leadership: {tech}")
        else:
            lines.append(f"{t}: no price history available")
    backdrop = macro.backdrop_line(macro_for(state, today, {}, fetch_fn))
    if backdrop:
        lines.append(f"Market backdrop: {backdrop}")
    budget = lambda: ai.take_budget(state, today)
    for c in said_for(state, today, client, budget, fresh=False):
        lines.append(f"Comment: {c['who']}, {c['date']}, {c['where']}: {c['summary']} ({c['url']})")
    return "\n".join(lines)


def run_ask(args) -> list[dict]:
    """Answer replies to desk emails, in the same thread. Off unless ASK_DESK=on."""
    from desk import ai
    from desk import inbox
    from desk import mailer
    if not inbox.enabled():
        log.info("ask: off (set ASK_DESK=on)")
        return []
    sheet = Sheet()
    tickers = [r["ticker"] for r in sheet.universe()]
    state = sheet.load_state()
    today = datetime.now(ET).date()
    imap = getattr(args, "imap", None)
    msgs = inbox.fetch(imap)
    allow, desk = inbox.allowlist(), env("DESK_EMAIL").lower()
    log.info("ask: %d address(es) may ask (domains: %s)", len(allow),
             ", ".join(sorted({a.split("@")[-1] for a in allow})) or "none: set ALERT_TO or ASK_ALLOW")
    b = state.get("ask_budget") or {}
    used = b.get("count", 0) if b.get("date") == today.isoformat() else 0
    seen = list(state.get("ask_seen", []))
    done, out, flags, reads, pinged = [], [], [], {}, {}
    for m in msgs:
        if m["msg_id"] and m["msg_id"] in seen:
            continue                                   # answered on an earlier run
        if m["auto"] or m["sender"] == desk or m["sender"] not in allow or not m["question"]:
            why = ("auto-reply" if m["auto"] else "from the desk" if m["sender"] == desk else
                   "not on the allowlist" if m["sender"] not in allow else "no question text")
            # Domain only: enough to tell a Google notice from a teammate, without
            # putting addresses in the Actions log.
            log.info("ask: skipped (%s) from @%s: %.40s", why, m["sender"].split("@")[-1], m["subject"])
            continue
        if used >= inbox.per_day():
            log.info("ask: daily limit of %d reached; the rest wait for tomorrow", inbox.per_day())
            break
        t = inbox.find_ticker(m["subject"], m["question"], m["quoted"], tickers)
        try:
            facts = ask_facts(t, state, today, getattr(args, "client", None), getattr(args, "fetch_fn", None))
            answer, links = ai.answer_question(m["question"], m["quoted"], facts,
                                               client=getattr(args, "client", None),
                                               take_budget=lambda: ai.take_budget(state, today))
        except Exception as e:
            log.error("ask: answer failed: %s", describe_error(e))
            answer, links = "", []
        if not answer:
            answer = ("The desk could not answer this one reliably from today's data. "
                      "Please check it by hand, or ask again with the ticker named.")
        body = answer + ("\n\nSources: " + "  ".join(links) if links else "") + "\n\n--\n" + mailer.FOOTER
        subject = m["subject"] if m["subject"].lower().startswith("re:") else "Re: " + m["subject"]
        if args.dry_run:
            print(f"\n===== ASK THE DESK REPLY (dry run, not sent) =====\nTo: {m['sender']}\nSubject: {subject}\n\n{body}")
            ok = True
        else:
            ok = mailer.send(subject, body, to=m["sender"], in_reply_to=m["msg_id"], references=m["refs"])
        used += 1
        done.append(m["uid"])
        seen.append(m["msg_id"])
        key = t or "-"
        flags.append(Flag(key, "ASK", "question", m["question"][:200], 0.0))
        reads[key] = {"catalyst": answer[:500], "links": links}
        pinged[key] = "replied" if ok else "failed"
        out.append({"ticker": t, "question": m["question"], "answer": answer, "sent": ok})
    log_cost(state, today, args.dry_run)
    if not args.dry_run:
        state["ask_budget"] = {"date": today.isoformat(), "count": used}
        state["ask_seen"] = seen[-200:]
        inbox.mark_seen(done, imap)
        if flags:
            sheet.append_log(flags, reads, pinged)
        sheet.save_state(state)
    return out


def run_scorecard(args) -> int:
    sheet = Sheet()
    values = sheet.read_log()
    if len(values) < 2:
        log.info("scorecard: log empty")
        return 0
    bm = load_benchmarks()
    header = values[0]
    tcol = header.index("Ticker")
    tickers = sorted({r[tcol] for r in values[1:] if len(r) > tcol and r[tcol]}
                     | set(bm.get("market", {}).values()))
    bars = fetch_daily(tickers, period="6mo")
    updates = grade_rows(values, bars, bm)
    # Apply the grades to the in-memory copy, so the summary includes them.
    col = {c: i for i, c in enumerate(LOG_COLUMNS)}
    values[0] = LOG_COLUMNS
    for i, cells in updates:
        values[i] = values[i] + [""] * (len(LOG_COLUMNS) - len(values[i]))
        for c, v in cells.items():
            values[i][col[c]] = v
    summary = summarize(values, datetime.now(ET).date())
    if not args.dry_run:
        sheet.write_cells(updates)
        sheet.write_summary(summary)
        if len(summary) > 3:
            from desk import mailer
            subject, body, html_body = mailer.compose_scorecard(summary)
            mailer.send(subject, body, html_body=html_body)
    log.info("scorecard: %d rows updated, %d summary groups", len(updates), len(summary) - 3)
    for r in summary[1:-2]:
        log.info("  %s | n=%s | %s | %s", r[0], r[1], r[2], r[3])
    return len(updates)


def run_audit(args) -> list:
    """The daily health check (audit.py), once a day after 16:30 ET, emailed to
    AUDIT_TO (or the first ALERT_TO address). Never to the whole team."""
    from desk import ai
    from desk import audit
    from desk import mailer
    now = datetime.now(ET)
    sheet = Sheet()
    state = sheet.load_state()
    today_pt = datetime.now(PT).date().isoformat()
    if not args.force:
        if now.weekday() >= 5 or (now.hour, now.minute) < (16, 30):
            log.info("audit: runs once after 16:30 ET on weekdays; exiting")
            return []
        if state.get("audit_sent") == today_pt:
            log.info("audit: already sent today")
            return []
    found = audit.checks(sheet.read_log(), state, sheet.universe(), today_pt, ai.DAILY_AI_CALLS, mailer.caps())
    subject, body = audit.compose(found, audit.summary(sheet.read_log(), today_pt), today_pt)
    print(subject + "\n\n" + body)
    to = env("AUDIT_TO") or (mailer.recipients(env("ALERT_TO") or "").split(", ") or [""])[0]
    if not args.dry_run and to:
        if mailer.send(subject, body, to=to):
            state["audit_sent"] = today_pt
            sheet.save_state(state)
    return found


def main() -> None:
    p = argparse.ArgumentParser(description="AI Market Desk scanner")
    p.add_argument("mode", choices=["scan", "vip", "ask", "audit", "scorecard", "rulelab", "email-test"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--ignore-cooldowns", action="store_true",
                   help="dry run only: re-evaluate hits that already fired, to preview the email")
    args = p.parse_args()
    if args.ignore_cooldowns and not args.dry_run:
        p.error("--ignore-cooldowns only works with --dry-run, so it can never send a repeat alert")

    if args.mode == "scan":
        args.reads, args.email, args.digest = {}, None, None
        out = run_scan(args)
        print(json.dumps({"hits": [asdict(f) for f in out], "tape_reader": args.reads}, indent=1))
        if getattr(args, "digest", None):
            print("\n===== OPENING DIGEST PREVIEW (dry run, not sent) =====")
            print("Subject: " + args.digest["subject"] + "\n")
            print(args.digest["body"])
        if args.email:  # dry run: show the email that would have been sent
            print("\n===== EMAIL PREVIEW (dry run, not sent) =====")
            print("Subject: " + args.email["subject"] + "\n")
            print(args.email["body"])
            if args.email["held"]:
                print("Held over today's cap: " + ", ".join(args.email["held"]))
        elif args.dry_run:
            if not getattr(args, "digest", None):
                why = ("no new hits (already fired today; tick 'Preview' to ignore cooldowns)" if not out
                       else "no hit had a sourced ACCUMULATION or DISTRIBUTION read")
                print(f"\n===== NO EMAIL (dry run): {why} =====")
    elif args.mode == "vip":
        print(json.dumps(run_vip(args), indent=1, default=str))
    elif args.mode == "ask":
        print(json.dumps(run_ask(args), indent=1, default=str))
    elif args.mode == "audit":
        run_audit(args)
    elif args.mode == "rulelab":
        from desk import rulelab
        rows = rulelab.run(args)
        print("\n".join(" | ".join(str(x) for x in r) for r in rows[:40]))
    elif args.mode == "email-test":
        from desk import mailer
        # A made-up sample alert, so the test also shows how real alerts look.
        sample = {"ticker": "SAMPLE", "name": "Test only, not a real alert", "horizon": "SHORT",
                  "flags": [{"ticker": "SAMPLE", "horizon": "SHORT", "rule": "breakout_20d", "detail": "",
                             "price": 101.5, "rvol": 2.0, "day_change_pct": 3.0, "ref": 100.0}],
                  "catalyst": "If you can read this, the desk can send email.", "direction": "ACCUMULATION",
                  "confidence": "high", "scope": "company", "call": "Watch, do not chase",
                  "links": ["https://github.com/"],
                  "chartist": {"support": 100.0, "resistance": 105.0, "watch": "Sample levels only."}}
        _, body = mailer.compose([sample])
        ok = mailer.send("AI Market Desk: test email", body, html_body=mailer.compose_html([sample]))
        if not ok:
            raise SystemExit("Test email failed; see the 'email' line above.")
        print("test email sent")
    else:
        print(json.dumps({"updated": run_scorecard(args)}))


if __name__ == "__main__":
    main()
