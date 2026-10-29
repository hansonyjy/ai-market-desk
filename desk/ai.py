"""Claude API calls for the AI Market Desk.

Every call sends charters/00-shared.md plus one role's prompt as the system
prompt, so the files in charters/ are the only copy of each role's behavior.
Budgets are enforced here, in code, not left to the model.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("scanner")

CHARTERS = Path(__file__).resolve().parent.parent / "charters"   # the agents' instructions, one file per role
NOTHING = "NOTHING_TO_REPORT"

# Models are settings, not code: change them with an environment variable, no code change.
MODEL_ANALYST = os.getenv("MODEL_ANALYST") or "claude-sonnet-5"
MODEL_FAST = os.getenv("MODEL_FAST") or "claude-haiku-4-5-20251001"
DAILY_AI_CALLS = int(os.getenv("DAILY_AI_CALLS") or 40)      # hard stop per day, all roles
SEARCHES_PER_NAME = int(os.getenv("SEARCHES_PER_NAME") or 2)   # names with no Finnhub headline
SEARCHES_WITH_NEWS = 1                                           # names that already have headlines
HEADLINES_PER_NAME = 6
# Dollars per million tokens (in, out) and per web search, for the cost log only.
PRICES = {"claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}
SEARCH_PRICE = 0.01
USAGE = {"calls": 0, "in": 0, "out": 0, "searches": 0, "usd": 0.0}   # this run only
USAGE_BY_ROLE: dict[str, float] = {}                                   # this run only, dollars per role
# Strategist lens writes one or two sentences from facts it is given: the fast model is
# enough (Oct 14: the day cost $3.12; Sonnet for Strategist was one of the drivers).
MODEL_STRATEGIST = os.getenv("MODEL_STRATEGIST") or MODEL_FAST
NAMES_PER_CALL = int(os.getenv("NAMES_PER_CALL") or 3)   # small batches keep 3 searches per name
MAX_SEARCHES_PER_CALL = 10
WEB_SEARCH_TOOL = "web_search_20250305"
SOURCE_RULES_FILE = Path(__file__).resolve().parent / "config" / "source_rules.json"


def source_rules() -> dict:
    try:
        return json.loads(SOURCE_RULES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"blocked_domains": [], "not_a_source": []}


def is_real_source(url: str, rules: Optional[dict] = None) -> bool:
    """An article or filing page, not a homepage, quote page or blocked site."""
    rules = rules or source_rules()
    u = url.split("://", 1)[-1].lower()
    u = u[4:] if u.startswith("www.") else u
    host, _, path = u.partition("/")
    if not path.strip("/"):
        return False                                        # bare homepage
    if any(host == d or host.endswith("." + d) for d in rules.get("blocked_domains", [])):
        return False
    for p in rules.get("not_a_source", []):
        if p.startswith("re:"):
            if re.search(p[3:], u):
                return False
        elif p in u:
            return False
    return True


# ---------------------------------------------------------------- prompts
def load_prompt(role_file: str, charters: Path = CHARTERS) -> str:
    """The fenced block under '## Prompt' in a charter file."""
    text = (charters / role_file).read_text()
    m = re.search(r"^## Prompt\s*\n+```[a-z]*\n(.*?)\n```", text, flags=re.S | re.M)
    if not m:
        raise ValueError(f"{role_file}: no ```text block under '## Prompt'")
    return m.group(1).strip()


def system_prompt(role_file: str, charters: Path = CHARTERS) -> str:
    return load_prompt("00-shared.md", charters) + "\n\n" + load_prompt(role_file, charters)


# ---------------------------------------------------------------- budget
def take_budget(state: dict, today: date, n: int = 1, reserve: int = 0) -> bool:
    """Reserve n Claude calls for today. False once the daily cap is reached.
    reserve: calls to leave free for a later step (the Tape Reader leaves one
    for Chartist lens, so a run that reads names can still set their levels)."""
    b = state.get("ai_budget") or {}
    if b.get("date") != today.isoformat():
        b = {"date": today.isoformat(), "calls": 0}
    if b["calls"] + n + reserve > DAILY_AI_CALLS:
        state["ai_budget"] = b
        return False
    b["calls"] += n
    state["ai_budget"] = b
    return True


# ---------------------------------------------------------------- client
def _client():
    import anthropic

    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()  # pasted secrets often end in a newline
    if not key.startswith("sk-ant-"):
        raise RuntimeError("ANTHROPIC_API_KEY does not look like a Claude API key (should start with sk-ant-)")
    return anthropic.Anthropic(api_key=key)


def _count(model: str, role_file: str, usage) -> None:
    """Add one response's tokens and searches to USAGE, and log its cost."""
    if usage is None:
        return
    tin = int(getattr(usage, "input_tokens", 0) or 0)
    tout = int(getattr(usage, "output_tokens", 0) or 0)
    stu = getattr(usage, "server_tool_use", None)
    srch = int(getattr(stu, "web_search_requests", 0) or 0) if stu else 0
    pin, pout = next((v for k, v in PRICES.items() if model.startswith(k)), PRICES["claude-sonnet-5"])
    usd = tin / 1e6 * pin + tout / 1e6 * pout + srch * SEARCH_PRICE
    for k, v in (("calls", 1), ("in", tin), ("out", tout), ("searches", srch), ("usd", usd)):
        USAGE[k] += v
    role = role_file.split("-", 1)[-1].split(".")[0]           # "02-tape-reader.md" -> "tape-reader"
    USAGE_BY_ROLE[role] = round(USAGE_BY_ROLE.get(role, 0.0) + usd, 4)
    log.info("cost %s: %d in, %d out, %d searches, $%.3f", role_file.split(".")[0], tin, tout, srch, usd)


