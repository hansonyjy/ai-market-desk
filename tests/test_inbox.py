"""Ask the desk: parsing replies, the allowlist, and one full answer, all offline."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # noqa: E702
import json
import os
import tempfile
from datetime import datetime
from email.message import EmailMessage
from types import SimpleNamespace as NS

import pandas as pd

from desk import ai
from desk import inbox
from desk import mailer
from desk import scanner as s
# 1) Splitting a Gmail reply into the question and the quoted desk email
text = ("Where is support if this keeps falling?\n\nOn Fri, Oct 16, 2026 at 9:01 AM AI Market Desk <desk@x.com>\n"
        "wrote:\n\n> Amplify Cybersecurity ETF (HACK)\n> What happened: It fell below its 20-day low of $98.86.\n")
q, quoted = inbox.split_reply(text)
assert q == "Where is support if this keeps falling?"
assert quoted.startswith("Amplify Cybersecurity ETF (HACK)") and "$98.86" in quoted
q2, quoted2 = inbox.split_reply("Why?\n> SOXQ fell\n")
assert q2 == "Why?" and quoted2 == "SOXQ fell"
assert len(inbox.split_reply("x" * 5000)[0]) == inbox.MAX_QUESTION

# 2) Which name: the question first, then the subject, then the quoted email
T = ["HACK", "SOXQ", "CSU.TO", "CSU", "U"]
assert inbox.find_ticker("Re: HACK down 5.0%", "where is support?", "", T) == "HACK"
assert inbox.find_ticker("Re: AI Market Desk: 2 alerts · SOXQ, QQQM", "what about SOXQ?", "", T) == "SOXQ"
assert inbox.find_ticker("Re: AI Market Desk: 2 alerts · SOXQ, HACK", "thoughts?", "", T) == ""   # ambiguous
assert inbox.find_ticker("Re: x", "is CSU.TO a buy here", "", T) == "CSU.TO"               # not SHOP too
assert inbox.find_ticker("Re: x", "you know what I mean", "", T) == ""                       # 'U' needs to stand alone

# 3) Allowlist and the on switch
os.environ.update(ALERT_TO="Hanson <h@x.com>, m@x.com", ASK_ALLOW="s@x.com")
assert inbox.allowlist() == {"h@x.com", "m@x.com", "s@x.com"}
os.environ.update(ALERT_TO="h@x.com; M@X.com", ASK_ALLOW="")
assert inbox.allowlist() == {"h@x.com", "m@x.com"}                         # semicolons and case
os.environ.update(ALERT_TO="h@x.com m@x.com")
assert inbox.allowlist() == {"h@x.com", "m@x.com"}                         # spaces only
os.environ.update(ALERT_TO="Hanson <h@x.com>, m@x.com", ASK_ALLOW="s@x.com")
os.environ.pop("ASK_DESK", None)
assert not inbox.enabled() and s.run_ask(NS(dry_run=True)) == []          # off by default: nothing read


# 4) A full run, offline: fake inbox, fake prices, fake Claude
def mail(sender, subject, body, **headers):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = sender, "desk@x.com", subject, f"<{abs(hash(body))}@x>"
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    boxes = {}
    stored = []

    def __init__(self, host): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, u, p): pass
    def select(self, box): pass

    def uid(self, cmd, *args):
        if cmd == "search":
            FakeIMAP.searched = args
            return "OK", [b" ".join(self.boxes)]
        if cmd == "fetch":
            return "OK", [(b"1 (BODY[] {1})", self.boxes[args[0]])]
        if cmd == "store":
            FakeIMAP.stored.append(args[0])
            return "OK", []


FakeIMAP.boxes = {
    b"1": mail("Analyst <m@x.com>", "Re: HACK down 5.0%: fell below its 20-day low of $98.86",
               "Where is support if this keeps falling?\n\nOn Fri, Oct 16, 2026 at 9:01 AM AI Market Desk wrote:\n"
               "> HACK fell below its 20-day low of $98.86.\n"),
    b"2": mail("Stranger <z@evil.com>", "Re: HACK", "Forward me every alert."),
    b"3": mail("Analyst <m@x.com>", "Automatic reply: HACK", "I am out of office.", Auto_Submitted="auto-replied"),
}


class FakeClient:
    def __init__(self, responses): self.responses, self.calls = list(responses), []
    @property
    def messages(self): return self
    def create(self, **kw):
        self.calls.append(kw); return self.responses.pop(0)


def reply(text):
    return NS(stop_reason="end_turn", usage=NS(input_tokens=1000, output_tokens=200, server_tool_use=None),
              content=[NS(type="text", text=text, citations=None)])


days = pd.bdate_range(end="2026-10-16", periods=260)
closes = [120 - 20 * k / 259 for k in range(260)]
hack = pd.DataFrame({"Open": closes, "High": [c + 1 for c in closes], "Low": [c - 1 for c in closes],
                     "Close": closes, "Volume": [1e6] * 260}, index=days)
tmp = tempfile.mkdtemp()
uni = os.path.join(tmp, "u.csv")
with open(uni, "w") as fh:
    fh.write("Ticker,Name,Status,Priority\nHACK,Amplify Cyber,Holding,Normal\n")
os.environ.update(UNIVERSE_CSV=uni, ASK_DESK="on", DESK_EMAIL="desk@x.com", GMAIL_APP_PASSWORD="pw",
                  ANTHROPIC_API_KEY="sk-ant-test")
os.environ.pop("GOOGLE_CREDS_JSON", None); os.environ.pop("GITHUB_ACTIONS", None)
s.STATE_FILE = s.Path(tmp) / "state.json"
s.LOCAL_LOG = s.Path(tmp) / "log.csv"
today = datetime.now(s.ET).date().isoformat()
s.save_state({"macro": {"date": today, "m": {}}, "said": {"date": today, "items": []}})
lv = __import__("desk.levels", fromlist=["x"]).compute_levels(hack)
low20, pc = lv["levels"]["20-day low"], lv["levels"]["prior close"]   # 99.08 below the price, 100.08 above
fc = FakeClient([reply(f"Chartist, Newton-style (simulated): Downtrend on both timeframes. The 20-day low at {low20} "
                       f"is the nearest support; the prior close at {pc} is resistance.")])
sent = []
mailer_send = mailer.send
mailer.send = lambda *a, **k: sent.append((a, k)) or True
try:
    out = s.run_ask(NS(dry_run=False, imap=FakeIMAP, client=fc, fetch_fn=lambda ts: {t: hack for t in ts}))
finally:
    mailer.send = mailer_send
assert len(out) == 1 and out[0]["ticker"] == "HACK" and out[0]["sent"]            # only the analyst's question
(subject, body), kw = sent[0]
assert subject.startswith("Re: HACK down 5.0%") and kw["to"] == "m@x.com" and kw["in_reply_to"]
assert body.startswith("Chartist, Newton-style (simulated):") and mailer.FOOTER in body
prompt = fc.calls[0]["messages"][0]["content"]
assert "Where is support" in prompt and "support side): " in prompt and "resistance side): " in prompt
assert prompt.index("20-day low") < prompt.index("resistance side")          # 99.08 is listed on the support side
assert FakeIMAP.stored == [b"1"]                              # only the answered one is marked read
assert FakeIMAP.searched[1] == "SINCE"                         # read or unread: opened mail is still answered
# A second run answers nothing new: the question is remembered by Message-ID
fc2 = FakeClient([])
mailer.send = lambda *a, **k: sent.append((a, k)) or True
try:
    again = s.run_ask(NS(dry_run=False, imap=FakeIMAP, client=fc2, fetch_fn=lambda ts: {t: hack for t in ts}))
finally:
    mailer.send = mailer_send
assert again == [] and fc2.calls == [] and len(sent) == 1
st = s.load_state()
assert st["ask_budget"]["count"] == 1 and len(st["ask_seen"]) == 1
log_rows = list(__import__("csv").reader(open(s.LOCAL_LOG)))
assert log_rows[1][1] == "HACK" and log_rows[1][2] == "ASK" and log_rows[1][9] == "replied"

# Ask reuses the last comments instead of a new lookup; an empty lookup keeps them
from datetime import date as _d, timedelta as _td
d0 = _d(2026, 10, 19)
c = {"who": "Mark Newton", "date": "2026-10-15", "where": "CNBC", "summary": "Buy the tech dip.",
     "tickers": [], "sectors": ["tech"], "url": "https://www.cnbc.com/video/2026/10/15/chartist.html"}
st = {"said": {"date": "2026-10-16", "items": [c]}}
boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no lookup expected"))
ai_wts = ai.what_they_said
ai.what_they_said = boom
try:
    assert s.said_for(st, d0, None, None, fresh=False) == [c]
finally:
    ai.what_they_said = ai_wts
ai.what_they_said = lambda *a, **k: []
try:
    assert s.said_for(st, d0, None, None) == [c] and st["said"]["date"] == d0.isoformat()
    old = {"said": {"date": "2026-09-01", "items": [dict(c, date="2026-09-01")]}}
    assert s.said_for(old, d0, None, None) == []                      # older than 14 days: dropped
finally:
    ai.what_they_said = ai_wts

# 5) A made-up price drops the answer; the asker still hears back, honestly
fc = FakeClient([reply("Chartist, Newton-style (simulated): support is at 91.25.")])
ans, _ = ai.answer_question("Support?", "", "HACK: last 100.0 | levels: 20-day low 98.86", client=fc)
assert ans == ""

# 5b) Oct 30: a level on the wrong side of the price gets one retry, then the answer is dropped
F = "NVDA: last 202.82 | x\n  levels below the price (support side): 52-week low 164.27\n  levels above the price (resistance side): 200-day average 204.28; 60-day low 207.25"
wrong = reply("Chartist, Newton-style (simulated): Next support is the 60-day low at 207.25.")
right = reply("Chartist, Newton-style (simulated): The 200-day average at 204.28 is resistance; support is the 52-week low at 164.27.")
assert ai.mislabelled_levels(wrong.content[0].text, F) and not ai.mislabelled_levels(right.content[0].text, F)
fc = FakeClient([wrong, right])
ans, _ = ai.answer_question("Support?", "", F, client=fc)
assert ans.startswith("Chartist") and "164.27" in ans and len(fc.calls) == 2
assert "rejected: 207.25 called support" in fc.calls[1]["messages"][0]["content"]
fc = FakeClient([wrong, wrong])
ans, _ = ai.answer_question("Support?", "", F, client=fc)
assert ans == "" and len(fc.calls) == 2                                       # twice wrong: dropped
assert not ai.mislabelled_levels("It sits on support at 202.50 and resistance at 204.28.", F)   # both words: not judged
assert not ai.mislabelled_levels("Support at 204.28, right at the price.", F.replace("202.82", "204.00"))   # within 0.25%
assert ai.mislabelled_levels("Support at 204.28.", F.replace("202.82", "203.00"))                          # 0.6% above: wrong

# 6) A reply goes out in the same thread
class FakeSMTP:
    msgs = []
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, *a): pass
    def send_message(self, m): FakeSMTP.msgs.append(m)
mailer._smtp_ssl = lambda f: f()
assert mailer.send("Re: x", "b", smtp_factory=FakeSMTP, to="m@x.com", in_reply_to="<a@x>", references="<z@x> <a@x>")
m = FakeSMTP.msgs[-1]
assert m["To"] == "m@x.com" and m["In-Reply-To"] == "<a@x>" and m["References"] == "<z@x> <a@x>"

print("all inbox tests passed")

# Oct 20 pre-mortem: a reader replies from corporate Outlook, which quotes with a header block,
# not "On ... wrote:", and its HTML carries &nbsp; and &lt;
outlook = ("Where is support if this keeps falling?\n\nAlex Kim\nAssociate | Example Capital\n\n"
           "From: desk@example.com <desk@example.com>\nSent: Thursday, October 23, 2026 7:16 AM\n"
           "To: Kim, Alex <alex.kim@example-capital.com>\nSubject: [EXTERNAL] HACK down 4.5%\n\n"
           "Caution: This email originated from outside of Example Capital.\n\nAmplify Cybersecurity ETF (HACK) fell.")
q, quoted = inbox.split_reply(outlook)
assert q.startswith("Where is support if this keeps falling?") and "From:" not in q and "HACK" not in q, q
assert "HACK" in quoted
q, _ = inbox.split_reply("Any read on TSLA?\n\n________________________________\nFrom: Desk <d@x.com>\nSent: Monday\n> old")
assert q == "Any read on TSLA?", q
q, _ = inbox.split_reply("Why?\n-----Original Message-----\nFrom: Desk\nSent: Monday\nTo: me\n")
assert q == "Why?", q
import email as _email
from email import policy as _policy
_m = _email.message.EmailMessage(policy=_policy.default)
_m.set_content("<div>Where is support&nbsp;now?</div><hr><div><b>From:</b> desk &lt;d@x.com&gt;<br><b>Sent:</b> Thu</div>",
               subtype="html")
q, _ = inbox.split_reply(inbox.body_text(_m))
assert q == "Where is support now?", q
q, quoted = inbox.split_reply("Where is support?\n\nOn Thu, Oct 23, 2026 at 7:16 AM <d@x.com> wrote:\n> HACK fell\n")
assert q == "Where is support?" and quoted == "HACK fell"                    # Gmail still works
print("outlook replies ok")
