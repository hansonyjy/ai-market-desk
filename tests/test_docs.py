"""Every image the README shows must parse, or GitHub shows a broken picture
(Oct 29: a bare & in "S&P 500" broke both SVGs). Run: python tests/test_docs.py"""
import pathlib
import re
import xml.dom.minidom

ROOT = pathlib.Path(__file__).resolve().parents[1]
readme = (ROOT / "README.md").read_text()
local = [p for p in re.findall(r'src="([^"]+)"', readme) if not p.startswith(("http", "../"))]
assert local, "the README should show at least one local image"
for p in local:
    path = ROOT / p
    assert path.exists(), f"README image missing: {p}"
    if path.suffix == ".svg":
        xml.dom.minidom.parse(str(path))   # raises on invalid XML, such as a bare &
for svg in (ROOT / "docs").rglob("*.svg"):
    xml.dom.minidom.parse(str(svg))
print("docs images ok:", len(local), "in the README")