MAX_TOKENS_CAP = 16000


# Chartist picks levels from a list the code computed and writes one line per name: a
# lookup, not a puzzle. At the default effort it thought for 13,000 tokens and 3.7
# minutes on 9 alerts (Oct 18), long enough for the run to hit its time limit.
CHARTIST_EFFORT = os.getenv("CHARTIST_EFFORT") or "low"


def chartist_tokens(n: int) -> int:
    """Room for the Chartist answer: about 600 tokens of JSON and working per name.
    A flat 3000 ran out on 9 alerts (Oct 18 preview)."""
    return min(max(3000, 1500 + 700 * n), MAX_TOKENS_CAP)


def ask(role_file: str, user: str, *, model: str = MODEL_ANALYST, searches: int = 0,
        max_tokens: int = 1500, client=None, effort: Optional[str] = None) -> tuple[str, list[str]]:
    """One Claude call. Returns (final text, source URLs cited in it)."""
    client = client or _client()
    tools = ([{"type": WEB_SEARCH_TOOL, "name": "web_search",
               "max_uses": min(searches, MAX_SEARCHES_PER_CALL),
               "blocked_domains": source_rules().get("blocked_domains", [])}] if searches else [])
    messages = [{"role": "user", "content": user}]
    texts: list[str] = []
    urls: list[str] = []
    retried = False
    for _ in range(4):  # a long search can pause the turn; continue at most twice (+1 retry)
        kwargs = dict(model=model, max_tokens=max_tokens,
                      system=system_prompt(role_file), messages=messages)
        if tools:
            kwargs["tools"] = tools
        if effort:   # how hard the model thinks before answering: low, medium or high
            kwargs["output_config"] = {"effort": effort}
        resp = client.messages.create(**kwargs)
        _count(model, role_file, getattr(resp, "usage", None))
        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "web_search_tool_result":
                texts = []  # text before the last search is planning, not the answer
            elif btype == "text":
                texts.append(block.text)
                for c in getattr(block, "citations", None) or []:
                    u = getattr(c, "url", None)
                    if u and u not in urls:
                        urls.append(u)
        if resp.stop_reason == "max_tokens":
            kinds = sorted({getattr(b, "type", "?") for b in resp.content})
            log.warning("%s: answer cut off at max_tokens=%d; its JSON may be incomplete "
                        "(blocks: %s)", role_file.split(".")[0], max_tokens, ",".join(kinds))
            # Oct 18: Chartist used all 3000 tokens before writing any answer text (9 alerts
            # in one call). Ran out before answering: try once more with twice the room.
            if not "".join(texts).strip() and not retried:
                retried, max_tokens = True, min(max_tokens * 2, MAX_TOKENS_CAP)
                messages, texts, urls = [{"role": "user", "content": user}], [], []
                log.warning("%s: no answer text; retrying once with max_tokens=%d",
                            role_file.split(".")[0], max_tokens)
                continue
        if resp.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]
    return "".join(texts).strip(), urls


