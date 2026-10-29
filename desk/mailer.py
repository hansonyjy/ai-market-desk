"""Alert emails from the desk Gmail.

Which hits become an email is decided here, in code:
- only ACCUMULATION or DISTRIBUTION reads (NOISE is logged, never emailed)
- only reads with a real source (no source, no claim)
- at most SHORT_ALERTS_PER_DAY and LONG_ALERTS_PER_DAY; the rest are "held"
The footer is added here too, so it can never be left out.
"""
from __future__ import annotations

import html
import logging
import os
import re
import smtplib
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("scanner")

FOOTER = ("Automated message from AI Market Desk. Not reviewed by a person before sending. "
          "Research input, not advice.")
SMTP_HOST = "smtp.gmail.com"
CONF_RANK = {"high": 0, "medium": 1, "low": 2}


def _setting(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def min_rvol() -> float:
    """SHORT alerts need at least this relative volume to be emailed (logged either way).
    Oct 9: IMO.TO and FANG 'breakouts' on 0.4x and 0.5x volume were emailed."""
    return float(_setting("EMAIL_MIN_RVOL", "1.5"))


def sharp_drop(f: dict) -> bool:
    """A long-term signal that fired on a day down SHARP_DROP_PCT (3%) or more, on at
    least normal volume: selling may not be finished. Tagged in the Signal Log so the
    Scorecard can test whether these pullbacks keep falling (decided Oct 12)."""
    chg, rvol = f.get("day_change_pct"), f.get("rvol")
    return (f.get("horizon") == "LONG" and chg is not None and rvol is not None
            and chg <= -float(_setting("SHARP_DROP_PCT", "3")) and rvol >= 1.0)


def caps() -> dict[str, int]:
    return {"SHORT": int(_setting("SHORT_ALERTS_PER_DAY", "7")),
            "LONG": int(_setting("LONG_ALERTS_PER_DAY", "2")),
            "VIP": int(_setting("VIP_ALERTS_PER_DAY", "6"))}


# ---------------------------------------------------------------- what to send
# Levels the user set in the Universe. Reaching one is the news itself, so it is
# emailed even when no source explains the move, and it never uses up a daily cap.
OWN_LEVEL_RULES = {"target_entry_hit", "trim_level_hit"}
NO_NEWS = "No news found that explains this move."
def pick_alerts(flags: list[dict], reads: dict, state: dict, today: date) -> tuple[list[dict], list[dict]]:
    """One alert per ticker. Returns (send, held). Counts today's sends in state."""
    by_ticker: dict[str, list[dict]] = {}
    for f in flags:
        by_ticker.setdefault(f["ticker"], []).append(f)
    eligible = []
    for t, fs in by_ticker.items():
        r = reads.get(t) or {}
        own = any(f["rule"] in OWN_LEVEL_RULES for f in fs)
        if r.get("direction") not in ("ACCUMULATION", "DISTRIBUTION") or not r.get("links"):
            if not own:
                continue
            # Your own level with no sourced story: still sent, and it says so.
            r = {"catalyst": NO_NEWS, "links": [], "direction": "", "confidence": "", "scope": "",
                 "no_news": True}
        elif r.get("confidence") == "low" and not own:
            continue                                   # logged for the Scorecard, not emailed
        horizon = "LONG" if any(f["horizon"] == "LONG" for f in fs) else "SHORT"
        if horizon == "SHORT" and max((f.get("rvol") or 0) for f in fs) < min_rvol():
            continue                                   # a move on thin volume is not confirmed
        eligible.append({"ticker": t, "horizon": horizon, "flags": fs, "own_level": own, **r})
    eligible.sort(key=lambda a: (CONF_RANK.get(a.get("confidence"), 3),
                                 -max((f.get("rvol") or 0) for f in a["flags"])))
    sent = state.get("email_budget") or {}
    if sent.get("date") != today.isoformat():
        sent = {"date": today.isoformat(), "SHORT": 0, "LONG": 0, "VIP": 0}
    limit = caps()
    send, held = [], []
    for a in sorted(eligible, key=lambda a: not a["own_level"]):   # your levels first, outside the caps
        if a["own_level"]:
            send.append(a)
        elif sent.get(a["horizon"], 0) < limit[a["horizon"]]:
            sent[a["horizon"]] = sent.get(a["horizon"], 0) + 1
            send.append(a)
        else:
            held.append(a)
    state["email_budget"] = sent
    return send, held


def pick_digest(flags: list[dict], reads: dict) -> list[dict]:
    """The opening's SHORT hits worth reading: same quality bar as alerts (sourced,
    not NOISE, not low confidence, enough volume), but no daily cap."""
    by_ticker: dict[str, list[dict]] = {}
    for f in flags:
        if f["horizon"] == "SHORT":
            by_ticker.setdefault(f["ticker"], []).append(f)
    out = []
    for t, fs in by_ticker.items():
        r = reads.get(t) or {}
        if (r.get("direction") in ("ACCUMULATION", "DISTRIBUTION") and r.get("links")
                and r.get("confidence") != "low"
                and max((f.get("rvol") or 0) for f in fs) >= min_rvol()):
            out.append({"ticker": t, "horizon": "SHORT", "flags": fs, **r})
    out.sort(key=lambda a: (CONF_RANK.get(a.get("confidence"), 3), -abs(a["flags"][0].get("day_change_pct") or 0)))
    return out


VIP_ITEMS_PER_EMAIL = int(_setting("VIP_ITEMS_PER_EMAIL", "8"))


def take_vip(state: dict, today: date, n: int, force: bool = False) -> int:
    """How many of n VIP items go out now. The cap counts VIP emails, not headlines
    (Oct 19: one 6:00 PT email of 6 overnight headlines used the whole day's cap of 6,
    and every VIP headline after it was held, the day before SPCX earnings).
    One email carries at most VIP_ITEMS_PER_EMAIL items."""
    b = state.get("email_budget") or {}
    if b.get("date") != today.isoformat():
        b = {"date": today.isoformat(), "SHORT": 0, "LONG": 0, "VIP": 0}
    state["email_budget"] = b
    if not n or (b.get("VIP", 0) >= caps()["VIP"] and not force):
        return 0
    b["VIP"] = b.get("VIP", 0) + 1
    return min(n, VIP_ITEMS_PER_EMAIL)


def refund(state: dict, alerts: list[dict]) -> None:
    """Give back today's alert budget for alerts that failed to send."""
    b = state.get("email_budget") or {}
    for a in alerts:
        if b.get(a["horizon"], 0) > 0:
            b[a["horizon"]] -= 1


# ---------------------------------------------------------------- what it says
# Written for the reader, not the code: no rule names, no unexplained numbers.
HORIZON = {"SHORT": "Short-term signal (days to weeks)", "LONG": "Long-term entry signal",
           "VIP": "VIP news"}
READ = {"ACCUMULATION": "buying", "DISTRIBUTION": "selling"}
# A LONG signal is a pullback: the question is whether the thesis still holds.
READ_LONG = {"ACCUMULATION": "no news that breaks the thesis",
             "DISTRIBUTION": "news that puts the thesis at risk"}
SCOPE = {"company": "company news", "sector": "a sector-wide move", "macro": "a market-wide move"}
EXCHANGE = {".TO": "TSX", ".V": "TSXV", ".NE": "Cboe Canada"}


def horizon_label(a: dict) -> str:
    """The label under the name. A trim level is an exit, not an entry."""
    if a["horizon"] == "LONG" and any(f["rule"] == "trim_level_hit" for f in a["flags"]):
        return "Your trim level"
    return HORIZON.get(a["horizon"], a["horizon"])


def show_ticker(t: str) -> str:
    """'IMO.TO' -> 'IMO (TSX)'. Email apps turn 'IMO.TO' into a link to the website
    imo.to (.to is a country domain), so a Canadian ticker is never written that way."""
    for suffix, exch in EXCHANGE.items():
        if t.upper().endswith(suffix):
            return f"{t[:-len(suffix)]} ({exch})"
    return t


def money(x, ticker: str = "") -> str:
    """US$ by default; Canadian listings in C$."""
    cur = "C$" if any(ticker.upper().endswith(k) for k in EXCHANGE) else "$"
    return f"{cur}{x:,.2f}"


def title(a: dict) -> str:
    """'Tesla (TSLA)', 'CGI (GIB-A, TSX)', or just the ticker without a name."""
    t = show_ticker(a["ticker"])
    if not a.get("name"):
        return t
    return f"{a['name']} ({t.replace(' (', ', ').rstrip(')')})" if "(" in t else f"{a['name']} ({t})"


def what_happened(f: dict, t: str = "") -> str:
    """One rule hit in plain English."""
    ref, rule = f.get("ref"), f["rule"]
    m = lambda x: money(x, t or f.get("ticker", ""))
    if rule == "rvol_spike":
        return f"is trading {f['rvol']:.1f}x its normal volume for this time of day"
    if rule in ("gap_up", "gap_down") and ref is not None:
        return f"opened {abs(ref):.1f}% {'above' if ref > 0 else 'below'} yesterday's close"
    if rule.startswith("breakout_") and ref is not None:
        return f"broke above its {rule.split('_')[1].rstrip('d')}-day high of {m(ref)}"
    if rule.startswith("breakdown_") and ref is not None:
        return f"fell below its {rule.split('_')[1].rstrip('d')}-day low of {m(ref)}"
    if rule == "pullback_200dma" and ref is not None:
        if f.get("price") is not None and f["price"] < ref:
            # Oct 21 MSTR, Oct 28 ROP: "up 2.2%: pulled back to ..." while rising back toward it
            return (f"is just below its 200-day average of {m(ref)}, within 2%, after trading "
                    f"well above it this month (a level long-term buyers often defend)")
        return (f"pulled back to within 2% of its 200-day average of {m(ref)}, after trading "
                f"well above it (a level long-term buyers often defend)")
    if rule == "target_entry_hit" and ref is not None:
        return f"reached your target entry level of {m(ref)}"
    if rule == "trim_level_hit" and ref is not None:
        return f"reached your trim level of {m(ref)}"
    if rule == "earnings_soon":
        when = f["detail"].split("(")[0].replace("Earnings ", "").strip()
        if "(0 trading" in f["detail"]:
            return "reports earnings today"
        return f"has earnings coming up ({when})"
    if rule == "fast_move" and ref is not None:
        return f"moved {abs(ref):.1f}% {'higher' if ref > 0 else 'lower'} in the last 30 minutes"
    if rule == "day_move" and ref is not None and f.get("day_change_pct") is not None:
        d = f["day_change_pct"]
        return (f"is {'up' if d >= 0 else 'down'} {abs(d):.1f}% on the day, about {ref:.1f} times "
                f"its normal daily range")
    if rule == "gap_filled" and ref is not None:
        return (f"opened {abs(ref):.1f}% lower but has climbed back above yesterday's close "
                f"(the gap down was bought)")
    if rule == "gap_failed" and ref is not None:
        return (f"opened {ref:.1f}% higher but has given it all back, below yesterday's close "
                f"(the gap up was sold)")
    return f["detail"]


def headline(a: dict) -> str:
    """Email subject for one alert: 'TSLA up 3.2%: broke above its 20-day high of $386.83'."""
    f = a["flags"][0]
    move = ""
    if f.get("day_change_pct") is not None:
        d = f["day_change_pct"]
        move = f" {'up' if d >= 0 else 'down'} {abs(d):.1f}%"
    if f["rule"] == "day_move" and f.get("ref") is not None:
        event = f"about {f['ref']:.1f}x its normal daily range"   # the move itself is already in the subject
    else:
        event = what_happened(f, a["ticker"]).split(" (")[0].split(", after")[0]
        event = event[3:] if event.startswith("is ") else event
    return f"{show_ticker(a['ticker'])}{move}: {event}"


def call(a: dict, status: str = "") -> str:
    """The Call line, set by plain rules so the same facts always give the same call.
    Watch / Watch, do not chase / Consider adding / Consider trimming.
    A sector or market-wide selloff is never a reason to trim on its own."""
    held = status.strip().lower().startswith("hold")
    rules = {f["rule"] for f in a["flags"]}
    if held and "trim_level_hit" in rules:
        return "Consider trimming"                     # your own level
    if a.get("no_news"):
        # Your entry level, reached with no story behind it: a quiet drift to your
        # price is the entry you waited for; a sharp drop with no news found may
        # be news the desk missed, so wait.
        if "target_entry_hit" in rules and not any(sharp_drop(f) for f in a["flags"]):
            return "Consider adding"
        return "Watch"
    if a.get("confidence") == "low":
        return "Watch"
    if a.get("direction") == "DISTRIBUTION":
        if held and a.get("scope") == "company" and a.get("confidence") == "high":
            return "Consider trimming"
        return "Watch"
    if a.get("direction") == "ACCUMULATION" and a["horizon"] == "LONG":
        return "Consider adding"
    if a.get("direction") == "ACCUMULATION" and "gap_filled" in rules:
        return "Consider adding"                       # buyers stepped in: a timely entry, not a chase
    if a.get("direction") == "ACCUMULATION":
        return "Watch, do not chase"
    return "Watch"


LEVEL_NAME = {"pullback_200dma": "the 200-day average", "target_entry_hit": "your target entry"}


def risk_text(a: dict) -> str:
    """Shown under the Call, never instead of it: the idea stays visible, with its risk."""
    f = next((x for x in a["flags"] if sharp_drop(x)), None)
    if not f:
        return ""
    line = (f"Down {abs(f['day_change_pct']):.1f}% today on {f['rvol']:.1f}x normal volume, "
            f"so selling may not be finished.")
    if f.get("ref") is not None and f["rule"] in LEVEL_NAME:
        line += (f" A daily close below {LEVEL_NAME[f['rule']]} ({money(f['ref'], a['ticker'])}) "
                 f"would break the setup.")
    return line


def levels_text(n: dict, ticker: str = "") -> str:
    parts = []
    for side in ("support", "resistance"):
        if n.get(side) is not None:
            parts.append(f"{side} {money(n[side], ticker)}")
        elif (n.get("far") or {}).get(side):
            f = n["far"][side]
            where = "below" if side == "support" else "above"
            parts.append(f"no {side} nearby (next is {('the ' + f['name']) if f['name'] else 'a level'} at {money(f['price'], ticker)}, far {where})")
    text = ", ".join(parts)
    return f"{text[0].upper() + text[1:]}. {n['watch']}".strip()


VOTE = {"add": "Add", "trim": "Trim", "watch": "Watch"}


def vote(action: str, held: bool) -> str:
    """A lens's action as the reader sees it. Trim on a name we do not hold
    means do not buy here."""
    if action == "trim" and not held:
        return "Hold off"
    return VOTE.get(action, "")


def with_vote(view: str, action: str, held: bool) -> str:
    v = vote(action, held)
    return f"{v}. {view}" if v else view


def telling(flags: list[dict]) -> list[dict]:
    """A gap that reversed is described once, by the reversal (Oct 21 SNDK: "opened 3.8%
    above yesterday's close and opened 3.8% higher but has given it all back")."""
    rules = {f["rule"] for f in flags}
    drop = ({"gap_up"} if "gap_failed" in rules else set()) | ({"gap_down"} if "gap_filled" in rules else set())
    return [f for f in flags if f["rule"] not in drop] or flags


def sections(a: dict) -> list[tuple[str, str]]:
    """One alert as labelled sections. The plain-text and HTML emails are both
    built from this, so they can never say different things."""
    f = a["flags"][0]
    t = a["ticker"]
    events = " and ".join(what_happened(x, t) for x in telling(a["flags"]))
    facts = [f"now {money(f['price'], t)}"]
    if f.get("day_change_pct") is not None:
        d = f["day_change_pct"]
        facts.append(f"{'up' if d >= 0 else 'down'} {abs(d):.1f}% today")
    if f.get("rvol") is not None and not any(x["rule"] == "rvol_spike" for x in a["flags"]):
        facts.append(f"on {f['rvol']:.1f}x normal volume for this time of day")
    out = [("What happened", f"It {events}. It is {', '.join(facts)}."),
           ("Why", a["catalyst"])]
    if a.get("chartist"):
        out.append(("Levels", levels_text(a["chartist"], t)))
        if a["chartist"].get("view"):
            out.append(("Chartist, Newton-style (simulated)",
                        with_vote(a["chartist"]["view"], a["chartist"].get("action", ""), a.get("held", False))))
    strategist = a.get("strategist") or {}
    if strategist.get("view"):
        out.append(("Strategist, Lee-style (simulated)",
                    with_vote(strategist["view"], strategist.get("action", ""), a.get("held", False))))
    for c in strategist.get("said") or []:
        out.append(("What they said", f"{c['who']} ({said_date(c['date'])}, {c['where']}): {c['summary']} "
                                      f"[{_domain(c['url'])}]"))
    # "Thesis" wording only for company news: a sector or market move says nothing
    # about this name's thesis (Oct 16: QQQM in a rotation read "thesis at risk").
    out.append(("Call", f"{a.get('call') or call(a)}."))
    if not a.get("no_news"):   # no read to report: the Why line already says so
        long_words = a["horizon"] == "LONG" and not (a["direction"] == "DISTRIBUTION"
                                                     and a.get("scope") in ("sector", "macro"))
        read = (READ_LONG if long_words else READ).get(a["direction"], a["direction"].lower())
        scope = SCOPE.get(a.get("scope"))
        out.append(("Read", read[0].upper() + read[1:] + (f", driven by {scope}" if scope else "")
                    + f" ({a['confidence']} confidence)."))
    if risk_text(a):
        out.append(("Risk", risk_text(a)))
    return out


def risk_line(a: dict) -> str:
    r = risk_text(a)
    return f"Risk: {r}" if r else ""


def said_date(iso: str) -> str:
    """'2026-10-09' -> 'Oct 9'."""
    try:
        return date.fromisoformat(iso).strftime("%b %-d")
    except ValueError:
        return iso


def all_links(a: dict) -> list[str]:
    """The read's sources, then the links of any real comments shown."""
    out = list(a.get("links") or [])
    for c in (a.get("strategist") or {}).get("said") or []:
        if c.get("url") and c["url"] not in out:
            out.append(c["url"])
    return out


def format_alert(a: dict) -> str:
    lines = [f"{title(a)} · {horizon_label(a)}"]
    lines += [f"{k}: {v}" for k, v in sections(a)]
    lines.append("Sources: " + ("  ".join(all_links(a)) or "none found"))
    return "\n".join(lines)


def compose(alerts: list[dict]) -> tuple[str, str]:
    if len(alerts) == 1:
        subject = headline(alerts[0])
    else:
        subject = (f"AI Market Desk: {len(alerts)} alerts · "
                   + ", ".join(show_ticker(a["ticker"]) for a in alerts))
    body = "\n\n".join(format_alert(a) for a in alerts) + "\n\n--\n" + FOOTER + "\n"
    return subject, body


# ---------------------------------------------------------------- HTML version
# Inline styles only: Gmail and Outlook drop <style> blocks. Every piece of text
# is escaped, because the catalyst comes from the model and web pages.
_P = 'style="margin:6px 0"'
_RISK = 'style="margin:6px 0;color:#9a3412"'


def _domain(url: str) -> str:
    host = url.split("://", 1)[-1].split("/", 1)[0].lower()
    return host[4:] if host.startswith("www.") else host


def format_alert_html(a: dict) -> str:
    rows = []
    for k, v in sections(a):
        rows.append(f'<p {_RISK if k == "Risk" else _P}><b>{html.escape(k)}:</b> {html.escape(v)}</p>')
    links = " &middot; ".join(
        f'<a href="{html.escape(u, quote=True)}" style="color:#1d4ed8">{html.escape(_domain(u))}</a>'
        for u in all_links(a) if u.startswith(("https://", "http://")))
    rows.append(f'<p {_P}><b>Sources:</b> {links or "none found"}</p>')
    head = (f'<div style="font-size:16px;margin:0 0 4px"><b>{html.escape(title(a))}</b>'
            f'<span style="color:#6b7280"> &middot; {html.escape(horizon_label(a))}</span></div>')
    return f'<div style="margin:0 0 18px">{head}{"".join(rows)}</div>'


def compose_html(alerts: list[dict]) -> str:
    sep = '<hr style="border:none;border-top:1px solid #e5e7eb;margin:16px 0">'
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;'
            'color:#111827;max-width:680px">'
            + sep.join(format_alert_html(a) for a in alerts)
            + sep + f'<p style="color:#6b7280;font-size:12px">{html.escape(FOOTER)}</p></div>')


