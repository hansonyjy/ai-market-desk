"""Draw docs/img/rulelab.svg from a Rule Lab table (the recorded demo's by default).

    python scripts/rulelab_chart.py [demo/fixtures/rulelab/rule_lab.csv]

Each dot is one rule-and-condition group. x is its edge in the fit years, y in the
sealed (held-out) years, both in the rule's own direction (an add rule should beat
random days, a trim rule should lag them), in points over 20 days. A rule that
works sits up and to the right in both periods.
"""
import csv
import re
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "demo/fixtures/rulelab/rule_lab.csv"
rows = list(csv.reader(src.open()))
head = rows[0]
col = {h: i for i, h in enumerate(head)}
body = [r for r in rows[1:] if len(r) == len(head) and r[col["Verdict"]] in ("Pass", "Fail", "Not enough data")]


def num(x):
    try:
        return float(x.replace("%", "").replace("+", ""))
    except ValueError:
        return None


groups = len(body)
verdicts = [r[col["Verdict"]] for r in body]
passed = [r for r in body if r[col["Verdict"]] == "Pass"]
placebo = next((" ".join(r) for r in rows if r and r[0].startswith("Placebo")), "")
m = re.search(r"passed ([\d.]+) times on average \((\d+) runs", placebo)
placebo_avg, placebo_runs = (m.group(1), m.group(2)) if m else ("?", "?")
placebo_avg = placebo_avg[:-2] if placebo_avg.endswith(".0") else placebo_avg

pts = []
for r in body:
    s = 1 if r[col["Side"]] == "add" else -1
    fx, sx = num(r[col["Fit edge 20d"]]), num(r[col["Sealed edge 20d"]])
    if fx is not None and sx is not None and r[col["Verdict"]] in ("Pass", "Fail"):
        pts.append((s * fx, s * sx, r[col["Verdict"]] == "Pass"))

# plot geometry
X0, X1, Y0, Y1 = 560, 1150, 122, 432         # plot box (px)
XL, YL = 8, 10                                # axis limits (pts); beyond is pinned to the edge
sx = lambda v: X0 + (max(-XL, min(XL, v)) + XL) / (2 * XL) * (X1 - X0)
sy = lambda v: Y1 - (max(-YL, min(YL, v)) + YL) / (2 * YL) * (Y1 - Y0)
pinned = sum(1 for x, y, _ in pts if abs(x) > XL or abs(y) > YL)

INK, INK2, MUTED, GRID, SURF = "#e6edf7", "#c7d2e3", "#8b9bb4", "#2a3b5c", "#13203a"
CONTEXT, HILITE = "#6b7c99", "#0fa292"   # validated: HILITE passes all checks on the dark surface

o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="500" viewBox="0 0 1200 500" '
     f'font-family="Inter, -apple-system, Segoe UI, Helvetica, Arial, sans-serif" role="img" '
     f'aria-label="Rule Lab: {groups} rule groups backtested on the S&amp;P 500; {len(passed)} passed; '
     f'{placebo_avg} passed on random dates">',
     '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b1220"/>'
     '<stop offset="1" stop-color="#16213b"/></linearGradient></defs>',
     '<rect width="1200" height="500" rx="18" fill="url(#bg)"/>',
     f'<text x="40" y="52" fill="{INK}" font-size="22" font-weight="700">Rule Lab: every price rule, backtested on the S&amp;P 500</text>',
     f'<text x="40" y="78" fill="{MUTED}" font-size="14">Five years, against condition-matched random days. Latest years sealed until the end.</text>']

# the headline numbers (ink, not series colour)
def stat(y, big, label, dot=None):
    o.append(f'<text x="40" y="{y}" fill="{INK}" font-size="44" font-weight="800" letter-spacing="-1">{escape(big)}</text>')
    lx = 40 + 30 * len(big) + 14
    if dot:
        o.append(f'<circle cx="{lx + 6}" cy="{y - 15}" r="6" fill="{dot}" stroke="{SURF}" stroke-width="2"/>')
        lx += 20
    o.append(f'<text x="{lx}" y="{y - 9}" fill="{INK2}" font-size="16">{escape(label)}</text>')

stat(150, str(groups), "rule-and-condition groups tested")
stat(214, str(len(passed)), "passed, in the fit years and the sealed years", HILITE)
stat(278, placebo_avg, f"passed on random dates (placebo, {placebo_runs} runs)")
lines = ["Both survivors are one idea: a volume spike on a",
         "down day while the stock is overbought (RSI over 70)",
         "lagged random days by about 3 points over 20 days.",
         "Price rules alone time almost nothing, so the desk",
         "leans on the news read, and the Scorecard checks it."]
