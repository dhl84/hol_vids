"""Optional pipeline step: derive place names from GoPro GPS.

    exiftool (GPMF GPS) ─▶ per-clip coordinate ─▶ reverse geocode ─▶ review.json `geo`

GoPro Hero 5+ embeds a GPS track in a `gpmd` data stream. This step pulls a
representative coordinate per clip (median of the valid fixes), reverse-geocodes
it, and writes a `geo` field per clip into review.json — and, when
`[geo].fill_empty_location` is on, fills any *empty* `location` label (it never
overwrites a hand-written one). The build can additionally prefix each day
divider with the day's city (`[geo].day_includes_city`).

Coverage is whatever the camera actually recorded: a clip with no satellite fix
(indoors, or before GPS acquired) gets nothing and keeps its visual label. So
GPS is a helper that fills/verifies coarse location, not a replacement for the
contact-sheet read.

Geocoding modes:
  * offline (default, fully local): `reverse_geocoder` → nearest city + country.
    Tells trip legs apart (Seoul vs Paris); cannot name landmarks within a city.
  * online (`[geo].online = true`, opt-in): OpenStreetMap Nominatim → a
    landmark/street name. Richer captions, but it SENDS YOUR COORDINATES to a
    third-party server — hence opt-in, rate-limited, and announced when it runs.

Needs `exiftool` on PATH; the offline step needs `reverse_geocoder` (pip). Both
are imported/checked lazily so the rest of the tool stays dependency-light.

All coordinates are decimal degrees (WGS-84), the GPMF/Nominatim convention.
"""
from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import time
import urllib.parse
import urllib.request

from .config import Config

# A 0,0 reading ("null island") is GoPro's no-fix sentinel; treat anything that
# close to the origin as no fix.
_NULL_EPS = 1e-4

# Minimal ISO-3166 alpha-2 → country name for the offline geocoder's `cc`
# (reverse_geocoder returns the code only). Falls back to the bare code.
_COUNTRY = {
    "KR": "South Korea", "FR": "France", "GB": "United Kingdom", "US": "USA",
    "JP": "Japan", "DE": "Germany", "IT": "Italy", "ES": "Spain",
    "NL": "Netherlands", "BE": "Belgium", "CH": "Switzerland", "AT": "Austria",
    "PT": "Portugal", "IE": "Ireland", "CA": "Canada", "AU": "Australia",
    "CN": "China", "TW": "Taiwan", "HK": "Hong Kong", "SG": "Singapore",
    "TH": "Thailand", "VN": "Vietnam", "ID": "Indonesia", "MY": "Malaysia",
    "IN": "India", "MX": "Mexico", "BR": "Brazil", "NZ": "New Zealand",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
    "PL": "Poland", "CZ": "Czechia", "GR": "Greece", "TR": "Turkey",
}


# --- GPS extraction (exiftool / GPMF) --------------------------------------

def _exiftool() -> str:
    exe = shutil.which("exiftool")
    if not exe:
        raise SystemExit(
            "[geo] exiftool not found — install it (e.g. `brew install exiftool`) "
            "to read GoPro GPS, or disable the geo step.")
    return exe


def extract_coord(path: str) -> dict | None:
    """Median (lat, lon) of a clip's valid GPS fixes, or None if it has no fix.
    Returns {"lat", "lon", "fixes"} (fixes = number of valid samples used)."""
    r = subprocess.run(
        [_exiftool(), "-ee", "-api", "LargeFileSupport=1", "-n",
         "-p", "${GPSLatitude;1} ${GPSLongitude;1}", path],
        capture_output=True, text=True)
    lats, lons = [], []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if abs(lat) < _NULL_EPS and abs(lon) < _NULL_EPS:
            continue                                   # no-fix sentinel
        lats.append(lat)
        lons.append(lon)
    if not lats:
        return None
    return {"lat": round(statistics.median(lats), 6),
            "lon": round(statistics.median(lons), 6), "fixes": len(lats)}


def load_coords(cfg: Config, clips: list[dict], force: bool = False) -> dict:
    """{clip_name: {"lat","lon","fixes"} | None}, cached in _edit/gps.json."""
    cache = (json.loads(cfg.gps_json.read_text())
             if cfg.gps_json.exists() and not force else {})
    changed = False
    for c in clips:
        if c["name"] in cache:
            continue
        coord = extract_coord(c["path"])
        cache[c["name"]] = coord
        changed = True
        print(f"[geo] {c['name']}: "
              + (f"{coord['lat']:.5f}, {coord['lon']:.5f} ({coord['fixes']} fixes)"
                 if coord else "no GPS fix"))
    if changed:
        cfg.edit_dir.mkdir(parents=True, exist_ok=True)
        cfg.gps_json.write_text(json.dumps(cache, indent=2))
    return cache


# --- reverse geocoding ------------------------------------------------------

