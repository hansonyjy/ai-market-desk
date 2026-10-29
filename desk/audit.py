"""Daily health check: the problems we have already met, checked every day after the
close from the Signal Log and the State tab, so a regression is reported instead of
being found by reading the inbox. Pure functions; scanner.run_audit does the I/O.

Each check names the day it was first seen. When a new kind of problem turns up,
add a check here with a test, so it cannot come back unnoticed."""
from __future__ import annotations

import re

FAIL, WARN = "FAIL", "WARN"


def _key(ticker: str, headline: str) -> str:
    return f"{ticker}: " + re.sub(r"[^a-z0-9]+", " ", str(headline).lower()).strip()


def _rows(values: list[list[str]]) -> list[dict]:
    if not values:
        return []
    head = values[0]
    return [{h: (r[i] if i < len(r) else "") for i, h in enumerate(head)} for r in values[1:] if r]


def checks(values: list[list[str]], state: dict, universe: list[dict], today: str,
           ai_cap: int, caps: dict) -> list[tuple[str, str]]:
    """today: the Signal Log's date (Pacific), 'YYYY-MM-DD'. Returns (level, message)."""
    rows = _rows(values)
    day = [r for r in rows if r.get("Timestamp", "").startswith(today)]
    earlier = sorted({r["Timestamp"][:10] for r in rows if r.get("Timestamp", "")[:10] < today})
    prev = [r for r in rows if earlier and r.get("Timestamp", "").startswith(earlier[-1])]
    out: list[tuple[str, str]] = []

    # Oct 13: emails that never went out
    for r in day:
        if r.get("Pinged") == "failed":
            out.append((FAIL, f"Email failed to send: {r['Ticker']} {r.get('Rule', '')[:60]}"))

    # Oct 16 / Oct 20: the same VIP story sent twice, today or again from yesterday
    vip = [r for r in day if r.get("Horizon") == "VIP"]
    sent, seen_prev = {}, {_key(r["Ticker"], r.get("Rule", "").split(" | ", 1)[-1])
                           for r in prev if r.get("Horizon") == "VIP" and r.get("Pinged") in ("yes", "held")}
    for r in vip:
        k = _key(r["Ticker"], r.get("Rule", "").split(" | ", 1)[-1])
        if r.get("Pinged") != "yes":
            continue
        if k in sent:
            out.append((FAIL, f"VIP story emailed twice today: {k[:90]}"))
        elif k in seen_prev:
            out.append((FAIL, f"VIP story from {earlier[-1]} emailed again: {k[:90]}"))
        sent[k] = True
    # Oct 19: VIP news held by the cap
    held = [r for r in vip if r.get("Pinged") == "held"]
    if held:
        out.append((WARN, f"{len(held)} VIP item(s) held by the daily cap, e.g. "
                          f"{held[-1]['Ticker']}: {held[-1].get('Rule', '').split(' | ', 1)[-1][:80]}"))

    # Oct 18: alerts that went out without a Chartist or Strategist line
    alerts = [r for r in day if r.get("Horizon") in ("SHORT", "LONG") and r.get("Pinged") == "yes"]
    by_name = {}
    for r in alerts:
        by_name.setdefault(r["Ticker"], r)
    no_chartist = sorted(t for t, r in by_name.items() if not r.get("Chartist"))
    no_strategist = sorted(t for t, r in by_name.items() if not r.get("Strategist"))
    if no_chartist:
        out.append((WARN, f"Alert(s) without a Chartist line: {', '.join(no_chartist)}"))
    if no_strategist:
        out.append((WARN, f"Alert(s) without a Strategist line: {', '.join(no_strategist)}"))

    # Oct 14: an emailed read with no source
    for r in day:
        if r.get("Pinged") in ("yes", "digest") and r.get("Catalyst", "").startswith("[no source]"):
            out.append((FAIL, f"Emailed without a source: {r['Ticker']}"))

    # Oct 17: Ask the desk questions that got no real answer
    for r in day:
        if r.get("Horizon") == "ASK" and "could not answer" in r.get("Catalyst", "").lower():
            out.append((WARN, f"Ask the desk could not answer: {r['Ticker'] or 'general'}: "
                              f"{r.get('Rule', '').split(' | ', 1)[-1][:80]}"))

    # Oct 14 / Oct 19: budgets and caps
    b = state.get("ai_budget") or {}
    if b.get("date") == today:
        calls, usd = b.get("calls", 0), b.get("usd", 0)
        if calls >= 0.8 * ai_cap:
            out.append((WARN, f"AI calls {calls} of {ai_cap}: later hits may go unread"))
        if usd and float(usd) > 2.0:
            out.append((WARN, f"AI spend ${float(usd):.2f} today (target about $1.50)"))
    e = state.get("email_budget") or {}
    # VIP may pass its cap on purpose in an earnings window (Oct 21: 8 of 6), and held
    # VIP items are reported above, so only SHORT and LONG are checked here.
    for h in (("SHORT", "LONG") if e.get("date") == today else ()):
        if e.get(h, 0) >= caps.get(h, 99):
            out.append((WARN, f"{h} email cap reached ({e.get(h)} of {caps[h]})"))

    # Oct 20: Universe names that do not match the security (SPCX "Space Exploration ETF")
    earn = (state.get("earnings") or {}).get("map", {})
    for u in universe:
        t, name = u.get("ticker", ""), u.get("name", "")
        if not name:
            out.append((WARN, f"Universe: {t} has no name; emails will show the ticker only"))
        elif "etf" in name.lower() and t in earn:
            out.append((WARN, f"Universe: {t} is named '{name}' but has an earnings date "
                              f"({earn[t]}); an ETF does not report. Check the name."))

    if not day:
        out.append((WARN, "Nothing logged today: check the Actions tab and cron-job.org"))
    return out


def summary(values: list[list[str]], today: str) -> str:
    rows = [r for r in _rows(values) if r.get("Timestamp", "").startswith(today)]
    count = lambda f: sum(1 for r in rows if f(r))
    names = lambda f: len({r["Ticker"] for r in rows if f(r)})
    tagged = sorted({r["Ticker"] for r in rows if "| dm_buy_volume" in r.get("Rule", "")})
    return (f"Logged {len(rows)} rows. Digest names {names(lambda r: r.get('Pinged') == 'digest')}, "
            f"alerts {names(lambda r: r.get('Horizon') in ('SHORT', 'LONG') and r.get('Pinged') == 'yes')}, "
            f"VIP sent {count(lambda r: r.get('Horizon') == 'VIP' and r.get('Pinged') == 'yes')}, "
            f"questions answered {count(lambda r: r.get('Horizon') == 'ASK' and r.get('Pinged') == 'replied')}."
            + (f" Hypothesis tag: {', '.join(tagged)}." if tagged else ""))


def compose(findings: list[tuple[str, str]], line: str, today: str) -> tuple[str, str]:
    fails = sum(1 for lv, _ in findings if lv == FAIL)
    warns = len(findings) - fails
    status = "all clear" if not findings else f"{fails} fail, {warns} warn"
    body = [f"AI Market Desk health check for {today}: {status}.", "", line, ""]
    body += [f"{lv}: {msg}" for lv, msg in sorted(findings, key=lambda x: x[0] != FAIL)] or ["No problems found."]
    body += ["", "Checks live in audit.py. Each one is a problem met before; add one when a new kind turns up."]
    return f"AI Market Desk health · {today} · {status}", "\n".join(body)
