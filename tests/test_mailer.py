"""Offline tests for alert emails. No network. Run: python test_mailer.py"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # noqa: E702
import os
from datetime import date
from types import SimpleNamespace as NS

from desk import mailer
from desk import scanner as s
today = date(2026, 10, 5)


def flag(t, horizon="SHORT", rvol=1.5, rule="breakout_20d"):
    return {"ticker": t, "horizon": horizon, "rule": rule, "detail": f"{t} detail", "price": 100.0,
            "rvol": rvol, "day_change_pct": 2.0, "bar_date": "2026-10-05"}


def read(direction="ACCUMULATION", conf="medium", links=("https://news.example/a",)):
    return {"catalyst": "A sourced catalyst.", "direction": direction, "confidence": conf, "links": list(links)}


# 1) Only sourced ACCUMULATION / DISTRIBUTION reads are emailed
flags = [flag("MSFT"), flag("EQB.TO"), flag("MSTR", "LONG", rule="pullback_200dma"), flag("INTC")]
reads = {"MSFT": read(conf="high"), "EQB.TO": read("NOISE"),
         "MSTR": read(conf="low", links=()), "INTC": read("DISTRIBUTION")}
send, held = mailer.pick_alerts(flags, reads, {}, today)
assert [a["ticker"] for a in send] == ["MSFT", "INTC"] and held == []   # high confidence first

# 2) Daily caps: SHORT 7, LONG 2; the rest is held; the count resets the next day
many = [flag(f"S{i}", rvol=i + 2) for i in range(9)] + [flag(f"L{i}", "LONG") for i in range(3)]
rd = {f["ticker"]: read() for f in many}
st = {}
send, held = mailer.pick_alerts(many, rd, st, today)
assert sum(a["horizon"] == "SHORT" for a in send) == 7 and sum(a["horizon"] == "LONG" for a in send) == 2
assert len(held) == 3 and st["email_budget"] == {"date": "2026-10-05", "SHORT": 7, "LONG": 2, "VIP": 0}
assert [a["ticker"] for a in send if a["horizon"] == "SHORT"][:2] == ["S8", "S7"]   # highest RVOL first
send2, held2 = mailer.pick_alerts([flag("X")], {"X": read()}, st, today)
assert send2 == [] and len(held2) == 1                                   # cap already used today
send3, _ = mailer.pick_alerts([flag("X")], {"X": read()}, st, date(2026, 10, 6))
assert len(send3) == 1                                                   # new day, new budget

# 3) Two rules on one name become one alert; the email always ends with the footer
send, _ = mailer.pick_alerts([flag("WDC", rule="gap_up"), flag("WDC")], {"WDC": read()}, {}, today)
assert len(send) == 1
subject, body = mailer.compose(send)
assert subject == mailer.headline(send[0]) and body.rstrip().endswith(mailer.FOOTER)
assert "Sources: https://news.example/a" in body
assert "gap_up" not in body and "breakout_20d" not in body and "RVOL" not in body    # no code words
two, _ = mailer.pick_alerts([flag("A"), flag("B")], {"A": read(), "B": read()}, {}, today)
assert mailer.compose(two)[0] == "AI Market Desk: 2 alerts · A, B"


# 4) Sending: 465 first, 587 as fallback on a dropped connection; never retried on a bad password
import smtplib


class FakeSMTP:
    sent, ports = [], []
    def __init__(self, host, port, timeout):
        assert host == "smtp.gmail.com"; FakeSMTP.ports.append(port); self.port = port
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def starttls(self): pass
    def login(self, user, pw): FakeSMTP.login_args = (user, pw)
    def send_message(self, msg): FakeSMTP.sent.append(msg)


for k in ("DESK_EMAIL", "ALERT_TO", "GMAIL_APP_PASSWORD"):
    os.environ.pop(k, None)
assert mailer.send("s", "b", smtp_factory=FakeSMTP) is False and FakeSMTP.sent == []   # settings missing
os.environ.update(DESK_EMAIL="desk@example.com", ALERT_TO="hanson@example.com",
                  GMAIL_APP_PASSWORD="abcd efgh ijkl mnop\n")
assert mailer.send(subject, body, smtp_factory=FakeSMTP) is True and FakeSMTP.ports == [465]
m = FakeSMTP.sent[-1]
assert FakeSMTP.login_args == ("desk@example.com", "abcdefghijklmnop")
assert m["To"] == "hanson@example.com" and m["From"] == "AI Market Desk <desk@example.com>"


class DropsOn465(FakeSMTP):
    def login(self, user, pw):
        if self.port == 465:
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        super().login(user, pw)


FakeSMTP.ports = []
assert mailer.send("s", "b", smtp_factory=DropsOn465) is True and FakeSMTP.ports == [465, 587]


class BadPassword(FakeSMTP):
    def login(self, user, pw): raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")


FakeSMTP.ports = []
assert mailer.send("s", "b", smtp_factory=BadPassword) is False and FakeSMTP.ports == [465]
assert mailer._reason(smtplib.SMTPAuthenticationError(535, b"x"), "login", 465) == \
    "SMTPAuthenticationError at login on port 465 (server code 535)"

# 5) Scanner wiring: dry run previews and sends nothing; a real run marks Pinged in the Signal Log
f1 = s.Flag("MSFT", "SHORT", "breakout_20d", "Above 20-day high 522.85", 525.18, 1.64, 1.48, "2026-10-05", 522.85)
f2 = s.Flag("EQB.TO", "SHORT", "rvol_spike", "RVOL 3.7x", 167.22, 3.7, -0.5, "2026-10-05")
rd = {"MSFT": read(conf="high"), "EQB.TO": read("NOISE")}
args = NS(dry_run=True, email=None)
calls = []
mailer_send = mailer.send
mailer.send = lambda subj, b, html_body="": calls.append(subj) or True
p = s.email_alerts([f1, f2], rd, {}, today, args)
assert calls == [] and args.email["subject"] == "MSFT up 1.5%: broke above its 20-day high of $522.85" and p == {}
args = NS(dry_run=False, email=None)
p = s.email_alerts([f1, f2], rd, {}, today, args)
assert calls and p == {"MSFT": "yes"}
# a failed send refunds today's budget and marks the run as failed
mailer.send = lambda subj, b, html_body="": False
st = {}
args = NS(dry_run=False, email=None)
p_fail = s.email_alerts([f1, f2], rd, st, today, args)
assert p_fail == {"MSFT": "failed"} and args.email_failed is True
assert st["email_budget"]["SHORT"] == 0          # the failed alert did not use the cap
mailer.send = mailer_send


class WS:
    def __init__(self): self.rows = [s.LOG_COLUMNS]
    def row_values(self, i): return self.rows[i - 1] if len(self.rows) >= i else []
    def append_rows(self, rows, value_input_option=None): self.rows += rows


ws = WS()
s.Sheet(spreadsheet=NS(worksheet=lambda n: ws)).append_log([f1, f2], rd, p)
pinged = {r[1]: r[s.LOG_COLUMNS.index("Pinged")] for r in ws.rows[1:]}
assert pinged == {"MSFT": "yes", "EQB.TO": "no"}

# 6) Call line: fixed rules; low confidence and sector or market-wide moves never trim
def alert(direction="ACCUMULATION", conf="medium", horizon="SHORT", rule="breakout_20d", scope="company"):
    return {"ticker": "X", "horizon": horizon, "flags": [flag("X", horizon, rule=rule)],
            **read(direction, conf), "scope": scope}
assert mailer.call(alert(), "Holding") == "Watch, do not chase"
assert mailer.call(alert(horizon="LONG", rule="pullback_200dma"), "Watchlist") == "Consider adding"
assert mailer.call(alert("DISTRIBUTION", "high"), "Holding") == "Consider trimming"
assert mailer.call(alert("DISTRIBUTION", "medium"), "Holding") == "Watch"            # CEG, Oct 7
assert mailer.call(alert("DISTRIBUTION", "high", scope="macro"), "Holding") == "Watch"   # SIVR, Oct 7
assert mailer.call(alert("DISTRIBUTION", "high"), "Watchlist") == "Watch"
assert mailer.call(alert("DISTRIBUTION", "low", horizon="LONG", rule="trim_level_hit"), "Holding") == "Consider trimming"

# 7) Plain English: Levels line between Why and Call; no Chartist read, no Levels line
a = alert(); a["chartist"] = {"trend": "up", "timeframe": "daily", "support": 42.2, "resistance": 45.04,
                            "watch": "A close back below 45.04 fails the breakout."}
lines = mailer.format_alert(a).splitlines()
assert lines[3] == "Levels: Support $42.20, resistance $45.04. A close back below 45.04 fails the breakout."
assert lines[4] == "Call: Watch, do not chase."
assert lines[5] == "Read: Buying, driven by company news (medium confidence)."
assert not any(l.startswith("Levels") for l in mailer.format_alert(alert()).splitlines())

# 8) Reader-friendly wording (team feedback, Oct 10)
def f2(rule, ref, t="TSLA", price=387.12, chg=3.2, rvol=2.0):
    return {"ticker": t, "horizon": "SHORT", "rule": rule, "detail": "", "price": price, "rvol": rvol,
            "day_change_pct": chg, "bar_date": "2026-10-09", "ref": ref}
tsla = {"ticker": "TSLA", "name": "Tesla", "horizon": "SHORT", "flags": [f2("breakout_20d", 386.83)],
        **read(conf="high"), "scope": "company", "call": "Watch, do not chase"}
assert mailer.headline(tsla) == "TSLA up 3.2%: broke above its 20-day high of $386.83"
body = mailer.format_alert(tsla)
assert body.startswith("Tesla (TSLA) · Short-term signal (days to weeks)\n"
                       "What happened: It broke above its 20-day high of $386.83. "
                       "It is now $387.12, up 3.2% today, on 2.0x normal volume for this time of day."), body
bbd = {"ticker": "GIB-A.TO", "name": "CGI", "horizon": "LONG",
       "flags": [dict(f2("pullback_200dma", 288.27, "GIB-A.TO", 293.77, -0.2, 0.5), horizon="LONG")],
       **read(), "scope": "sector"}
assert mailer.headline(bbd) == "GIB-A (TSX) down 0.2%: pulled back to within 2% of its 200-day average of C$288.27"
body = mailer.format_alert(bbd)
assert body.startswith("CGI (GIB-A, TSX) · Long-term entry signal") and ".TO" not in body
assert "Read: No news that breaks the thesis, driven by a sector-wide move" in body
assert mailer.show_ticker("IMO.TO") == "IMO (TSX)" and mailer.show_ticker("FANG") == "FANG"
assert mailer.what_happened(f2("gap_down", -3.1)) == "opened 3.1% below yesterday's close"

# 9) Not emailed: low confidence, and SHORT moves on thin volume (logged either way)
thin = [flag("IMO.TO", rvol=0.4), flag("FANG", rvol=0.5), flag("LITE", rvol=2.4), flag("GIB", "LONG", rvol=0.5, rule="pullback_200dma"),
        flag("LOW", rvol=3.0)]
send, held = mailer.pick_alerts(thin, {"IMO.TO": read(), "FANG": read(), "LITE": read(), "GIB": read(),
                                       "LOW": read(conf="low")}, {}, today)
assert sorted(a["ticker"] for a in send) == ["GIB", "LITE"] and held == []

# 10) Risk line (Oct 12, ALAB): a long-term pullback on a sharp down day keeps its Call,
#     gets a Risk line, and is tagged in the Signal Log for the Scorecard
alab_f = dict(f2("pullback_200dma", 292.64, "ALAB", 295.56, -5.5, 1.3), horizon="LONG")
alab = {"ticker": "ALAB", "name": "Astera Labs", "horizon": "LONG", "flags": [alab_f],
        **read(), "scope": "sector", "call": "Consider adding"}
body = mailer.format_alert(alab).splitlines()
assert body[3] == "Call: Consider adding."
assert body[5] == ("Risk: Down 5.5% today on 1.3x normal volume, so selling may not be finished. "
                   "A daily close below the 200-day average ($292.64) would break the setup."), body[4]
calm = dict(alab, flags=[dict(alab_f, day_change_pct=-0.6)])
assert not any(l.startswith("Risk") for l in mailer.format_alert(calm).splitlines())
assert not mailer.sharp_drop(dict(alab_f, rvol=0.8))                       # quiet volume: no risk line
assert not mailer.sharp_drop(dict(alab_f, horizon="SHORT"))                # SHORT signals: not this rule
ws2 = WS()
fl = s.Flag("ALAB", "LONG", "pullback_200dma", "+1.0% from 200DMA 292.64", 295.56, 1.3, -5.5, "2026-10-12", 292.64)
s.Sheet(spreadsheet=NS(worksheet=lambda n: ws2)).append_log([fl], {}, {})
assert ws2.rows[1][s.LOG_COLUMNS.index("Rule")].endswith(" | sharp_down_day")
fl.tags = [s.HYPOTHESIS_TAG]                                               # log-only tags follow
s.Sheet(spreadsheet=NS(worksheet=lambda n: ws2)).append_log([fl], {}, {})
assert ws2.rows[-1][s.LOG_COLUMNS.index("Rule")].endswith(f" | sharp_down_day | {s.HYPOTHESIS_TAG}")

# 11) HTML version: bold labels, one line per section, escaped text, clickable sources
alab_bad = dict(alab, catalyst='Optics <script>alert(1)</script> & "AI" demand',
                links=["https://www.gurufocus.com/news/1/x?a=1&b=2", "javascript:alert(1)"])
h = mailer.compose_html([alab_bad, tsla])
assert "<b>What happened:</b>" in h and "<b>Why:</b>" in h and "<b>Call:</b>" in h and "<b>Risk:</b>" in h
assert "<script>" not in h and "&lt;script&gt;" in h                       # model text is escaped
assert 'href="https://www.gurufocus.com/news/1/x?a=1&amp;b=2"' in h and ">gurufocus.com</a>" in h
assert "javascript:" not in h                                               # only web links become links
assert h.count("<hr") == 2 and h.rstrip().endswith("</p></div>")
_, plain = mailer.compose([tsla])
assert plain.rstrip().endswith(mailer.FOOTER)

class Capture:
    msgs = []
    def __init__(self, host, port, timeout): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, u, p): pass
    def send_message(self, m): Capture.msgs.append(m)
os.environ.update(DESK_EMAIL="desk@example.com", ALERT_TO="me@example.com", GMAIL_APP_PASSWORD="abcd efgh")
assert mailer.send("s", plain, smtp_factory=Capture, html_body=h)
m = Capture.msgs[-1]
assert m.is_multipart() and [p.get_content_type() for p in m.iter_parts()] == ["text/plain", "text/html"]

# 12) Chartist (Newton-style) view appears under Levels, labelled simulated
nv = dict(tsla, chartist={"support": 386.83, "resistance": 391.13, "watch": "w", "view": "Uptrend; leading SMH."})
secs = [k for k, _ in mailer.sections(nv)]
assert secs[secs.index("Levels") + 1] == "Chartist, Newton-style (simulated)"
assert "<b>Chartist, Newton-style (simulated):</b> Uptrend; leading SMH." in mailer.compose_html([nv])

# 13) New wording: fast moves, gap reversals, earnings today
assert mailer.what_happened(f2("fast_move", 3.4)) == "moved 3.4% higher in the last 30 minutes"
assert mailer.what_happened(f2("fast_move", -3.1)) == "moved 3.1% lower in the last 30 minutes"
assert mailer.what_happened(f2("gap_filled", -5.0)).startswith("opened 5.0% lower but has climbed back")
assert mailer.what_happened(f2("gap_failed", 4.2)).startswith("opened 4.2% higher but has given it all back")
amgn = dict(f2("earnings_soon", None), detail="Earnings 2026-10-13 (0 trading days)")
assert mailer.what_happened(amgn) == "reports earnings today"
assert mailer.what_happened(dict(amgn, detail="Earnings 2026-10-15 (2 trading days)")) == "has earnings coming up (2026-10-15)"
gf = {"ticker": "X", "horizon": "SHORT", "flags": [f2("gap_filled", -5.0)], **read(), "scope": "company"}
assert mailer.call(gf, "Holding") == "Consider adding"                 # a bought-back gap is a timely entry

# 14) Opening digest: same quality bar as alerts, no cap, grouped by theme
op = [flag(t, rvol=2.4) for t in ("MU", "WDC", "STX", "SNDK", "AMGN", "THIN")] + [flag("NEM", "LONG", rule="pullback_200dma")]
op[-2]["rvol"] = 0.8                                                      # THIN: not enough volume
rd = {t: read("DISTRIBUTION", "high") for t in ("MU", "WDC", "STX", "SNDK", "AMGN", "THIN", "NEM")}
rd["AMGN"] = read("NOISE")
dg = mailer.pick_digest(op, rd)
assert sorted(a["ticker"] for a in dg) == ["MU", "SNDK", "STX", "WDC"]   # no NOISE, no thin volume, no LONG
themes = {"MU": "Memory / semis", "WDC": "Memory / storage", "STX": "Memory / storage", "SNDK": "Memory / storage"}
subj, body, h = mailer.compose_digest(dg, themes)
assert subj.startswith("AI Market Desk: opening digest · 4 names · ")
assert body.index("MEMORY / STORAGE") < body.index("MEMORY / SEMIS")    # biggest group first
assert "<b>Memory / storage</b>" in h and body.rstrip().endswith(mailer.FOOTER)

# 15) VIP: news with its link, fast moves, within a daily cap
items = [{"ticker": "SPCX", "type": "news", "headline": "Starship flight cleared <b>", "source": "Reuters",
          "url": "https://www.reuters.com/x"},
         {"ticker": "TSLA", "type": "price_move", "headline": "+3.2% in 15 min, last 412.30", "source": "", "url": ""}]
subj, body, h = mailer.compose_vip(items, {"TSLA": "Tesla"})
assert subj == "VIP · 2 items · SPCX, TSLA"
assert "News: Starship flight cleared <b> (Reuters)" in body and "Tesla (TSLA)\nFast move: +3.2% in 15 min" in body
assert "&lt;b&gt;" in h and ">reuters.com</a>" in h
st = {}
# The cap counts emails, not headlines (Oct 19: 6 overnight headlines in one email used it all)
assert mailer.take_vip(st, today, 6) == 6 and st["email_budget"]["VIP"] == 1
assert mailer.take_vip(st, today, 0) == 0 and st["email_budget"]["VIP"] == 1   # nothing to send, nothing used
assert mailer.take_vip(st, today, 20) == mailer.VIP_ITEMS_PER_EMAIL             # one email carries at most 8
for _ in range(4):
    assert mailer.take_vip(st, today, 1) == 1
assert st["email_budget"]["VIP"] == 6 and mailer.take_vip(st, today, 1) == 0    # 6 emails, then held
assert mailer.take_vip(st, date(2026, 10, 6), 1) == 1                    # new day, new cap

# 16) Wiring: at the open, SHORT hits go in the digest and keep the caps free; LONG still alerts
fl = [s.Flag("MU", "SHORT", "gap_down", "Gap -5.6%", 944.84, 2.4, -4.5, "2026-10-13", -5.6),
      s.Flag("NEM", "LONG", "pullback_200dma", "x", 187.82, 1.0, -0.6, "2026-10-13", 189.41)]
rd2 = {"MU": read("DISTRIBUTION", "high"), "NEM": read()}
st = {}
args = NS(dry_run=True, email=None, digest=None, opening=True)
p = s.email_alerts(fl, rd2, st, today, args)
assert p == {"MU": "digest"} and args.digest["subject"].startswith("AI Market Desk: opening digest · 1 name · MU")
assert args.email["subject"].startswith("NEM ")                          # the LONG alert still goes
assert st["email_budget"]["SHORT"] == 0                                   # the digest used no cap
args = NS(dry_run=True, email=None, digest=None, opening=False)
p = s.email_alerts(fl, rd2, {}, today, args)
assert args.digest is None and "MU" in args.email["subject"]             # after 10:15: normal alerts

# 17) Day move wording
assert mailer.what_happened(f2("day_move", 2.1, chg=-4.2)) == "is down 4.2% on the day, about 2.1 times its normal daily range"

# 18) Strategist (Lee-style) line and real comments: labelled, dated, linked in Sources
c = {"who": "Tom Lee", "date": "2026-10-05", "where": "CNBC", "summary": "Expects dip buying into year-end.",
     "tickers": [], "sectors": ["semis"], "url": "https://www.cnbc.com/2026/10/05/tom-lee-dip.html"}
lv2 = dict(tsla, strategist={"view": "Healthy tape; dips get bought.", "said": [c]})
secs = dict(mailer.sections(lv2))
assert secs["Strategist, Lee-style (simulated)"] == "Healthy tape; dips get bought."
assert secs["What they said"] == "Tom Lee (Oct 5, CNBC): Expects dip buying into year-end. [cnbc.com]"
assert mailer.all_links(lv2)[-1] == c["url"] and c["url"] in mailer.format_alert(lv2)
assert ">cnbc.com</a>" in mailer.compose_html([lv2])
# Lens votes: each lens line leads with its action; trim on a name not held reads "Hold off"
lv3 = dict(tsla, held=False, chartist={"trend": "up", "timeframe": "daily", "support": 1.0, "resistance": 2.0,
                                     "watch": "x", "view": "Stretched into resistance.", "action": "trim"},
           strategist={"view": "Healthy tape; dips get bought.", "said": [], "action": "add"})
secs = dict(mailer.sections(lv3))
assert secs["Chartist, Newton-style (simulated)"] == "Hold off. Stretched into resistance."
assert secs["Strategist, Lee-style (simulated)"] == "Add. Healthy tape; dips get bought."
assert dict(mailer.sections(dict(lv3, held=True)))["Chartist, Newton-style (simulated)"].startswith("Trim. ")
assert dict(mailer.sections(dict(lv3, strategist={"view": "v", "said": []})))["Strategist, Lee-style (simulated)"] == "v"
# One-sided levels: the far side is named, not hidden (Oct 16, HACK)
hack = {"support": None, "resistance": 98.86, "watch": "Reclaim 98.86 to stabilize.",
        "far": {"support": {"price": 86.84, "name": "52-week low"}}}
assert mailer.levels_text(hack) == ("No support nearby (next is the 52-week low at $86.84, far below), "
                                    "resistance $98.86. Reclaim 98.86 to stabilize.")
# A LONG signal sold with its sector reads as selling, not "thesis at risk" (Oct 16, QQQM)
qqqm = alert("DISTRIBUTION", "medium", horizon="LONG", rule="pullback_200dma", scope="sector")
assert dict(mailer.sections(dict(qqqm, catalyst="Rotation")))["Read"].startswith("Selling, driven by a sector-wide move")
assert "thesis" in dict(mailer.sections(dict(qqqm, scope="company", catalyst="c")))["Read"]
subj, body, h = mailer.compose_digest(dg, themes, "S&P 500 5.9% above its 200-day average.")
assert body.splitlines()[1] == "Market backdrop: S&P 500 5.9% above its 200-day average."
assert "<b>Market backdrop:</b>" in h

# 19) Oct 14: subjects without repeats, digest lines with at most two events, Strategist read in the digest
hack = {"ticker": "HACK", "horizon": "SHORT", "flags": [f2("day_move", 1.5, "HACK", 103.86, -3.4, 1.5)], **read()}
assert mailer.headline(hack) == "HACK down 3.4%: about 1.5x its normal daily range"
nem = {"ticker": "NEM", "horizon": "SHORT", "flags": [f2("rvol_spike", 2.8, "NEM", 176.2, -5.1, 2.8)], **read()}
assert mailer.headline(nem) == "NEM down 5.1%: trading 2.8x its normal volume for this time of day"
nbis = {"ticker": "NBIS", "horizon": "SHORT", **read("DISTRIBUTION"), "scope": "sector",
        "flags": [f2("rvol_spike", 4.0, "NBIS"), f2("gap_down", -5.7, "NBIS"),
                  f2("breakdown_20d", 197.6, "NBIS"), f2("fast_move", -4.5, "NBIS")]}
line = mailer.digest_line(nbis)
assert line.count(" and ") == 1 and "opened 5.7% below" in line and "20-day low" in line
_, body, h = mailer.compose_digest([nbis], {}, "S&P 500 7.0% above.", "Hot CPI lifted yields.")
assert body.splitlines()[1:3] == ["Market backdrop: S&P 500 7.0% above.", "Strategist, Lee-style market read (simulated): Hot CPI lifted yields."]
assert "<b>Strategist, Lee-style market read (simulated):</b>" in h

# 20) A name already in today's digest is not alerted again unless something new happens
st = {"digest": {"date": today.isoformat(), "tickers": ["NEM"]}}
fl2 = [s.Flag("NEM", "SHORT", "rvol_spike", "x", 176.2, 2.8, -5.1, "2026-10-05", 2.8),
       s.Flag("NEM", "SHORT", "fast_move", "x", 176.2, 2.8, -5.1, "2026-10-05", -3.4),
       s.Flag("IAU", "SHORT", "breakdown_20d", "x", 373.96, 1.5, -2.0, "2026-10-05", 374.23)]
args = NS(dry_run=True, email=None, digest=None, opening=False)
p = s.email_alerts(fl2, {"NEM": read("DISTRIBUTION", "high"), "IAU": read("DISTRIBUTION", "high")}, st, today, args)
assert "moved 3.4% lower in the last 30 minutes" in args.email["body"]         # the new story goes out
assert "trading 2.8x its normal volume" not in args.email["body"]             # the repeat does not
assert "IAU" in args.email["subject"]
args = NS(dry_run=True, email=None, digest=None, opening=False)
p = s.email_alerts(fl2[:1], {"NEM": read("DISTRIBUTION", "high")}, st, today, args)
assert args.email is None and p == {"NEM": "in digest"}

print("all mailer tests passed")

# Oct 20 pre-mortem: ALERT_TO with semicolons or spaces still reaches everyone
assert mailer.recipients("a@x.com; Alex.Kim@example-capital.com") == "a@x.com, Alex.Kim@example-capital.com"
assert mailer.recipients("a@x.com alex@example-capital.com, a@x.com") == "a@x.com, alex@example-capital.com"
assert mailer.recipients('"Kim, Alex" <alex@example-capital.com>') == "alex@example-capital.com"
os.environ["ALERT_TO"] = "a@x.com; alex@example-capital.com"
assert mailer.send("s", "b", smtp_factory=FakeSMTP)
assert FakeSMTP.sent[-1]["To"] == "a@x.com, alex@example-capital.com"
print("recipients ok")

# Oct 21 (SNDK): a gap that reversed is described once, by the reversal
_fl = [{"ticker": "SNDK", "horizon": "SHORT", "rule": "gap_up", "detail": "", "price": 1380.0, "rvol": 2.2,
        "day_change_pct": -0.3, "ref": 3.8},
       {"ticker": "SNDK", "horizon": "SHORT", "rule": "gap_failed", "detail": "", "price": 1380.0, "rvol": 2.2,
        "day_change_pct": -0.3, "ref": 3.8}]
_line = mailer.digest_line({"ticker": "SNDK", "name": "Sandisk", "horizon": "SHORT", "flags": _fl,
                            "direction": "DISTRIBUTION", "scope": "sector", "catalyst": "x", "links": ["https://a.example"]})
assert _line.count("opened 3.8%") == 1 and "given it all back" in _line, _line
print("gap wording ok")

# Oct 22: a 15-name digest; at most 8 are written out, the rest named in one line
def _dg(t, d):
    return {"ticker": t, "horizon": "SHORT", "direction": "DISTRIBUTION", "scope": "sector", "confidence": "high",
            "catalyst": "x", "links": ["https://a.example/x"],
            "flags": [{"ticker": t, "horizon": "SHORT", "rule": "gap_down", "detail": "", "price": 10.0, "rvol": 2.0,
                       "day_change_pct": d, "ref": d}]}
_items = [_dg(f"N{i}", -1.0 - i) for i in range(15)]
_sub, _body, _html = mailer.compose_digest(_items, {})
assert _sub.startswith("AI Market Desk: opening digest · 15 names · ") and _sub.endswith("+7 more"), _sub
assert _body.count("- N") == 8 and "Also moved: N8 down 9.0%" in _body and "N14 down 15.0%" in _body
assert "Also moved:" in _html
_sub, _body, _ = mailer.compose_digest(_items[:3], {})
assert "more" not in _sub and "Also moved" not in _body
print("digest cap ok")

# Oct 28 ROP: below the 200-day and rising toward it is not "pulled back to"
_f = {"ticker": "ROP", "horizon": "LONG", "rule": "pullback_200dma", "detail": "", "price": 392.0, "ref": 396.11,
      "rvol": 1.0, "day_change_pct": 2.2}
assert mailer.what_happened(_f, "ROP").startswith("is just below its 200-day average of $396.11")
assert mailer.headline({"ticker": "ROP", "flags": [_f]}) == "ROP up 2.2%: just below its 200-day average of $396.11, within 2%", mailer.headline({"ticker": "ROP", "flags": [_f]})
_f2 = dict(_f, price=399.0, day_change_pct=-1.0)
assert mailer.what_happened(_f2, "ROP").startswith("pulled back to within 2%")
print("pullback wording ok")

# No mail server: DESK_EMAIL_SINK writes each email to a folder, numbered in order
import tempfile as _tf
_sink = _tf.mkdtemp()
os.environ["DESK_EMAIL_SINK"] = _sink
assert mailer.send("NVDA up 3.0%: broke above its 20-day high", "body", html_body="<p>x</p>")
assert mailer.send("Second", "b2")
_files = sorted(os.listdir(_sink))
assert _files == ["001-nvda-up-3-0-broke-above-its-20-day-high.html", "001-nvda-up-3-0-broke-above-its-20-day-high.txt",
                  "002-second.txt"], _files
os.environ.pop("DESK_EMAIL_SINK")
print("email sink ok")