# ---------------------------------------------------------------- opening digest
DIGEST_ORDER = ["gap", "breakout", "breakdown", "fast", "day", "earnings", "rvol"]


def digest_line(a: dict) -> str:
    f = a["flags"][0]
    d = f.get("day_change_pct") or 0
    vol = f" on {f['rvol']:.1f}x volume" if f.get("rvol") is not None else ""
    # At most the two most telling events (Oct 14: NBIS chained four with "and").
    top = sorted(telling(a["flags"]), key=lambda x: DIGEST_ORDER.index(x["rule"].split("_")[0])
                 if x["rule"].split("_")[0] in DIGEST_ORDER else len(DIGEST_ORDER))[:2]
    events = " and ".join(what_happened(x, a["ticker"]) for x in top)
    read = READ.get(a["direction"], a["direction"].lower())
    scope = SCOPE.get(a.get("scope"))
    return (f"{title(a)}: {'up' if d >= 0 else 'down'} {abs(d):.1f}%{vol}. It {events}. "
            f"{a['catalyst']} Read: {read}" + (f", {scope}" if scope else "") + ".")


DIGEST_MAX = int(_setting("DIGEST_MAX", "8"))


def compose_digest(items: list[dict], themes: dict, backdrop: str = "",
                   market_read: str = "") -> tuple[str, str, str]:
    """One email for the open, grouped by the Universe tab's Theme column,
    under one line on the market backdrop. At most DIGEST_MAX names are written out,
    in the order given (pick_digest puts the clearest first); the rest are named in
    one line (Oct 22: a 15-name digest)."""
    items, more = items[:DIGEST_MAX], items[DIGEST_MAX:]
    groups: dict[str, list[dict]] = {}
    for a in items:
        groups.setdefault(themes.get(a["ticker"]) or "Other", []).append(a)
    order = sorted(groups, key=lambda g: -len(groups[g]))
    n = len(items) + len(more)
    subject = (f"AI Market Desk: opening digest · {n} name{'s' if n != 1 else ''} · "
               + ", ".join(show_ticker(a["ticker"]) for g in order for a in groups[g])
               + (f" +{len(more)} more" if more else ""))
    plain = ["What moved at the open, and why. Live alerts follow during the day.", ""]
    rich = ['<p style="margin:0 0 10px;color:#6b7280">What moved at the open, and why. '
            'Live alerts follow during the day.</p>']
    if backdrop:
        plain[1:1] = [f"Market backdrop: {backdrop}"]
        rich.append(f'<p style="margin:0 0 6px"><b>Market backdrop:</b> {html.escape(backdrop)}</p>')
    if market_read:
        plain.insert(2 if backdrop else 1, f"Strategist, Lee-style market read (simulated): {market_read}")
        rich.append(f'<p style="margin:0 0 12px"><b>Strategist, Lee-style market read (simulated):</b> '
                    f'{html.escape(market_read)}</p>')
    for g in order:
        plain.append(g.upper())
        rich.append(f'<div style="font-size:15px;margin:14px 0 4px"><b>{html.escape(g)}</b></div>')
        for a in groups[g]:
            line = digest_line(a)
            plain.append(f"- {line}  Sources: {'  '.join(a['links'])}")
            links = " &middot; ".join(
                f'<a href="{html.escape(u, quote=True)}" style="color:#1d4ed8">{html.escape(_domain(u))}</a>'
                for u in a["links"] if u.startswith(("https://", "http://")))
            rich.append(f'<p style="margin:4px 0 8px">{html.escape(line)} {links}</p>')
        plain.append("")
    if more:
        also = ", ".join(f"{show_ticker(a['ticker'])} {'up' if (a['flags'][0].get('day_change_pct') or 0) >= 0 else 'down'} "
                         f"{abs(a['flags'][0].get('day_change_pct') or 0):.1f}%" for a in more)
        plain += [f"Also moved: {also}. Reads for these are in the Signal Log.", ""]
        rich.append(f'<p style="margin:8px 0;color:#374151"><b>Also moved:</b> {html.escape(also)}. '
                    f'Reads for these are in the Signal Log.</p>')
    plain += ["--", FOOTER]
    rich.append(f'<hr style="border:none;border-top:1px solid #e5e7eb;margin:16px 0">'
                f'<p style="color:#6b7280;font-size:12px">{html.escape(FOOTER)}</p>')
    body_html = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;'
                 'color:#111827;max-width:680px">' + "".join(rich) + "</div>")
    return subject, "\n".join(plain) + "\n", body_html


