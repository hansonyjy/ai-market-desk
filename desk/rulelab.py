"""Rule Lab: replay the desk's price rules on years of daily bars and measure what
acting on each one earned against the market. Spec: "Rule Lab and Call v2: build
spec" (Oct 17). No AI calls; run by hand (Actions, mode rulelab).

Signals mirror scanner.evaluate() on completed daily bars; test_rulelab.py checks
them against evaluate() itself, so the test and the desk cannot drift apart.
Entry is the next day's open; returns are against SPY (US) or XIU.TO (TSX) over
the same days. Rules are chosen on the fitting years only; the sealed years are
scored once, for the verdict."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from statistics import mean, median, stdev
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("ai-market-desk")

FIT_START, FIT_END = pd.Timestamp("2021-10-01"), pd.Timestamp("2024-12-31")
SEALED_START = pd.Timestamp("2025-01-01")
HORIZONS = (5, 20, 60)
DEDUP_DAYS = 10                 # one fire per name per rule per 10 trading days
FIT_YEARS = (2022, 2023, 2024)

# Pass bar (spec section 6; decision 2)
MIN_FIRES, MIN_NAMES = 30, 10
EDGE_BAR = 1.0                  # points of 20-day excess beyond the names' own baseline
MIN_T = 2.0                     # and at least 2 standard errors from zero (Oct 17: on random
                                # prices the 1-point bar alone passed ~8 of ~360 groups per run)
PLACEBO_RUNS = 3
SEALED_MIN_FIRES = 10
SEALED_BAR = 0.5                # the sealed years must keep at least half the edge, same sign

# side: what acting on the signal means. add = expect it to beat the market;
# trim = expect it to lag. Up/down variants split rules whose meaning depends on direction.
RULES = {
    "pullback_200dma": "add",
    "gap_filled": "add",
    "gap_down_unfilled": "trim",
    "gap_up": "add",
    "gap_failed": "trim",
    "breakout_20d": "add",
    "breakdown_20d": "trim",
    "rvol_spike_up": "add",
    "rvol_spike_down": "trim",
    "day_move_up": "add",
    "day_move_down": "trim",
}

CFG = {"RVOL_MIN": 2.5, "GAP_MIN_PCT": 3.0, "BREAKOUT_DAYS": 20, "NEAR_200DMA_PCT": 2.0,
       "PULLBACK_FROM_PCT": 5.0, "MOVE_PCT": 3.0, "DAY_MOVE_ATR": 1.5}


def load_cfg() -> None:
    """Use the live thresholds, so the lab tests what the desk runs."""
    from desk import scanner
    for k in CFG:
        if k in scanner.CFG:
            CFG[k] = scanner.CFG[k]


# ---------------------------------------------------------------- signals
def signals(df: pd.DataFrame) -> pd.DataFrame:
    """One row per day, one boolean column per rule, as of that day's close.
    Mirrors scanner.evaluate() with the session over (no time-of-day scaling)."""
    d = df.dropna(subset=["Close"]).copy()
    c, o, h, l, v = d["Close"], d["Open"], d["High"], d["Low"], d["Volume"]
    pc = c.shift(1)
    chg = (c / pc - 1) * 100
    avg20 = v.shift(1).rolling(20).mean()
    rvol = v / avg20.where(avg20 > 0)
    gap = (o / pc - 1) * 100
    n = CFG["BREAKOUT_DAYS"]
    hi, lo = h.shift(1).rolling(n).max(), l.shift(1).rolling(n).min()
    sma200 = c.shift(1).rolling(200).mean()
    dist = (c / sma200 - 1) * 100
    came = c.shift(1).rolling(20).max() > sma200 * (1 + CFG["PULLBACK_FROM_PCT"] / 100)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    normal = (tr.rolling(14).mean() / c * 100).shift(1)      # from completed bars, as live
    need = np.maximum(CFG["MOVE_PCT"], CFG["DAY_MOVE_ATR"] * normal)
    big_gap = gap.abs() >= CFG["GAP_MIN_PCT"]
    spike = rvol >= CFG["RVOL_MIN"]
    moved = chg.abs() >= need
    out = pd.DataFrame(index=d.index)
    out["pullback_200dma"] = (dist.abs() <= CFG["NEAR_200DMA_PCT"]) & came
    out["gap_up"] = big_gap & (gap > 0)
    out["gap_filled"] = big_gap & (gap < 0) & (c >= pc)
    out["gap_down_unfilled"] = big_gap & (gap < 0) & (c < pc)
    out["gap_failed"] = big_gap & (gap > 0) & (c <= pc)
    out["breakout_20d"] = c > hi
    out["breakdown_20d"] = (c < lo) & ~(c > hi)
    out["rvol_spike_up"] = spike & (chg >= 0)
    out["rvol_spike_down"] = spike & (chg < 0)
    out["day_move_up"] = moved & (chg > 0)
    out["day_move_down"] = moved & (chg < 0)
    return out.fillna(False).astype(bool)


# ---------------------------------------------------------------- conditions
def td_series(c: pd.Series) -> pd.DataFrame:
    """Per day: DeMark setup side and count (capped at 9) and the simplified 13
    countdown, computed in one pass. Same rules as levels.td_setup / td_countdown."""
    return _td(c, None, None)


def td_series_hl(df: pd.DataFrame) -> pd.DataFrame:
    return _td(df["Close"], df["Low"], df["High"])


def _td(c: pd.Series, lo: Optional[pd.Series], hi: Optional[pd.Series]) -> pd.DataFrame:
    cl = c.tolist()
    lw = lo.tolist() if lo is not None else cl
    hw = hi.tolist() if hi is not None else cl
    side, run, cd_side, cd, done_at = "", 0, "", 0, None
    rows = []
    for i in range(len(cl)):
        if i >= 4:
            now = "buy" if cl[i] < cl[i - 4] else "sell" if cl[i] > cl[i - 4] else ""
            run = run + 1 if now and now == side else (1 if now else 0)
            side = now
            if run == 9:
                cd_side, cd, done_at = side, 0, None
            if cd_side and done_at is None and i >= 2:
                hit = cl[i] <= lw[i - 2] if cd_side == "buy" else cl[i] >= hw[i - 2]
                if hit:
                    cd += 1
                    if cd == 13:
                        done_at = i
        fresh13 = done_at is not None and i - done_at <= 10
        rows.append((side, min(run, 9), cd_side if cd else "", cd, fresh13))
    return pd.DataFrame(rows, index=c.index, columns=["setup_side", "setup", "cd_side", "cd", "cd13"])


def rsi_series(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def weekly_trend(c: pd.Series) -> pd.Series:
    """Up / down / range from the 10- and 40-week averages of completed weeks, as of
    each day (the current week's partial close is the day's close)."""
    wk = c.resample("W-FRI").last()
    w10 = wk.rolling(10).mean().shift(1).reindex(c.index, method="ffill")
    w40 = wk.rolling(40).mean().shift(1).reindex(c.index, method="ffill")
    out = pd.Series("range", index=c.index)
    out[(c > w10) & (w10 > w40)] = "up"
    out[(c < w10) & (w10 < w40)] = "down"
    out[w10.isna() | w40.isna()] = "unknown"
    return out


def conditions(df: pd.DataFrame, sector: Optional[pd.Series], spy: Optional[pd.Series],
               breadth50: Optional[pd.Series]) -> pd.DataFrame:
    """The section-4 conditions, each as a label known at that day's close."""
    c = df["Close"]
    td = td_series_hl(df)
    out = pd.DataFrame(index=df.index)
    out["weekly_trend"] = weekly_trend(c)
    exh = pd.Series("neither", index=df.index)
    exh[(td["setup_side"] == "sell") & (td["setup"] == 9)] = "sell 9"
    exh[(td["setup_side"] == "buy") & (td["setup"] == 9)] = "buy 9"
    exh[(td["cd_side"] == "sell") & td["cd13"]] = "sell 13"
    exh[(td["cd_side"] == "buy") & td["cd13"]] = "buy 13"
    out["demark"] = exh.where(~exh.isin(["buy 9", "buy 13"]), "buy 9 or 13") \
                       .where(lambda s: ~s.isin(["sell 9", "sell 13"]), "sell 9 or 13")
    r = rsi_series(c)
    out["rsi"] = pd.cut(r, [-1, 30, 70, 101], labels=["under 30", "30 to 70", "over 70"]).astype(str)
    if sector is not None:
        ratio = c / sector.reindex(c.index).ffill()
        chg = (ratio / ratio.shift(20) - 1) * 100
        out["vs_sector"] = np.select([chg > 2, chg < -2], ["leading", "lagging"], "in line")
        out.loc[chg.isna(), "vs_sector"] = "unknown"
    else:
        out["vs_sector"] = "no sector ETF"
    if spy is not None:
        s = spy.reindex(c.index).ffill()
        out["backdrop"] = np.where(s > s.rolling(200).mean(), "S&P above 200-day", "S&P below 200-day")
    if breadth50 is not None:
        b = breadth50.reindex(c.index)
        out["breadth"] = np.where(b >= 50, "breadth 50% or more", "breadth under 50%")
    return out


