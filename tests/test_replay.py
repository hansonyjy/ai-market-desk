"""Record and replay: what is recorded comes back identical, with no network,
no key, and the clock frozen at the recorded moment. Run: python tests/test_replay.py"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # noqa: E702
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import requests
import yfinance as yf

os.environ["DESK_HOME"] = tempfile.mkdtemp()
from desk import ai, replay, scanner  # noqa: E402

folder = pathlib.Path(tempfile.mkdtemp())
idx = pd.bdate_range(end="2026-10-28", periods=30)
daily = pd.concat({t: pd.DataFrame({"Open": np.arange(30.0), "High": np.arange(30.0) + 1, "Low": np.arange(30.0) - 1,
                                    "Close": np.arange(30.0) + 0.5, "Volume": np.full(30, 1e6)}, index=idx)
                   for t in ("NVDA", "AAPL")}, axis=1)
mins = pd.date_range("2026-10-28 09:30", periods=5, freq="min", tz="America/New_York")
intraday = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0, 5.0]}, index=mins)
calls = {"yf": 0, "http": 0, "ai": 0}


def fake_dl(tickers, *a, **kw):
    calls["yf"] += 1
    return intraday if kw.get("interval") == "1m" else daily


class FakeHTTP:
    status_code, url = 200, "https://final.example/a"
    text = '[{"id": 7, "headline": "Nvidia beats"}]'
    ok = True
    def json(self): return [{"id": 7, "headline": "Nvidia beats"}]


def fake_get(url, params=None, **kw):
    calls["http"] += 1
    return FakeHTTP()


class FakeAI:
    @property
    def messages(self): return self
    def create(self, **kw):
        calls["ai"] += 1
        return NS(stop_reason="end_turn", usage=NS(input_tokens=10, output_tokens=5, server_tool_use=None),
                  content=[NS(type="web_search_tool_result"),
                           NS(type="text", text="NVDA | beat | ACCUMULATION | high", citations=[NS(url="https://a.example")])])


yf.download, requests.get, ai._client = fake_dl, fake_get, lambda: FakeAI()

# ---- record
store = replay._Store(folder, "record")
undo = replay._install(store, None)
d1 = yf.download(["NVDA", "AAPL"], period="14mo", interval="1d", auto_adjust=False, actions=True)
d2 = yf.download("NVDA", period="1d", interval="1m")
r1 = requests.get("https://finnhub.io/api/v1/company-news", params={"symbol": "NVDA", "token": "SECRET-123"})
kw = dict(model="m", system="role A", messages=[{"role": "user", "content": "hi"}], max_tokens=10)
a1 = ai._client().messages.create(**kw)
undo(); store.save()
assert calls == {"yf": 2, "http": 1, "ai": 1}
assert "SECRET-123" not in (folder / "index.json").read_text()           # API tokens never stored

# ---- replay: the real functions would fail loudly if called
def boom(*a, **k): raise AssertionError("network used during replay")
yf.download, requests.get, ai._client = boom, boom, boom
store2 = replay._Store(folder, "replay")
clock = datetime(2026, 10, 28, 14, 5, tzinfo=timezone.utc)
undo = replay._install(store2, clock)
try:
    e1 = yf.download(["AAPL", "NVDA"], period="14mo", interval="1d", auto_adjust=False, actions=True)   # order-free key
    e2 = yf.download("NVDA", period="1d", interval="1m")
    pd.testing.assert_frame_equal(e1, d1, check_freq=False, check_dtype=False)
    pd.testing.assert_frame_equal(e2, d2, check_freq=False, check_dtype=False)
    q = requests.get("https://finnhub.io/api/v1/company-news", params={"symbol": "NVDA", "token": "OTHER"})
    assert q.ok and q.json() == [{"id": 7, "headline": "Nvidia beats"}]
    b = ai._client().messages.create(**kw)
    text = "".join(x.text for x in b.content if x.type == "text")
    assert text == "NVDA | beat | ACCUMULATION | high" and b.content[1].citations[0].url == "https://a.example"
    assert b.usage.input_tokens == 10
    # a changed prompt for the same agent still gets that agent's recorded reply
    b2 = ai._client().messages.create(**dict(kw, messages=[{"role": "user", "content": "hi, reworded"}]))
    assert b2.content[1].text == text and store2.misses == ["ai:" + replay._role(kw)]
    # the clock is frozen in desk modules
    assert scanner.datetime.now(scanner.ET).strftime("%Y-%m-%d %H:%M") == "2026-10-28 10:05"
    # a lookup that was never recorded is reported, not fetched
    assert requests.get("https://finnhub.io/api/v1/other").status_code == 404 and "http:" in store2.misses[-1]
finally:
    undo()
print("all replay tests passed")
