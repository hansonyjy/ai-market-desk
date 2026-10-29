"""Offline tests for the daily health check. Run: python test_audit.py"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # noqa: E702
from desk import audit
H = ["Timestamp", "Ticker", "Horizon", "Rule", "Price", "RVOL", "Catalyst", "Direction", "Confidence",
     "Pinged", "Ret5d", "Ret20d", "Call", "Chartist", "Strategist", "Ex5d", "Ex20d"]
def row(ts, t, h, rule, pinged, catalyst="x | Sources: https://a", chartist="", strategist=""):
    r = dict.fromkeys(H, "")
    r.update(Timestamp=ts, Ticker=t, Horizon=h, Rule=rule, Pinged=pinged, Catalyst=catalyst, Chartist=chartist, Strategist=strategist)
    return [r[c] for c in H]

caps = {"SHORT": 7, "LONG": 2, "VIP": 6}
# A replay of Oct 19 and 20 as logged
values = [H,
    row("2026-10-19 06:00", "TSLA", "VIP", "news | Elon Musk Says Optimus, Cybercab Will Be ‘the Vast Majority’ of Tesla’s Value", "yes"),
    row("2026-10-19 09:31", "SPCX", "VIP", "news | Musk: Starlink Will Be a “Must-Have for Airlines”", "held"),
    row("2026-10-20 07:03", "VRTX", "SHORT", "breakout_20d | Above 20-day high", "digest"),
    row("2026-10-20 07:16", "STX", "LONG", "pullback_200dma | -0.9%", "yes", chartist="add", strategist="watch"),
    row("2026-10-20 08:01", "CTC-A.TO", "SHORT", "rvol_spike | RVOL 2.6x", "yes", chartist="", strategist="trim"),
    row("2026-10-20 07:31", "TSLA", "VIP", "news | Elon Musk Says Optimus, Cybercab Will Be 'the Vast Majority' of Tesla's Value", "yes"),
    row("2026-10-20 10:16", "SPCX", "VIP", "news | Musk: Starlink Will Be a “Must-Have for Airlines”", "yes"),
    row("2026-10-20 11:46", "SPCX", "VIP", "news | HawkEye 360 Successfully Launches Cluster 15", "held"),
    row("2026-10-20 12:00", "PLTR", "SHORT", "gap_up | Gap", "yes", catalyst="[no source] guess", chartist="watch", strategist="watch"),
    row("2026-10-20 12:30", "AXON", "SHORT", "rvol_spike | RVOL 3.2x | dm_buy_volume", "digest"),
]
state = {"ai_budget": {"date": "2026-10-20", "calls": 34, "usd": 0.68},
         "email_budget": {"date": "2026-10-20", "SHORT": 1, "LONG": 2, "VIP": 6},
         "earnings": {"map": {"SPCX": "2026-10-20"}}}
universe = [{"ticker": "SPCX", "name": "Space Exploration ETF"}, {"ticker": "TSLA", "name": "Tesla"},
            {"ticker": "VPU", "name": ""}]
f = audit.checks(values, state, universe, "2026-10-20", 40, caps)
msgs = "\n".join(f"{lv}: {m}" for lv, m in f)
print(msgs)
again = [m for lv, m in f if lv == "FAIL" and "from 2026-10-19 emailed again" in m]
assert len(again) == 2 and "TSLA: elon musk says optimus" in again[0]          # sent yesterday, quotes differ
assert "SPCX: musk starlink will be a must have for airlines" in again[1]        # held yesterday
assert any(lv == "WARN" and "1 VIP item(s) held" in m for lv, m in f)
assert ("WARN", "Alert(s) without a Chartist line: CTC-A.TO") in f
assert ("FAIL", "Emailed without a source: PLTR") in f
assert ("WARN", "AI calls 34 of 40: later hits may go unread") in f
assert ("WARN", "LONG email cap reached (2 of 2)") in f
assert not any("VIP email cap" in m for _, m in f)                            # earnings days may pass it
assert any("Universe: SPCX is named 'Space Exploration ETF'" in m for _, m in f)
assert any("Universe: VPU has no name" in m for _, m in f)
assert not any("VRTX" in m or "STX" in m for _, m in f)                      # healthy rows raise nothing
# A clean day is all clear; a day with nothing logged is flagged
clean = [H, row("2026-10-21 07:03", "VRTX", "SHORT", "breakout_20d | x", "digest")]
assert audit.checks(clean, {}, [{"ticker": "VRTX", "name": "Vertex"}], "2026-10-21", 40, caps) == []
assert audit.checks([H], {}, [], "2026-10-21", 40, caps)[0][1].startswith("Nothing logged today")
# Budgets from another day are not today's problem
assert audit.checks(clean, {"ai_budget": {"date": "2026-10-20", "calls": 40}}, [], "2026-10-21", 40, caps) == []
subj, body = audit.compose(f, audit.summary(values, "2026-10-20"), "2026-10-20")
assert subj.startswith("AI Market Desk health · 2026-10-20 · ") and "fail" in subj
assert body.index("FAIL:") < body.index("WARN:")                              # failures first
assert "Hypothesis tag: AXON" in body and "VIP sent 2" in body
subj, _ = audit.compose([], "Logged 1 rows.", "2026-10-21")
assert subj.endswith("all clear")
print("all audit tests passed")

# Oct 29: your own level, emailed with no news found, is not a sourcing failure
from desk import mailer
own = [H, row("2026-10-20 10:30", "COST", "LONG", "target_entry_hit | At/below target entry 850.00", "yes",
              catalyst=mailer.NO_NEWS, chartist="add", strategist="watch")]
assert not any("without a source" in m for _, m in audit.checks(own, state, universe, "2026-10-20", 40, caps))
print("own level without news ok")