def breadth_series(all_bars: dict) -> pd.Series:
    """Share of our names above their 50-day average, per day."""
    above = []
    for df in all_bars.values():
        c = df["Close"].dropna()
        above.append((c > c.rolling(50).mean()).astype(float).where(c.rolling(50).count() == 50))
    m = pd.concat(above, axis=1)
    cnt = m.count(axis=1)
    return (m.sum(axis=1) / cnt.replace(0, np.nan) * 100)


# ---------------------------------------------------------------- outcomes
def forward_excess(df: pd.DataFrame, bench: pd.DataFrame, h: int) -> pd.Series:
    """Per signal day t: return from the open of t+1 to the close of t+h, minus the
    benchmark's over the same days, in points."""
    o, c = df["Open"], df["Close"]
    own = c.shift(-h) / o.shift(-1) - 1
    bo = bench["Open"].reindex(df.index)
    bc = bench["Close"].reindex(df.index)
    mkt = bc.shift(-h) / bo.shift(-1) - 1
    return (own - mkt) * 100


@dataclass
class Fire:
    ticker: str
    rule: str
    day: pd.Timestamp
    ex: dict          # horizon -> excess (points), None if not yet known
    adj20: Optional[float]
    cond: dict
    adjs: dict = None   # group key -> edge against days with the same condition(s) in this name


