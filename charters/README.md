# Role prompts

One file per AI role. The code sends `00-shared.md` plus the role's file to Claude with each task, so **these files are the only copy of each role's instructions.** To change how a role behaves, edit its file in a pull request.

## File format

Each role file has a `## Prompt` section containing one fenced `text` block. The code sends exactly that block. Everything outside it (the one-liner, `## Notes`) is for humans only.

| File | Agent | Model | Status |
| --- | --- | --- | --- |
| `00-shared.md` | Rules every agent follows | (all) | live |
| `01-scanner.md` | Scanner: the rules engine. No prompt, no AI | none | live |
| `02-tape-reader.md` | Tape Reader: why is it moving, right now, with sources | Sonnet + web search | live |
| `04-chartist-lens.md` | Chartist lens (Newton-style framework): levels, trend, exhaustion | Sonnet, low effort | live |
| `05-strategist-lens.md` | Strategist lens (Lee-style framework): liquidity, sentiment, breadth | Haiku | live |
| `07-what-they-said.md` | Real, dated public comments by the strategists the lenses follow | Sonnet + web search, once a day | live |
| `08-vip-filter.md` | Keeps only material breaking news for VIP names | Haiku | live |
| `09-ask-desk.md` | Answers a reply to any alert, as the right specialist | Sonnet + web search | live |
| `03-desk-head.md` | Desk Head: routes requests, writes long-form briefs | Haiku + Sonnet | designed, not wired |
| `06-street-analyst.md` | Street Analyst: is the good news already in the price? | Sonnet + web search | designed, not wired |

## Writing a good prompt change

- Change one thing at a time, and say in the pull request which alerts or replies made you change it.
- Keep each role's job narrow. If a change makes one role do another role's work, move it to that role instead.
- Prefer a concrete rule ("never give a price target") over a tone instruction ("be careful").
