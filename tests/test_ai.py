"""Offline tests for ai.py and the Tape Reader wiring. No network, no API key.
Run: python test_ai.py"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # noqa: E702
import json
import os
from datetime import date
from types import SimpleNamespace as NS

from desk import ai
from desk import scanner as s
# 1) Every role file with a prompt loads, and the shared rules always come first
for f in ["02-tape-reader.md", "03-desk-head.md", "04-chartist-lens.md",
          "05-strategist-lens.md", "06-street-analyst.md", "07-what-they-said.md", "08-vip-filter.md"]:
    sp = ai.system_prompt(f)
    assert sp.startswith("You are one role on the AI Market Desk"), f
    assert "NOTHING_TO_REPORT" in sp and len(sp) > 500, f
# The scanner has no prompt on purpose
try:
    ai.load_prompt("01-scanner.md"); raise AssertionError("01-scanner.md should have no prompt")
except ValueError:
    pass

# 2) Daily budget: resets each day, stops at the cap
st = {}
d1, d2 = date(2026, 10, 6), date(2026, 10, 7)
assert all(ai.take_budget(st, d1) for _ in range(ai.DAILY_AI_CALLS))
assert not ai.take_budget(st, d1)
assert ai.take_budget(st, d2) and st["ai_budget"] == {"date": "2026-10-07", "calls": 1}

# 3) Parsing: one line per ticker, odd formatting tolerated, unknown tickers and junk ignored
text = """Here is my read.
**SPCX** | Starship flight test cleared by FAA | Accumulation | High, FAA notice | https://a.example/faa https://b.example/x).
CSU.TO | No clear catalyst found | NOISE | low | none
ZZZZ | not flagged | ACCUMULATION | high | https://z.example
U | half a line"""
r = ai.parse_tape(text, ["SPCX", "CSU.TO", "U"])
assert set(r) == {"SPCX", "CSU.TO"}, r
assert r["SPCX"]["direction"] == "ACCUMULATION" and r["SPCX"]["confidence"] == "high"
assert r["SPCX"]["links"] == ["https://a.example/faa", "https://b.example/x"]
assert r["CSU.TO"]["direction"] == "NOISE" and r["CSU.TO"]["links"] == []


# 3b) JSON output (the current format): suffix-less tickers matched, unsourced reads marked
js = """Here you go:
[{"ticker": "EQB", "catalyst": "Q4 preview  notes ahead of earnings", "direction": "noise",
  "confidence": "Medium", "sources": []},
 {"ticker": "HON", "catalyst": "Honeywell Aerospace spin-off completed Oct 2; price reset", "direction": "NOISE",
  "confidence": "high", "sources": ["https://news.example/ctva-spin"]},
 {"ticker": "ZZZ", "catalyst": "not flagged", "direction": "ACCUMULATION", "confidence": "high", "sources": []}]"""
r = ai.parse_tape(js, ["EQB.TO", "HON", "INTC"])
assert set(r) == {"EQB.TO", "HON"}, r                                  # INTC missing, ZZZ ignored
assert r["EQB.TO"]["catalyst"] == "[no source] Q4 preview notes ahead of earnings"
assert r["EQB.TO"]["confidence"] == "low" and r["EQB.TO"]["direction"] == "NOISE"   # no source caps confidence
assert r["HON"] == {"catalyst": "Honeywell Aerospace spin-off completed Oct 2; price reset", "direction": "NOISE",
                     "confidence": "high", "links": ["https://news.example/ctva-spin"], "scope": "unknown"}
assert ai.parse_tape('[{"ticker": "HON", "direction": "NOISE", "scope": "Company-specific", '
                     '"sources": ["https://news.example/x"]}]', ["HON"])["HON"]["scope"] == "company"
assert ai._match_ticker("wcn", ["WCN.TO", "EQB.TO"]) == "WCN.TO"
assert ai._match_ticker("GIB", ["GIB-A.TO"]) is None                   # different share class: not guessed
assert ai.parse_tape("[not json", ["A"]) == {}

# 3c) Source rules: quote pages, homepages and blocked sites are not sources
assert ai.is_real_source("https://www.benzinga.com/markets/tech/26/10/62156369/whats-going-on-with-seagate")
assert not ai.is_real_source("https://robinhood.com/us/en/stocks/MSFT/")
assert not ai.is_real_source("https://www.tradingview.com/symbols/NASDAQ-NVDA/")
assert not ai.is_real_source("https://www.marketbeat.com/stocks/TSE/WCN/")
assert not ai.is_real_source("https://nvidianews.nvidia.com/")
assert not ai.is_real_source("https://www.timothysykes.com/news/cortevainc-ctva-news-2026_10_02-2/")
assert ai.is_real_source("https://www.marketbeat.com/instant-alerts/filing-jnj-2026-10-02/")   # an article is fine
msft = ai.parse_tape('[{"ticker": "MSFT", "catalyst": "Melius upgrade to Buy", "direction": "ACCUMULATION", '
                     '"confidence": "high", "sources": ["https://robinhood.com/us/en/stocks/MSFT/", '
                     '"https://www.whyisstock.com/stocks/MSFT"]}]', ["MSFT"])["MSFT"]
assert msft["links"] == [] and msft["confidence"] == "low" and msft["catalyst"].startswith("[no source]")
# round 3 slips (Oct 5): listing, quote and overview pages, and the same spam network
assert not ai.is_real_source("https://www.cnn.com/markets/stocks/WDC")
assert not ai.is_real_source("https://www.stocktitan.net/news/LITE/")
assert ai.is_real_source("https://www.stocktitan.net/news/LITE/lumentum-to-present-at-ocp-summit-abc123.html")
assert not ai.is_real_source("https://tradingeconomics.com/canada/stock-market")
assert not ai.is_real_source("https://www.defenseworld.net/2026/10/04/cybersecurity-stocks-worth-watching-october-4th.html")
assert not ai.is_real_source("https://www.tickerreport.com/banking-finance/13605979/waste-connections-sets-new-52-week-high.html")
assert not ai.is_real_source("https://example-news.com/quote/AAPL/")
assert ai.is_real_source("https://www.globenewswire.com/news-release/2026/10/01/3372855/0/en/waste-connections-expands.html")
assert not ai.is_real_source("https://247wallst.com/cards/cloudflare-set-a-fresh-52-week-high-at-359-49-01m477f32pe24d8kjw11fhhd8p")
blocked = ai.source_rules()["blocked_domains"]
assert "timothysykes.com" in blocked

# 4) ask(): keeps only the text after the last search, collects citations,
#    sends shared + role prompt, and continues a paused turn
class FakeClient:
    def __init__(self, responses): self.responses, self.calls = list(responses), []
    @property
    def messages(self): return self
    def create(self, **kw):
        self.calls.append(kw); return self.responses.pop(0)


paused = NS(stop_reason="pause_turn", content=[
    NS(type="text", text="Let me search.", citations=None),
    NS(type="server_tool_use"), NS(type="web_search_tool_result")])
done = NS(stop_reason="end_turn", content=[
    NS(type="web_search_tool_result"),
    NS(type="text", text="SPCX | FAA cleared | ACCUMULATION | high | https://a.example/faa",
       citations=[NS(url="https://a.example/faa")])])
fc = FakeClient([paused, done])
out, urls = ai.ask("02-tape-reader.md", "hi", searches=50, client=fc)
assert out.startswith("SPCX | FAA cleared") and "Let me search" not in out, out
assert urls == ["https://a.example/faa"]
assert len(fc.calls) == 2 and fc.calls[0]["tools"][0]["max_uses"] == ai.MAX_SEARCHES_PER_CALL
assert fc.calls[0]["tools"][0]["blocked_domains"] == blocked               # bad sites are never searched
assert fc.calls[0]["system"].startswith("You are one role on the AI Market Desk")

# 4b) Batching: 7 names -> 3 calls of at most NAMES_PER_CALL; the budget can stop mid-way
def line_for(batch_user):
    tks = [l.split(" | ")[0] for l in batch_user.splitlines() if " | " in l]
    return NS(stop_reason="end_turn", content=[NS(type="text", citations=None, text="\n".join(
        f"{t} | catalyst for {t} | ACCUMULATION | medium | https://src.example/{t}" for t in tks))])


class EchoClient:
    def __init__(self): self.calls = []
    @property
    def messages(self): return self
    def create(self, **kw):
        self.calls.append(kw); return line_for(kw["messages"][0]["content"])


many = [{"ticker": t, "horizon": "SHORT", "rule": "gap_up", "detail": "x", "price": 1, "rvol": 2,
         "day_change_pct": 3} for t in ["A", "B", "C", "D", "E", "F", "G"]]
ec = EchoClient()
r = ai.tape_reader(many, today=d1, client=ec)
assert len(ec.calls) == 3 and set(r) == {"A", "B", "C", "D", "E", "F", "G"}, (len(ec.calls), r)
assert all(c["tools"][0]["max_uses"] <= ai.SEARCHES_PER_NAME * ai.NAMES_PER_CALL for c in ec.calls)
assert r["E"]["links"] == ["https://src.example/E"]          # each name keeps its own source
left = iter([True, False])
ec = EchoClient()
r = ai.tape_reader(many, today=d1, client=ec, take_budget=lambda: next(left))
assert len(ec.calls) == 1 and set(r) == {"A", "B", "C"}      # budget stopped the rest

# 5) Scanner wiring: reads land in the Signal Log row; no key or no budget means no call
flag = s.Flag("SPCX", "SHORT", "breakout_20d", "Above 20-day high 159.84", 171.09, 1.83, 2.1, "2026-10-05")
os.environ.pop("ANTHROPIC_API_KEY", None)
assert s.read_tape([flag], {}, d1) == {}                       # no key: skipped, hits still logged
fc = FakeClient([done])
reads = s.read_tape([flag], {}, d1, client=fc)
assert reads["SPCX"]["direction"] == "ACCUMULATION" and len(fc.calls) == 1
full = {"ai_budget": {"date": d1.isoformat(), "calls": ai.DAILY_AI_CALLS}}
fc = FakeClient([done])
assert s.read_tape([flag], full, d1, client=fc) == {} and fc.calls == []   # budget used up


class Boom:
    @property
    def messages(self): raise RuntimeError("API down")


assert s.read_tape([flag], {}, d1, client=Boom()) == {}       # an outage never loses the hits


class FakeWS:
    def __init__(self): self.rows, self.opt = [], None
    def row_values(self, i): return list(self.rows[i - 1]) if len(self.rows) >= i else []
    def update(self, values, range_name="A1"): self.rows[:len(values)] = [list(v) for v in values]
    def append_rows(self, rows, value_input_option=None): self.rows += rows; self.opt = value_input_option


ws = FakeWS()
sh = s.Sheet(spreadsheet=NS(worksheet=lambda n: ws))
sh.append_log([flag], reads)
row = dict(zip(s.LOG_COLUMNS, ws.rows[1]))
assert row["Direction"] == "ACCUMULATION" and row["Confidence"] == "high"
assert row["Catalyst"] == "FAA cleared | Sources: https://a.example/faa", row["Catalyst"]
assert ws.opt == "RAW"                                          # CSU.TO stays text, not a link

# 6) The client: trailing newline stripped; a value that is not a Claude key fails fast
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test123\n"
assert ai._client().api_key == "sk-ant-test123"
os.environ["ANTHROPIC_API_KEY"] = "'sk-ant-test123'"
try:
    ai._client(); raise AssertionError("expected a format error")
except RuntimeError as e:
    assert "sk-ant-" in str(e) and "test123" not in str(e)
os.environ.pop("ANTHROPIC_API_KEY", None)


# 6) Levels: computed from completed bars; the model may only use those prices
import numpy as np
import pandas as pd
from desk import levels as L
idx = pd.bdate_range("2025-08-01", periods=260)
close = pd.Series(np.linspace(80, 120, 260), index=idx)
bars = pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close,
                     "Volume": 1_000_000}, index=idx)
lv = L.compute_levels(bars)
assert lv["trend_daily"] == "up" and lv["trend_weekly"] == "up" and lv["last"] == 120.0
assert lv["levels"]["20-day high"] < 121 and "200-day average" in lv["levels"]
assert L.compute_levels(bars.iloc[:10]) == {}                       # too little history
assert L.near_a_level(lv["levels"]["20-day low"] * 1.005, lv)
assert not L.near_a_level(lv["last"] * 1.2, lv)

ok = ai.check_levels({"support": lv["levels"]["20-day low"], "resistance": lv["levels"]["prior day high"],
                      "trend": "Up", "timeframe": "daily", "watch": "Close below the 20-day low."}, lv)
assert ok and ok["trend"] == "up" and ok["support"] < ok["resistance"]
assert ai.check_levels({"support": lv["levels"]["20-day low"], "resistance": 150}, lv) is None   # made-up price
assert ai.check_levels({"support": lv["last"], "resistance": lv["levels"]["20-day low"]}, lv) is None  # upside down
assert ai.check_levels({"support": "n/a", "resistance": 1}, lv) is None
# Oct 18 (Metro): both levels below the price. "Resistance" the stock has already cleared
# is support, and the watch line built on it is wrong, so the read is dropped.
below = {"support": lv["levels"]["200-day average"], "resistance": lv["levels"]["20-day low"]}
assert below["resistance"] < lv["last"] and ai.check_levels(below, lv) is None
sitting = dict(lv, last=lv["levels"]["prior day high"] * 1.002)                 # price on the level: still fine
assert ai.check_levels({"support": lv["levels"]["20-day low"], "resistance": lv["levels"]["prior day high"]},
                       sitting) is not None

# 7) chartist_lens(): one call, no web search, bad levels dropped, budget respected
def reply(text):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=text, citations=None)])
send = [{"ticker": t, "horizon": "SHORT", "flags": [{"rule": "breakout_20d"}],
         "direction": "ACCUMULATION", "catalyst": "x"} for t in ("AAA", "BBB")]
hi, lo = lv["levels"]["20-day high"], lv["levels"]["20-day low"]
fc = FakeClient([reply(f'[{{"ticker": "AAA", "timeframe": "daily", "trend": "up", "support": {lo}, '
                       f'"resistance": {hi}, "watch": "Back below the 20-day high."}}, '
                       f'{{"ticker": "BBB", "support": {lo}, "resistance": 999}}]')])
got = ai.chartist_lens(send, {"AAA": lv, "BBB": lv}, client=fc)
assert set(got) == {"AAA"} and got["AAA"]["resistance"] == hi
assert len(fc.calls) == 1 and "tools" not in fc.calls[0]
assert fc.calls[0]["output_config"] == {"effort": "low"}                       # a lookup: think briefly
assert "20-day high " + str(hi) in fc.calls[0]["messages"][0]["content"]
fc = FakeClient([reply("[]")])
ai.chartist_lens(send, {"AAA": lv}, client=fc, breadth="40% of the team's 86 names are above their 50-day average")
assert "Breadth today: 40% of the team's 86 names" in fc.calls[0]["messages"][0]["content"]
fc = FakeClient([])
assert ai.chartist_lens(send, {"AAA": lv}, client=fc, take_budget=lambda: False) == {} and fc.calls == []
# Oct 18: 9 alerts used all 3000 tokens before any answer text. Room now grows with
# the number of alerts, and a call that runs out before answering is retried once.
assert ai.chartist_tokens(2) == 3000 and ai.chartist_tokens(9) == 7800 and ai.chartist_tokens(40) == ai.MAX_TOKENS_CAP
cut = NS(stop_reason="max_tokens", content=[NS(type="thinking")])
fc = FakeClient([cut, reply(f'[{{"ticker": "AAA", "support": {lo}, "resistance": {hi}}}]')])
got = ai.chartist_lens(send, {"AAA": lv}, client=fc)
assert set(got) == {"AAA"} and len(fc.calls) == 2, (got, len(fc.calls))
assert fc.calls[1]["max_tokens"] == 2 * fc.calls[0]["max_tokens"]
assert fc.calls[1]["messages"] == fc.calls[0]["messages"]                    # a fresh start, not a continuation
fc = FakeClient([cut, cut])                                                  # retried once only, then gives up
assert ai.chartist_lens(send, {"AAA": lv}, client=fc) == {} and len(fc.calls) == 2
half = NS(stop_reason="max_tokens", content=[NS(type="text", text='[{"ticker": "AAA"', citations=None)])
fc = FakeClient([half])                                                      # some text came back: no retry
ai.chartist_lens(send, {"AAA": lv}, client=fc)
assert len(fc.calls) == 1

# 8) Scanner wiring: levels and Call added before the email; a Chartist failure keeps the alert
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
st = {}
send2 = [dict(send[0], confidence="medium", links=["https://src.example/a"])]
fc = FakeClient([reply(f'[{{"ticker": "AAA", "support": {lo}, "resistance": {hi}, "trend": "up", '
                       f'"timeframe": "daily", "watch": "x"}}]')])
s.add_levels(send2, {"AAA": bars}, {"AAA": {"status": "Holding"}}, st, d1, client=fc, fetch_fn=lambda t: {})
assert send2[0]["call"] == "Watch, do not chase" and send2[0]["chartist"]["support"] == lo
assert st["ai_budget"]["calls"] == 1
class Boom:
    @property
    def messages(self): raise RuntimeError("down")
send3 = [dict(send[0], confidence="medium", links=["https://src.example/a"])]
s.add_levels(send3, {"AAA": bars}, {}, {}, d1, client=Boom(), fetch_fn=lambda t: {})
assert "chartist" not in send3[0] and send3[0]["call"] == "Watch, do not chase"
os.environ.pop("ANTHROPIC_API_KEY")


# 9) Cost controls: Finnhub headlines first, fewer searches, cost counted per call
assert ai.json_array('Levels below. [note] ```json\n[{"ticker": "A"}]\n```') == [{"ticker": "A"}]
assert ai.json_array('{"alerts": [{"ticker": "B"}]}') == [{"ticker": "B"}]
assert ai.json_array("no json here") == []

ec = EchoClient()
flags3 = many[:3]
news = {"A": [{"headline": "A wins contract", "source": "Reuters", "url": "https://r.example/a", "time": "10-07 13:00"}]}
ai.tape_reader(flags3, today=d1, client=ec, news=news)
call = ec.calls[0]
assert "A | 10-07 13:00 | Reuters | A wins contract | https://r.example/a" in call["messages"][0]["content"]
assert call["tools"][0]["max_uses"] == ai.SEARCHES_WITH_NEWS + 2 * ai.SEARCHES_PER_NAME   # A has news, B and C do not
ec = EchoClient()
ai.tape_reader(flags3, today=d1, client=ec)
assert "Finnhub" not in ec.calls[0]["messages"][0]["content"]

before = dict(ai.USAGE)
usage = NS(input_tokens=100_000, output_tokens=2_000, server_tool_use=NS(web_search_requests=4))
fc = FakeClient([NS(stop_reason="end_turn", usage=usage, content=[NS(type="text", text="x", citations=None)])])
ai.ask("02-tape-reader.md", "hi", client=fc)
assert ai.USAGE["calls"] == before["calls"] + 1 and ai.USAGE["searches"] == before["searches"] + 4
assert abs((ai.USAGE["usd"] - before["usd"]) - (0.2 + 0.02 + 0.04)) < 1e-9          # $2/M in, $10/M out, $0.01/search

# 10) Scanner side: headlines fetched without leaking the key, caps respected, dry runs capped
class Resp:
    def __init__(self, ok, data): self.ok, self._d = ok, data
    def json(self): return self._d
os.environ["FINNHUB_KEY"] = "test-key"
got_params = []
def fake_get(url, params, timeout):
    got_params.append(params["symbol"])
    if params["symbol"] == "BAD":
        raise RuntimeError("https://finnhub.io/api/v1/company-news?token=test-key")
    return Resp(True, [{"headline": "old", "url": "https://n.example/1", "datetime": 1},
                       {"headline": "new", "url": "https://n.example/2", "datetime": 2, "source": "Yahoo"},
                       {"headline": "no link", "url": "", "datetime": 3}])
import io, logging
buf = io.StringIO(); h = logging.StreamHandler(buf); logging.getLogger("scanner").addHandler(h)
cn = s.company_news(["AAA", "EQB.TO", "BAD"], d1, d1, get=fake_get)
logging.getLogger("scanner").removeHandler(h)
assert got_params == ["AAA", "BAD"]                                   # Canadian names skipped
assert [n["headline"] for n in cn["AAA"]] == ["new", "old"] and "BAD" not in cn
assert "test-key" not in buf.getvalue() and "RuntimeError" in buf.getvalue()   # the key never reaches the log
os.environ.pop("FINNHUB_KEY")

st = {"email_budget": {"date": d1.isoformat(), "SHORT": 7, "LONG": 1}}
assert s.full_horizons(st, d1) == {"SHORT"}
assert s.full_horizons(st, d2) == set()
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
ec = EchoClient()
fl = [s.Flag("AAA", "SHORT", "gap_up", "x", 1, 2, 3, "2026-10-06"), s.Flag("BBB", "LONG", "pullback_200dma", "x", 1, 2, 3, "2026-10-06")]
r = s.read_tape(fl, st, d1, client=ec, news_fn=lambda *a: {})
assert set(r) == {"BBB"} and "AAA" not in ec.calls[0]["messages"][0]["content"]     # SHORT cap full: no read
st = {"ai_budget": {"date": d1.isoformat(), "calls": 3}}
many_flags = [s.Flag(t, "SHORT", "gap_up", "x", 1, 2, 3, "2026-10-06") for t in "ABCDEFGHIJKLMNOPQRSTUVWX"]
ec = EchoClient()
s.CFG["MAX_READS_PER_SCAN"] = 0                                                      # triage off: test the dry-run cap alone
s.read_tape(many_flags, st, d1, client=ec, dry_run=True, news_fn=lambda *a: {})
s.CFG["MAX_READS_PER_SCAN"] = 8
assert len(ec.calls) == s.DRY_RUN_AI_CALLS - 4                                     # 4 kept: Chartist, the lookup,
assert all(ai.take_budget(st, d1) for _ in range(4)) and not ai.take_budget(st, d1)  # Strategist, the market read
os.environ.pop("ANTHROPIC_API_KEY")

st = {"ai_budget": {"date": d1.isoformat(), "calls": 2, "usd": 1.5}}
ai.USAGE.update(calls=2, usd=0.5)
s.log_cost(st, d1, dry_run=False)
assert st["ai_budget"]["usd"] == 2.0
s.log_cost(st, d1, dry_run=True)
assert st["ai_budget"]["usd"] == 2.0                                                # dry runs never write cost


# 11) Chartist lens: prose with no JSON gives no levels (and a logged reason), never a crash
fc = FakeClient([reply("I cannot set levels without intraday data.")])
assert ai.chartist_lens(send, {"AAA": lv}, client=fc) == {}


# 12) Emailed Finnhub links are resolved to the article; failures keep the original
class R:
    def __init__(self, code, loc=""): self.status_code, self.headers = code, {"Location": loc}
def fake(u, allow_redirects, timeout):
    assert allow_redirects is False
    if u.endswith("=ok"): return R(302, "https://www.reuters.com/markets/pltr-barclays-2026-10-09/")
    if u.endswith("=quote"): return R(302, "https://www.marketbeat.com/stocks/NASDAQ/PLTR/")
    raise RuntimeError("down")
got = s.resolve_links(["https://finnhub.io/api/news?id=ok", "https://finnhub.io/api/news?id=quote",
                       "https://finnhub.io/api/news?id=err", "https://x.example/a"], get=fake)
assert got == ["https://www.reuters.com/markets/pltr-barclays-2026-10-09/", "https://finnhub.io/api/news?id=quote",
               "https://finnhub.io/api/news?id=err", "https://x.example/a"], got


# 13) Chartist v2: RSI, DeMark setup counts and relative strength come from code
c = pd.Series(np.r_[np.linspace(80, 120, 280), np.linspace(119, 110, 10)],
              index=pd.bdate_range("2025-08-01", periods=290, tz="America/New_York"))
nb = pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1e6})
spy = nb.copy(); spy["Close"] = np.linspace(400, 440, 290)
spy.index = spy.index.tz_localize(None)                     # different time zone handling still lines up
tech = L.technicals(nb, spy, "SPY")
assert tech["td_daily"] == {"side": "buy", "count": 9}      # 10 lower closes: a completed buy setup
assert tech["rsi14"] < 30 and type(tech["rsi14"]) is float
assert tech["rel_strength"][0]["vs"] == "SPY" and tech["rel_strength"][0]["label"] == "lagging"
assert L.td_setup(pd.Series([1, 2, 3, 4, 5, 6, 7])) == {"side": "sell", "count": 3}

# DeMark 13 countdown (Oct 17): a steady decline completes a buy setup, then counts to 13
def _ohlc(closes):
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({"Close": c, "High": c + 0.5, "Low": c - 0.5})
down = [100 - i for i in range(30)]                      # every close below the low 2 bars back
cd = L.td_countdown(_ohlc(down))
assert cd["side"] == "buy" and cd["count"] == 13 and cd["bars_ago"] > 0
assert L.td_countdown(_ohlc(down + [down[-1]] * 20)) == {}          # a 13 long ago is not reported
part = L.td_countdown(_ohlc(down[:16]))
assert part == {"side": "buy", "count": 4, "bars_ago": 0}            # setup 9 at bar 12, 4 countdown bars so far
assert L.td_countdown(_ohlc([100 + i for i in range(14)]))["side"] == "sell"
assert L.td_countdown(_ohlc([100] * 30)) == {}
assert "countdown: buy 4 of 13" in ai.tech_line({"td_countdown": part})
# Breadth across the team's names
b = L.breadth({"UP": _ohlc([100 + i for i in range(60)]), "DN": _ohlc(down * 2), "NEW": _ohlc([1, 2])})
assert b == {"names": 2, "above_50d_pct": 50, "above_200d_pct": None}
assert ai.breadth_line(b) == "50% of the team's 2 names are above their 50-day average"
line = ai.tech_line(tech)
assert "DeMark daily: buy setup 9 of 9" in line and "vs SPY 20d: lagging" in line

bm = {"market": {"US": "SPY", "CA": "XIU.TO"}, "sector": {"NVDA": "SMH", "EQB.TO": "XFN.TO", "SOXQ": "SOXQ"}}
assert s.benchmarks_for("NVDA", bm) == ("SPY", "SMH")
assert s.benchmarks_for("EQB.TO", bm) == ("XIU.TO", "XFN.TO")
assert s.benchmarks_for("SOXQ", bm) == ("SPY", "")          # an ETF is not its own benchmark
assert s.benchmarks_for("ZZZ", bm) == ("SPY", "")
real = s.load_benchmarks()
assert real["sector"]["NVDA"] == "SMH" and all(not v.endswith(".TO") for k, v in real["sector"].items() if not k.endswith(".TO"))
assert all(v.endswith(".TO") for k, v in real["sector"].items() if k.endswith(".TO"))   # no currency mix

fetched = []
lvx = {"AAA": dict(L.compute_levels(nb))}
s.add_technicals(lvx, {"AAA": nb}, fetch_fn=lambda t: fetched.append(t) or {"SPY": spy})
assert fetched == [["SPY"]] and lvx["AAA"]["tech"]["rel_strength"][0]["vs"] == "SPY"

# the view is kept when it has no prices, dropped (levels kept) when it quotes one
base = {"support": lo, "resistance": hi, "trend": "up", "timeframe": "daily", "watch": "x"}
v = "Uptrend intact; a completed DeMark buy 9 suggests selling is near exhaustion; lagging SPY, not a leader yet."
assert ai.check_levels(dict(base, view=v), lv)["view"] == v
assert ai.check_levels(dict(base, view="Buy near $116.23 support"), lv)["view"] == ""
assert ai.check_levels(dict(base, view="Watch 116.23 closely"), lv)["view"] == ""
# Lens votes (Oct 15): an action is kept only with a view that explains it
assert ai.check_levels(dict(base, view=v, action="Add"), lv)["action"] == "add"
assert ai.check_levels(dict(base, view=v, action="sell short"), lv)["action"] == ""      # not a desk action
assert ai.check_levels(dict(base, view="Buy near $116.23", action="add"), lv)["action"] == ""  # no view, no vote


# 14) Strategist lens: backdrop in code, real comments validated, the model only points at them
from desk import macro as M
mi = pd.bdate_range("2025-08-01", periods=300)
mk = lambda v: pd.DataFrame({"Close": v}, index=mi)
mb = {"SPY": mk(np.linspace(500, 600, 300)), "^VIX": mk(np.r_[np.full(295, 20.0), [19, 18, 17, 16, 15.5]]),
      "^TNX": mk(np.linspace(4.5, 4.1, 300)), "DX-Y.NYB": mk(np.linspace(100, 98, 300)),
      "RSP": mk(np.linspace(170, 180, 300))}
snap = M.snapshot(mb)
assert snap["vix"] == 15.5 and snap["vix_5d_chg"] == -4.5 and snap["us10y_pct"] == 4.1
line = M.backdrop_line(snap)
assert line.startswith("S&P 500 5.9% above its 200-day average, at its 52-week high; VIX 15.5 (down 4.5 in 5 days)")
assert M.snapshot({"^TNX": mk(np.linspace(45, 41, 300))})["us10y_pct"] == 4.1   # old x10 quotes handled
assert M.snapshot({}) == {} and M.backdrop_line({}) == ""

raw = [{"who": "Tom Lee", "date": "2026-10-05", "where": "CNBC", "summary": "Expects dip buying into year-end; likes semis.",
        "tickers": ["nvda"], "sectors": ["semis"], "url": "https://www.cnbc.com/2026/10/05/tom-lee-dip.html"},
       {"who": "Mark Newton", "date": "2026-09-01", "where": "X", "summary": "too old", "url": "https://x.com/a/status/1"},
       {"who": "Jim Cramer", "date": "2026-10-05", "where": "TV", "summary": "not ours", "url": "https://www.cnbc.com/a/b"},
       {"who": "Mark Newton", "date": "2026-10-06", "where": "X", "summary": "no link"},
       {"who": "mark newton (Fundstrat)", "date": "2026-10-06", "where": "Yahoo Finance",
        "summary": "SPX support near the 50-day; breadth improving.", "url": "https://finance.yahoo.com/video/chartist-spx-1.html"},
       {"who": "Tom Lee", "date": "2026-10-06", "where": "site", "summary": "homepage", "url": "https://fundstrat.com/"}]
kept = ai.check_said(raw, d1)
assert [k["who"] for k in kept] == ["Mark Newton", "Tom Lee"] and kept[1]["tickers"] == ["NVDA"]

said_reply = reply("Found these: " + json.dumps(raw))
fc = FakeClient([said_reply])
got_said = ai.what_they_said(d1, client=fc)
assert len(got_said) == 2 and fc.calls[0]["tools"][0]["max_uses"] == 4

alerts = [{"ticker": "NVDA", "horizon": "SHORT", "flags": [{"rule": "gap_down", "day_change_pct": -4.0}],
           "direction": "DISTRIBUTION", "scope": "sector", "catalyst": "Semis sold off"},
          {"ticker": "CSCO", "horizon": "SHORT", "flags": [{"rule": "fast_move", "day_change_pct": 3.1}],
           "direction": "ACCUMULATION", "scope": "company", "catalyst": "Beat"}]
strategist_reply = reply('[{"ticker": "NVDA", "view": "Yields easing and VIX falling: a sector selloff in a healthy tape is usually a dip to buy.", "said": [1, 7, 1]},'
                  ' {"ticker": "CSCO", "view": "Buy near $210.55 support", "said": []}]')
fc = FakeClient([strategist_reply])
strategist = ai.strategist_lens(alerts, line, got_said, client=fc)
assert strategist["NVDA"]["said"] == [got_said[1]]                              # bad and repeated numbers ignored
assert "CSCO" not in strategist                                                   # view with a price dropped, nothing left
prompt = fc.calls[0]["messages"][0]["content"]
assert "Market backdrop today: S&P 500 5.9% above" in prompt and "1. Tom Lee, 2026-10-05, CNBC" in prompt
assert ai.strategist_lens(alerts, "", got_said, client=FakeClient([])) == {}       # no backdrop, no call
fc = FakeClient([reply('[{"ticker": "NVDA", "view": "Sector dip in a healthy tape.", "action": "add", "said": []},'
                       ' {"ticker": "CSCO", "view": "", "action": "trim", "said": [0]}]')])
strategist = ai.strategist_lens(alerts, line, got_said, client=fc)
assert strategist["NVDA"]["action"] == "add" and strategist["CSCO"]["action"] == ""   # no view, no vote
fc = FakeClient([reply('[]')])
ai.strategist_lens(alerts, line, got_said, client=fc, market_read="Dip-buy setup weakens without liquidity.")
assert "already sent to the team: Dip-buy setup weakens" in fc.calls[0]["messages"][0]["content"]
# Oct 18: given only "CTC-A.TO", the model called Canadian Tire "Helios".
fc = FakeClient([reply('[]')])
ai.strategist_lens([dict(alerts[0], name="NVIDIA"), alerts[1]], line, got_said, client=fc)
p = fc.calls[0]["messages"][0]["content"]
assert "NVDA (NVIDIA) | SHORT" in p and "CSCO | SHORT" in p                  # the name when we have it
fc = FakeClient([reply('[]')])
tech = {"rsi14": 28.1, "td_daily": {"side": "buy", "count": 9},
        "rel_strength": [{"vs": "SPY", "days": 20, "label": "lagging", "chg_pct": -6.0}]}
ai.strategist_lens([dict(alerts[0], tech=tech)], line, got_said, client=fc)
p = fc.calls[0]["messages"][0]["content"]
assert "vs SPY 20d: lagging" in p and "RSI" not in p and "DeMark" not in p   # leadership only

# 15) Scanner wiring: backdrop and comments cached once a day
st = {}
fetched = []
m1 = s.macro_for(st, d1, {"SPY": mb["SPY"]}, fetch_fn=lambda t: fetched.append(t) or {k: mb[k] for k in t})
assert fetched == [["^VIX", "^TNX", "DX-Y.NYB", "RSP"]] and st["macro"]["date"] == d1.isoformat()
assert s.macro_for(st, d1, {}, fetch_fn=lambda t: 1 / 0) == m1          # cached: no second fetch
fc = FakeClient([said_reply])
assert len(s.said_for(st, d1, fc, None)) == 2 and len(s.said_for(st, d1, FakeClient([]), None)) == 2


# 16) Oct 14 review fixes
# VIP filter: opinion pieces dropped, price moves always pass, failures keep everything
vip_items = [{"ticker": "TSLA", "type": "news", "headline": "Tesla: The Next Growth Engine Is Taking Shape", "source": "SeekingAlpha"},
             {"ticker": "SPCX", "type": "news", "headline": "Global X Launches PowerStock ETF Suite", "source": "Yahoo"},
             {"ticker": "TSLA", "type": "news", "headline": "Tesla recalls 200,000 Cybertrucks", "source": "Reuters"},
             {"ticker": "TSLA", "type": "price_move", "headline": "+3.2% in 15 min"}]
fc = FakeClient([reply('[{"n": 0, "keep": false}, {"n": 1, "keep": false}, {"n": 2, "keep": true, "why": "recall"}]')])
kept = ai.vip_filter(vip_items, client=fc)
assert [i["headline"] for i in kept] == ["+3.2% in 15 min", "Tesla recalls 200,000 Cybertrucks"]
assert fc.calls[0]["model"] == ai.MODEL_FAST
assert ai.vip_filter(vip_items, client=Boom()) == vip_items                  # failure: nothing lost
assert ai.vip_filter(vip_items, client=FakeClient([reply("sorry")])) == vip_items
fc = FakeClient([reply('[{"n": 2, "keep": true}]')])
ai.vip_filter(vip_items, client=fc, sent_today=["TSLA: Tesla unveils Roadster"])
assert "Already emailed or held" in fc.calls[0]["messages"][0]["content"] and "Roadster" in fc.calls[0]["messages"][0]["content"]
# Oct 20: exact repeats from yesterday are dropped in code, before the model; names go in the prompt
rep = [{"ticker": "SPCX", "type": "news", "headline": "Musk: Starlink Will Be a “Must-Have for Airlines”"},
       {"ticker": "SPCX", "type": "news", "headline": "SpaceX files S-1 amendment"}]
fc = FakeClient([reply('[{"n": 0, "keep": true}]')])
kept = ai.vip_filter(rep, client=fc, names={"SPCX": "SpaceX"},
                     sent_today=["SPCX: Musk: Starlink will be a \"must-have for airlines\""])
assert [i["headline"] for i in kept] == ["SpaceX files S-1 amendment"], kept
p = fc.calls[0]["messages"][0]["content"]
assert "0. SPCX (SpaceX) |" in p and "S-1" in p and "1. " not in p.split("Headlines:")[1]
fc = FakeClient([])
assert ai.vip_filter(rep[:1], client=fc, sent_today=["SPCX: Musk: Starlink Will Be a “Must-Have for Airlines”"]) == []
assert fc.calls == []                                                          # nothing new: no model call

# Stale sources (Oct 16): links dated more than one trading day back are dropped
fri, mon = date(2026, 10, 16), date(2026, 10, 19)
assert ai.business_days_between(fri, mon) == 1 and ai.link_date("https://x.com/2026/10/14/a") == date(2026, 10, 14)
r = {"catalyst": "Cyber fell", "direction": "DISTRIBUTION", "confidence": "high", "scope": "sector",
     "links": ["https://247wallst.com/investing/2026/10/14/palo-alto-falls/", "https://biztoc.com/x/86b2"]}
out = ai.drop_stale(r, fri)
assert out["links"] == ["https://biztoc.com/x/86b2"] and out["confidence"] == "high"        # undated link kept
assert ai.drop_stale(r, date(2026, 10, 15))["links"] == r["links"]                           # one day back: fine
out = ai.drop_stale(dict(r, links=r["links"][:1]), fri)
assert out["links"] == [] and out["confidence"] == "low" and out["catalyst"].startswith(ai.NO_SOURCE)
assert ai.drop_stale(dict(r, links=["https://x.com/2026/10/16/a"]), mon)["links"]          # Friday's news on Monday

# Levels: the last price is not a level; levels far from the price are not useful today
lv2 = L.compute_levels(nb)
assert not L.near_a_level(lv2["last"], dict(lv2, levels={}))
nem_lv = {"last": 176.2, "atr14": 6.0, "levels": {"52-week low": 137.85, "20-day low": 177.74, "60-day low": 170.1}}
nem = ai.check_levels({"support": 137.85, "resistance": 177.74, "view": "v"}, nem_lv)      # the NEM case: 6 ATR away
assert nem["support"] is None and nem["resistance"] == 177.74                               # one side kept (Oct 16, HACK)
assert nem["far"] == {"support": {"price": 137.85, "name": "52-week low"}} and nem["view"] == "v"
assert ai.check_levels({"support": 170.1, "resistance": 177.74}, nem_lv)["support"] == 170.1
cls_lv = {"last": 464.0, "atr14": 20.0, "levels": {"prior day low": 468.56, "20-day low": 470.0}}
assert ai.check_levels({"support": 463.98, "resistance": 468.56}, cls_lv) is None        # the DSG case: last price

# Comments: shown only when they are about the name, its sector or theme
bm = s.load_benchmarks()
strategist_c = {"who": "Tom Lee", "tickers": [], "sectors": ["jobs", "Fed", "crypto"], "url": "u1"}
chartist_c = {"who": "Mark Newton", "tickers": [], "sectors": ["tech", "healthcare"], "url": "u2"}
assert not s.comment_fits(strategist_c, {"ticker": "IAU", "theme": "Precious metals"}, bm)       # the IAU case
assert s.comment_fits(chartist_c, {"ticker": "VHT", "theme": "Healthcare ETF"}, bm)
assert s.comment_fits(chartist_c, {"ticker": "NVDA", "theme": "Mag7 / AI semis"}, bm)         # via SMH: semi, tech
assert s.comment_fits({"tickers": ["NVDA"], "sectors": []}, {"ticker": "NVDA", "theme": ""}, bm)
assert not s.comment_fits(chartist_c, {"ticker": "NEM", "theme": "Gold miner"}, bm)
assert s.comment_fits({"tickers": ["EQB"], "sectors": []}, {"ticker": "EQB.TO", "theme": ""}, bm)

# Strategist market read: once a day, cached
fc = FakeClient([reply('[{"ticker": "MARKET", "view": "Hot CPI lifted yields; with the S&P near highs, Strategist would call this a pause, not a top.", "said": []}]')])
st = {"said": {"date": d1.isoformat(), "items": []}}
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
r1 = s.strategist_market_for(st, d1, "S&P 500 7.0% above its 200-day average.", fc)
assert r1.startswith("Hot CPI lifted yields") and s.strategist_market_for(st, d1, "x", FakeClient([])) == r1
assert ai.strategist_market("", [], client=FakeClient([])) == ""
os.environ.pop("ANTHROPIC_API_KEY")


# 17) Oct 14 cost: no read for hits that can never be emailed; cost per role recorded
wr = [s.Flag("THIN", "SHORT", "day_move", "x", 1, 0.9, -3.0, "2026-10-14", 1.6),
      s.Flag("NEM", "SHORT", "rvol_spike", "x", 1, 2.8, -5.1, "2026-10-14", 2.8),
      s.Flag("NEM", "SHORT", "fast_move", "x", 1, 2.8, -5.1, "2026-10-14", -3.4),
      s.Flag("NBIS", "SHORT", "breakdown_20d", "x", 1, 4.0, -9.9, "2026-10-14", 197.6),
      s.Flag("FNV.TO", "LONG", "pullback_200dma", "x", 1, 0.4, -2.5, "2026-10-14", 185.26)]
st = {"digest": {"date": "2026-10-14", "tickers": ["NEM"]}}
kept = s.worth_reading(wr, st, date(2026, 10, 14))
assert [(f.ticker, f.rule) for f in kept] == [("NEM", "fast_move"), ("NBIS", "breakdown_20d"), ("FNV.TO", "pullback_200dma")]
assert len(s.worth_reading(wr, {}, date(2026, 10, 15))) == 4                 # a new day: the digest no longer counts

ai.USAGE_BY_ROLE.clear(); ai.USAGE.update(calls=0, usd=0.0)
fc = FakeClient([NS(stop_reason="end_turn", usage=NS(input_tokens=1_000_000, output_tokens=0, server_tool_use=None),
                    content=[NS(type="text", text="[]", citations=None)])])
ai.ask("05-strategist-lens.md", "x", model=ai.MODEL_STRATEGIST, client=fc)
assert ai.MODEL_STRATEGIST == ai.MODEL_FAST and ai.USAGE_BY_ROLE == {"strategist-lens": 1.0}   # $1/M on the fast model
st = {"ai_budget": {"date": d1.isoformat(), "calls": 1}}
s.log_cost(st, d1, dry_run=False)
assert st["ai_budget"]["by_role"] == {"strategist-lens": 1.0} and st["ai_budget"]["usd"] == 1.0
# No row yet today (only unbudgeted calls ran, or yesterday's row): cost is still recorded (Oct 15)
for start in ({}, {"ai_budget": {"date": "2026-10-01", "calls": 9, "usd": 4.0}}):
    st = dict(start)
    s.log_cost(st, d1, dry_run=False)
    assert st["ai_budget"] == {"date": d1.isoformat(), "calls": 0, "usd": 1.0, "all_calls": 1,
                               "by_role": {"strategist-lens": 1.0}}
st = {}
s.log_cost(st, d1, dry_run=True)
assert st == {}                                                                    # dry runs still write nothing

print("all ai tests passed")