WARNED: set = set()


def label(v) -> str:
    """Every condition value as a text label; blanks (Oct 18: a missing value in real
    data broke the sort) become 'unknown'."""
    if v is None or (isinstance(v, float) and math.isnan(v)) or str(v) in ("nan", "None", ""):
        return "unknown"
    return str(v)


def dedup(days: list, gap: int = DEDUP_DAYS, index: Optional[pd.Index] = None) -> list:
    """Keep a fire only if the last kept one is at least `gap` trading days earlier."""
    kept, last = [], None
    pos = {d: i for i, d in enumerate(index)} if index is not None else None
    for d in days:
        p = pos[d] if pos is not None else d
        if last is None or p - last >= gap:
            kept.append(d)
            last = p
    return kept


def collect(bars: dict, bench_of, sector_of, spy: Optional[pd.DataFrame], breadth50,
            placebo_seed: Optional[int] = None) -> list[Fire]:
    """Every deduplicated fire of every rule in every name, with outcomes and conditions.
    placebo_seed: replace each rule's fire days with as many random days in the same
    name, keeping the conditions of those days. No rule can have an edge then, so any
    Pass is luck: that count is the bar the real run has to clear."""
    rng = np.random.default_rng(placebo_seed) if placebo_seed is not None else None
    fires = []
    for t, df in bars.items():
        df = df.dropna(subset=["Close"])
        df = df[~df.index.duplicated(keep="last")].sort_index()   # Yahoo sometimes repeats a day
        bench = bench_of(t)
        if bench is None or len(df) < 260:
            continue
        sig = signals(df)
        ex = {h: forward_excess(df, bench, h) for h in HORIZONS}
        sec = sector_of(t)
        cond = conditions(df, None if sec is None else sec["Close"],
                          None if spy is None else spy["Close"], breadth50)
        # Baselines: the name's average 20-day excess on ANY day, and on any day with
        # the same condition label(s). A group's edge is measured against days that
        # share its condition, so it shows what the signal adds, not what the regime does.
        daily = cond.apply(lambda col: col.map(label))
        daily["ex20"] = ex[20]
        daily["period"] = np.where(daily.index <= FIT_END, "fit", "sealed")
        daily = daily[(daily.index >= FIT_START) & daily["ex20"].notna()]
        base = daily.groupby("period")["ex20"].mean().to_dict()
        base1 = {c: daily.groupby(["period", c])["ex20"].mean().to_dict() for c in CONDITIONS if c in daily}
        base2 = {c: daily.groupby(["period", "weekly_trend", c])["ex20"].mean().to_dict()
                 for c in CONDITIONS[1:] if c in daily}
        for rule in RULES:
            days = [d for d in sig.index[sig[rule]] if d >= FIT_START]
            kept = dedup(days, index=df.index)
            if rng is not None and kept:
                pool = df.index[df.index >= FIT_START]
                kept = sorted(rng.choice(pool, size=min(len(kept), len(pool)), replace=False))
                kept = [pd.Timestamp(d) for d in kept]
            for d in kept:
                e = {h: (None if pd.isna(ex[h].get(d)) else float(ex[h][d])) for h in HORIZONS}
                per = "fit" if d <= FIT_END else "sealed"
                raw = cond.loc[d].to_dict()
                for k, v in raw.items():
                    if not isinstance(v, str) and k not in WARNED:
                        WARNED.add(k)
                        log.warning("rulelab: condition %s had a %s value (%r) for %s on %s; treated as unknown",
                                    k, type(v).__name__, v, t, d.date())
                cd = {k: label(v) for k, v in raw.items()}
                b = base.get(per)
                adj = None if e[20] is None or b is None else e[20] - b
                adjs = {"all": adj}
                if e[20] is not None:
                    for c, m in base1.items():
                        v = m.get((per, cd.get(c)))
                        adjs[c] = None if v is None else e[20] - v
                    for c, m in base2.items():
                        v = m.get((per, cd.get("weekly_trend"), cd.get(c)))
                        adjs[f"weekly_trend + {c}"] = None if v is None else e[20] - v
                fires.append(Fire(t, rule, d, e, adj, cd, adjs))
    return fires


