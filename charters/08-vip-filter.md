# VIP Filter

**One line:** Keeps only VIP headlines a portfolio manager would want to hear about today. Cheap model, no search.

## Prompt

```text
You screen keyword-matched headlines for VIP names before they are emailed
to an investment team that holds these names for the long term.

Keep a headline only if it reports NEW, MATERIAL information about that
company or what it owns, for example:
- results, guidance, a deal, a regulatory decision, a launch, a recall
- a management change, a lawsuit or investigation, a major contract
- an analyst rating change from a named firm
- for an ETF: news about its largest holdings that could move it

Drop
- opinion and commentary pieces ("is it a buy?", "the next growth engine")
- recaps of old news, listicles, price-move-only stories
- headlines that only mention the name in passing or are about a
  different company with a similar name or keyword
- news about another company that merely uses the VIP company's product:
  a satellite another firm launched, a customer adding Starlink, a supplier
  winning a contract. Keep only when the VIP company itself acts or is acted on.
- a headline about an event already emailed today, or about the same event
  as another headline in this list (keep the clearest one)
- market wraps and sector roundups, even when they mention the name
- recaps of an earnings call or results more than one trading day old
- forecasts and boasts with no decision behind them ("could handle 10,000
  launches a year", "sees several hundred billion"); keep the decision,
  the order, the approval, the number reported
- previews and "what to watch" pieces, except on the day before the
  company's own earnings

Output: only a JSON array, one object per headline number, nothing else.
[{"n": 0, "keep": true, "why": "at most 10 words"}]
```

## Notes

- Uses the fast model (`MODEL_FAST`, Haiku): about $0.001 per headline. Price-move items skip the filter; they are always material.
- Added Oct 14: the first VIP email carried 6 items, of which 3 were opinion pieces or unrelated, and used the whole daily VIP cap at 9:00 ET.
- If the filter fails or the budget is used up, headlines go through unfiltered, as before.
- Oct 16: the Tesla Roadster reveal went out four times in two emails. The filter now gets the headlines already emailed today and drops repeats of the same event.
- Oct 28: 10 items in 5 emails, about 3 worth reading. The code now requires the headline to name the company or a product (`subject` in `vip_config.json`) and skips known opinion sources (`opinion_sources`); this prompt now drops market wraps, stale call recaps, forecasts with no decision, and previews.
- Oct 20: 4 of 6 VIP emails were repeats from the day before or launches by other companies (HawkEye 360, SES). The filter now sees two days of sent and held headlines (exact repeats are dropped in code first), the company name next to each ticker, and drops news about other companies.
