"""Offline tests for the rule engine. Run: python test_scanner.py"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # noqa: E702
from datetime import date, datetime

import numpy as np
import pandas as pd

from desk import scanner as s
def make_bars(n=260, price=100.0, vol=1_000_000, end="2026-09-24"):
    idx = pd.bdate_range(end=end, periods=n)
    close = np.full(n, price)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": np.full(n, vol, dtype=float)}, index=idx)


def rules(flags):
    return sorted(f.rule for f in flags)


NOW_CLOSED = datetime(2026, 9, 25, 8, 0, tzinfo=s.ET)  # before open; last bar = 9/24, full day

# 1) Quiet name: nothing fires
b = make_bars()
assert rules(s.evaluate("AAA", b, {}, NOW_CLOSED)) == [], rules(s.evaluate("AAA", b, {}, NOW_CLOSED))

# 2) RVOL spike on a completed day
b = make_bars(); b.iloc[-1, b.columns.get_loc("Volume")] = 3_000_000
assert "rvol_spike" in rules(s.evaluate("AAA", b, {}, NOW_CLOSED))

# 3) Gap up + breakout
b = make_bars(); b.iloc[-1, b.columns.get_loc("Open")] = 105; b.iloc[-1, b.columns.get_loc("Close")] = 106
r = rules(s.evaluate("AAA", b, {}, NOW_CLOSED))
assert "gap_up" in r and "breakout_20d" in r, r

# 4) Breakdown
b = make_bars(); b.iloc[-1, b.columns.get_loc("Close")] = 95
assert "breakdown_20d" in rules(s.evaluate("AAA", b, {}, NOW_CLOSED))

# 5) Earnings within 5 trading days
b = make_bars()
assert "earnings_soon" in rules(s.evaluate("AAA", b, {}, NOW_CLOSED, date(2026, 9, 30)))
assert "earnings_soon" not in rules(s.evaluate("AAA", b, {}, NOW_CLOSED, date(2026, 10, 20)))

# 6) Pullback to 200DMA after being well above it
b = make_bars(price=100.0)
b.iloc[-21:-1, b.columns.get_loc("Close")] = 110  # was 10% above
b.iloc[-1, b.columns.get_loc("Close")] = 101.5
r = s.evaluate("AAA", b, {}, NOW_CLOSED)
assert "pullback_200dma" in rules(r), rules(r)
# ...but not when it never left the 200DMA (flat name)
assert "pullback_200dma" not in rules(s.evaluate("AAA", make_bars(), {}, NOW_CLOSED))

# 7) Sheet levels
b = make_bars()
assert "target_entry_hit" in rules(s.evaluate("AAA", b, {"target entry level": "100.50"}, NOW_CLOSED))
assert "trim_level_hit" in rules(s.evaluate("AAA", b, {"trim level": "$99"}, NOW_CLOSED))
assert rules(s.evaluate("AAA", b, {"target entry level": "", "trim level": "n/a"}, NOW_CLOSED)) == []

# 8) Live session: time-of-day RVOL adjustment
live_now = datetime(2026, 9, 24, 11, 30, tzinfo=s.ET)  # 120 min in, profile says ~33% traded
b = make_bars(); b.iloc[-1, b.columns.get_loc("Volume")] = 900_000  # 0.9x full-day avg already
f = [x for x in s.evaluate("AAA", b, {}, live_now) if x.rule == "rvol_spike"]
assert f and 2.6 < f[0].rvol < 2.8, f  # 0.9 / 0.33 = 2.7x projected
# first 30 minutes: no SHORT flags at all, and no RVOL figure on LONG flags
early = datetime(2026, 9, 24, 9, 45, tzinfo=s.ET)
assert "rvol_spike" not in rules(s.evaluate("AAA", b, {}, early))
g = make_bars(); g.iloc[-1, g.columns.get_loc("Open")] = 105; g.iloc[-1, g.columns.get_loc("Close")] = 106
assert [x.horizon for x in s.evaluate("AAA", g, {}, early)] == []              # gap and breakout wait
assert {"gap_up", "breakout_20d"} <= set(rules(s.evaluate("AAA", g, {}, live_now)))  # ...until 10:00
lg = s.evaluate("AAA", g, {"trim level": "100"}, early)
assert [x.rule for x in lg] == ["trim_level_hit"] and lg[0].rvol is None

# 9) session_fraction shape
assert s.session_fraction(datetime(2026, 9, 24, 9, 0, tzinfo=s.ET)) == 0.0
assert s.session_fraction(datetime(2026, 9, 24, 16, 30, tzinfo=s.ET)) == 1.0

# 10) cooldown
st = {}
s.mark_fired(st, "AAA|rvol_spike", "2026-09-24")
assert not s.cooled_down(st, "AAA|rvol_spike", date(2026, 9, 24), 1)
assert s.cooled_down(st, "AAA|rvol_spike", date(2026, 9, 25), 1)

# 11) Universe CSV parsing (local file mode) and log round-trip
import csv, json, os, tempfile
# Local file mode is for local runs only; in GitHub Actions the Sheet is required.
os.environ.pop("GITHUB_ACTIONS", None)
os.environ.pop("GOOGLE_CREDS_JSON", None)
tmp = tempfile.mkdtemp()
p = os.path.join(tmp, "u.csv")
with open(p, "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["Ticker", "Name", "Status", "Priority", "Sector", "Target entry level",
                "Trim level", "Theme", "Owner", "Notes"])
    w.writerow(["spcx", "SpaceX", "Holding", "VIP", "No", "", "", "Space", "Alex", ""])
    w.writerow(["CSU.TO", "Constellation Software", "Watchlist", "Normal", "Software", "150", "", "", "", ""])
os.environ["UNIVERSE_CSV"] = p
u = s.Sheet().universe()
assert [r["ticker"] for r in u] == ["SPCX", "CSU.TO"] and u[0]["priority"] == "VIP"

s.LOCAL_LOG = s.Path(tmp) / "log.csv"
sh = s.Sheet()
sh.append_log([s.Flag("U", "SHORT", "rvol_spike", "RVOL 2.8x", 44.1, 2.8)])
vals = sh.read_log()
assert vals[0] == s.LOG_COLUMNS and vals[1][1] == "U"
sh.write_cells([(1, {"Ret5d": "3.10%", "Ex5d": "1.20%"})])
back = sh.read_log()
assert back[0] == s.LOG_COLUMNS and back[1][s.LOG_COLUMNS.index("Ret5d")] == "3.10%"
assert back[1][s.LOG_COLUMNS.index("Ex5d")] == "1.20%"


# 12) Credentials: JSON content (GitHub secret) vs file path (local) vs unset
assert s.google_credentials('{"type": "service_account"}') == {"type": "service_account"}
assert s.google_credentials("/path/creds.json") == "/path/creds.json"
assert s.google_credentials("") is None


# 13) Sheet mode against an in-memory fake of gspread
class FakeWS:
    def __init__(self, rows=None, grid=1000):
        self.rows = [list(r) for r in (rows or [])]; self.grid = grid; self.fail_update = False
    @property
    def row_count(self): return self.grid
    def add_rows(self, n): self.grid += n
    def batch_clear(self, ranges):
        for rg in ranges:                                   # "A{n}:B{m}": blank those rows
            a, b = rg.split(":"); lo, hi = int(a[1:]), int(b[1:])
            for i in range(lo - 1, min(hi, len(self.rows))): self.rows[i] = ["", ""]
    def row_values(self, i): return list(self.rows[i - 1]) if len(self.rows) >= i else []
    def get_all_values(self): return [list(r) for r in self.rows]
    def get_all_records(self):
        h = self.rows[0]; return [dict(zip(h, r)) for r in self.rows[1:]]
    def update(self, values, range_name="A1"):
        assert range_name == "A1"
        if self.fail_update: raise RuntimeError("Google API error")
        assert len(values) <= self.grid, "write past the grid"
        for i, r in enumerate(values):
            if i < len(self.rows): self.rows[i] = list(r)
            else: self.rows.append(list(r))
    def append_rows(self, rows, value_input_option=None): self.rows += [list(r) for r in rows]
    def clear(self): self.rows = []


class FakeSheet:
    def __init__(self, tabs): self.tabs = tabs
    def worksheet(self, name):
        if name not in self.tabs: raise KeyError(name)
        return self.tabs[name]
    def add_worksheet(self, name, rows, cols):
        self.tabs[name] = FakeWS(); return self.tabs[name]


universe = FakeWS([["Ticker", "Name", "Priority"], ["tsla", "Tesla", "VIP"]])
fake = FakeSheet({"Universe": universe, "Signal Log": FakeWS(), "State": FakeWS()})  # tabs made empty by hand
sh = s.Sheet(spreadsheet=fake)
assert [r["ticker"] for r in sh.universe()] == ["TSLA"]

# Signal Log: header written into the empty tab, then the row
sh.append_log([s.Flag("TSLA", "SHORT", "gap_up", "Gap +4.0%", 250.0, 1.8)])
assert fake.tabs["Signal Log"].rows[0] == s.LOG_COLUMNS and fake.tabs["Signal Log"].rows[1][1] == "TSLA"
# A log made before the vote columns gets them added to its header; votes land at the end
old_log = FakeWS([s.LOG_COLUMNS[:12]])
sh2 = s.Sheet(spreadsheet=FakeSheet({"Universe": universe, "Signal Log": old_log, "State": FakeWS()}))
sh2.append_log([s.Flag("TSLA", "SHORT", "gap_up", "Gap +4.0%", 250.0, 1.8)],
               {"TSLA": {"votes": {"call": "Watch, do not chase", "chartist": "watch", "strategist": "add"}}}, {"TSLA": "yes"})
assert old_log.rows[0] == s.LOG_COLUMNS and old_log.rows[1][-3:] == ["Watch, do not chase", "watch", "add"]

# State: round trip through the Sheet, one row per cooldown, old cooldowns pruned
today = datetime.now(s.ET).date()
st = {"fired": {"TSLA|gap_up": today.isoformat(), "U|rvol_spike": "2020-01-01"},
      "seen": [f"fh:{i}" for i in range(800)], "vip_primed": True,
      "earnings": {"date": today.isoformat(), "map": {"TSLA": "2026-10-21"}}}
sh.save_state(st)
rows = fake.tabs["State"].rows
assert rows[0] == s.STATE_HEADER and ["fired:TSLA|gap_up", today.isoformat()] in rows
assert not any(r[0] == "fired:U|rvol_spike" for r in rows)       # older than 30 days: pruned
assert max(len(r[1]) for r in rows) < 50_000                       # Google's per-cell limit
back = s.Sheet(spreadsheet=fake).load_state()
assert back["fired"] == {"TSLA|gap_up": today.isoformat()}
assert back["vip_primed"] is True and len(back["seen"]) == s.SEEN_KEEP and back["seen"][-1] == "fh:799"
assert back["earnings"]["map"]["TSLA"] == "2026-10-21"
assert s.rows_to_state([]) == {}

# State save never clears first: a failed write keeps the old state, and a
# shorter state blanks the leftover rows instead of leaving stale keys (Oct 15)
before = [list(r) for r in fake.tabs["State"].rows]
fake.tabs["State"].fail_update = True
try:
    sh.save_state({"vip_primed": False})
    raise AssertionError("expected the API error to surface")
except RuntimeError:
    pass
assert fake.tabs["State"].rows == before                            # nothing lost
fake.tabs["State"].fail_update = False
sh.save_state({"vip_primed": False})
back = s.Sheet(spreadsheet=fake).load_state()
assert back == {"vip_primed": False}                               # old rows blanked, not left behind
small = FakeWS(grid=2)                                             # a tab smaller than the state grows first
s.write_state_rows(small, [["Key", "Value"], ["a", "1"], ["b", "2"]])
assert small.grid == 3 and small.rows[2] == ["b", "2"]

# Same-day repeats (Oct 16, HACK): a name emailed by a live alert needs a new story to be read again
from datetime import date as _d
st = {}
s.mark_emailed(st, _d(2026, 10, 16), ["HACK"])
assert s.emailed_today(st, _d(2026, 10, 16)) == {"HACK"} and s.emailed_today(st, _d(2026, 10, 19)) == set()
again = [s.Flag("HACK", "SHORT", "rvol_spike", "x", 98.3, 3.8, -5.0, "2026-10-16"),
         s.Flag("HACK", "SHORT", "fast_move", "x", 98.3, 3.8, -5.0, "2026-10-16")]
assert [f.rule for f in s.worth_reading(again, st, _d(2026, 10, 16))] == ["fast_move"]

# Scorecard v2 (Oct 17): excess return against the market, and a summary by side
import pandas as _pd
days = _pd.bdate_range("2026-10-05", periods=30)
def _bars(start, step):
    return _pd.DataFrame({"Close": [start * (1 + step) ** k for k in range(30)]}, index=days)
bars = {"AAA": _bars(100, 0.0), "BBB": _bars(50, -0.01), "SPY": _bars(500, -0.02), "XIU.TO": _bars(30, 0.0),
        "CCC.TO": _bars(10, 0.01)}
H = s.LOG_COLUMNS
def row(t, h, price, direction, pinged, call="", chartist="", strategist="", day="2026-10-05"):
    r = [""] * len(H)
    for k, v in {"Timestamp": f"{day} 07:03", "Ticker": t, "Horizon": h, "Price": price, "Direction": direction,
                 "Pinged": pinged, "Call": call, "Chartist": chartist, "Strategist": strategist}.items():
        r[H.index(k)] = v
    return r
old_header = H[:12]                                       # a log made before the new columns
vals = [old_header, row("AAA", "SHORT", "100", "ACCUMULATION", "yes")[:12],
        row("BBB", "LONG", "50", "DISTRIBUTION", "no")[:12], row("TSLA", "VIP", "", "", "yes")[:12],
        row("CCC.TO", "SHORT", "10", "ACCUMULATION", "digest")[:12]]
up = dict(s.grade_rows(vals, bars, s.load_benchmarks()))
spy5 = (1 - 0.02) ** 5 - 1
assert up[1]["Ret5d"] == "0.00%" and up[1]["Ex5d"] == f"{-spy5 * 100:.2f}%"     # flat name, falling market: beat it
assert up[2]["Ex5d"] == f"{((0.99 ** 5 - 1) - spy5) * 100:.2f}%"
assert 3 not in up                                                                 # VIP: no price, not graded
assert up[4]["Ex5d"] == f"{((1.01 ** 5 - 1) - 0) * 100:.2f}%"                    # a TSX name vs XIU.TO
filled = [r[:] for r in vals]
filled[0] = H
for i, cells in up.items():
    filled[i] = filled[i] + [""] * (len(H) - len(filled[i]))
    for c, v in cells.items():
        filled[i][H.index(c)] = v
filled.append(row("AAA", "SHORT", "100", "ACCUMULATION", "yes"))   # same name, same day: counted once
filled[-1][H.index("Ex5d")] = "9.99%"
summ = s.summarize(filled, _d(2026, 10, 17))
g = {r[0]: r for r in summ[1:] if len(r) >= 5}
assert g["Tape Reader: accumulation, emailed"][1] == 2 and g["Tape Reader: accumulation, emailed"][3] == "2 of 2"
assert g["Tape Reader: distribution, not emailed"][3] == "0 of 1"                  # fell, but less than the market
from desk import mailer as _mailer
sub, body, html_body = _mailer.compose_scorecard(summ)
assert sub.startswith("AI Market Desk: weekly scorecard") and "<table" in html_body and "2 of 2" in body
votes = [H, row("AAA", "SHORT", "100", "ACCUMULATION", "yes", "Consider adding", "watch", "add")]
votes[1][H.index("Ex5d")] = "1.00%"
g = {r[0]: r for r in s.summarize(votes, _d(2026, 10, 17))[1:] if len(r) >= 5}
assert g["Call: Consider adding"][3] == "1 of 1" and g["Strategist: add"][3] == "1 of 1" and g["Chartist: watch"][3] == ""

# Hypothesis tag (Oct 17 Rule Lab): volume surge on an up day while a DeMark buy 9 shows
def _downtrend_then_bounce():
    b = make_bars()
    close = np.r_[np.full(240, 100.0), 100 - np.arange(1, 21, dtype=float)]   # 20 lower closes
    close[-1] = close[-2] + 0.5                         # up day, still below the close 4 bars back
    b["Close"], b["Open"] = close, close
    b["High"], b["Low"] = close * 1.01, close * 0.99
    b.iloc[-1, b.columns.get_loc("Volume")] = 3_000_000
    return b
spike = [f for f in s.evaluate("AAA", _downtrend_then_bounce(), {}, NOW_CLOSED) if f.rule == "rvol_spike"]
assert spike and spike[0].tags == [s.HYPOTHESIS_TAG], spike
b = make_bars(); b.iloc[-1, b.columns.get_loc("Volume")] = 3_000_000           # spike, no DeMark buy
assert all(f.tags == [] for f in s.evaluate("AAA", b, {}, NOW_CLOSED))
b = _downtrend_then_bounce(); b.iloc[-1, b.columns.get_loc("Close")] = 80.0      # spike on a down day
assert all(f.tags == [] for f in s.evaluate("AAA", b, {}, NOW_CLOSED))
tagged = [H, row("AAA", "SHORT", "82", "", "no")]
tagged[1][H.index("Rule")] = f"rvol_spike | RVOL 3.0x, +0.6% on day | {s.HYPOTHESIS_TAG}"
tagged[1][H.index("Ex5d")] = "2.00%"
g = {r[0]: r for r in s.summarize(tagged, _d(2026, 10, 17))[1:] if len(r) >= 5}
assert g["Watching: DeMark buy + volume surge, up day"][3] == "1 of 1", g

# 14) A Sheet asked for (SHEET_ID) without credentials fails loudly, instead of
#     silently writing to local files
os.environ["SHEET_ID"] = "some-sheet"
try:
    s.Sheet()
    raise AssertionError("expected SystemExit with SHEET_ID but no GOOGLE_CREDS_JSON")
except SystemExit as e:
    assert "GOOGLE_CREDS_JSON" in str(e)
finally:
    os.environ.pop("SHEET_ID", None)

# 15) Settings: pasted secrets with a trailing newline or spaces still work
os.environ["SOME_KEY"] = "  abc123\n"
assert s.env("SOME_KEY") == "abc123"
os.environ["SOME_NUM"] = "2.5\n"
assert s.env("SOME_NUM", 1.0, float) == 2.5
os.environ["SOME_BLANK"] = "\n"
assert s.env("SOME_BLANK", "dflt") == "dflt"
err = RuntimeError("Connection error.")
err.__cause__ = ValueError("Illegal header value b'sk-ant-SECRET\\n'")
assert s.describe_error(err) == "RuntimeError (caused by ValueError)"   # the key never reaches the log

# 16) Fresh data only. The Oct 5 evening dry run got Friday's bars from Yahoo and
#     would have emailed Friday's moves as new. Expected session per exchange calendar:
mon_evening = datetime(2026, 10, 5, 21, 45, tzinfo=s.ET)          # after the close
assert s.expected_session("WDC", mon_evening) == date(2026, 10, 5)
assert s.expected_session("WDC", datetime(2026, 10, 5, 8, 0, tzinfo=s.ET)) == date(2026, 10, 2)   # before the open
assert s.expected_session("WDC", datetime(2026, 10, 5, 11, 0, tzinfo=s.ET)) == date(2026, 10, 5)  # open: today
thanksgiving = datetime(2026, 10, 12, 18, 0, tzinfo=s.ET)       # Canadian Thanksgiving: TSX closed, NYSE open
assert s.expected_session("EQB.TO", thanksgiving) == date(2026, 10, 9)
assert s.expected_session("EQB", thanksgiving) == date(2026, 10, 12)

friday = make_bars(end="2026-10-02"); monday = make_bars(end="2026-10-05")
fresh, stale = s.split_stale({"WDC": friday, "MSFT": monday}, mon_evening)
assert list(fresh) == ["MSFT"] and stale == {"WDC": (date(2026, 10, 2), date(2026, 10, 5))}
fresh, stale = s.split_stale({"EQB.TO": make_bars(end="2026-10-09")}, thanksgiving)
assert list(fresh) == ["EQB.TO"] and not stale                       # a holiday is not staleness


# 17) Preview can never send: --ignore-cooldowns is refused without --dry-run
import subprocess
import sys
from pathlib import Path
r = subprocess.run([sys.executable, "-m", "desk.scanner", "scan", "--ignore-cooldowns"],
                   capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1])
assert r.returncode == 2 and "only works with --dry-run" in r.stderr, r.stderr


# 18) Live alerts (Oct 13): gap reversals, fast moves, opening window
g = make_bars(); g.iloc[-1, g.columns.get_loc("Open")] = 95; g.iloc[-1, g.columns.get_loc("Close")] = 100.5
r = set(rules(s.evaluate("AAA", g, {}, live_now)))
assert {"gap_down", "gap_filled"} <= r                                   # gapped down 5%, now above prior close
g.iloc[-1, g.columns.get_loc("Close")] = 97
assert "gap_filled" not in set(rules(s.evaluate("AAA", g, {}, live_now)))
u = make_bars(); u.iloc[-1, u.columns.get_loc("Open")] = 105; u.iloc[-1, u.columns.get_loc("Close")] = 99.5
assert "gap_failed" in set(rules(s.evaluate("AAA", u, {}, live_now)))

idx = pd.date_range("2026-10-13 10:00", periods=61, freq="1min")
steady = pd.Series(100.0, index=idx)
jump = pd.Series(np.r_[np.full(40, 100.0), np.linspace(100, 103.5, 21)], index=idx)    # +3.5% in 20 min
drop = pd.Series(np.r_[np.full(50, 100.0), np.linspace(100, 96.8, 11)], index=idx)     # -3.2%
fm = s.fast_moves({"A": steady, "B": jump, "C": drop, "D": jump.iloc[:20]}, 30, 3.0)
assert set(fm) == {"B", "C"} and fm["B"] == (3.5, 103.5) and fm["C"][0] == -3.2
assert s.fast_moves({"B": jump}, 30, 4.0) == {}                         # threshold respected

st = {}
now = datetime(2026, 10, 13, 11, 0)
assert s.cooled_down_min(st, "B|fast_move", now, 120)
s.mark_fired(st, "B|fast_move", now.isoformat())
assert not s.cooled_down_min(st, "B|fast_move", now.replace(hour=12, minute=30), 120)
assert s.cooled_down_min(st, "B|fast_move", now.replace(hour=13, minute=1), 120)
s.mark_fired(st, "C|gap_up", "2026-10-13")                              # day-based keys do not break it
assert s.cooled_down_min(st, "C|gap_up", now, 120)

rv, chg = s.day_stats(make_bars(), live_now)
assert chg == 0.0 and rv is not None


# 19) Day move: scaled to the name's normal range, once a day, only if not mentioned yet
calm = make_bars()                                       # High/Low +-1%: a normal day is about 2%
assert 1.9 < s.normal_day_pct(calm) < 2.1
st = {}
s.mark_fired(st, "MU|gap_down", "2026-10-13")
assert s.mentioned_today(st, "MU", date(2026, 10, 13)) and not s.mentioned_today(st, "MU", date(2026, 10, 14))
assert not s.mentioned_today(st, "M", date(2026, 10, 13))      # "M" is not "MU"

print("all tests passed")

# VIP (Oct 19): a held headline is remembered too, so the filter drops its repeats later
from types import SimpleNamespace as _NS
from datetime import datetime as _dt
_today = _dt.now(s.ET).date().isoformat()
_st = {"email_budget": {"date": _today, "SHORT": 0, "LONG": 0, "VIP": 99}}
_items = [{"ticker": "SPCX", "type": "news", "headline": "Starlink a must-have for airlines", "url": ""}]
assert s.send_vip(_items, _st, {}, _NS(dry_run=False)) == "held"
assert "SPCX: Starlink a must-have for airlines" in _st["vip_sent"]["headlines"]
print("vip held-memory ok")

# VIP (Oct 20): yesterday's headlines are remembered for one more day
_st = {"vip_sent": {"date": "2026-10-19", "headlines": ["TSLA: Optimus, Cybercab"]}}
assert s.vip_recent(_st, "2026-10-20") == ["TSLA: Optimus, Cybercab"]
_st["email_budget"] = {"date": _today, "SHORT": 0, "LONG": 0, "VIP": 99}
s.send_vip([{"ticker": "SPCX", "type": "news", "headline": "HawkEye 360 launches", "url": ""}], _st, {}, _NS(dry_run=False))
assert _st["vip_sent"]["before"] == ["TSLA: Optimus, Cybercab"]
assert set(s.vip_recent(_st, _today)) == {"TSLA: Optimus, Cybercab", "SPCX: HawkEye 360 launches"}
# VIP (Oct 20): a name reporting today or yesterday is not held by the cap
from datetime import date as _date, timedelta as _td
_d = _dt.now(s.ET).date()
_st["earnings"] = {"map": {"SPCX": _d.isoformat(), "TSLA": (_d + _td(days=1)).isoformat()}}
assert s.earnings_window(_st, _d) == {"SPCX"}
assert s.send_vip([{"ticker": "SPCX", "type": "news", "headline": "SpaceX Q3 results", "url": ""}],
                  _st, {}, _NS(dry_run=True)) == "yes"                          # cap full, still sent
assert s.send_vip([{"ticker": "TSLA", "type": "news", "headline": "Tesla preview", "url": ""}],
                  _st, {}, _NS(dry_run=True)) == "held"                         # not its earnings day yet
assert s.earnings_window({"earnings": {"map": {"X": "2026-10-16"}}}, _date(2026, 10, 19)) == {"X"}  # Friday -> Monday
print("vip memory and earnings window ok")

# VIP (Oct 21): material events match for every VIP name, as whole words
_cfg = s.load_vip_config()
_w = s.vip_words(_cfg, "TSLA")
assert s.keyword_hit("Tesla and SpaceX Just Formed Special Committees to Weigh a Merger", _w) == ["merger", "special committee"] or \
       set(s.keyword_hit("Tesla and SpaceX Just Formed Special Committees to Weigh a Merger", _w)) >= {"merger"}
assert "Q3" in s.keyword_hit("SpaceX Q3 Earnings: AI now brings in half the revenue", s.vip_words(_cfg, "SPCX"))
assert "board" not in s.keyword_hit("Starlink onboard every United jet", s.vip_words(_cfg, "SPCX"))
assert s.keyword_hit("Tesla recall widens", s.vip_words(_cfg, "TSLA")) == ["recall"]
print("vip material words ok")
assert s.keyword_hit("HawkEye 360 Successfully Launches Cluster 15", s.vip_words(_cfg, "SPCX")) == ["launch"]   # endings ok
assert s.keyword_hit("Second sector rotation day", s.vip_words(_cfg, "TSLA")) == []                     # SEC is whole-word
assert s.keyword_hit("Tesla CEO comments", s.vip_words(_cfg, "TSLA")) == ["CEO"]
print("keyword boundaries ok")

# Oct 26 (HON): a spin-off in the last 200 bars turns off the 200-day signals
import numpy as _np
def _pull_bars(n=260):
    b = make_bars(n)
    c = _np.r_[_np.full(n - 20, 100.0), _np.linspace(100, 108, 10), _np.linspace(108, 100.5, 10)]  # 8% above, back
    b["Close"] = b["Open"] = c; b["High"] = c * 1.01; b["Low"] = c * 0.99
    return b
b = _pull_bars()
assert "pullback_200dma" in rules(s.evaluate("AAA", b, {}, NOW_CLOSED)), rules(s.evaluate("AAA", b, {}, NOW_CLOSED))
spin = b.copy(); spin["Stock Splits"] = 0.0; spin.iloc[-18, spin.columns.get_loc("Stock Splits")] = 5.83
assert s.corporate_action(spin) == spin.index[-18].date().isoformat()
assert "pullback_200dma" not in rules(s.evaluate("AAA", spin, {}, NOW_CLOSED))
split = b.copy(); split["Stock Splits"] = 0.0; split.iloc[-18, split.columns.get_loc("Stock Splits")] = 10.0
assert s.corporate_action(split) is None                                       # a plain 10-for-1 split is fine
assert "pullback_200dma" in rules(s.evaluate("AAA", split, {}, NOW_CLOSED))
div = b.copy(); div["Adj Close"] = div["Close"]; div.iloc[:-18, div.columns.get_loc("Adj Close")] *= 0.2
assert s.corporate_action(div) is not None                                     # spin booked as a big distribution
small = b.copy(); small["Adj Close"] = small["Close"]; small.iloc[:-18, small.columns.get_loc("Adj Close")] *= 0.99
assert s.corporate_action(small) is None                                       # an ordinary dividend
tgt = spin.copy()
assert "target_entry_hit" in rules(s.evaluate("AAA", tgt, {"target entry level": "101"}, NOW_CLOSED))  # team levels still apply

# Oct 22: the earnings window lasts 3 trading days and outlives the earnings map
_st2 = {"earnings": {"map": {"SPCX": "2026-10-20", "TSLA": "2026-10-21"}}}
assert s.earnings_window(_st2, _date(2026, 10, 20)) == {"SPCX"}
_st2["earnings"]["map"] = {"TSLA": "2026-10-21"}                               # SPCX dropped once its date passed
assert s.earnings_window(_st2, _date(2026, 10, 22)) == {"SPCX", "TSLA"}
assert s.earnings_window(_st2, _date(2026, 10, 23)) == {"TSLA"}                # SPCX: Oct 20 is 4 trading days back
assert s.earnings_window(_st2, _date(2026, 10, 26)) == set() and _st2["reported"] == {}   # Mon: Oct 21 is 4 back
print("corporate actions and earnings window ok")

# Oct 28: the ten VIP headlines the team got; the code now drops the five not about the company or opinion
_c = s.load_vip_config()
_oct28 = [("TSLA", "Elon Musk Says Tesla Robotaxi Expansion to Miami, Tampa Beaches Is 'Coming Soon'", "https://finance.yahoo.com/a", True),
          ("TSLA", "TSLA Stock Rises As Cybercabs Overtake Model Ys In Authorized Texas Robotaxi Fleet", "https://stocktwits.com/a", True),
          ("SPCX", "Elon Musk Says SpaceX's Louisiana Starbase Could Handle 10,000 Starship Launches A Year", "https://stocktwits.com/b", True),
          ("TSLA", "Nasdaq 100 Climbs on Chip Rally, Boeing Sinks 10%: Stock Market Today", "https://www.benzinga.com/a", False),
          ("SPCX", "Google Earnings Prediction Market Preview: What Will Sundar Pichai Say?", "https://www.benzinga.com/b", False),
          ("SPCX", "Jeff Bezos Just Got a New Boost in His Rivalry With Elon Musk — Amazon Wins FCC Approval", "https://www.benzinga.com/c", False),
          ("SPCX", "SpaceX's AI Boom Has Yet To Pay For Its Build-Out", "https://seekingalpha.com/article/4958342-x", False),
          ("TSLA", "Tesla Cancels Cybertruck in Two Countries: An Omen for the Vehicle's Future?", "https://finance.yahoo.com/b", True),
          ("TSLA", "TSLA Q3 Earnings Call Highlights Autonomy and Expansion Plans", "https://finance.yahoo.com/c", True),
          ("SPCX", "SpaceX's Starlink Just Added 1.6 Million Customers. But That's Not the Best News", "https://www.fool.com/investing/x", False)]
for _t, _h, _u, _keep in _oct28:
    assert (s.vip_reject(_c, _t, _h, _u) == "") == _keep, (_t, _h, s.vip_reject(_c, _t, _h, _u))
assert s.vip_reject(_c, "TSLA", "Tesla recalls 200,000 Cybertrucks", "https://reuters.com/x") == ""
assert s.vip_reject({}, "XYZ", "Anything", "") == ""                              # names without a list: no gate
print("vip subject gate ok")
# ...and last week's good VIP items still pass the gate
for _t, _h in [("TSLA", "Barclays upgrades Tesla, says SpaceX merger now a key part of bull case"),
               ("SPCX", "SpaceX (SPCX) Lands Rare Double Upgrade As Compute Demand Tightens Supply"),
               ("TSLA", "Elon Musk Says Tesla's Megapacks Are Nearly Sold Out Through 2027"),
               ("SPCX", "Astranis Upgrades Falcon 9 Launch Contract to Dedicated Mission"),
               ("SPCX", "Elon Musk On Sunday, Posted On X: SpaceX Closed $40B Of Debt Financing For GPUs"),
               ("TSLA", "Tesla and SpaceX Just Formed Special Committees to Weigh a Merger"),
               ("SPCX", "SPCX Stock Jumps 10% Premarket: Morningstar Boosts SpaceX's Fair Value"),
               ("TSLA", "Intel Shares Rise Nearly 4% Following Cisco Upgrade and Tesla Foundry Agreement")]:
    assert s.vip_reject(_c, _t, _h, "https://finance.yahoo.com/x") == "", _h
print("vip gate keeps last week's good items")

# ---- earnings: one calendar call per week covers every name (500 calls would take 9+ minutes)
import os
calls = []
class _R:
    ok = True
    def __init__(self, items): self._i = items
    def json(self): return {"earningsCalendar": self._i}
def _get(url, params=None, **kw):
    calls.append(params)
    assert "symbol" not in params
    return _R([{"symbol": "BRK.B", "date": params["from"]}, {"symbol": "AAPL", "date": params["to"]},
               {"symbol": "NOTOURS", "date": params["from"]}])
os.environ["FINNHUB_KEY"] = "k"
st = {}
got = s.earnings_dates(["AAPL", "BRK-B", "CSU.TO"], st, get=_get)
os.environ.pop("FINNHUB_KEY")
assert len(calls) == 3 and set(got) == {"AAPL", "BRK-B"}                       # Yahoo spelling back, others ignored
assert got["BRK-B"] == min(date.fromisoformat(c["from"]) for c in calls)      # the earliest date wins
assert s.earnings_dates(["AAPL"], st, get=lambda *a, **k: 1 / 0) == got and len(calls) == 3   # cached for the day
print("earnings calendar ok")