# ---------------------------------------------------------------- Tape Reader
DIRECTIONS = ("ACCUMULATION", "DISTRIBUTION", "NOISE")
NO_SOURCE = "[no source] "


def _match_ticker(raw: str, wanted: list[str]) -> Optional[str]:
    """'EQB' or 'eqb.to' -> 'EQB.TO' when that is unambiguous within this batch."""
    t = raw.strip(" *`").upper()
    if t in wanted:
        return t
    base = [w for w in wanted if w.split(".")[0] == t.split(".")[0]]
    return base[0] if len(base) == 1 else None


SCOPES = ("company", "sector", "macro")


def _clean(t: str, d: str, c: str, catalyst: str, links: list[str], scope: str = "") -> dict:
    direction = next((x for x in DIRECTIONS if x in str(d).upper()), "NOISE")
    scope = next((x for x in SCOPES if x in str(scope).lower()), "unknown")
    conf = next((x for x in ("high", "medium", "low") if x in str(c).lower()), "low")
    links = [u.rstrip(").,;'\"") for u in links if str(u).startswith("http")]
    links = [u for u in links if is_real_source(u)]  # quote pages and homepages are not sources
    catalyst = " ".join(str(catalyst).split())
    if not links:  # no source, no claim: keep the read visible but mark it and cap confidence
        conf = "low"
        if not catalyst.startswith(NO_SOURCE):
            catalyst = NO_SOURCE + catalyst
    return {"catalyst": catalyst, "direction": direction, "confidence": conf, "links": links,
            "scope": scope}


def json_array(text: str) -> list:
    """The first JSON array of objects in the text, wherever it starts.
    Tolerates prose or a ```json fence around it. [] if there is none."""
    dec = json.JSONDecoder()
    i = text.find("[")
    while i != -1:
        try:
            val, _ = dec.raw_decode(text[i:])
            if isinstance(val, list) and (not val or isinstance(val[0], dict)):
                return val
        except json.JSONDecodeError:
            pass
        i = text.find("[", i + 1)
    return []


URL_DATE = re.compile(r"/(20\d\d)[/-](\d{1,2})[/-](\d{1,2})(?:/|-|$)")


