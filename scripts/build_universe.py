"""Rebuild desk/config/universe.csv and the sector map in benchmarks.json from
the current S&P 500 list (GICS sectors). Run it when the index changes:

    python scripts/build_universe.py                 # downloads the public list
    python scripts/build_universe.py constituents.csv

Source: github.com/datasets/s-and-p-500-companies (Symbol, Security, GICS Sector,
GICS Sub-Industry). Extra VIP names that are not in the index are kept at the end.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "desk" / "config"
SOURCE = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

# One ETF per GICS sector; a few sub-industries get a closer peer group.
SECTOR_ETF = {
    "Information Technology": "XLK", "Financials": "XLF", "Health Care": "VHT",
    "Industrials": "XLI", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Utilities": "VPU", "Real Estate": "XLRE", "Materials": "XLB",
    "Communication Services": "XLC", "Energy": "XLE",
}
SUB_ETF = {
    "Semiconductors": "SMH", "Semiconductor Materials & Equipment": "SMH",
    "Application Software": "XSW", "Systems Software": "XSW", "Biotechnology": "XBI",
}
VIP = {"TSLA"}
# Second share class of the same company: same news, same hits, so one is enough.
SKIP = {"GOOG", "FOX", "NWS"}
# Not in the index, followed for news only (VIP): ticker, name, theme, sector ETF
EXTRA = [("SPCX", "SpaceX", "Space / satellites", "XLI")]


SUFFIXES = (", Inc.", " Inc.", " Incorporated", " Corporation", " Corp.", " Company", " Co.",
            " plc", " PLC", " Ltd.", " N.V.", " Holdings", " Group")


def short_name(name: str) -> str:
    """'Tesla, Inc.' -> 'Tesla': the name as headlines write it, for news matching."""
    name = re.sub(r"\s*\(Class [A-Z]\)$", "", name)   # GOOG / GOOGL are both "Alphabet"
    for _ in range(2):   # "Xylem Holdings Inc." takes two passes
        for s in SUFFIXES:
            if name.endswith(s) and len(name) > len(s) + 2:
                name = name[: -len(s)].rstrip(" ,")
    return name


def rows(src: str | None) -> list[dict]:
    if src:
        text = Path(src).read_text()
    else:
        import requests
        text = requests.get(SOURCE, timeout=30).text
    return list(csv.DictReader(io.StringIO(text)))


def main(argv: list[str]) -> int:
    data = rows(argv[0] if argv else None)
    if len(data) < 490:
        raise SystemExit(f"only {len(data)} rows in the source list; not writing")
    header = ["Ticker", "Name", "Status", "Priority", "Target entry level", "Trim level", "Theme"]
    out, sector = [], {}
    for r in sorted(data, key=lambda r: r["Symbol"]):
        t = r["Symbol"].strip().upper().replace(".", "-")   # BRK.B -> BRK-B, as Yahoo spells it
        if t in SKIP:
            continue
        sub = r["GICS Sub-Industry"].strip()
        out.append([t, short_name(r["Security"].strip()), "Holding", "VIP" if t in VIP else "Normal", "", "", sub])
        sector[t] = SUB_ETF.get(sub) or SECTOR_ETF[r["GICS Sector"].strip()]
    for t, name, theme, etf in EXTRA:
        if t not in sector:
            out.append([t, name, "Watch", "VIP", "", "", theme])
            sector[t] = etf
    with (CONFIG / "universe.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(out)
    bm_file = CONFIG / "benchmarks.json"
    bm = json.loads(bm_file.read_text())
    bm["_note"] = ("Relative-strength benchmarks for the Chartist lens. 'market' is per listing "
                   "(US, or CA for TSX names). 'sector' maps a ticker to its sector ETF; a ticker "
                   "not listed is compared with its market only. Written by scripts/build_universe.py "
                   "from GICS sectors; edit freely.")
    bm["sector"] = sector
    bm_file.write_text(json.dumps(bm, indent=2) + "\n")
    print(f"universe.csv: {len(out)} names; benchmarks.json: {len(set(sector.values()))} sector ETFs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
