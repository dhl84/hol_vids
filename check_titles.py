#!/usr/bin/env python3
"""Check a built FCPXML: no two lower-third titles (location lane 1 + `subs`
lane 4) are ever on screen at once, and none is too short to read.

    python check_titles.py _edit/My_Trip.fcpxml
"""
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction

LOWER_THIRD_LANES = {1, 4}
MIN_S = 1.0


def _s(v):
    return float(Fraction(v.rstrip("s"))) if v else 0.0


def titles(path):
    out = []
    for ac in ET.parse(path).getroot().find(".//spine"):
        if ac.tag != "asset-clip":
            continue
        base = _s(ac.get("offset")) - _s(ac.get("start"))
        for t in ac.findall("title"):
            a = base + _s(t.get("offset"))
            out.append((a, a + _s(t.get("duration")), int(t.get("lane", 0)),
                        " ".join(t.itertext()).strip()))
    return sorted(out)


def main(path):
    rows = [r for r in titles(path) if r[2] in LOWER_THIRD_LANES]
    bad = [f"overlap: {x!r} ({a:.2f}-{b:.2f}) vs {x2!r} ({a2:.2f})"
           for (a, b, _, x), (a2, _, _, x2) in zip(rows, rows[1:]) if a2 < b]
    bad += [f"too short: {x!r} ({b - a:.2f}s)" for a, b, _, x in rows if b - a < MIN_S]
    print("\n".join(bad) or f"OK — {len(rows)} lower-third titles, none overlapping")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