def link_date(url: str) -> Optional[date]:
    """The publish date written in a link's path (/2026/10/14/), if any."""
    m = URL_DATE.search(url)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def business_days_between(a: date, b: date) -> int:
    """Weekdays after a, up to and including b (Fri -> Mon is 1)."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def drop_stale(read: dict, today: date, max_bdays: int = 1) -> dict:
    """Drop source links dated more than one trading day before today: they
    explain an earlier move, not today's (Oct 16: HACK's 'why' cited an Oct 14
    story). A link with no date in it is kept. No links left: no source, no claim."""
    keep = [u for u in read.get("links", [])
            if not (d := link_date(u)) or business_days_between(d, today) <= max_bdays]
    if len(keep) == len(read.get("links", [])):
        return read
    log.info("dropped %d stale source link(s)", len(read["links"]) - len(keep))
    out = dict(read, links=keep)
    if not keep:
        out["confidence"] = "low"
        if not out["catalyst"].startswith(NO_SOURCE):
            out["catalyst"] = NO_SOURCE + out["catalyst"]
    return out


def parse_tape(text: str, tickers: list[str]) -> dict[str, dict]:
    """The model's JSON array -> per-ticker dict. Falls back to the older
    'TICKER | catalyst | direction | confidence | links' lines. Anything
    unparseable is skipped, never guessed."""
    wanted = [t.upper() for t in tickers]
    out: dict[str, dict] = {}
    items = json_array(text)
    if items:
        for it in items:
            if not isinstance(it, dict):
                continue
            t = _match_ticker(str(it.get("ticker", "")), wanted)
            if t and t not in out:
                src = it.get("sources") or []
                out[t] = _clean(t, it.get("direction", ""), it.get("confidence", ""),
                                it.get("catalyst", ""), src if isinstance(src, list) else [src],
                                it.get("scope", ""))
        if out:
            return out
    for line in text.splitlines():
        parts = [p.strip(" *`") for p in line.split("|")]
        if len(parts) < 4:
            continue
        t = _match_ticker(parts[0], wanted)
        if not t or t in out:
            continue
        out[t] = _clean(t, parts[2], parts[3], parts[1], re.findall(r"https?://\S+", " ".join(parts[4:])))
    return out


def tape_reader(flags: list[dict], *, today: date, client=None, take_budget=None,
                news: Optional[dict] = None) -> dict[str, dict]:
    """Explain flagged names in small batches (NAMES_PER_CALL per Claude call).
    news: ticker -> Finnhub headlines with links. Those are given to the model
    first; a name with headlines gets 1 web search, a name without gets
    SEARCHES_PER_NAME. Search results are most of the cost, so this is the main
    cost control. take_budget() is asked before each call; once it says no, the
    remaining names are logged without a read."""
    news = news or {}
    if not flags:
        return {}
    by_ticker: dict[str, list[dict]] = {}
    for f in flags:
        by_ticker.setdefault(f["ticker"], []).append(f)
    tickers = sorted(by_ticker)
    reads: dict[str, dict] = {}
    client = client or _client()
    for i in range(0, len(tickers), NAMES_PER_CALL):
        batch = tickers[i:i + NAMES_PER_CALL]
        if take_budget and not take_budget():
            log.warning("AI budget used up; %d name(s) logged without a read", len(tickers) - i)
            break
        lines = [f"{f['ticker']} | {f['horizon']} | {f['rule']} | {f['detail']} | "
                 f"price {f['price']} | RVOL {f.get('rvol') or 'n/a'} | "
                 f"day change {f.get('day_change_pct')}% | session {f.get('bar_date', 'n/a')}"
                 for t in batch for f in by_ticker[t]]
        heads = []
        for t in batch:
            for n in (news.get(t) or [])[:HEADLINES_PER_NAME]:
                heads.append(f"{t} | {n.get('time', '')} | {n.get('source', '')} | "
                             f"{' '.join(str(n.get('headline', '')).split())} | {n.get('url', '')}")
        user = (f"Today is {today.isoformat()}. The rules script flagged these names. "
                "Each line ends with the trading session its numbers come from; the catalyst "
                "must explain that session's move:\n"
                + "\n".join(lines)
                + ("\n\nRecent headlines from Finnhub (ticker | time UTC | source | headline | link). "
                   "Use these first and cite their links. Search the web only for a name "
                   "whose headlines do not explain the move:\n" + "\n".join(heads) if heads else "")
                + "\n\nReturn the JSON array only, one object per ticker, "
                  "using the tickers exactly as written above.")
        searches = sum(SEARCHES_WITH_NEWS if news.get(t) else SEARCHES_PER_NAME for t in batch)
        try:
            text, _cited = ask("02-tape-reader.md", user, max_tokens=4096,
                               searches=searches, client=client)
        except Exception as e:  # one failed batch must not lose the others
            cause = e.__cause__ or e.__context__
            log.error("Tape Reader batch %s failed: %s%s", ",".join(batch), type(e).__name__,
                      f" (caused by {type(cause).__name__})" if cause else "")
            continue
        if text and text.strip() != NOTHING:
            # A line without its own links keeps none: sources are never guessed across tickers.
            got = {t: drop_stale(r, today) for t, r in parse_tape(text, batch).items()}
            missing = [t for t in batch if t not in got]
            if missing:
                log.warning("Tape Reader returned no read for %s", ",".join(missing))
            reads.update(got)
    return reads


# ---------------------------------------------------------------- Chartist lens
TRENDS = ("up", "down", "range")
ACTIONS = ("add", "trim", "watch")


def action_of(item: dict) -> str:
    """A lens's suggested action, or "" when it gave none we recognise."""
    a = str(item.get("action", "")).strip().lower()
    return a if a in ACTIONS else ""
TIMEFRAMES = ("daily", "weekly")


def _num(x) -> Optional[float]:
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return None


SIDE_TOL = 0.0025   # 0.25%: a level the price is sitting on still counts on either side