def _offline_geocode(coords: list[tuple[float, float]]) -> list[dict]:
    """Nearest city/country per coordinate, fully offline (reverse_geocoder)."""
    try:
        import reverse_geocoder
    except ImportError:
        raise SystemExit(
            "[geo] reverse_geocoder not installed — `uv pip install reverse_geocoder` "
            "for offline geocoding, or set [geo].online with no offline step.")
    # mode=1 (single-threaded): a few coords per project don't need the
    # multiprocessing path, which also misbehaves when re-importing __main__.
    results = reverse_geocoder.search(coords, mode=1, verbose=False)
    out = []
    for r in results:
        cc = (r.get("cc") or "").upper()
        out.append({"city": r.get("name", ""),
                    "country": _COUNTRY.get(cc, cc)})
    return out


def _online_label(cfg: Config, lat: float, lon: float) -> dict:
    """Landmark/street name from OpenStreetMap Nominatim (sends the coordinate
    off-machine). Returns {"place","city","country"}; empties on any failure."""
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": lat, "lon": lon,
                                "zoom": cfg.geo.online_zoom, "addressdetails": 1})
    req = urllib.request.Request(f"{cfg.geo.nominatim_url}?{q}",
                                 headers={"User-Agent": cfg.geo.user_agent})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except Exception as e:                              # network / rate-limit / JSON
        print(f"  [warn] Nominatim lookup failed for {lat},{lon}: {e}")
        return {"place": "", "city": "", "country": ""}
    a = d.get("address", {})
    # prefer the most specific human-meaningful feature down to the street
    place = (d.get("name")
             or a.get("tourism") or a.get("attraction") or a.get("leisure")
             or a.get("historic") or a.get("building") or a.get("amenity")
             or a.get("road") or a.get("pedestrian") or "")
    city = (a.get("city") or a.get("town") or a.get("village")
            or a.get("municipality") or a.get("suburb") or "")
    return {"place": place[:48], "city": city, "country": a.get("country", "")}


def detect(cfg: Config, clips: list[dict], force: bool = False) -> dict:
    """Extract per-clip GPS, reverse-geocode, write `geo` into review.json
    (filling empty location labels when configured). Returns the geo map."""
    coords = load_coords(cfg, clips, force=force)
    fixed = [(c["name"], coords[c["name"]]) for c in clips if coords.get(c["name"])]
    if not fixed:
        print("[geo] no clip has a GPS fix — nothing to geocode "
              "(indoor footage or GPS was off)")
        return {}

    # offline city/country for every fixed clip (one batched call)
    offline = _offline_geocode([(co["lat"], co["lon"]) for _n, co in fixed])
    geo: dict[str, dict] = {}
    for (name, co), off in zip(fixed, offline):
        geo[name] = {"lat": co["lat"], "lon": co["lon"], "fixes": co["fixes"],
                     "city": off["city"], "country": off["country"]}

    # opt-in online enrichment (landmark/street). Rate-limited; announced.
    if cfg.geo.online:
        print(f"[geo] online enrichment ON — sending {len(fixed)} coordinate(s) "
              f"to {cfg.geo.nominatim_url} (OpenStreetMap)")
        for i, (name, co) in enumerate(fixed):
            if i:
                time.sleep(cfg.geo.request_pause_s)
            lab = _online_label(cfg, co["lat"], co["lon"])
            if lab["place"]:
                geo[name]["place"] = lab["place"]
            if lab["city"]:                            # Nominatim city is finer
                geo[name]["city"] = lab["city"]
            if lab["country"]:
                geo[name]["country"] = lab["country"]

    _merge_into_review(cfg, geo)
    _print_report(cfg, clips, geo)
    return geo


def _label_for(g: dict) -> str:
    """The caption a clip's geo implies: landmark if we have one, else city."""
    return g.get("place") or g.get("city") or ""


def _merge_into_review(cfg: Config, geo: dict) -> None:
    review = (json.loads(cfg.review_json.read_text())
              if cfg.review_json.exists() else {"clips": {}})
    clips = review.setdefault("clips", {})
    filled = 0
    for name, g in geo.items():
        entry = clips.setdefault(name, {"location": "", "summary": "", "dead": []})
        entry["geo"] = g
        if cfg.geo.fill_empty_location and not (entry.get("location") or "").strip():
            label = _label_for(g)
            if label:
                entry["location"] = label
                filled += 1
    cfg.edit_dir.mkdir(parents=True, exist_ok=True)
    cfg.review_json.write_text(json.dumps(review, ensure_ascii=False, indent=2))
    if filled:
        print(f"[geo] filled {filled} empty location label(s) from GPS")


def _print_report(cfg: Config, clips: list[dict], geo: dict) -> None:
    """Show coordinate → place vs the existing hand label, so you can verify."""
    review = (json.loads(cfg.review_json.read_text())
              if cfg.review_json.exists() else {"clips": {}})
    rclips = review.get("clips", {})
    n_fix = len(geo)
    print(f"[geo] {n_fix}/{len(clips)} clips have a fix:")
    for c in clips:
        g = geo.get(c["name"])
        if not g:
            continue
        loc = (rclips.get(c["name"], {}).get("location") or "").strip()
        where = ", ".join(x for x in (g.get("place"), g.get("city"),
                                      g.get("country")) if x)
        print(f"    {c['name']}: {g['lat']:.4f},{g['lon']:.4f}  ->  {where}"
              + (f'   [label: "{loc}"]' if loc else ""))
    print("[geo] verify these against your labels; run `build` to apply "
          "(day-divider city needs [geo].day_includes_city).")
