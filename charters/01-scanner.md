# Scanner

**One line:** The rules script, `scanner.py`. It finds names worth a look and never interprets them. No AI, no cost.

There is no prompt for this role. It is listed here so every part of the desk is described in one place.

## What it checks

| Horizon | Rule | Fires when (default thresholds, in `scanner.py`) |
| --- | --- | --- |
| SHORT | `rvol_spike` | Relative volume, adjusted for time of day, is above 2.5x the 20-day average. |
| SHORT | `gap_up` / `gap_down` | Opens more than 3% away from yesterday's close |
| SHORT | `breakout_20d` / `breakdown_20d` | Closes above the 20-day high or below the 20-day low |
| SHORT | `gap_filled` / `gap_failed` | A gap of 3% or more reverses: a gap down climbs back above yesterday's close, or a gap up falls below it |
| SHORT | `fast_move` | Moves 3% or more (`MOVE_PCT`) within 30 minutes (`MOVE_WINDOW_MIN`), from 1-minute prices. Can fire again after 120 minutes. VIP names have their own check |
| SHORT | `day_move` | Up or down on the day by at least 3% and 1.5 times its normal daily range (`DAY_MOVE_ATR`, 14-day average true range), whichever is larger. Only for a name nothing else mentioned today; once a day |
| SHORT | `earnings_soon` | Earnings within 5 trading days (US names, from Finnhub) |
| LONG | `pullback_200dma` | Within 2% of the 200-day average after closing at least 5% above it in the last 20 days |
| LONG | `target_entry_hit` / `trim_level_hit` | Price reaches the Sheet's target entry or trim level |
| VIP | news and fast move | New Finnhub headline matching `vip_config.json` keywords, or a move over 3% within 15 minutes |

No SHORT rule fires in the first 30 minutes after the open, and no flag shows an RVOL figure then: a few minutes of volume projected to a full day is meaningless (WDC showed 24x at 9:35 on Oct 7).

The open: SHORT hits found before 10:15 ET (`DIGEST_UNTIL_MIN`) go in one **opening digest** email, grouped by the Universe tab's Theme, outside the daily caps. Added Oct 13: the 10:00 batch had filled the day's caps and the desk went quiet by 10:30.

Every hit is logged in the Signal Log. Only some are emailed (`mailer.py`): sourced, not NOISE, not low confidence, and for SHORT signals at least `EMAIL_MIN_RVOL` (1.5x) normal volume. On Oct 9, IMO.TO and FANG breakouts on 0.4x and 0.5x volume were emailed; they no longer are.

Each rule has a cooldown (SHORT 1 day, LONG 5 days, VIP move 60 minutes) so one move does not alert twice.

## Notes

- Change a threshold in a pull request, with the Scorecard numbers that justify it.
- Every rule has a test in `test_scanner.py`. A new rule needs a new test.