def check_levels(item: dict, lv: dict) -> Optional[dict]:
    """Keep a Chartist read only if its prices come from the levels we gave it,
    with support below resistance. Works for names going up or down: the desk
    adds near support and trims into resistance; it never shorts."""
    from desk.levels import snap, within_reach

    sup, res = _num(item.get("support")), _num(item.get("resistance"))
    if not lv:
        return None
    if sup is None or res is None:
        return None
    sup, res = snap(sup, lv), snap(res, lv)
    if sup is None or res is None or sup >= res:
        return None                                  # a price not on our list: the read is not trusted
    # Support sits at or below the price, resistance at or above (Oct 18: Metro at 70.06
    # got "resistance 69.59", a level it had already cleared). A small allowance keeps a
    # level the price is sitting on. A read on the wrong side is not trusted at all,
    # because its watch line is built around the mislabelled level.
    last = lv.get("last")
    if last and (sup > last * (1 + SIDE_TOL) or res < last * (1 - SIDE_TOL)):
        return None
    # One side out of reach no longer drops the whole read (Oct 16: HACK broke its
    # 20-day low and the next support, the 52-week low, was 12% below; the broken
    # low as resistance and the view were lost with it). The far side is named instead.
    far = {}
    for side, p in (("support", sup), ("resistance", res)):
        if p is not None and not within_reach(p, lv):
            far[side] = {"price": p, "name": next((k for k, v in lv["levels"].items() if v == p), "")}
    sup = None if "support" in far else sup
    res = None if "resistance" in far else res
    if sup is None and res is None:
        return None
    trend = str(item.get("trend", "")).lower()
    tf = str(item.get("timeframe", "")).lower()
    words = " ".join(str(item.get("watch", "")).split()).split(" ")
    view = " ".join(str(item.get("view", "")).split())
    # Prices belong in support/resistance, where they are checked. A view that
    # quotes a price could carry an unchecked number, so the view alone is dropped.
    if "$" in view or re.search(r"\d+\.\d{2}\b", view) or len(view.split()) > 45:
        view = ""
    return {"trend": trend if trend in TRENDS else "range",
            "timeframe": tf if tf in TIMEFRAMES else "daily",
            "support": sup, "resistance": res, "far": far,
            "watch": " ".join(words[:25]).strip(), "view": view,
            # A vote needs its reason: no view, no action shown.
            "action": action_of(item) if view else ""}


def tech_line(t: dict) -> str:
    """Chartist's facts as one line for the prompt."""
    if not t:
        return ""
    parts = [f"RSI14 {t['rsi14']}" if t.get("rsi14") is not None else ""]
    for k, name in (("td_daily", "DeMark daily"), ("td_weekly", "DeMark weekly")):
        d = t.get(k) or {}
        if d.get("side"):
            parts.append(f"{name}: {d['side']} setup {d['count']} of 9")
    cd = t.get("td_countdown") or {}
    if cd:
        when = "" if not cd.get("bars_ago") else f", completed {cd['bars_ago']} bars ago"
        parts.append(f"DeMark daily countdown: {cd['side']} {cd['count']} of 13{when}")
    for r in t.get("rel_strength", []):
        parts.append(f"vs {r['vs']} {r['days']}d: {r['label']} ({r['chg_pct']:+.1f}%)")
    return " | ".join(p for p in parts if p)


def breadth_line(b: dict) -> str:
    if not b:
        return ""
    s = f"{b['above_50d_pct']}% of the team's {b['names']} names are above their 50-day average"
    if b.get("above_200d_pct") is not None:
        s += f", {b['above_200d_pct']}% above their 200-day"
    return s