# ---------------------------------------------------------------- scoring
def score(fs: list[Fire], side: str, months: float, key: str = "all") -> dict:
    """The section-5 measures for one group of fires (one period). key: which baseline
    the edge is measured against ("all", or the group's condition)."""
    fs = [Fire(f.ticker, f.rule, f.day, f.ex, (f.adjs or {}).get(key, f.adj20), f.cond, f.adjs) for f in fs]
    f20 = [f for f in fs if f.ex[20] is not None]
    if not f20:
        return {"n": 0}
    ex20 = [f.ex[20] for f in f20]
    ex60 = [f.ex[60] for f in fs if f.ex[60] is not None]
    adj = [f.adj20 for f in f20 if f.adj20 is not None]
    right = (lambda e: e > 0) if side == "add" else (lambda e: e < 0)
    good = [e for e in ex20 if right(e)]
    bad = [e for e in ex20 if not right(e)]
    loss = mean(map(abs, bad)) if bad else 0.0
    payoff = mean(map(abs, good)) / loss if good and loss > 0 else None
    by_name = {}
    for f in f20:
        if f.adj20 is not None:
            by_name.setdefault(f.ticker, []).append(f.adj20)
    best = max(by_name, key=lambda k: sum(by_name[k]) * (1 if side == "add" else -1)) if by_name else None
    without = [f.adj20 for f in f20 if f.adj20 is not None and f.ticker != best]
    years = {}
    for f in f20:
        if f.adj20 is not None and f.day.year in FIT_YEARS:
            years.setdefault(f.day.year, []).append(f.adj20)
    return {"n": len(f20), "names": len({f.ticker for f in f20}),
            "per_month": len(f20) / months if months else None,
            "avg20": mean(ex20), "med20": median(ex20), "avg60": mean(ex60) if ex60 else None,
            "adj20": mean(adj) if adj else None, "med_adj20": median(adj) if adj else None,
            "t": clustered_t([f for f in f20 if f.adj20 is not None]),
            "right": len(good) / len(ex20), "payoff": payoff, "worst20": min(ex20) if side == "add" else max(ex20),
            "adj_wo_best": mean(without) if without else None,
            "years_right": sum(1 for v in years.values() if (mean(v) > 0 if side == "add" else mean(v) < 0))}


