"""The market backdrop Strategist lens reasons from, computed in code once a day.

S&P 500 trend and drawdown, VIX, the 10-year yield, the dollar, and breadth
(equal-weight vs cap-weight S&P). All from daily bars; no AI, no invented numbers.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

TICKERS = {"spx": "SPY", "vix": "^VIX", "tnx": "^TNX", "dxy": "DX-Y.NYB", "rsp": "RSP"}


def _close(bars: dict, key: str) -> Optional[pd.Series]:
    df = bars.get(TICKERS[key])
    if df is None or "Close" not in df:
        return None
    c = df["Close"].dropna()
    return c if len(c) else None


def _pct(a: float, b: float) -> float:
    return round((a / b - 1) * 100, 1)


def snapshot(bars: dict) -> dict:
    """Facts only. Any input that is missing is simply left out."""
    out: dict = {}
    spx = _close(bars, "spx")
    if spx is not None and len(spx) >= 200:
        last = float(spx.iloc[-1])
        out["spx_vs_200d_pct"] = _pct(last, float(spx.iloc[-200:].mean()))
        out["spx_from_high_pct"] = _pct(last, float(spx.iloc[-252:].max()))
        out["spx_20d_pct"] = _pct(last, float(spx.iloc[-21]))
    vix = _close(bars, "vix")
    if vix is not None and len(vix) >= 6:
        out["vix"] = round(float(vix.iloc[-1]), 1)
        out["vix_5d_chg"] = round(float(vix.iloc[-1] - vix.iloc[-6]), 1)
    tnx = _close(bars, "tnx")
    if tnx is not None and len(tnx) >= 21:
        scale = 10 if float(tnx.iloc[-1]) > 20 else 1        # older feeds quote the yield x10
        out["us10y_pct"] = round(float(tnx.iloc[-1]) / scale, 2)
        out["us10y_20d_bp"] = int(round((float(tnx.iloc[-1]) - float(tnx.iloc[-21])) / scale * 100))
    dxy = _close(bars, "dxy")
    if dxy is not None and len(dxy) >= 21:
        out["dollar_20d_pct"] = _pct(float(dxy.iloc[-1]), float(dxy.iloc[-21]))
    rsp = _close(bars, "rsp")
    if rsp is not None and spx is not None and len(rsp) >= 21 and len(spx) >= 21:
        out["breadth_20d_pct"] = round(_pct(float(rsp.iloc[-1]), float(rsp.iloc[-21]))
                                       - _pct(float(spx.iloc[-1]), float(spx.iloc[-21])), 1)
    return out


def backdrop_line(m: dict) -> str:
    """One plain sentence for the email and the prompt."""
    parts = []
    if "spx_vs_200d_pct" in m:
        v = m["spx_vs_200d_pct"]
        h = abs(m["spx_from_high_pct"])
        parts.append(f"S&P 500 {abs(v):.1f}% {'above' if v >= 0 else 'below'} its 200-day average, "
                     + ("at its 52-week high" if h < 0.5 else f"{h:.1f}% below its 52-week high"))
    if "vix" in m:
        d = m["vix_5d_chg"]
        parts.append(f"VIX {m['vix']:.1f} ({'up' if d >= 0 else 'down'} {abs(d):.1f} in 5 days)")
    if "us10y_pct" in m:
        bp = m["us10y_20d_bp"]
        parts.append(f"10-year yield {m['us10y_pct']:.2f}% ({bp:+d}bp in 20 days)")
    if "dollar_20d_pct" in m:
        parts.append(f"dollar {m['dollar_20d_pct']:+.1f}% in 20 days")
    if "breadth_20d_pct" in m:
        b = m["breadth_20d_pct"]
        parts.append(f"equal-weight S&P {'leading' if b > 0 else 'lagging'} by {abs(b):.1f} points in 20 days")
    return "; ".join(parts) + "." if parts else ""