def chartist_lens(alerts: list[dict], levels: dict[str, dict], *, client=None,
                take_budget=None, breadth: str = "") -> dict[str, dict]:
    """Levels for the alerts about to be emailed, in one call, no web search.
    Every price is checked against the code's levels; anything else is dropped."""
    names = [a for a in alerts if levels.get(a["ticker"])]
    if not names:
        return {}
    if take_budget and not take_budget():
        log.warning("Chartist lens skipped: no AI calls left (daily cap DAILY_AI_CALLS=%d, or the "
                    "dry-run cap); %d alert(s) go without levels", DAILY_AI_CALLS, len(names))
        return {}
    blocks = []
    for a in names:
        lv = levels[a["ticker"]]
        rules = ", ".join(f["rule"] for f in a["flags"])
        lvl = "; ".join(f"{k} {v}" for k, v in lv["levels"].items())
        tech = tech_line(lv.get("tech") or {})
        blocks.append(f"{a['ticker']} | {a['horizon']} | {rules} | Tape Reader: {a.get('direction')}, "
                      f"{a.get('catalyst')}\n  last {lv['last']} | daily trend {lv['trend_daily']} | "
                      f"weekly trend {lv['trend_weekly']} | ATR14 {lv.get('atr14')}\n  levels: {lvl}"
                      + (f"\n  momentum and leadership: {tech}" if tech else ""))
    user = ("Set levels for these alerts. Use only the prices listed for each name.\n\n"
            + (f"Breadth today: {breadth}.\n\n" if breadth else "")
            + "\n\n".join(blocks)
            + "\n\nReturn the JSON array only, one object per ticker.")
    text, _ = ask("04-chartist-lens.md", user, max_tokens=chartist_tokens(len(names)),
                  effort=CHARTIST_EFFORT, client=client or _client())
    items = json_array(text)
    if not items:
        # Model output only (no secrets in it): enough to see what went wrong.
        log.warning("Chartist lens: no JSON array in the answer. It began: %r", " ".join(text.split())[:300])
    wanted = [a["ticker"] for a in names]
    out: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        t = _match_ticker(str(it.get("ticker", "")), wanted)
        if not t:
            log.warning("Chartist lens: answer for unknown ticker %r ignored", it.get("ticker"))
        elif t not in out:
            ok = check_levels(it, levels[t])
            if ok:
                out[t] = ok
            else:
                log.warning("Chartist lens levels for %s dropped: support %r, resistance %r, last %s",
                            t, it.get("support"), it.get("resistance"), levels[t].get("last"))
    log.info("Chartist lens: levels kept for %d of %d alert(s)", len(out), len(names))
    return out


# ---------------------------------------------------------------- What they said (daily)
# Whose real, sourced public comments to quote. Configurable; the defaults are the two
# strategists whose public frameworks the Chartist and Strategist lenses are modelled on.
PEOPLE = tuple(p.strip() for p in (os.getenv("WATCHED_STRATEGISTS") or "Tom Lee,Mark Newton").split(",") if p.strip())
SAID_MAX_AGE_DAYS = 14


def check_said(items: list, today: date) -> list[dict]:
    """Keep only sourced, dated, recent comments by the people we follow."""
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        who = next((p for p in PEOPLE if p.lower() in str(it.get("who", "")).lower()), "")
        url = str(it.get("url", "")).strip()
        try:
            d = date.fromisoformat(str(it.get("date", ""))[:10])
        except ValueError:
            continue
        if not who or not url.startswith("http") or not is_real_source(url):
            continue
        if not (0 <= (today - d).days <= SAID_MAX_AGE_DAYS):
            continue
        summary = " ".join(" ".join(str(it.get("summary", "")).split()).split(" ")[:40])
        if not summary:
            continue
        lst = lambda k: [str(x).strip() for x in (it.get(k) or []) if str(x).strip()][:8] \
            if isinstance(it.get(k), list) else []
        out.append({"who": who, "date": d.isoformat(), "where": " ".join(str(it.get("where", "")).split())[:40],
                    "summary": summary, "tickers": [t.upper() for t in lst("tickers")],
                    "sectors": lst("sectors"), "url": url})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:8]


def what_they_said(today: date, *, client=None, take_budget=None) -> list[dict]:
    """Recent public calls by the people in PEOPLE (last 14 days), one call a day."""
    if take_budget and not take_budget():
        log.warning("What they said skipped: no AI calls left")
        return []
    user = (f"Today is {today.isoformat()}. Find {' and '.join(p + chr(39) + 's' for p in PEOPLE)} most recent public "
            f"comments from the last {SAID_MAX_AGE_DAYS} days. Return the JSON array only.")
    # 6000: up to 8 comments with links run past 3000 tokens (Oct 17: cut off at
    # 3148, the JSON was incomplete and all comments were lost).
    text, _ = ask("07-what-they-said.md", user, max_tokens=6000, searches=4, client=client or _client())
    items = check_said(json_array(text), today)
    log.info("What they said: %d sourced comment(s) kept", len(items))
    return items


