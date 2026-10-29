"""Ask the Desk: read replies to desk emails and answer them in the same thread.

Off unless ASK_DESK=on. Only allowlisted senders are answered. The email body is
treated as a question, never as instructions; the charter says the same."""
from __future__ import annotations

import email
import html
import imaplib
import logging
import os
import re
from datetime import date, timedelta
from email import policy
from email.utils import parseaddr

log = logging.getLogger("ai-market-desk")

IMAP_HOST = "imap.gmail.com"
QUOTE_START = re.compile(r"\n?On [^\n]{0,200}?(?:\n[^\n]{0,200}?)?wrote:\s*\n", re.S)
# Outlook (corporate mail) does not write "On ... wrote:". It starts the quoted email with a
# header block, sometimes after a rule line or "-----Original Message-----":
#   From: desk@example.com <...>
#   Sent: Thursday, October 23, 2026 7:16 AM
OUTLOOK_START = re.compile(
    r"\n?(?:_{10,}\s*\n|-{3,}\s*Original Message\s*-{3,}\s*\n)?\s*From:[^\n]*\n\s*(?:Sent|Date):", re.I)
MAX_QUESTION = 1000
MAX_CONTEXT = 4000


def _setting(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def enabled() -> bool:
    return _setting("ASK_DESK").lower() == "on"


def allowlist() -> set[str]:
    """Addresses that may ask: ALERT_TO plus ASK_ALLOW. Accepts commas, semicolons
    or spaces between addresses, with or without display names (Oct 17: a valid
    sender was refused, so parsing is now forgiving and the count is logged)."""
    raw = ",".join([_setting("ALERT_TO"), _setting("ASK_ALLOW")])
    return {m.lower() for m in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", raw)}


def per_day() -> int:
    try:
        return int(_setting("ASK_PER_DAY", "20"))
    except ValueError:
        return 20


def body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"<br\s*/?>|</p>|</div>|<hr[^>]*>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text).replace("\xa0", " ")   # &nbsp; and &lt; in Outlook mail
    return text.replace("\r\n", "\n")


def split_reply(text: str) -> tuple[str, str]:
    """(the new question, the quoted email it replies to). Handles Gmail's
    'On ... wrote:' line, which may wrap, Outlook's 'From: / Sent:' header block,
    and '>' quoting. Whichever comes first starts the quoted part."""
    m = min((x for x in (QUOTE_START.search(text), OUTLOOK_START.search(text)) if x),
            key=lambda x: x.start(), default=None)
    if m:
        new, quoted = text[:m.start()], text[m.end():]
    else:
        new = "\n".join(l for l in text.splitlines() if not l.startswith(">"))
        quoted = "\n".join(l for l in text.splitlines() if l.startswith(">"))
    quoted = "\n".join(re.sub(r"^>\s?", "", l) for l in quoted.splitlines())
    new = " ".join(new.split())
    return new[:MAX_QUESTION], quoted.strip()[:MAX_CONTEXT]


def is_auto(msg) -> bool:
    return (msg.get("Auto-Submitted", "no").lower() != "no"
            or msg.get("X-Autoreply") is not None
            or msg.get("Precedence", "").lower() in ("bulk", "auto_reply", "junk")
            or re.match(r"(automatic reply|out of office|auto:)", msg.get("Subject", ""), re.I) is not None)


def find_ticker(subject: str, question: str, quoted: str, tickers: list[str]) -> str:
    """The name the question is about: named in the question first, then the
    email subject, then the quoted email. '' if none or more than one."""
    for text in (question, subject, quoted[:600]):
        hits = [t for t in tickers if re.search(rf"(?<![A-Za-z0-9.]){re.escape(t)}(?![A-Za-z0-9])", text)]
        hits = sorted(set(hits), key=len, reverse=True)
        hits = [h for h in hits if not any(h != o and h in o for o in hits)]   # SHOP vs CSU.TO
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return ""
    return ""


def fetch(imap_factory=None, days: int = 2, today: date | None = None) -> list[dict]:
    """Messages in the desk inbox from the last `days` days, read without marking
    them seen. Read or unread does not matter: opening the desk inbox in a
    browser marks mail read (Oct 17: the first test question was skipped that
    way). The caller skips anything already answered, by Message-ID.
    Each: {uid, msg_id, refs, sender, subject, question, quoted, auto}."""
    user, pw = _setting("DESK_EMAIL"), _setting("GMAIL_APP_PASSWORD").replace(" ", "")
    if not (user and pw):
        log.warning("ask: DESK_EMAIL or GMAIL_APP_PASSWORD not set")
        return []
    out = []
    with (imap_factory or imaplib.IMAP4_SSL)(IMAP_HOST) as m:
        m.login(user, pw)
        m.select("INBOX")
        since = ((today or date.today()) - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = m.uid("search", None, "SINCE", since)
        for uid in (data[0].split() if typ == "OK" and data and data[0] else []):
            typ, parts = m.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), b"") if typ == "OK" else b""
            if not raw:
                continue
            msg = email.message_from_bytes(raw, policy=policy.default)
            question, quoted = split_reply(body_text(msg))
            out.append({"uid": uid, "msg_id": msg.get("Message-ID", ""),
                        "refs": " ".join(x for x in (msg.get("References", ""), msg.get("Message-ID", "")) if x),
                        "sender": parseaddr(msg.get("From", ""))[1].lower(),
                        "subject": str(msg.get("Subject", "")), "question": question,
                        "quoted": quoted, "auto": is_auto(msg)})
    return out


def mark_seen(uids: list, imap_factory=None) -> None:
    if not uids:
        return
    user, pw = _setting("DESK_EMAIL"), _setting("GMAIL_APP_PASSWORD").replace(" ", "")
    with (imap_factory or imaplib.IMAP4_SSL)(IMAP_HOST) as m:
        m.login(user, pw)
        m.select("INBOX")
        for uid in uids:
            m.uid("store", uid, "+FLAGS", "(\\Seen)")
