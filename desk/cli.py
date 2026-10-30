"""The `desk` command.

  desk demo                 replay a recorded session offline: no keys, no cost
  desk scan                 one live pass: rules, news read, lenses, email
  desk vip                  breaking news and fast moves for VIP names
  desk ask "NVDA: where is support?"   ask the desk from the terminal
  desk ask                  answer replies to desk emails (IMAP; ASK_DESK=on)
  desk audit                the daily health check
  desk scorecard            grade logged calls against the market
  desk rulelab              backtest the price rules against random days
  desk record <session>     record a live session for the demo (your keys)
  desk email-test           send a sample alert

Add --dry-run to preview without writing or sending, --force to run outside market hours.
Live commands read your keys from the environment or .env (see .env.example).
"""
from __future__ import annotations

import os
import sys

LIVE = {"scan", "vip", "ask", "audit", "scorecard", "rulelab", "email-test"}


def _need(*names: str) -> None:
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise SystemExit(
            f"`desk` needs {', '.join(missing)} for a live run. Put your own key(s) in .env "
            "(copy .env.example), or try `desk demo`, which needs none.")


def ask_terminal(text: str) -> str:
    """Answer one question from the terminal, with the same facts and checks as an
    email reply. A ticker in the question picks the name ("NVDA: ...")."""
    from datetime import datetime

    from desk import ai, inbox, mailer, scanner

    sheet = scanner.Sheet()
    state = sheet.load_state()
    today = datetime.now(scanner.ET).date()
    t = inbox.find_ticker("", text, "", [r["ticker"] for r in sheet.universe()])
    facts = scanner.ask_facts(t, state, today)
    answer, links = ai.answer_question(text, "", facts, take_budget=lambda: ai.take_budget(state, today))
    if not answer:
        answer = "The desk could not answer this one reliably from today's data."
    body = answer + ("\n\nSources: " + "  ".join(links) if links else "") + "\n\n--\n" + mailer.FOOTER
    print(f"\nQ: {text}\n\n{body}")
    if os.getenv("DESK_EMAIL_SINK"):
        q = text.split(":", 1)[1].strip() if t and text.upper().startswith(t.upper() + ":") else text
        short = q if len(q) <= 70 else q[:70].rsplit(" ", 1)[0] + "…"   # whole words only
        mailer.send(f"Re: {t or 'Question'}: {short}", body, to="(terminal)")   # not "NVDA: NVDA: ..."
    scanner.log_cost(state, today, False)
    sheet.save_state(state)
    return answer


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:   # your keys, from .env next to the repo (git-ignored)
        from pathlib import Path

        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "demo":
        from desk import replay
        return replay.demo(rest)
    if cmd == "record":
        from desk import replay
        return replay.record(rest)
    if cmd == "ask" and rest and not rest[0].startswith("-"):
        _need("ANTHROPIC_API_KEY")
        ask_terminal(" ".join(rest))
        return 0
    if cmd not in LIVE:
        print(f"unknown command: {cmd}\n{__doc__}")
        return 2
    if cmd in ("scan", "vip", "ask") and "--dry-run" not in rest:
        _need("ANTHROPIC_API_KEY")
    from desk import scanner
    sys.argv = ["desk", cmd, *rest]
    scanner.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