# ---------------------------------------------------------------- Strategist lens
def strategist_lens(alerts: list[dict], backdrop: str, said: list[dict], *, client=None,
             take_budget=None, market_read: str = "") -> dict[str, dict]:
    """A Strategist (Lee-style) view per alert, plus the real comments that fit it.
    The model can only point at comments by number; the code prints them."""
    if not alerts or not backdrop:
        return {}
    if take_budget and not take_budget():
        log.warning("Strategist lens skipped: no AI calls left; %d alert(s) go without it", len(alerts))
        return {}
    blocks = []
    for a in alerts:
        f = a["flags"][0]
        # Leadership only. DeMark counts and RSI are Chartist's; given them, the Strategist line
        # repeated them (Oct 20 NBIS: "RSI deeply oversold, DeMark buy setup").
        t_all = (a.get("chartist") or {}).get("tech") or a.get("tech") or {}
        tech = tech_line({"rel_strength": t_all.get("rel_strength", [])})
        name = f" ({a['name']})" if a.get("name") else ""
        blocks.append(f"{a['ticker']}{name} | {a['horizon']} | {', '.join(x['rule'] for x in a['flags'])} | "
                      f"day change {f.get('day_change_pct')}% | Tape Reader: {a.get('direction')}, "
                      f"scope {a.get('scope', 'unknown')}: {a.get('catalyst')}"
                      + (f"\n  leadership: {tech}" if tech else ""))
    listed = "\n".join(f"{i}. {s['who']}, {s['date']}, {s['where']}: {s['summary']} "
                       f"(tickers: {', '.join(s['tickers']) or 'none'}; sectors: {', '.join(s['sectors']) or 'none'})"
                       for i, s in enumerate(said)) or "(none found)"
    read = (f"\n\nToday's market read, already sent to the team: {market_read}\n"
            "Your views must be consistent with it. If it is cautious, say what makes this "
            "name different before calling its move a dip to buy." if market_read else "")
    user = (f"Market backdrop today: {backdrop}{read}\n\nRecent public comments:\n{listed}\n\n"
            "Alerts:\n" + "\n\n".join(blocks)
            + "\n\nReturn the JSON array only, one object per ticker.")
    text, _ = ask("05-strategist-lens.md", user, model=MODEL_STRATEGIST, max_tokens=2500, client=client or _client())
    items = json_array(text)
    if not items:
        log.warning("Strategist lens: no JSON array in the answer. It began: %r", " ".join(text.split())[:300])
    wanted = [a["ticker"] for a in alerts]
    out: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        t = _match_ticker(str(it.get("ticker", "")), wanted)
        if not t or t in out:
            continue
        view = " ".join(str(it.get("view", "")).split())
        if "$" in view or re.search(r"\d+\.\d{2}\b", view) or len(view.split()) > 45:
            view = ""                               # prices belong to Chartist's checked levels
        picks = []
        for n in it.get("said") or []:
            if isinstance(n, int) and 0 <= n < len(said) and said[n] not in picks:
                picks.append(said[n])
        if view or picks:
            out[t] = {"view": view, "said": picks[:2], "action": action_of(it) if view else ""}
    log.info("Strategist lens: view for %d of %d alert(s)", len(out), len(alerts))
    return out


# ---------------------------------------------------------------- VIP filter
def headline_key(ticker: str, headline: str) -> str:
    """The same story reposted by another outlet usually keeps its exact headline."""
    return f"{ticker}: " + re.sub(r"[^a-z0-9]+", " ", str(headline).lower()).strip()