# ---------------------------------------------------------------- VIP
def compose_vip(items: list[dict], names: dict) -> tuple[str, str, str]:
    """VIP news and fast moves, as found: keyword-matched headlines with their own
    link, and price moves. No AI in between, so they arrive fast."""
    def one(i):
        t = show_ticker(i["ticker"])
        who = f"{names.get(i['ticker'])} ({t})" if names.get(i["ticker"]) else t
        if i["type"] == "price_move":
            return who, f"Fast move: {i['headline']}", ""
        src = f" ({i['source']})" if i.get("source") else ""
        return who, f"News: {i['headline']}{src}", i.get("url") or ""
    rows = [one(i) for i in items]
    first = rows[0]
    subject = (f"VIP · {first[0]} · {first[1]}"[:150] if len(rows) == 1
               else f"VIP · {len(rows)} items · " + ", ".join(sorted({show_ticker(i['ticker']) for i in items})))
    plain = [f"{w}\n{t}" + (f"\n{u}" if u else "") for w, t, u in rows]
    rich = []
    for w, t, u in rows:
        link = (f' <a href="{html.escape(u, quote=True)}" style="color:#1d4ed8">{html.escape(_domain(u))}</a>'
                if u.startswith(("https://", "http://")) else "")
        rich.append(f'<p style="margin:6px 0 12px"><b>{html.escape(w)}</b><br>{html.escape(t)}{link}</p>')
    body = "\n\n".join(plain) + "\n\n--\n" + FOOTER + "\n"
    body_html = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;'
                 'color:#111827;max-width:680px">' + "".join(rich)
                 + f'<p style="color:#6b7280;font-size:12px">{html.escape(FOOTER)}</p></div>')
    return subject, body, body_html