def clustered_t(fs: list) -> float:
    """t-statistic of the edge with fires grouped by calendar month: fires in the same
    month share the same market move, so they are not independent evidence. Counting
    them one by one overstates confidence (Oct 17: random prices still passed ~5
    groups a run). Needs at least 6 months with fires."""
    by_month = {}
    for f in fs:
        by_month.setdefault((f.day.year, f.day.month), []).append(f.adj20)
    cm = [mean(v) for v in by_month.values()]
    if len(cm) < 6 or stdev(cm) == 0:
        return 0.0
    return mean(cm) / (stdev(cm) / math.sqrt(len(cm)))


def verdict(fit: dict, sealed: dict, side: str) -> tuple[str, str]:
    """Pass only if every section-6 check holds. Returns (verdict, reason)."""
    sgn = 1 if side == "add" else -1
    if fit.get("n", 0) < MIN_FIRES or fit.get("names", 0) < MIN_NAMES:
        return "Not enough data", f"{fit.get('n', 0)} fires in {fit.get('names', 0)} names (need {MIN_FIRES} in {MIN_NAMES})"
    if fit["adj20"] is None or sgn * fit["adj20"] < EDGE_BAR or sgn * (fit["med_adj20"] or 0) <= 0:
        return "Fail", f"edge {fit['adj20']:+.2f} pts vs baseline (need {sgn * EDGE_BAR:+.1f}, median same way)"
    if sgn * fit["t"] < MIN_T:
        return "Fail", f"could be luck: t = {fit['t']:+.1f} (need {sgn * MIN_T:+.1f})"
    if fit["adj_wo_best"] is None or sgn * fit["adj_wo_best"] < EDGE_BAR:
        return "Fail", "edge depends on one name"
    if fit["years_right"] < 2:
        return "Fail", f"right in {fit['years_right']} of 3 years"
    if sealed.get("n", 0) < SEALED_MIN_FIRES:
        return "Not enough data", f"only {sealed.get('n', 0)} fires in the sealed years"
    if sealed["adj20"] is None or sgn * sealed["adj20"] < SEALED_BAR:
        return "Fail", f"sealed years edge {sealed['adj20']:+.2f} pts (need {sgn * SEALED_BAR:+.1f})"
    return "Pass", f"edge {fit['adj20']:+.2f} pts fit, {sealed['adj20']:+.2f} sealed"


CONDITIONS = ("weekly_trend", "demark", "rsi", "vs_sector", "backdrop", "breadth")


def groups(fires: list[Fire]):
    """(rule, condition, group, fires): each rule alone, then split by one condition,
    then by weekly trend plus one other (at most 2 conditions at a time)."""
    by_rule = {}
    for f in fires:
        by_rule.setdefault(f.rule, []).append(f)
    for rule, fs in by_rule.items():
        yield rule, "all", "all", fs
        for cnd in CONDITIONS:
            labels = sorted({label(f.cond.get(cnd)) for f in fs if cnd in f.cond})
            for lab in labels:
                yield rule, cnd, lab, [f for f in fs if label(f.cond.get(cnd)) == lab]
        for cnd in CONDITIONS[1:]:
            for wt in ("up", "down"):
                labels = sorted({label(f.cond.get(cnd)) for f in fs if f.cond.get("weekly_trend") == wt
                                 and cnd in f.cond})
                for lab in labels:
                    yield rule, f"weekly_trend + {cnd}", f"{wt} + {lab}", \
                        [f for f in fs if f.cond.get("weekly_trend") == wt and label(f.cond.get(cnd)) == lab]


