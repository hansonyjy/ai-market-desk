"""Price levels for Chartist lens, computed in code from daily bars.

The model never invents a number: it picks its entry and stop from this list,
and ai.chartist_lens() rejects any price that is not near one of them.
Pure functions, no I/O.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd


def _trend(close: float, fast: Optional[float], slow: Optional[float]) -> str:
    if fast is None or slow is None:
        return "unknown"
    if close > fast > slow:
        return "up"
    if close < fast < slow:
        return "down"
    return "range"


def _r(x) -> Optional[float]:
    return None if x is None or pd.isna(x) else round(float(x), 2)


def compute_levels(bars: pd.DataFrame) -> dict:
    """Daily bars (ascending; the last row may be today's partial bar) ->
    trend on daily and weekly, plus named support and resistance levels.
    Levels come from completed bars only, so today's move cannot set them."""
    bars = bars.dropna(subset=["Close"])
    if len(bars) < 22:
        return {}
    done = bars.iloc[:-1]
    last = float(bars["Close"].iloc[-1])
    c = done["Close"]

    sma50 = _r(c.iloc[-50:].mean()) if len(c) >= 50 else None
    sma200 = _r(c.iloc[-200:].mean()) if len(c) >= 200 else None
    weekly = c.resample("W-FRI").last().dropna() if isinstance(c.index, pd.DatetimeIndex) else c.iloc[::5]
    wk10 = _r(weekly.iloc[-10:].mean()) if len(weekly) >= 10 else None
    wk40 = _r(weekly.iloc[-40:].mean()) if len(weekly) >= 40 else None

    tr = pd.concat([done["High"] - done["Low"],
                    (done["High"] - c.shift()).abs(),
                    (done["Low"] - c.shift()).abs()], axis=1).max(axis=1)

    named = {
        "20-day high": _r(done["High"].iloc[-20:].max()),
        "20-day low": _r(done["Low"].iloc[-20:].min()),
        "60-day high": _r(done["High"].iloc[-60:].max()),
        "60-day low": _r(done["Low"].iloc[-60:].min()),
        "50-day average": sma50,
        "200-day average": sma200,
        "prior close": _r(c.iloc[-1]),
        "prior day low": _r(done["Low"].iloc[-1]),
        "prior day high": _r(done["High"].iloc[-1]),
        # So a breakdown or breakout past every shorter level still has one beyond it.
        "52-week low": _r(done["Low"].iloc[-252:].min()),
        "52-week high": _r(done["High"].iloc[-252:].max()),
    }
    return {
        "last": round(last, 2),
        "trend_daily": _trend(last, sma50, sma200),
        "trend_weekly": _trend(last, wk10, wk40),
        "atr14": _r(tr.iloc[-14:].mean()),
        "levels": {k: v for k, v in named.items() if v is not None},
    }


def rsi(close: pd.Series, n: int = 14) -> Optional[float]:
    """Wilder's RSI on closes."""
    d = close.diff().dropna()
    if len(d) < n + 1:
        return None
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    if dn == 0:
        return 100.0
    return round(float(100 - 100 / (1 + up / dn)), 1)


def td_setup(close: pd.Series) -> dict:
    """DeMark TD Sequential setup count. A buy setup counts consecutive closes below
    the close 4 bars earlier; a sell setup, above. A count of 9 completes the setup,
    which Chartist reads as likely exhaustion of the move. Counts past 9 are capped."""
    c = close.dropna().tolist()
    side, count = "", 0
    for i in range(4, len(c)):
        now = "buy" if c[i] < c[i - 4] else "sell" if c[i] > c[i - 4] else ""
        count = count + 1 if now and now == side else (1 if now else 0)
        side = now
    return {"side": side, "count": min(count, 9)} if side else {"side": "", "count": 0}


def td_countdown(bars: pd.DataFrame, stale_bars: int = 10) -> dict:
    """DeMark TD Sequential countdown, simplified. After a buy setup completes (9),
    count bars whose close is at or below the low two bars earlier; 13 completes
    the countdown, which Chartist reads as a stronger exhaustion signal than the 9.
    A sell countdown mirrors it with the high. A completed setup on the other side
    cancels a running countdown. A 13 older than `stale_bars` bars is not reported.
    Returns {"side", "count", "bars_ago"} or {} when no countdown is running."""
    b = bars.dropna(subset=["Close"])
    c, lo, hi = b["Close"].tolist(), b["Low"].tolist(), b["High"].tolist()
    side, run = "", 0              # setup tracking
    cd_side, cd, done_at = "", 0, None
    for i in range(4, len(c)):
        now = "buy" if c[i] < c[i - 4] else "sell" if c[i] > c[i - 4] else ""
        run = run + 1 if now and now == side else (1 if now else 0)
        side = now
        if run == 9:               # a setup completes: start (or restart) its countdown
            cd_side, cd, done_at = side, 0, None
        if cd_side and done_at is None and i >= 2:
            hit = c[i] <= lo[i - 2] if cd_side == "buy" else c[i] >= hi[i - 2]
            if hit:
                cd += 1
                if cd == 13:
                    done_at = i
    if not cd_side or cd == 0:
        return {}
    ago = len(c) - 1 - done_at if done_at is not None else 0
    if done_at is not None and ago > stale_bars:
        return {}
    return {"side": cd_side, "count": cd, "bars_ago": ago}


def breadth(all_bars: dict) -> dict:
    """Share of our own names above their 50-day and 200-day averages: how broad a
    move is across what the team follows. Chartist watches breadth to judge whether
    an index move has sponsorship."""
    a50 = a200 = n50 = n200 = 0
    for df in all_bars.values():
        c = df["Close"].dropna()
        if len(c) >= 50:
            n50 += 1
            a50 += float(c.iloc[-1]) > float(c.iloc[-50:].mean())
        if len(c) >= 200:
            n200 += 1
            a200 += float(c.iloc[-1]) > float(c.iloc[-200:].mean())
    if not n50:
        return {}
    return {"names": n50, "above_50d_pct": round(a50 / n50 * 100),
            "above_200d_pct": round(a200 / n200 * 100) if n200 else None}


def rel_strength(close: pd.Series, bench: Optional[pd.Series], vs: str, n: int = 20) -> Optional[dict]:
    """How the name did against a benchmark over n days: leading, lagging or in line."""
    if bench is None or bench.dropna().empty:
        return None
    a = pd.concat([close.rename("s"), bench.rename("b")], axis=1, join="inner").dropna()
    if len(a) <= n:
        return None
    ratio = a["s"] / a["b"]
    chg = round(float((ratio.iloc[-1] / ratio.iloc[-1 - n] - 1) * 100), 1)
    label = "leading" if chg > 2 else "lagging" if chg < -2 else "in line with"
    return {"vs": vs, "days": n, "chg_pct": chg, "label": label}


def _by_day(s: pd.Series) -> pd.Series:
    """Index by calendar day, so a stock and its ETF line up even if their bar
    timestamps differ (time zones, the live bar)."""
    if isinstance(s.index, pd.DatetimeIndex):
        return s.set_axis(s.index.tz_localize(None).normalize() if s.index.tz is not None else s.index.normalize())
    return s


def technicals(bars: pd.DataFrame, market: Optional[pd.DataFrame] = None, market_name: str = "",
               sector: Optional[pd.DataFrame] = None, sector_name: str = "") -> dict:
    """The facts Chartist lens reasons from, all computed here. Includes today's
    latest price, so a count completing today shows today."""
    c = bars["Close"].dropna()
    if len(c) < 30:
        return {}
    wk = c.resample("W-FRI").last().dropna() if isinstance(c.index, pd.DatetimeIndex) else c.iloc[::5]
    out = {"rsi14": rsi(c), "td_daily": td_setup(c), "td_weekly": td_setup(wk)}
    if {"High", "Low"} <= set(bars.columns):
        cd = td_countdown(bars)
        if cd:
            out["td_countdown"] = cd
    cs = _by_day(c)
    rs = [r for r in (rel_strength(cs, None if sector is None else _by_day(sector["Close"]), sector_name),
                      rel_strength(cs, None if market is None else _by_day(market["Close"]), market_name)) if r]
    out["rel_strength"] = rs
    return out


def near_a_level(price: float, lv: dict, tol_pct: float = 1.0) -> bool:
    """True when price is within tol_pct of a named level. The last price alone is
    not a level (Oct 14: DSG got 'support C$463.98' with the price at C$464.00)."""
    refs = list(lv.get("levels", {}).values())
    return any(r and abs(price / r - 1) * 100 <= tol_pct for r in refs)


def snap(price: float, lv: dict, tol_pct: float = 1.0) -> Optional[float]:
    """The named level a price was meant to be, so the email shows the real level,
    not the model's approximation of it. None when no level is that close."""
    refs = [r for r in lv.get("levels", {}).values() if r]
    best = min(refs, key=lambda r: abs(price / r - 1), default=None)
    return best if best is not None and abs(price / best - 1) * 100 <= tol_pct else None


def within_reach(price: float, lv: dict, atrs: float = 4.0) -> bool:
    """A level more than `atrs` normal days away tells the reader nothing useful
    today (Oct 14: NEM got its 52-week low, 22% below, as support)."""
    last, atr = lv.get("last"), lv.get("atr14")
    if not last or not atr:
        return True
    return abs(price - last) <= atrs * atr