# ---------------------------------------------------------------- sending
def _smtp_ssl(factory):
    return factory(SMTP_HOST, 465, timeout=30)


def _smtp_starttls(factory):
    s = factory(SMTP_HOST, 587, timeout=30)
    s.ehlo()
    s.starttls()
    s.ehlo()
    return s


def _reason(e: Exception, stage: str, port: int) -> str:
    """Type, stage and the server's reply code. Never the password: smtplib
    errors carry the server's reply, not our credentials."""
    code = getattr(e, "smtp_code", "")
    return f"{type(e).__name__} at {stage} on port {port}" + (f" (server code {code})" if code else "")


def recipients(raw: str) -> str:
    """Addresses as 'a@x.com, b@y.com'. ALERT_TO may be written with commas,
    semicolons (the Outlook habit) or spaces; a header with semicolons reaches only
    the first address, so the second reader would silently get nothing (Oct 20 pre-mortem)."""
    return ", ".join(dict.fromkeys(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", raw or "")))


def write_to_sink(folder: str, subject: str, body: str, html_body: str, to: str, in_reply_to: str) -> bool:
    """Save an email as NNN-subject.txt (and .html) instead of sending it. Numbered in
    the order sent, so a folder reads like the inbox would."""
    import pathlib

    d = pathlib.Path(folder)
    d.mkdir(parents=True, exist_ok=True)
    n = len(list(d.glob("*.txt"))) + 1
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:60]
    head = f"Subject: {subject}\nTo: {to or '(ALERT_TO)'}\n" + (f"In-Reply-To: {in_reply_to}\n" if in_reply_to else "")
    (d / f"{n:03d}-{slug}.txt").write_text(head + "\n" + body)
    if html_body:
        (d / f"{n:03d}-{slug}.html").write_text(f"<!-- {subject} -->\n" + html_body)
    log.info("email written to %s: %s", d / f"{n:03d}-{slug}.txt", subject)
    return True


def send(subject: str, body: str, smtp_factory=None, html_body: str = "", to: str = "",
         in_reply_to: str = "", references: str = "") -> bool:
    """Send from DESK_EMAIL to ALERT_TO through Gmail: port 465 (SSL) first,
    then 587 (STARTTLS) if the connection drops. False on any failure.
    With html_body, the email carries both versions; mail apps show the HTML
    one and fall back to plain text."""
    sink = _setting("DESK_EMAIL_SINK")
    if sink:   # no mail server: write each email to a folder (demo, recording, trying it out)
        return write_to_sink(sink, subject, body, html_body, to or _setting("ALERT_TO"), in_reply_to)
    sender, pw = _setting("DESK_EMAIL"), _setting("GMAIL_APP_PASSWORD")
    to = recipients(to or _setting("ALERT_TO"))
    pw = pw.replace(" ", "")  # Google shows app passwords in groups of four
    if not (sender and to and pw):
        log.warning("email not sent: DESK_EMAIL, ALERT_TO or GMAIL_APP_PASSWORD is not set")
        return False
    msg = EmailMessage()
    msg["From"] = formataddr(("AI Market Desk", sender))
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:   # a reply in the same thread (Ask the desk)
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    attempts = [(465, _smtp_ssl, smtp_factory or smtplib.SMTP_SSL),
                (587, _smtp_starttls, smtp_factory or smtplib.SMTP)]
    for port, connect, factory in attempts:
        stage = "connect"
        try:
            with connect(factory) as s:
                stage = "login"
                s.login(sender, pw)
                stage = "send"
                s.send_message(msg)
            log.info("email sent to %s on port %d: %s", to, port, subject)
            return True
        except smtplib.SMTPAuthenticationError as e:
            log.error("email failed: %s. Check the app password and look for a Google "
                      "security alert in the desk inbox.", _reason(e, stage, port))
            return False  # a wrong password will not work on the other port either
        except Exception as e:
            log.error("email failed: %s", _reason(e, stage, port))
    return False


def compose_scorecard(rows: list[list]) -> tuple[str, str, str]:
    """The Saturday scorecard email: the Scorecard tab as a small table."""
    import html as h

    head, body_rows = rows[0], [r for r in rows[1:] if r and len(r) >= 5]
    note = next((r[0] for r in rows[1:] if r and len(r) == 1), "")
    subject = f"AI Market Desk: weekly scorecard · {head[-1].replace('Updated ', '')}"
    lines = ["How the desk's calls did against the market over 5 trading days.", ""]
    lines += [f"- {r[0]}: {r[1]} names, {r[2]} vs market" + (f", right way {r[3]} ({r[4]})" if r[3] else "")
              for r in body_rows]
    lines += ["", note, "", FOOTER]
    cells = "".join(
        "<tr>" + "".join(f'<td style="padding:4px 10px;border-bottom:1px solid #eee">{h.escape(str(c))}</td>'
                         for c in r[:5]) + "</tr>" for r in body_rows)
    ths = "".join(f'<th style="text-align:left;padding:4px 10px">{h.escape(c)}</th>' for c in head[:5])
    html_body = (f'<div style="font-family:Arial,sans-serif;font-size:14px">'
                 f"<p>How the desk's calls did against the market over 5 trading days.</p>"
                 f"<table style=\"border-collapse:collapse\"><tr>{ths}</tr>{cells}</table>"
                 f'<p style="color:#666">{h.escape(note)}</p><hr><p style="color:#888;font-size:12px">'
                 f"{h.escape(FOOTER)}</p></div>")
    return subject, "\n".join(lines), html_body
