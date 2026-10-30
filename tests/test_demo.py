"""End to end: a fake market and a fake model go through `desk record open`, then
`desk demo` replays it with the network and the model blocked. The emails must be
identical. Run: python tests/test_demo.py"""
import os, sys, json, re, shutil, tempfile, pathlib
from datetime import datetime, timezone
from types import SimpleNamespace as NS
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
import time; time.sleep = lambda s: None   # Finnhub rate-limit pauses
import numpy as np, pandas as pd, requests, yfinance as yf

home = tempfile.mkdtemp(); sink = tempfile.mkdtemp()
os.environ.update(DESK_HOME=home, DESK_EMAIL_SINK=sink, ANTHROPIC_API_KEY="fake-anthropic-key-for-tests", FINNHUB_KEY="fake-finnhub-key-for-tests")
CLOCK = datetime(2026, 10, 28, 14, 5, tzinfo=timezone.utc)   # 10:05 New York, a Wednesday
import csv
U = [r["Ticker"] for r in csv.DictReader(open(ROOT / "desk/config/universe.csv"))]
rng = np.random.default_rng(1)
idx = pd.bdate_range(end="2026-10-28", periods=300)
def bars(t):
    c = 100 * np.cumprod(1 + rng.normal(0.0005, 0.015, len(idx)))
    o = c * (1 + rng.normal(0, 0.003, len(idx))); v = np.full(len(idx), 1e6)
    if t in ("NVDA", "AMD", "MU"):   # gap up 5% on 3x volume today
        o[-1] = c[-2] * 1.05; c[-1] = c[-2] * 1.06; v[-1] = 3e6
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.01, "Low": np.minimum(o, c) * 0.99,
                         "Close": c, "Adj Close": c, "Volume": v, "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)
DATA = {t: bars(t) for t in U + ["SPY", "QQQ", "^VIX", "^TNX", "DX-Y.NYB", "RSP", "XIU.TO"] + sorted(set(json.load(open(ROOT/"desk/config/benchmarks.json"))["sector"].values()))}
def fake_dl(tickers, *a, **kw):
    ts = [tickers] if isinstance(tickers, str) else list(tickers)
    if kw.get("interval") == "1m":
        mi = pd.date_range("2026-10-28 09:30", "2026-10-28 10:05", freq="min", tz="America/New_York")
        return pd.concat({t: pd.DataFrame({"Close": np.linspace(100, 101, len(mi)), "Volume": 1e4}, index=mi) for t in ts}, axis=1)
    return pd.concat({t: DATA.get(t, bars(t)) for t in ts}, axis=1)
class R:   # like requests: .url is the final URL, query string and token included
    def __init__(s, obj, url="", status=200, headers=None):
        s.status_code, s.ok, s.url, s.text, s.headers = status, status < 400, url, json.dumps(obj), headers or {}
    def json(s): return json.loads(s.text)
def fake_get(url, params=None, **kw):
    full = url + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
    if "finnhub.io/api/news" in url:   # a news link: redirects to the article
        return R({}, full, 302, {"Location": f"https://www.reuters.com/markets/{url.split('id=')[1]}-rallies"})
    if "company-news" in url:
        t = (params or {}).get("symbol", "")
        return R([{"id": hash(t) % 10**6, "headline": f"{t} rallies as chip demand jumps", "summary": "results beat",
                   "url": f"https://finnhub.io/api/news?id={t.lower()}", "source": "Reuters", "datetime": int(CLOCK.timestamp()) - 3600}], full)
    if "calendar/earnings" in url:
        return R({"earningsCalendar": []}, full)
    return R({}, full)
class FakeAI:
    @property
    def messages(s): return s
    def create(s, **kw):
        sysp, u = str(kw.get("system", "")), kw["messages"][0]["content"]
        if "Tape Reader" in sysp[:4000] and " | " in u:
            tks = sorted({l.split(" | ")[0].strip() for l in u.splitlines() if " | " in l and l.split(" | ")[0].strip() in U})
            text = "\n".join(f"{t} | Chip demand news lifted shares | ACCUMULATION | high | https://finnhub.io/api/news?id={t.lower()}" for t in tks)
        else:
            text = "[]"
        return NS(stop_reason="end_turn", usage=NS(input_tokens=100, output_tokens=20, server_tool_use=None),
                  content=[NS(type="text", text=text, citations=[])])
yf.download, requests.get = fake_dl, fake_get
from desk import ai, replay
ai._client = lambda: FakeAI()
replay.FIXTURES = pathlib.Path(tempfile.mkdtemp())
replay.record(["open"], clock=CLOCK)
# Oct 30: a response's final URL carried the Finnhub token into the recording.
key = os.environ["FINNHUB_KEY"]
for f in replay.FIXTURES.rglob("*"):
    if f.is_file():
        raw = f.read_bytes()
        text = (__import__("gzip").decompress(raw) if f.suffix == ".gz" else raw).decode(errors="ignore")
        assert key not in text and "token=" not in text, f"secret saved in {f.name}"
recorded = sorted(p.name for p in pathlib.Path(sink).glob("*.txt"))
assert recorded, "the recording should produce emails"
# replay with everything real-network blocked
def boom(*a, **k): raise AssertionError("network during demo")
yf.download, requests.get = boom, boom
ai._client = boom
replay.ROOT = pathlib.Path(tempfile.mkdtemp())
replay.demo([])
out = sorted(p.name for p in (replay.ROOT / "out/demo/mail").glob("*.txt"))
same = all((pathlib.Path(sink)/a).read_text() == (replay.ROOT/"out/demo/mail"/b).read_text() for a, b in zip(recorded, out))
assert recorded == out and same, (recorded, out)
# Oct 30: news links resolve to the article in the replay too, each to its own
mail = "".join((replay.ROOT / "out/demo/mail" / n).read_text() for n in out)
assert "reuters.com/markets/nvda-rallies" in mail and "reuters.com/markets/amd-rallies" in mail, mail[:2000]
assert "finnhub.io/api/news" not in mail
print("demo replays the recording exactly:", len(out), "email(s)")