def vip_filter(items: list[dict], *, client=None, take_budget=None, sent_today=None,
               names: Optional[dict] = None) -> list[dict]:
    """Keep only material VIP headlines (fast model, one call). Price moves always
    pass. Any failure keeps everything, so a VIP item is never lost silently.
    sent_today: headlines emailed or held recently ("TICKER: headline"). An exact
    repeat is dropped here in code; the model drops other write-ups of the same event."""
    names = names or {}
    seen = {headline_key(*h.split(": ", 1)) for h in (sent_today or []) if ": " in h}
    repeats = [i for i in items if i.get("type") == "news"
               and headline_key(i["ticker"], i.get("headline", "")) in seen]
    if repeats:   # Oct 20: yesterday's "Optimus, Cybercab" and "Starlink for airlines" went out again
        log.info("VIP filter: %d exact repeat(s) dropped", len(repeats))
        items = [i for i in items if i not in repeats]
    news = [i for i in items if i.get("type") == "news"]
    if not news or (take_budget and not take_budget()):
        return items
    label = lambda t: f"{t} ({names[t]})" if names.get(t) else t
    listed = "\n".join(f"{n}. {label(i['ticker'])} | keywords: {', '.join(i.get('keywords') or [])} | "
                       f"{' '.join(str(i.get('headline', '')).split())} ({i.get('source', '')})"
                       for n, i in enumerate(news))
    try:
        before = ("Already emailed or held in the last two days (drop any headline about the same "
                  "event):\n" + "\n".join(f"- {h}" for h in sent_today) + "\n\n") if sent_today else ""
        text, _ = ask("08-vip-filter.md", f"{before}Headlines:\n{listed}\n\nReturn the JSON array only.",
                      model=MODEL_FAST, max_tokens=1000, client=client or _client())
    except Exception as e:
        log.error("VIP filter failed, sending unfiltered: %s", type(e).__name__)
        return items
    verdicts = json_array(text)
    if not verdicts:
        log.warning("VIP filter: no JSON answer; sending unfiltered")
        return items
    keep = {v.get("n") for v in verdicts if isinstance(v, dict) and v.get("keep") is True}
    kept_news = [i for n, i in enumerate(news) if n in keep]
    log.info("VIP filter: kept %d of %d headline(s)", len(kept_news), len(news))
    return [i for i in items if i.get("type") != "news"] + kept_news


def strategist_market(backdrop: str, said: list[dict], *, client=None, take_budget=None) -> str:
    """One Strategist (Lee-style) read of the whole market for the day, shown once at the
    top of the opening digest instead of repeated on every alert."""
    if not backdrop or (take_budget and not take_budget()):
        return ""
    listed = "\n".join(f"{i}. {s['who']}, {s['date']}, {s['where']}: {s['summary']}"
                       for i, s in enumerate(said)) or "(none found)"
    user = (f"Market backdrop today: {backdrop}\n\nRecent public comments:\n{listed}\n\n"
            "There are no alerts this time. Give the market read only, as one object: "
            '[{"ticker": "MARKET", "view": "...", "said": []}]')
    text, _ = ask("05-strategist-lens.md", user, model=MODEL_STRATEGIST, max_tokens=800, client=client or _client())
    for it in json_array(text):
        if isinstance(it, dict):
            view = " ".join(str(it.get("view", "")).split())
            if view and "$" not in view and len(view.split()) <= 45:
                return view
    return ""


# ---------------------------------------------------------------- Ask the desk
ASK_LABELS = ("Chartist, Newton-style (simulated):", "Strategist, Lee-style (simulated):", "Tape Reader:")


def answer_question(question: str, quoted: str, facts: str, *, client=None, take_budget=None) -> tuple[str, list[str]]:
    """One answer to a team member's reply, as the specialist who owns it.
    Returns (answer, source links). A price not in the facts drops the answer."""
    if take_budget and not take_budget():
        return "", []
    user = (f"Question from the team:\n{question}\n\nThe desk email they replied to:\n{quoted or '(not quoted)'}"
            f"\n\nFacts computed today:\n{facts or '(none: no name identified)'}\n\nAnswer now.")
    text, cited = ask("09-ask-desk.md", user, max_tokens=1200, searches=2, client=client or _client())
    text = text.strip()
    allowed = set(re.findall(r"\d+\.\d{2}", facts + " " + quoted))
    stray = [p for p in re.findall(r"\$?(\d+\.\d{2})\b", text) if p not in allowed]
    if stray:
        log.warning("Ask the desk: answer dropped, prices not in the facts: %s", ", ".join(stray[:5]))
        return "", []
    links = [u for u in cited if is_real_source(u)]
    return text, links
