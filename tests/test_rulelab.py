"""Rule Lab: signals match the live scanner, DeMark matches levels.py, outcomes and
verdicts are computed as the spec says. All offline, on synthetic prices."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # noqa: E702
import os
import random
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd

from desk import levels as L
from desk import rulelab as R
from desk import scanner as s
R.load_cfg()


def walk(seed, n=420, start="2021-01-04", drift=0.0004):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, periods=n)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.02, n)))
    o = c * np.exp(rng.normal(0, 0.012, n))
    o[::37] = c[::37] * 0.95            # some big gaps down
    o[5::41] = c[5::41] * 1.05          # and up
    h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.008, n)))
    l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.008, n)))
    v = rng.integers(8e5, 1.2e6, n).astype(float)
    v[::23] *= 4                        # volume spikes
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}, index=days)


# 1) Signals match scanner.evaluate() on completed days
LIVE = {"pullback_200dma": "pullback_200dma", "gap_up": "gap_up", "gap_filled": "gap_filled",
        "gap_failed": "gap_failed", "breakout_20d": "breakout_20d", "breakdown_20d": "breakdown_20d",
        "rvol_spike_up": "rvol_spike", "rvol_spike_down": "rvol_spike"}
checked = 0
for seed in (1, 2, 3):
    df = walk(seed)
    sig = R.signals(df)
    for i in range(225, len(df), 3):
        d = df.index[i]
        after = datetime(d.year, d.month, d.day, 18, 0, tzinfo=s.ET) + timedelta(days=1)
        live = {f.rule for f in s.evaluate("X", df.iloc[: i + 1], {}, after)}
        mine = {LIVE[k] for k in LIVE if sig.loc[d, k]}
        if sig.loc[d, "gap_filled"] or sig.loc[d, "gap_down_unfilled"]:
            mine.add("gap_down")
        assert live == mine, (seed, d.date(), live, mine)
        checked += 1
assert checked > 150

# day_move uses the live normal range (14-day ATR % from completed bars)
df = walk(4)
sig = R.signals(df)
for i in range(30, len(df), 7):
    d = df.index[i]
    normal = s.normal_day_pct(df.iloc[: i + 1])
    chg = (df["Close"].iloc[i] / df["Close"].iloc[i - 1] - 1) * 100
    need = max(R.CFG["MOVE_PCT"], R.CFG["DAY_MOVE_ATR"] * normal)
    want = abs(chg) >= need - 0.01 and abs(abs(chg) - need) > 0.02   # skip knife-edge rounding cases
    if abs(abs(chg) - need) > 0.02:
        assert bool(sig.loc[d, "day_move_up"] or sig.loc[d, "day_move_down"]) == want, d

# 2) DeMark per day matches levels.td_setup and td_countdown on every prefix
df = walk(5, n=160)
td = R.td_series_hl(df)
for i in range(10, len(df)):
    st = L.td_setup(df["Close"].iloc[: i + 1])
    assert (td["setup_side"].iloc[i], td["setup"].iloc[i]) == (st["side"], st["count"]) or st["count"] == 0
    cd = L.td_countdown(df.iloc[: i + 1])
    if cd:
        assert td["cd_side"].iloc[i] == cd["side"] and td["cd"].iloc[i] == cd["count"], i

# 3) Forward excess: next open to close h days later, minus the market's
days = pd.bdate_range("2022-01-03", periods=30)
name = pd.DataFrame({"Open": [100.0] * 30, "Close": [100.0 + k for k in range(30)]}, index=days)
mkt = pd.DataFrame({"Open": [50.0] * 30, "Close": [50.0] * 30}, index=days)
fx = R.forward_excess(name, mkt, 5)
assert abs(fx.iloc[0] - 5.0) < 1e-9          # open of day 1 (100) to close of day 5 (105): +5%, market flat
assert pd.isna(fx.iloc[-1])                   # the future is unknown

# 4) Dedup: one fire per 10 trading days
idx = pd.bdate_range("2022-01-03", periods=40)
assert R.dedup([idx[0], idx[3], idx[10], idx[12], idx[25]], index=idx) == [idx[0], idx[10], idx[25]]


# 5) Verdicts follow the section-6 checks
def fires(n, names, adj, year=2023, rule="pullback_200dma", spread=1.0):
    """n fires a week apart, edge `adj` plus a small alternating wobble so the
    month-clustered t-statistic is defined."""
    out = []
    for k in range(n):
        d = pd.Timestamp(f"{year}-01-02") + pd.Timedelta(days=7 * k)
        a = adj + (spread if k % 2 else -spread) * (1 + (k % 3) / 3)
        out.append(R.Fire(f"T{k % names}", rule, d, {5: a, 20: a, 60: a}, a, {}, {"all": a}))
    return out
good = fires(12, 12, 2.0, 2022) + fires(12, 12, 2.0, 2023) + fires(12, 12, 2.0, 2024)
sealed = fires(12, 12, 1.0, 2025)
fit_s, sealed_s = R.score(good, "add", 39), R.score(sealed, "add", 20)
assert R.verdict(fit_s, sealed_s, "add")[0] == "Pass"
assert R.verdict(R.score(good[:20], "add", 39), sealed_s, "add")[0] == "Not enough data"
assert R.verdict(R.score(fires(36, 12, 0.5), "add", 39), sealed_s, "add")[0] == "Fail"       # edge too small
bad_sealed = R.score(fires(12, 12, -1.0, 2025), "add", 20)
assert R.verdict(fit_s, bad_sealed, "add")[0] == "Fail"                                      # sealed years disagree
one_name = fires(36, 12, 0.2) + [R.Fire("STAR", "pullback_200dma", pd.Timestamp("2023-06-01"),
                                        {5: 90.0, 20: 90.0, 60: 90.0}, 90.0, {}, {"all": 90.0})]
assert R.verdict(R.score(one_name, "add", 39), sealed_s, "add")[1] in ("edge depends on one name",) or \
    R.verdict(R.score(one_name, "add", 39), sealed_s, "add")[0] == "Fail"
trim = fires(12, 12, -2.0, 2022, "breakdown_20d") + fires(12, 12, -2.0, 2023, "breakdown_20d") + \
    fires(12, 12, -2.0, 2024, "breakdown_20d")
trim_sealed = R.score(fires(12, 12, -1.0, 2025, "breakdown_20d"), "trim", 20)
assert R.verdict(R.score(trim, "trim", 39), trim_sealed, "trim")[0] == "Pass"               # lagging = right for trim
noisy = fires(12, 12, 2.0, 2022, spread=40) + fires(12, 12, 2.0, 2023, spread=40) + fires(12, 12, 2.0, 2024, spread=40)
assert R.verdict(R.score(noisy, "add", 39), sealed_s, "add")[1].startswith("could be luck")  # big edge, bigger noise

# Edge per group is measured against days with the same condition in the same name
f = R.Fire("T", "gap_filled", pd.Timestamp("2023-01-02"), {5: 3.0, 20: 3.0, 60: 3.0}, 3.0, {},
           {"all": 3.0, "backdrop": 0.5})
assert R.score([f], "add", 1, "backdrop")["adj20"] == 0.5 and R.score([f], "add", 1)["adj20"] == 3.0

# Real-data quirks (Oct 18): blank condition values and a repeated day must not break the run
assert R.label(float("nan")) == "unknown" and R.label(None) == "unknown" and R.label("up") == "up"
q = walk(21, n=700, start="2021-01-04")
q = pd.concat([q, q.iloc[[-1]]])                                   # the last day twice
q.loc[q.index[300:320], "Close"] = np.nan                           # a gap in the data
spy_q = walk(22, n=700, start="2021-01-04")
fq = R.collect({"Q": q}, lambda t: spy_q, lambda t: None, spy_q, None)
assert fq and all(isinstance(v, str) for f in fq for v in f.cond.values())
assert list(R.groups(fq))
# The Oct 18 crash: a float among text labels in one condition. Grouping must not sort mixed types.
mixed = [R.Fire("A", "gap_filled", pd.Timestamp("2023-01-02"), {5: 1.0, 20: 1.0, 60: 1.0}, 1.0, {"rsi": "30 to 70"}),
         R.Fire("B", "gap_filled", pd.Timestamp("2023-02-01"), {5: 1.0, 20: 1.0, 60: 1.0}, 1.0, {"rsi": float("nan")})]
labs = {g[2] for g in R.groups(mixed) if g[1] == "rsi"}
assert labs == {"30 to 70", "unknown"}

# 6) End to end, offline: universe file, fake prices, the Rule Lab tab written
tmp = tempfile.mkdtemp()
uni = os.path.join(tmp, "u.csv")
with open(uni, "w") as fh:
    fh.write("Ticker,Name,Status,Priority\n" + "".join(f"N{k},Name {k},Holding,Normal\n" for k in range(12)))
os.environ["UNIVERSE_CSV"] = uni
os.environ.pop("GOOGLE_CREDS_JSON", None); os.environ.pop("GITHUB_ACTIONS", None)
s.LOCAL_LOG = s.Path(tmp) / "log.csv"
fake = {f"N{k}": walk(10 + k, n=1500, start="2020-10-01") for k in range(12)}
fake["SPY"] = walk(99, n=1500, start="2020-10-01")
rows = R.run(NS(dry_run=False, fetch_fn=lambda ts: {t: fake[t] for t in ts if t in fake}))
assert rows[0] == R.COLUMNS and len(rows) > 20
body = rows[1:-2]
assert {r[17] for r in body} <= {"Pass", "Fail", "Not enough data"}
assert any(r[2] == "weekly_trend" for r in body) and any(r[2] == "weekly_trend + demark" for r in body)
assert rows[-2] == [] and rows[-1][0].startswith("Placebo check: the same groups on random days")
saved = open(os.path.join(tmp, "rule_lab.csv")).read().splitlines()
assert saved[0].startswith("Rule,Side,Condition")
print("all rulelab tests passed")
