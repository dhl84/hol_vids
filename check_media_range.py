#!/usr/bin/env python3
"""Check a built FCPXML: every spine clip reads media that actually exists —
inside its asset's own `start` .. `start + duration` window.

FCP rejects an out-of-range edit with "Invalid edit with no respective media"
(the clip imports as a blank gap), and the DTD does not catch it. The trap is
the retime `timeMap`: it REPLACES the clip's local timeline, so `start` and
`start + duration` must fall inside the map's `time` (adjusted) axis, while the
`value` axis must stay inside the asset's own media range. A 0s-based map on a
timecode-based asset satisfies neither.

    python check_media_range.py _edit/My_Trip.fcpxml
"""
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction


def _s(v):
    return float(Fraction(v.rstrip("s"))) if v else 0.0


def bad_edits(path):
    root = ET.parse(path).getroot()
    assets = {a.get("id"): (_s(a.get("start")),
                            _s(a.get("start")) + _s(a.get("duration")))
              for a in root.iter("asset")}
    out = []
    for i, ac in enumerate(root.find(".//spine").findall("asset-clip"), 1):
        lo, hi = assets[ac.get("ref")]
        tm = ac.find("timeMap")
        c0 = _s(ac.get("start"))
        c1 = c0 + _s(ac.get("duration"))
        if tm is None:
            s0, s1 = c0, c1
        else:
            # start/duration index into the map's adjusted ('time') axis...
            times = [_s(p.get("time")) for p in tm.findall("timept")]
            t0, t1 = min(times), max(times)
            if c0 < t0 - 1e-6 or c1 > t1 + 1e-6:
                out.append((i, ac.get("name"), c0, c1, t0, t1,
                            "start/duration outside the timeMap 'time' axis"))
                continue
            # ...and the 'value' axis is what reads the media.
            vals = [_s(p.get("value")) for p in tm.findall("timept")]
            s0, s1 = min(vals), max(vals)
        if s0 < lo - 1e-6 or s1 > hi + 1e-6:
            out.append((i, ac.get("name"), s0, s1, lo, hi, "outside media"))
    return out


def demo():
    """A clip-relative timeMap on a timecode-based asset must be caught."""
    import tempfile, os
    xml = """<fcpxml version="1.9"><resources>
      <asset id="v1" start="1854814962/60000s" duration="62798736/60000s"/>
      </resources><library><event><project><sequence><spine>
      <asset-clip ref="v1" name="ok" start="1856494640/60000s"
                  duration="1919918/60000s"/>
      <asset-clip ref="v1" name="retimed" start="1858414558/60000s"
                  duration="899899/60000s">
        <timeMap><timept time="0s" value="0s"/>
                 <timept time="899899/60000s" value="5400395/60000s"/></timeMap>
      </asset-clip></spine></sequence></project></event></library></fcpxml>"""
    fd, p = tempfile.mkstemp(suffix=".fcpxml")
    os.write(fd, xml.encode()); os.close(fd)
    try:
        found = bad_edits(p)
        assert len(found) == 1 and found[0][1] == "retimed", found
        assert "timeMap" in found[0][-1], found
    finally:
        os.unlink(p)
    print("demo OK")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        demo()
        raise SystemExit(0)
    bad = bad_edits(sys.argv[1])
    for i, name, s0, s1, lo, hi, why in bad:
        print(f"clip {i} ({name}): reads {s0:.3f}..{s1:.3f}s, "
              f"asset holds {lo:.3f}..{hi:.3f}s — {why}")
    print(f"{len(bad)} bad edit(s)")
    raise SystemExit(1 if bad else 0)
