"""Record a real session once, replay it anywhere with no keys.

`desk record` runs the real desk against live markets, with your own keys, and
saves everything it read from the outside world into demo/fixtures/<session>/:
  - market data (every yfinance download, as CSV)
  - HTTP replies (Finnhub headlines and earnings, link redirects; API tokens removed)
  - every Claude reply (text, citations, token counts)
  - the clock (the moment the session ran)

`desk demo` replays those sessions through the same pipeline, offline: same rules,
same levels, same Call, same emails. Nothing is fetched and no model is called, so
it needs no keys and costs nothing. Emails land in out/demo/mail/.

The recorder hooks the four boundaries (yfinance.download, requests.get, the
Anthropic client and datetime.now) and nothing else, so the code under test is the
code that runs live.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import time
from datetime import date as _date, datetime as _datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger("ai-market-desk")
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "demo" / "fixtures"
ET = ZoneInfo("America/New_York")
SECRET_PARAMS = {"token", "apikey", "api_key", "key"}


# ---------------------------------------------------------------- keys
def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _yf_key(tickers, kw) -> str:
    t = sorted(tickers) if isinstance(tickers, (list, tuple, set)) else [str(tickers)]
    return _h({"t": t, **{k: kw.get(k) for k in ("period", "interval", "auto_adjust", "actions")}})


def _http_key(url, params) -> str:
    p = {k: v for k, v in (params or {}).items() if k.lower() not in SECRET_PARAMS}
    return _h({"u": url.split("?")[0], "p": p})


def _ai_key(kw) -> str:
    msgs = kw.get("messages", [])
    first = msgs[0]["content"] if msgs and isinstance(msgs[0].get("content"), str) else str(msgs[:1])
    return _h({"m": kw.get("model"), "s": str(kw.get("system", ""))[:200], "u": first, "n": len(msgs)})


def _role(kw) -> str:
    """Which agent made the call: each charter gives a distinct system prompt."""
    return _h(str(kw.get("system", "")))


# ---------------------------------------------------------------- frames <-> CSV
def _df_to_csv(df: pd.DataFrame) -> str:
    d = df.copy()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = ["|".join(map(str, c)) for c in d.columns]
    d.index.name = d.index.name or "__none__"
    return d.to_csv()


def _csv_to_df(text: str) -> pd.DataFrame:
    d = pd.read_csv(io.StringIO(text), index_col=0)
    if d.empty:
        return d
    intraday = " " in str(d.index[0]) and ":" in str(d.index[0])
    idx = pd.to_datetime(d.index, utc=True, format="mixed")
    name = d.index.name
    d.index = idx.tz_convert(ET) if intraday else idx.tz_convert(None)   # daily bars are plain dates
    d.index.name = None if name == "__none__" else name
    if any("|" in c for c in d.columns):
        d.columns = pd.MultiIndex.from_tuples([tuple(c.split("|", 1)) for c in d.columns])
    return d


# ---------------------------------------------------------------- the four hooks
class _Store:
    def __init__(self, folder: Path, mode: str):
        self.dir, self.mode = folder, mode
        self.index = json.loads((folder / "index.json").read_text()) if (folder / "index.json").exists() else \
            {"yf": {}, "http": {}, "ai": {}, "clock": None}
        self.ai_queue: dict[str, list] = {}
        if mode == "replay":
            for k, v in self.index["ai"].items():
                for r in v:
                    self.ai_queue.setdefault(r["role"], []).append(r)
        self.misses: list[str] = []

    def save(self):
        (self.dir / "index.json").write_text(json.dumps(self.index, indent=1, default=str))


def _serialize_response(resp) -> dict:
    blocks = []
    for b in getattr(resp, "content", []) or []:
        t = getattr(b, "type", "")
        if t == "text":
            blocks.append({"type": "text", "text": b.text,
                           "citations": [{"url": getattr(c, "url", None)} for c in (getattr(b, "citations", None) or [])
                                         if getattr(c, "url", None)]})
        else:
            blocks.append({"type": t})
    u = getattr(resp, "usage", None)
    stu = getattr(u, "server_tool_use", None)
    return {"stop_reason": getattr(resp, "stop_reason", "end_turn"), "content": blocks,
            "usage": {"input_tokens": getattr(u, "input_tokens", 0), "output_tokens": getattr(u, "output_tokens", 0),
                      "web_search_requests": getattr(stu, "web_search_requests", 0) if stu else 0}}


def _deserialize_response(d: dict):
    content = []
    for b in d["content"]:
        if b["type"] == "text":
            content.append(NS(type="text", text=b["text"], citations=[NS(url=c["url"]) for c in b.get("citations", [])]))
        else:
            content.append(NS(type=b["type"]))
    u = d.get("usage", {})
    usage = NS(input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
               server_tool_use=NS(web_search_requests=u.get("web_search_requests", 0)))
    return NS(stop_reason=d.get("stop_reason", "end_turn"), content=content, usage=usage)


class _RecordingClient:
    def __init__(self, real, store: _Store):
        self._real, self._store = real, store

    @property
    def messages(self):
        return self

    def create(self, **kw):
        resp = self._real.messages.create(**kw)
        rec = {"role": _role(kw), **_serialize_response(resp)}
        self._store.index["ai"].setdefault(_ai_key(kw), []).append(rec)
        return resp


class _ReplayClient:
    def __init__(self, store: _Store):
        self._store = store
        self._used: dict[str, int] = {}

    @property
    def messages(self):
        return self

    def create(self, **kw):
        k = _ai_key(kw)
        hits = self._store.index["ai"].get(k)
        if hits:
            i = self._used.get(k, 0)
            self._used[k] = i + 1
            return _deserialize_response(hits[min(i, len(hits) - 1)])
        q = self._store.ai_queue.get(_role(kw)) or []   # prompt changed since recording: same agent, next reply
        if q:
            self._store.misses.append(f"ai:{_role(kw)}")
            return _deserialize_response(q.pop(0))
        raise RuntimeError(f"demo: no recorded reply for {_role(kw)}")


class _Resp:
    def __init__(self, d):
        self.status_code, self.ok, self.text, self.url = d["status"], 200 <= d["status"] < 400, d["text"], d.get("url", "")

    def json(self):
        return json.loads(self.text)


def _install(store: _Store, clock: _datetime | None):
    import requests
    import yfinance as yf

    from desk import ai

    real_dl, real_get, real_client = yf.download, requests.get, ai._client
    rec = store.mode == "record"

    def dl(tickers, *a, **kw):
        k = _yf_key(tickers, kw)
        f = store.dir / "market" / f"{k}.csv.gz"
        if rec:
            df = real_dl(tickers, *a, **kw)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(gzip.compress(_df_to_csv(df).encode()))
            store.index["yf"][k] = {"tickers": tickers if isinstance(tickers, str) else sorted(tickers), **kw}
            return df
        if f.exists():
            return _csv_to_df(gzip.decompress(f.read_bytes()).decode())
        store.misses.append(f"yf:{k}")
        return pd.DataFrame()

    def get(url, params=None, **kw):
        k = _http_key(url, params)
        if rec:
            r = real_get(url, params=params, **kw)
            store.index["http"][k] = {"url": url.split("?")[0], "status": r.status_code,
                                      "text": r.text[:400_000], "final": getattr(r, "url", "")}
            return r
        d = store.index["http"].get(k)
        if d is None:
            store.misses.append(f"http:{url.split('?')[0]}")
            return _Resp({"status": 404, "text": "[]"})
        return _Resp({"status": d["status"], "text": d["text"], "url": d.get("final", "")})

    def client():
        return _RecordingClient(real_client(), store) if rec else _ReplayClient(store)

    real_sleep = time.sleep
    yf.download, requests.get, ai._client = dl, get, client
    if not rec:
        time.sleep = lambda s: None   # rate-limit pauses (Finnhub) mean nothing offline
    if clock is not None:
        _freeze(clock)
    return lambda: (setattr(yf, "download", real_dl), setattr(requests, "get", real_get),
                    setattr(ai, "_client", real_client), setattr(time, "sleep", real_sleep))


def _freeze(at: _datetime):
    """datetime.now() and date.today() return the recorded moment, in every desk module."""
    class Frozen(_datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz else at.astimezone(timezone.utc).replace(tzinfo=None)

    class FrozenDate(_date):
        @classmethod
        def today(cls):
            return at.astimezone(ET).date()

    import datetime as real
    for name, mod in list(sys.modules.items()):
        if name.startswith("desk.") and name != "desk.replay":
            if isinstance(getattr(mod, "datetime", None), type) and issubclass(mod.datetime, real.datetime):
                mod.datetime = Frozen
            if isinstance(getattr(mod, "date", None), type) and issubclass(mod.date, real.date) \
                    and not issubclass(mod.date, real.datetime):
                mod.date = FrozenDate


# ---------------------------------------------------------------- sessions
SESSIONS = {
    # name: (what to run, when, in New York time)
    "open": (["scan", "vip"], "10:05"),
    "midday": (["scan", "vip"], "12:30"),
}
ASK_QUESTION = "Where is support if this keeps falling, and what would change the view?"


def _run_modes(modes: list[str]):
    from desk import scanner
    for m in modes:
        log.info("---- %s", m)
        old = sys.argv
        sys.argv = ["desk", m, "--force"]
        try:
            scanner.main()
        except SystemExit as e:
            if e.code not in (None, 0):
                log.warning("%s exited with %s", m, e.code)
        finally:
            sys.argv = old


def _load_desk_modules():
    from desk import ai, audit, inbox, levels, macro, mailer, scanner  # noqa: F401  (frozen clock reaches them all)


def record(argv: list[str], clock: _datetime | None = None) -> int:
    """Record the named sessions now (live data, your keys). Run each at its time.
    clock: tests only, to record "as of" a fixed moment."""
    import argparse

    p = argparse.ArgumentParser(prog="desk record")
    p.add_argument("session", choices=[*SESSIONS, "ask", "rulelab"])
    p.add_argument("--ticker", default="NVDA", help="ask: the name the question is about")
    a = p.parse_args(argv)
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("desk record needs ANTHROPIC_API_KEY (and FINNHUB_KEY) in your environment or .env")
    folder = FIXTURES / a.session
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    os.environ.setdefault("DESK_EMAIL_SINK", str(ROOT / "out" / "record" / "mail"))
    _load_desk_modules()
    if a.session == "rulelab":   # years of history: keep the results, not the raw prices
        from desk import scanner
        _run_modes(["rulelab"])
        res = scanner.LOCAL_LOG.parent / "rule_lab.csv"
        shutil.copy(res, folder / "rule_lab.csv")
        print(f"recorded rulelab results -> {folder / 'rule_lab.csv'}")
        return 0
    store = _Store(folder, "record")
    store.index["clock"] = (clock or _datetime.now(timezone.utc)).isoformat()
    undo = _install(store, clock)
    try:
        if a.session in SESSIONS:
            _run_modes(SESSIONS[a.session][0])
        else:
            from desk import cli
            store.index["question"] = {"ticker": a.ticker, "text": ASK_QUESTION}
            cli.ask_terminal(f"{a.ticker}: {ASK_QUESTION}")
    finally:
        undo()
        store.save()
    print(f"recorded {a.session}: {len(store.index['yf'])} downloads, {len(store.index['http'])} HTTP replies, "
          f"{sum(len(v) for v in store.index['ai'].values())} model replies -> {folder}")
    return 0


def demo(argv: list[str]) -> int:
    """Replay every recorded session in order, offline, into out/demo/."""
    out = ROOT / "out" / "demo"
    if out.exists():
        shutil.rmtree(out)
    (out / "mail").mkdir(parents=True)
    os.environ["DESK_HOME"] = str(out / "state")
    os.environ["DESK_EMAIL_SINK"] = str(out / "mail")
    os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY") or "demo-no-key"
    os.environ.setdefault("FINNHUB_KEY", "demo-no-key")
    os.environ["DRY_RUN_AI_CALLS"] = "99"
    for k in ("SHEET_ID", "GOOGLE_CREDS_JSON", "DESK_EMAIL", "GMAIL_APP_PASSWORD"):
        os.environ.pop(k, None)
    order = [s for s in (*SESSIONS, "ask") if (FIXTURES / s / "index.json").exists()]
    if not order:
        raise SystemExit("No recorded sessions in demo/fixtures/. Record them with `desk record open` (needs keys).")
    # the desk's paths are read at import, so import after DESK_HOME is set
    import desk
    for name in [n for n in sys.modules if n.startswith("desk.") and n not in ("desk.replay", "desk.cli")]:
        del sys.modules[name]
        if hasattr(desk, name.split(".", 1)[1]):   # `from desk import x` reads the package attribute first
            delattr(desk, name.split(".", 1)[1])
    _load_desk_modules()
    t0 = time.time()
    misses = []
    for s in order:
        store = _Store(FIXTURES / s, "replay")
        clock = _datetime.fromisoformat(store.index["clock"])
        print(f"\n== {s}: {clock.astimezone(ET):%a %b %d, %H:%M} New York (recorded) ==")
        undo = _install(store, clock)
        try:
            if s in SESSIONS:
                _run_modes(SESSIONS[s][0])
            else:
                from desk import cli
                q = store.index.get("question", {})
                cli.ask_terminal(f"{q.get('ticker', 'NVDA')}: {q.get('text', ASK_QUESTION)}")
        finally:
            undo()
        misses += store.misses
    mails = sorted((out / "mail").glob("*.txt"))
    print(f"\nReplayed {len(order)} session(s) in {time.time() - t0:.1f}s with no network and no API key.")
    print(f"{len(mails)} email(s) written to {out / 'mail'}:")
    for m in mails:
        print("  " + m.read_text().splitlines()[0].replace("Subject: ", ""))
    if misses:
        print(f"\n({len(misses)} lookup(s) not in the recording; the code has changed since it was made.)")
    return 0