for i, t in enumerate(lines):
    o.append(f'<text x="40" y="{326 + i * 22}" fill="{MUTED}" font-size="14.5">{escape(t)}</text>')

# axes: recessive grid, zero lines dashed
for v in (-8, -4, 4, 8):
    o.append(f'<line x1="{sx(v):.1f}" y1="{Y0}" x2="{sx(v):.1f}" y2="{Y1}" stroke="{GRID}" stroke-width="1" opacity=".5"/>')
    o.append(f'<text x="{sx(v):.1f}" y="{Y1 + 18}" fill="{MUTED}" font-size="11" text-anchor="middle">{v:+d}</text>')
for v in (-10, -5, 5, 10):
    o.append(f'<line x1="{X0}" y1="{sy(v):.1f}" x2="{X1}" y2="{sy(v):.1f}" stroke="{GRID}" stroke-width="1" opacity=".5"/>')
    o.append(f'<text x="{X0 - 8}" y="{sy(v) + 4:.1f}" fill="{MUTED}" font-size="11" text-anchor="end">{v:+d}</text>')
o.append(f'<line x1="{sx(0):.1f}" y1="{Y0}" x2="{sx(0):.1f}" y2="{Y1}" stroke="{MUTED}" stroke-width="1" stroke-dasharray="4 4"/>')
o.append(f'<line x1="{X0}" y1="{sy(0):.1f}" x2="{X1}" y2="{sy(0):.1f}" stroke="{MUTED}" stroke-width="1" stroke-dasharray="4 4"/>')
o.append(f'<text x="{sx(0):.1f}" y="{Y1 + 18}" fill="{MUTED}" font-size="11" text-anchor="middle">0</text>')
o.append(f'<text x="{(X0 + X1) / 2}" y="{Y1 + 40}" fill="{INK2}" font-size="13" text-anchor="middle">'
         f'Edge in the fit years (points over 20 days, in the rule\'s direction)</text>')
o.append(f'<text x="{X0 - 40}" y="{(Y0 + Y1) / 2}" fill="{INK2}" font-size="13" text-anchor="middle" '
         f'transform="rotate(-90 {X0 - 40} {(Y0 + Y1) / 2})">Edge in the sealed years</text>')
o.append(f'<text x="{X1 - 6}" y="{Y0 + 16}" fill="{MUTED}" font-size="12" text-anchor="end">works in both periods</text>')

# dots: failed groups as context, the passes on top with a surface ring
for x, y, ok in pts:
    if not ok:
        o.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3.2" fill="{CONTEXT}" opacity=".55"/>')
pp = [(x, y) for x, y, ok in pts if ok]
for x, y in pp:
    o.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="7" fill="{HILITE}" stroke="{SURF}" stroke-width="2"/>')
if pp:   # one direct label for the pair
    lx, ly = max(sx(x) for x, _ in pp) + 14, min(sy(y) for _, y in pp) - 14
    o.append(f'<text x="{lx:.1f}" y="{ly:.1f}" fill="{INK}" font-size="12.5" font-weight="600">volume spike on a down day,</text>')
    o.append(f'<text x="{lx:.1f}" y="{ly + 16:.1f}" fill="{INK}" font-size="12.5" font-weight="600">RSI over 70 (2 variants)</text>')

# legend (two series), one row above the plot
LX = X0
o.append(f'<circle cx="{LX + 5}" cy="{Y0 - 14}" r="4" fill="{CONTEXT}"/>'
         f'<text x="{LX + 15}" y="{Y0 - 10}" fill="{INK2}" font-size="12">failed ({sum(1 for *_, ok in pts if not ok)})</text>')
o.append(f'<circle cx="{LX + 115}" cy="{Y0 - 14}" r="5" fill="{HILITE}" stroke="{SURF}" stroke-width="2"/>'
         f'<text x="{LX + 126}" y="{Y0 - 10}" fill="{INK2}" font-size="12">passed ({len(pp)})</text>')
note = f"{groups - len(pts)} groups without enough data not shown"
note += f"; {pinned} beyond the axes pinned to the edge" if pinned else ""
o.append(f'<text x="40" y="474" fill="{MUTED}" font-size="11.5">{escape(note.capitalize())}.</text>')
o.append("</svg>")
out = ROOT / "docs/img/rulelab.svg"
out.write_text("\n".join(o) + "\n")
print(f"{out}: {len(pts)} dots, {len(pp)} passed, {pinned} pinned")