COLUMNS = ["Rule", "Side", "Condition", "Group", "Fit fires", "Fit names", "Fires per month",
           "Fit avg 20d", "Fit median 20d", "Fit avg 60d", "Fit edge 20d", "Fit right way", "Fit payoff",
           "Fit worst 20d", "Sealed fires", "Sealed edge 20d", "Sealed avg 60d", "Verdict", "Why"]


def _f(x, pct=False, digits=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    return f"{x * 100:.0f}%" if pct else f"{x:+.{digits}f}" if isinstance(x, float) else str(x)


def table(fires: list[Fire], last_day: pd.Timestamp) -> list[list]:
    fit_months = (FIT_END - FIT_START).days / 30.4
    sealed_months = max((last_day - SEALED_START).days / 30.4, 1)
    rows = [COLUMNS]
    for rule, cnd, lab, fs in groups(fires):
        side = RULES[rule]
        key = "all" if cnd == "all" else cnd
        fit = score([f for f in fs if f.day <= FIT_END], side, fit_months, key)
        sealed = score([f for f in fs if f.day >= SEALED_START], side, sealed_months, key)
        v, why = verdict(fit, sealed, side)
        rows.append([rule, side, cnd, lab, fit.get("n", 0), fit.get("names", ""),
                     _f(fit.get("per_month"), digits=1).lstrip("+"), _f(fit.get("avg20")), _f(fit.get("med20")),
                     _f(fit.get("avg60")), _f(fit.get("adj20")), _f(fit.get("right"), pct=True),
                     _f(fit.get("payoff")).lstrip("+"), _f(fit.get("worst20")), sealed.get("n", 0),
                     _f(sealed.get("adj20")), _f(sealed.get("avg60")), v, why])
    order = {"Pass": 0, "Fail": 1, "Not enough data": 2}
    body = sorted(rows[1:], key=lambda r: (order[r[17]], r[0], r[2] != "all", r[2], r[3]))
    return [rows[0]] + body


# ---------------------------------------------------------------- run
def run(args) -> list[list]:
    from desk import scanner
    load_cfg()
    sheet = scanner.Sheet()
    tickers = [r["ticker"] for r in sheet.universe()]
    bm = scanner.load_benchmarks()
    extra = sorted(set(bm.get("market", {}).values()) | set(bm.get("sector", {}).values()) | {"SPY"})
    fetch = getattr(args, "fetch_fn", None) or (lambda ts: scanner.fetch_daily(ts, period="6y", adjust=True))
    bars = fetch(sorted(set(tickers) | set(extra)))
    names = {t: bars[t] for t in tickers if t in bars}
    log.info("rulelab: %d of %d names with data", len(names), len(tickers))
    breadth50 = breadth_series(names)
    bench_of = lambda t: bars.get(scanner.market_of(t, bm))
    sector_of = lambda t: bars.get(scanner.benchmarks_for(t, bm)[1]) if scanner.benchmarks_for(t, bm)[1] else None
    fires = collect(names, bench_of, sector_of, bars.get("SPY"), breadth50)
    last_day = max(df.index.max() for df in names.values())
    rows = table(fires, last_day)
    passed = [r for r in rows[1:] if r[17] == "Pass"]
    luck = []
    for seed in range(PLACEBO_RUNS):
        prows = table(collect(names, bench_of, sector_of, bars.get("SPY"), breadth50, placebo_seed=seed), last_day)
        luck.append(sum(1 for r in prows[1:] if r[17] == "Pass"))
    expected = sum(luck) / len(luck)
    log.info("rulelab: %d fires, %d groups, %d pass; on random days %.1f pass (runs: %s)",
             len(fires), len(rows) - 1, len(passed), expected, luck)
    rows += [[], [f"Placebo check: the same groups on random days in the same names passed "
                  f"{expected:.1f} times on average ({PLACEBO_RUNS} runs: {', '.join(map(str, luck))}). "
                  f"The real run passed {len(passed)}. Trust the passes only if the real count is clearly higher."]]
    for r in passed[:20]:
        log.info("  PASS %s | %s = %s | fit edge %s, sealed %s", r[0], r[2], r[3], r[10], r[15])
    if not args.dry_run:
        sheet.write_tab("Rule Lab", rows)
    return rows
