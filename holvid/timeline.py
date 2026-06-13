"""Step 3 of the pipeline: build the master FCPXML timeline.

Consumes:
  _edit/clips.json   ordered clip manifest (from probe.py)
  _edit/review.json  visual review: per-clip location + dead ranges (schema below)

Produces a single chronological timeline (all clips, all days) with:
  * an opening movie title over the first clip
  * an optional day-divider title at each day's first clip (calendar-date
    change; [titles].day_dividers, on by default)
  * a location lower-third whenever the location label changes (its stamp
    carries the year by default)
  * an optional closing card (the trip's date range) over the final fade-out
    ([titles].closing_s, off by default)
  * every title fading gently in/out ([titles].fade_s)
  * obvious-junk dead spans removed; ambiguous ones left as REVIEW markers
  * subtle cross-dissolves at scene boundaries + fade in/out; day boundaries
    dip through black instead ([transitions].day_dip_s) — "time has passed"
  * an optional low background-music bed under the whole edit ([music].files)
  * a <chapter-marker> at each chapter start (label change: `chapter` >
    location > day), plus _edit/chapters.txt and _edit/youtube_description.txt
    with `M:SS Title` lines for YouTube chapter navigation

Times are snapped to the clips' frame grid (fps from the first clip). Validated
against Apple's FCPXML DTD with xmllint when Final Cut Pro is installed.

== The title-sequence logic (the reusable bit) ==
Titles are only ever emitted on the FIRST kept segment of a clip (`clip_first`),
in three stacked lanes, each appearing when its trigger fires:

  lane 3  OPENING   first clip only          -> cfg.title           (centre)
  lane 2  DAY       calendar date changes    -> formatted date      (centre)
  lane 1  LOCATION  location label changes    -> "Place\n<stamp>"   (lower third)

"Calendar date" is the clip's wall-clock day (`datetime[:10]`), so a shoot that
runs past midnight still counts as one day's divider. "Location label" is the
human/AI-supplied `location` from review.json; a new title fires only when it
differs from the previous one, so a run of clips at the same place gets one
lower-third. The location stamp shows the local date+time of that clip.

review.json schema:
{
  "clips": {
     "<clip filename>": {
        "location": "Eiffel Tower",        # label; a title appears when it changes
        "chapter": "Eiffel Tower at Night", # YouTube chapter label; a new chapter
                                            # opens when it changes (falls back to
                                            # location, then day; see chapters.py)
        "summary": "...",                   # free text (notes only; unused in XML)
        "dead": [[start_s, end_s, "reason"], ...],  # clip-local seconds (footage removed)
        "mute": [[start_s, end_s, "reason"], ...],  # clip-local seconds: keep the
                                                    # picture, silence the audio
                                                    # (sensitive speech / arguments)
        "speed": [[start_s, end_s, factor, "reason"], ...]  # clip-local seconds:
                                                    # keep the picture, play it
                                                    # `factor`x faster and muted
                                                    # (boring transit; see pace.py)
     }, ...
  },
  "title": "optional movie-title override"
}
"""
from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import pathname2url

from .config import BASIC_TITLE_UID, CROSS_DISSOLVE_UID, Config


# --- orientation ----------------------------------------------------------

def _upright_path(c: dict) -> Path:
    p = Path(c["path"])
    return p.with_name(f"{p.stem}_upright.MP4")


def _probe_rotation(path: Path) -> int:
    """Net display rotation in degrees (0/90/180/270), 0 if none/unreadable."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream_side_data=rotation:stream_tags=rotate",
         "-of", "json", str(path)],
        capture_output=True, text=True).stdout
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return 0
    st = (d.get("streams") or [{}])[0]
    rot = st.get("tags", {}).get("rotate")
    for sd in st.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = sd["rotation"]
    try:
        return abs(int(round(float(rot)))) % 360 if rot is not None else 0
    except (TypeError, ValueError):
        return 0


def _probe_frames(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 0


def bake_upright(cfg: Config, clips: list[dict]) -> list[str]:
    """Render a pillarboxed landscape copy for each rotated clip so FCP never has
    to rotate/conform. Returns the filenames that were baked. Idempotent: skips
    clips whose <stem>_upright.MP4 already exists."""
    rotated = _rotated_names(cfg, clips)
    done = []
    for c in clips:
        if c["name"] not in rotated:
            continue
        dst = _upright_path(c)
        if dst.exists():
            print(f"[upright] {dst.name} exists, skip")
            done.append(c["name"])
            continue
        w, h = c["width"], c["height"]
        # transpose to portrait content, then pad back into a landscape WxH frame.
        vf = (f"transpose=1,scale=-2:{h},"
              f"pad={w}:{h}:(ow-iw)/2:0:black,setsar=1")
        print(f"[upright] baking {dst.name} …")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", c["path"], "-vf", vf,
             "-c:v", "hevc_videotoolbox", "-q:v", "55", "-c:a", "copy", str(dst)],
            check=True)
        done.append(c["name"])
    if not done:
        print("[upright] no rotated clips to bake")
    return done


def _rotated_names(cfg: Config, clips: list[dict]) -> set[str]:
    names = set(cfg.rotated_clips)
    if cfg.auto_detect_rotation:
        for c in clips:
            if _probe_rotation(Path(c["path"])) in (90, 270):
                names.add(c["name"])
    return names


# --- dead-span / cut logic ------------------------------------------------

def _is_obvious_dead(reason: str, cfg: Config) -> bool:
    r = reason.lower()
    if any(k in r for k in cfg.cuts.keep_words):
        return False
    return any(k in r for k in cfg.cuts.cut_words)


def _kept_and_markers(dur: float, dead: list, cfg: Config):
    """Split a clip into kept media ranges (obvious-dead removed) and the
    ambiguous spans to leave as review markers. Returns (kept[(s0,s1)], marks).

    A span whose reason names a cut-word is an explicit cut and is removed at any
    length (camera glitches are deliberately brief). `min_dead_s` only filters
    *ambiguous* spans — the ones that would otherwise become review markers — so
    momentary wobble doesn't litter the timeline with markers."""
    obvious, marks = [], []
    for span in dead:
        s0, s1 = max(0.0, float(span[0])), min(dur, float(span[1]))
        reason = span[2] if len(span) > 2 else "boring"
        span_len = s1 - s0
        if span_len <= 0.05:                       # empty / nonsense
            continue
        if _is_obvious_dead(reason, cfg):
            obvious.append((s0, s1))               # explicit cut: any length
        elif span_len >= cfg.cuts.min_dead_s:
            marks.append((s0, s1, reason))         # ambiguous + long enough: mark
        # else: ambiguous + short -> ignore as noise
    obvious.sort()
    merged = []
    for a, b in obvious:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    kept, cur = [], 0.0
    for a, b in merged:
        if a > cur:
            kept.append((cur, a))
        cur = max(cur, b)
    if cur < dur:
        kept.append((cur, dur))
    return (kept or [(0.0, dur)]), marks


def _apply_speed(kept, speed_spans):
    """Subdivide kept (s0,s1) ranges at speed-span boundaries, tagging each
    resulting sub-range with its playback factor. A sub-range that falls inside a
    speed span gets that span's factor (>1 = faster); everything else is 1.0
    (normal). Returns an ordered list of (s0, s1, factor). `speed_spans` is a list
    of (s0, s1, factor) in the same clip-local seconds as `kept`."""
    out = []
    for a, b in kept:
        cuts = {a, b}
        for s0, s1, _f in speed_spans:
            if s1 > a and s0 < b:                      # overlaps this kept range
                cuts.add(max(a, s0))
                cuts.add(min(b, s1))
        pts = sorted(cuts)
        for x, y in zip(pts, pts[1:]):
            if y - x < 1e-6:
                continue
            mid = (x + y) / 2.0
            factor = 1.0
            for s0, s1, f in speed_spans:
                if s0 <= mid < s1:
                    factor = f
                    break
            out.append((x, y, factor))
    return out


# --- time helpers ---------------------------------------------------------

def _file_url(p: Path) -> str:
    # pathname2url already percent-encodes (spaces -> %20); do NOT quote() again
    # or "Paris 2026" becomes "Paris%252020..." and FCP reports Missing File.
    return "file://" + pathname2url(str(p.resolve()))


def parse_timecode(tc: str, fps_num: int, fps_den: int) -> tuple[int, bool]:
    """Embedded timecode -> (absolute frame number, is_drop_frame)."""
    drop = ";" in tc
    h, m, s, f = (int(x) for x in re.split("[:;]", tc.strip()))
    fps_round = round(fps_num / fps_den)
    if drop:
        dropn = fps_round // 15
        total_min = 60 * h + m
        fn = ((h * 3600 + m * 60 + s) * fps_round + f
              - dropn * (total_min - total_min // 10))
    else:
        fn = (h * 3600 + m * 60 + s) * fps_round + f
    return fn, drop


class _T:
    """Frame <-> rational-time helper for one fps."""
    def __init__(self, fps_num: int, fps_den: int):
        self.n, self.d = fps_num, fps_den

    def frame_dur(self) -> str:
        return f"{self.d}/{self.n}s"

    def frames(self, count: int) -> str:
        v = count * self.d
        return f"{v}/{self.n}s" if v else "0s"

    def secs(self, sec: float) -> int:
        return round(sec * self.n / self.d)


def _indent(elem, level=0):
    pad = "\n" + "    " * level
    if len(elem):
        if not (elem.text or "").strip():
            elem.text = pad + "    "
        for child in elem:
            _indent(child, level + 1)
            if not (child.tail or "").strip():
                child.tail = pad + "    "
        if not (elem[-1].tail or "").strip():
            elem[-1].tail = pad
    elif level and not (elem.tail or "").strip():
        elem.tail = pad


# --- titles ---------------------------------------------------------------

def _title(parent, T, cfg, ref, lane, offset_f, dur_f, ts_id, text,
           font_size, y_pos):
    """Anchored Basic Title; offset is in the parent clip's media-time frames."""
    t = ET.SubElement(parent, "title", ref=ref, lane=str(lane),
                      offset=T.frames(offset_f),
                      name=text.replace("\n", " ")[:40],
                      start="0s", duration=T.frames(dur_f))
    # DTD order inside <title>: param* , text* , text-style-def* , adjust-blend …
    if y_pos is not None:
        ET.SubElement(t, "param", name="Position",
                      key="9999/999166631/999166633/1/100/101",
                      value=f"0 {y_pos}")
    txt = ET.SubElement(t, "text")
    style = ET.SubElement(txt, "text-style", ref=ts_id)
    style.text = text
    tsd = ET.SubElement(t, "text-style-def", id=ts_id)
    ET.SubElement(tsd, "text-style", font=cfg.titles.font, fontSize=str(font_size),
                  fontFace="Bold", fontColor="1 1 1 1",
                  shadowColor="0 0 0 0.75", shadowOffset="3 315",
                  alignment="center")
    # gentle fade in/out rather than popping on/off (fade_s = 0 disables; each
    # fade is capped at a third of the title so short titles still read).
    fade_f = min(T.secs(cfg.titles.fade_s), dur_f // 3)
    if fade_f > 0:
        ab = ET.SubElement(t, "adjust-blend", amount="1")
        p = ET.SubElement(ab, "param", name="amount", value="1")
        ET.SubElement(p, "fadeIn", type="easeIn", duration=T.frames(fade_f))
        ET.SubElement(p, "fadeOut", type="easeOut", duration=T.frames(fade_f))
    return t


# --- chapters ---------------------------------------------------------------

def _yt_time(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _finalize_chapters(points: list, total_s: float, min_s: float) -> list:
    """Apply YouTube's chapter rules to raw (timeline_s, label) points: the first
    chapter must start at 0:00 and every chapter must run at least `min_s`
    seconds. A chapter cut short by the next label change loses its own label —
    the longer-running new label takes over its slot (keeping the earlier
    start). Consecutive duplicate labels collapse into one chapter."""
    if not points:
        return []
    pts = [(0.0, points[0][1])] + points[1:]
    out: list = []
    for t, label in pts:
        if out and label.lower() == out[-1][1].lower():
            continue                            # same chapter continues
        if out and t - out[-1][0] < min_s:
            out[-1] = (out[-1][0], label)       # previous slot too short: new label takes it
            if len(out) > 1 and out[-2][1].lower() == label.lower():
                out.pop()                       # …and collapses into the one before
        else:
            out.append((t, label))
    while len(out) > 1 and total_s - out[-1][0] < min_s:
        out.pop()                               # a final stub extends the previous chapter
    return out


def _write_chapter_files(cfg: Config, chapters: list, movie_title: str) -> None:
    """chapters.txt (bare `M:SS Title` lines) + youtube_description.txt (title,
    blank line, the chapter list) — paste the latter into the YouTube video
    description. YouTube needs >= 3 timestamps starting at 0:00 to show the
    chapter bar."""
    if not chapters:
        print("[chapters] no chapter labels (chapter/location empty everywhere) "
              "— chapter files not written")
        return
    block = "".join(f"{_yt_time(t)} {label}\n" for t, label in chapters)
    cfg.chapters_txt.write_text(block, encoding="utf-8")
    cfg.youtube_description.write_text(f"{movie_title}\n\n{block}",
                                       encoding="utf-8")
    note = ("" if len(chapters) >= 3 else
            "  (note: YouTube shows the chapter bar only with >=3 chapters)")
    print(f"[chapters] {len(chapters)} chapters -> {cfg.chapters_txt.name} + "
          f"{cfg.youtube_description.name} (paste into the YouTube description)"
          f"{note}")


# --- music bed --------------------------------------------------------------

def _probe_audio_s(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _music_files(cfg: Config) -> list[tuple[Path, float]]:
    """Resolve [music].files (relative to the project dir) -> [(path, seconds)].
    Missing or unreadable files are skipped with a warning, not fatal."""
    out = []
    for f in cfg.music.files:
        p = Path(f)
        if not p.is_absolute():
            p = cfg.project_dir / p
        dur = _probe_audio_s(p) if p.exists() else 0.0
        if dur <= 0:
            print(f"[music] skipping {f}: "
                  f"{'not found' if not p.exists() else 'unreadable/zero-length'}")
            continue
        out.append((p, dur))
    return out


# --- closing card -----------------------------------------------------------

def _closing_text(cfg: Config, dts: list[datetime]) -> str:
    """The trip's date range, with shared month/year elided from the first date:
    "28 May – 4 June 2026" / "3 – 9 August 2026" / a single date for one day."""
    if cfg.titles.closing_text:
        return cfg.titles.closing_text
    if not dts:
        return ""
    a, b = min(dts), max(dts)
    full = cfg.titles.closing_range_format
    if a.date() == b.date():
        return b.strftime(full)
    if a.year != b.year:
        return f"{a.strftime(full)} – {b.strftime(full)}"
    if a.month != b.month:
        return f"{a.strftime('%-d %B')} – {b.strftime(full)}"
    return f"{a.strftime('%-d')} – {b.strftime(full)}"


# --- builder --------------------------------------------------------------

def build(cfg: Config, clips: list[dict], review: dict, out_path: Path) -> Path:
    # Sequence rate/raster = the dominant source format by total duration; other
    # sources keep their native rate via their own <format> resource and FCP
    # conforms them on the timeline (e.g. a 29.97 GoPro in a 59.94 DJI edit).
    weights: dict[tuple, float] = {}
    for c in clips:
        key = (c["fps_num"], c["fps_den"], c["width"], c["height"])
        weights[key] = weights.get(key, 0.0) + c["duration"]
    fps_num, fps_den, width, height = max(weights, key=weights.__getitem__)
    T = _T(fps_num, fps_den)
    if len(weights) > 1:
        others = ", ".join(f"{n}/{d} {w}x{h} ({weights[(n, d, w, h)]:.0f}s)"
                           for n, d, w, h in weights)
        print(f"[fcpxml] mixed source formats — timeline {fps_num}/{fps_den} "
              f"{width}x{height}; sources: {others}")
    rclips = review.get("clips", {})
    movie_title = review.get("title") or cfg.title
    rotated = _rotated_names(cfg, clips)
    camera_time = set(cfg.timezone.camera_time_clips)
    seams = {tuple(p) for p in cfg.transitions.continuous_seams}

    def local_dt(c: dict):
        dt = datetime.fromisoformat(c["datetime"]) if c["datetime"] else None
        if dt is None:
            return None
        if c["name"] in camera_time:
            return dt
        return dt + timedelta(hours=cfg.timezone.offset_hours)

    fcpxml = ET.Element("fcpxml", version=cfg.fcpxml_version)
    resources = ET.SubElement(fcpxml, "resources")
    fmt_ids: dict[tuple, str] = {}
    for n, d, w, h in [(fps_num, fps_den, width, height)] + sorted(weights):
        if (n, d, w, h) in fmt_ids:
            continue
        fid = f"r{len(fmt_ids) + 1}"
        fmt_ids[(n, d, w, h)] = fid
        ET.SubElement(resources, "format", id=fid,
                      name=f"FFVideoFormat{h}p{n // 1000}",
                      frameDuration=f"{d}/{n}s",
                      width=str(w), height=str(h),
                      colorSpace="1-1-1 (Rec. 709)")
    fmt_id = fmt_ids[(fps_num, fps_den, width, height)]   # the sequence format
    ET.SubElement(resources, "effect", id="rTitle", name="Basic Title",
                  uid=BASIC_TITLE_UID)
    ET.SubElement(resources, "effect", id="rDis", name="Cross Dissolve",
                  uid=CROSS_DISSOLVE_UID)
    ET.SubElement(resources, "effect", id="rAud", name="Audio Crossfade",
                  uid="FFAudioTransition")

    # one asset per clip. All segment/media maths below run in SEQUENCE frame
    # units; each clip carries a `scale` (sequence frames per source frame —
    # e.g. 2.0 for a 29.97 source in a 59.94 timeline) and cut points snap to
    # the clip's own frame grid first, so media in/out always lands on a real
    # source frame.
    def clip_f(c: dict, sec: float) -> int:
        """Clip-local seconds -> sequence frames, on the clip's frame grid."""
        return round(round(sec * c["fps_num"] / c["fps_den"]) * c["scale"])

    for i, c in enumerate(clips, 1):
        c["ref"] = f"v{i}"
        c["daykey"] = c["datetime"][:10] if c["datetime"] else c["day"]
        c["rotated"] = c["name"] in rotated
        c["scale"] = (fps_num * c["fps_den"]) / (c["fps_num"] * fps_den)
        if abs(c["scale"] - round(c["scale"])) > 1e-9:
            print(f"[fcpxml] note: {c['name']} ({c['fps_num']}/{c['fps_den']}) "
                  f"is not an integer multiple of the timeline rate — its cuts "
                  f"are rounded to the nearest timeline frame")
        if c["rotated"]:
            src_path = _upright_path(c)
            if not src_path.exists():
                raise SystemExit(
                    f"[fcpxml] {c['name']} is rotated but {src_path.name} is "
                    f"missing — run `holvid <project> upright` first.")
            c["frames"] = _probe_frames(src_path) or c["frames"]
            c["duration"] = c["frames"] * c["fps_den"] / c["fps_num"]
            c["media_start_f"], c["tcfmt"] = 0, "NDF"
        else:
            sf, drop = (parse_timecode(c["timecode"], c["fps_num"], c["fps_den"])
                        if c["timecode"] else (0, False))
            c["media_start_f"] = round(sf * c["scale"])
            c["tcfmt"] = "DF" if drop else "NDF"
            src_path = Path(c["path"])
        c["frames_sq"] = round(c["frames"] * c["scale"])
        asset = ET.SubElement(resources, "asset", id=c["ref"],
                              name=Path(c["name"]).stem,
                              start=T.frames(c["media_start_f"]),
                              duration=T.frames(c["frames_sq"]),
                              hasVideo="1", hasAudio="1",
                              format=fmt_ids[(c["fps_num"], c["fps_den"],
                                              c["width"], c["height"])],
                              videoSources="1", audioSources="1",
                              audioChannels="2", audioRate="48000")
        ET.SubElement(asset, "media-rep", kind="original-media",
                      src=_file_url(src_path))

    # background-music assets (audio-only, connected under the first spine clip)
    music = []                       # [(ref, seconds)]
    for mi, (mpath, mdur) in enumerate(_music_files(cfg), 1):
        ref = f"m{mi}"
        masset = ET.SubElement(resources, "asset", id=ref, name=mpath.stem,
                               start="0s", duration=T.frames(T.secs(mdur)),
                               hasAudio="1", audioSources="1",
                               audioChannels="2", audioRate="48000")
        ET.SubElement(masset, "media-rep", kind="original-media",
                      src=_file_url(mpath))
        music.append((ref, mdur))

    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", name=cfg.event_name)
    project = ET.SubElement(event, "project", name=f"{cfg.event_name} — master edit")
    sequence = ET.SubElement(project, "sequence", format=fmt_id,
                             tcStart="0s", tcFormat="NDF",
                             audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(sequence, "spine")

    ts_counter = [0]

    def next_ts():
        ts_counter[0] += 1
        return f"ts{ts_counter[0]}"

    stats = {"cuts": 0, "cut_s": 0.0, "markers": 0, "segments": 0,
             "transitions": 0, "day_dips": 0, "mutes": 0, "mute_s": 0.0,
             "speedups": 0, "speed_src_s": 0.0, "speed_saved_s": 0.0,
             "chapters": 0, "music_s": 0.0}

    # ---- pass 1: flatten clips into kept segments (obvious dead removed) ----
    segs = []
    for c in clips:
        msf = c["media_start_f"]
        rc = rclips.get(c["name"], {})
        kept, marks = _kept_and_markers(c["duration"], rc.get("dead", []), cfg)
        # sensitive-speech spans to silence (clip-local seconds, from `sanitize`).
        # We keep the picture and mute the audio over each span, so they survive
        # into kept segments rather than removing footage.
        mute_spans = [(max(0.0, float(m[0])), min(c["duration"], float(m[1])))
                      for m in rc.get("mute", []) if float(m[1]) > float(m[0])]
        # speed-up spans (boring transit/eating/driving, from `pace`): keep the
        # picture but play it faster and muted. Each entry is
        # [s0, s1, factor?, reason?]; a missing factor defaults to cfg.pace.factor.
        speed_spans = []
        for sp in rc.get("speed", []):
            s0, s1 = max(0.0, float(sp[0])), min(c["duration"], float(sp[1]))
            if s1 <= s0:
                continue
            factor = float(sp[2]) if len(sp) > 2 and sp[2] else cfg.pace.factor
            if factor > 1.0:
                speed_spans.append((s0, s1, factor))
        cut_total = c["duration"] - sum(b - a for a, b in kept)
        if cut_total > 0.05:
            stats["cuts"] += 1
            stats["cut_s"] += cut_total
        # clamp media in/out to the asset's real frame range: ffprobe's float
        # duration can round 1-2 frames past the frame count, which FCP rejects.
        fr = c["frames_sq"]
        subsegs = _apply_speed(kept, speed_spans)
        for sidx, (s0, s1, factor) in enumerate(subsegs):
            in_f = msf + min(clip_f(c, s0), fr)
            out_f = msf + min(clip_f(c, s1), fr)
            # clip each mute span to this sub-range (spans inside removed footage
            # vanish automatically); store as clip-local seconds. Speed segments
            # are muted wholesale, so they don't carry per-span mutes.
            seg_mutes = []
            if factor == 1.0:
                for m0, m1 in mute_spans:
                    a, b = max(s0, m0), min(s1, m1)
                    if b - a > 0.02:
                        seg_mutes.append((a, b))
            segs.append({
                "ref": c["ref"], "name": Path(c["name"]).stem, "cname": c["name"],
                "in_f": in_f, "out_f": out_f, "msf": msf, "mend": msf + fr,
                "tcfmt": c["tcfmt"], "rotated": c["rotated"], "factor": factor,
                "daykey": c["daykey"], "ldt": local_dt(c),
                "loc": (rc.get("location") or "").strip(),
                "geo_city": ((rc.get("geo") or {}).get("city") or "").strip(),
                "chapter": (rc.get("chapter") or "").strip(),
                "clip_first": sidx == 0,
                "marks": [(a, b, r) for (a, b, r) in marks if s0 <= a < s1],
                "mutes": seg_mutes,
            })

    # ---- pass 2: decide dissolves + handle trims ----
    D = T.secs(cfg.transitions.dissolve_s)         # dissolve length (frames)
    H = T.secs(cfg.transitions.dissolve_s / 2) + 3  # handle trimmed per dissolving edge
    # A sped-up (retimed) segment never lends or takes a dissolve handle — the
    # handle math assumes 1:1 source/timeline frames, so boring sections hard-cut
    # in and out of normal speed.
    elig = [s["factor"] == 1.0 and (s["out_f"] - s["in_f"]) >= 2 * D + 2 * H
            for s in segs]
    trans = [False] * len(segs)                    # trans[i] = dissolve between i and i+1
    if cfg.transitions.enabled:
        for i in range(len(segs) - 1):
            if (segs[i]["cname"], segs[i + 1]["cname"]) in seams:
                continue                            # one continuous recording -> hard join
            if elig[i] and elig[i + 1]:
                trans[i] = True
    # Day boundaries dip through black instead of dissolving — the classic
    # "time has passed" cue: fade the old day out, hard cut, fade the new day in.
    for s in segs:
        s["fade_in_f"] = s["fade_out_f"] = 0
    if cfg.transitions.day_dip_s > 0:
        dip_f = max(2, T.secs(cfg.transitions.day_dip_s / 2))
        for i in range(len(segs) - 1):
            a, b = segs[i], segs[i + 1]
            if a["daykey"] == b["daykey"] or (a["cname"], b["cname"]) in seams:
                continue
            trans[i] = False
            a["fade_out_f"] = dip_f
            b["fade_in_f"] = dip_f
            stats["day_dips"] += 1
    for i, s in enumerate(segs):
        s["lh"] = i > 0 and trans[i - 1]
        s["rh"] = i < len(segs) - 1 and trans[i]
        s["vin"] = s["in_f"] + (H if s["lh"] else 0)
        s["vout"] = s["out_f"] - (H if s["rh"] else 0)

    # ---- pass 3: emit spine ----
    def _out_dur_f(s):
        src = max(1, min(s["vout"] - s["vin"], s["mend"] - s["vin"]))
        return src if s["factor"] == 1.0 else max(1, round(src / s["factor"]))

    total_out_f = sum(_out_dur_f(s) for s in segs)   # movie length (output frames)
    # per-day city for the day divider ([geo].day_includes_city): the first
    # GPS-fixed city seen that day (early clips often lack a fix, so we don't
    # restrict to the day's very first clip).
    day_city: dict = {}
    if cfg.geo.day_includes_city:
        for s in segs:
            if s["geo_city"] and s["daykey"] not in day_city:
                day_city[s["daykey"]] = s["geo_city"]
    # closing card needs clear air after the opening (both sit on lane 3)
    closing_text = (_closing_text(cfg, [s["ldt"] for s in segs if s["ldt"]])
                    if cfg.titles.closing_s > 0 and total_out_f * T.d / T.n
                    > cfg.titles.opening_s + cfg.titles.closing_s + 2 else "")
    cursor = 0
    prev_day = prev_loc = prev_chap = None
    chapter_pts = []          # (timeline seconds, label) at each chapter start
    for i, s in enumerate(segs):
        is_first = (i == 0)
        is_last = (i == len(segs) - 1)
        factor = s["factor"]
        vin = s["vin"]
        # source frames this segment consumes (after any dissolve handles), then
        # the timeline (output) frames it occupies. At 1x they're equal; a speed
        # span of factor f compresses src/f frames of timeline.
        src_dur = max(1, s["vout"] - vin)
        src_dur = min(src_dur, s["mend"] - vin)     # never exceed available media
        out_dur = src_dur if factor == 1.0 else max(1, round(src_dur / factor))

        if s["lh"]:                                 # dissolve straddling this cut
            tr = ET.SubElement(spine, "transition", name="Cross Dissolve",
                               offset=T.frames(cursor - D // 2),
                               duration=T.frames(D))
            fv = ET.SubElement(tr, "filter-video", ref="rDis", name="Cross Dissolve")
            ET.SubElement(fv, "param", name="Look", key="1", value="11 (Video)")
            ET.SubElement(fv, "param", name="Amount", key="2", value="100")
            ET.SubElement(fv, "param", name="Ease", key="50", value="2 (In & Out)")
            ET.SubElement(tr, "filter-audio", ref="rAud", name="Audio Crossfade")
            stats["transitions"] += 1

        clip = ET.SubElement(spine, "asset-clip", ref=s["ref"],
                             offset=T.frames(cursor), name=s["name"],
                             duration=T.frames(out_dur), start=T.frames(vin),
                             tcFormat=s["tcfmt"], audioRole="dialogue")
        stats["segments"] += 1

        # ---- SPEED RAMP (retime, first in DTD order: timing-params) ----
        # `time` is the output/timeline axis (clip-start-relative), `value` the
        # source axis (also clip-start-relative, 0 = this clip's `start`). Mapping
        # out_dur of timeline onto src_dur of source = constant factor playback.
        if factor != 1.0:
            tm = ET.SubElement(clip, "timeMap")
            ET.SubElement(tm, "timept", time="0s", value="0s", interp="linear")
            ET.SubElement(tm, "timept", time=T.frames(out_dur),
                          value=T.frames(src_dur), interp="linear")
            stats["speedups"] += 1
            stats["speed_src_s"] += src_dur * T.d / T.n
            stats["speed_saved_s"] += (src_dur - out_dur) * T.d / T.n

        # picture fades: movie start/end + day-boundary dips (a segment can carry
        # both, e.g. a single-clip day that dips in and out).
        fin, fout = s["fade_in_f"], s["fade_out_f"]
        if is_first and cfg.transitions.start_fade_s > 0:
            fin = max(fin, T.secs(cfg.transitions.start_fade_s))
        if is_last and cfg.transitions.end_fade_s > 0:
            fout = max(fout, T.secs(cfg.transitions.end_fade_s))
        fin, fout = min(fin, out_dur // 2), min(fout, out_dur // 2)
        if fin or fout:
            ab = ET.SubElement(clip, "adjust-blend", amount="1")
            p = ET.SubElement(ab, "param", name="amount", value="1")
            if fin:
                ET.SubElement(p, "fadeIn", type="easeIn", duration=T.frames(fin))
            if fout:
                ET.SubElement(p, "fadeOut", type="easeOut", duration=T.frames(fout))

        # Speed sections are muted wholesale (sped audio is unusable). adjust-volume
        # is intrinsic-params-audio, so it follows adjust-blend (video) in DTD order
        # and is always honored on import (unlike srcEnable).
        if factor != 1.0:
            ET.SubElement(clip, "adjust-volume", amount="-96dB")

        # ---- TITLE SEQUENCE LOGIC (only on a clip's first kept segment) ----
        if s["clip_first"]:
            tt = cfg.titles
            ldt = s["ldt"]
            new_day = s["daykey"] != prev_day
            if is_first:                            # opening movie title, lane 3
                _title(clip, T, cfg, "rTitle", 3, s["vin"], T.secs(tt.opening_s),
                       next_ts(), movie_title, tt.opening_font_size, None)
            if new_day and ldt is not None:         # new calendar day
                if tt.day_dividers:                 # centered day-divider card, lane 2
                    day_off = s["vin"] + (T.secs(tt.opening_s) if is_first else 0)
                    date_txt = ldt.strftime(tt.date_format)
                    city = day_city.get(s["daykey"])
                    if city:                        # "Paris · Saturday 28 March 2026"
                        date_txt = f"{city} · {date_txt}"
                    _title(clip, T, cfg, "rTitle", 2, day_off, T.secs(tt.day_title_s),
                           next_ts(), date_txt, tt.day_font_size, None)
                prev_day = s["daykey"]
                prev_loc = None                     # re-announce location each new day
            if s["loc"] and s["loc"] != prev_loc:   # location lower-third, lane 1
                stamp = ldt.strftime(tt.location_stamp_format) if ldt else ""
                text = f"{s['loc']}\n{stamp}" if stamp else s["loc"]
                _title(clip, T, cfg, "rTitle", 1, s["vin"], T.secs(tt.location_title_s),
                       next_ts(), text, tt.location_font_size, tt.location_y)
                prev_loc = s["loc"]

            # ---- MUSIC BED (connected to the first clip, lane -1) ----
            # The files tile the timeline in order at a low constant volume; the
            # bed fades in at the start and out at the movie's end (or its own).
            # Connected-clip offsets are in the host's media time, so timeline
            # frame F sits at vin + F (the first clip's spine offset is 0).
            if is_first and music:
                if factor != 1.0:
                    print("[music] note: the first segment is retimed — music "
                          "timing may drift; consider not speeding up clip 1")
                pos = 0                              # timeline frames so far
                for mj, (mref, mdur) in enumerate(music):
                    if pos >= total_out_f:
                        break
                    mdur_f = min(T.secs(mdur), total_out_f - pos)
                    mclip = ET.SubElement(
                        clip, "asset-clip", ref=mref, lane="-1",
                        offset=T.frames(vin + pos), name=f"music {mj + 1}",
                        duration=T.frames(mdur_f), start="0s",
                        srcEnable="audio", audioRole="music")
                    av = ET.SubElement(mclip, "adjust-volume",
                                       amount=f"{cfg.music.volume_db:g}dB")
                    pa = ET.SubElement(av, "param", name="amount")
                    last = mj == len(music) - 1 or pos + mdur_f >= total_out_f
                    if mj == 0 and cfg.music.fade_in_s > 0:
                        ET.SubElement(pa, "fadeIn", type="easeIn",
                                      duration=T.frames(min(T.secs(cfg.music.fade_in_s),
                                                            mdur_f // 2)))
                    if last and cfg.music.fade_out_s > 0:
                        ET.SubElement(pa, "fadeOut", type="easeOut",
                                      duration=T.frames(min(T.secs(cfg.music.fade_out_s),
                                                            mdur_f // 2)))
                    pos += mdur_f
                    stats["music_s"] += mdur_f * T.d / T.n

        # ---- CLOSING CARD (lane 3, ending with the final fade-out) ----
        # An anchor item, so it must precede this clip's chapter-marker/markers.
        if is_last and closing_text:
            cdur_f = min(T.secs(cfg.titles.closing_s), out_dur)
            _title(clip, T, cfg, "rTitle", 3, vin + max(0, out_dur - cdur_f),
                   cdur_f, next_ts(), closing_text,
                   cfg.titles.closing_font_size, None)

        if s["clip_first"]:
            # ---- CHAPTERS (FCP <chapter-marker> + the YouTube timestamps) ----
            # Label preference: explicit `chapter` (from the chapters step or by
            # hand) > location > a day divider. A chapter opens when the label
            # changes; the marker's `start` is source media time (like markers),
            # while the YouTube timestamp is the output-timeline cursor.
            # (`ldt`/`new_day` carry over from the title block above — same
            # iteration, and `new_day` predates the prev_day update.)
            chap = s["chapter"] or s["loc"]
            if not chap and new_day and ldt is not None:
                chap = ldt.strftime(cfg.chapters.day_format)
            if chap and chap.lower() != (prev_chap or "").lower():
                ET.SubElement(clip, "chapter-marker",
                              start=T.frames(vin), value=chap[:80])
                chapter_pts.append((cursor * T.d / T.n, chap))
                prev_chap = chap

        for ms0, ms1, reason in s["marks"]:
            ET.SubElement(clip, "marker", start=T.frames(s["msf"] + T.secs(ms0)),
                          duration=T.frames(max(1, T.secs(ms1) - T.secs(ms0))),
                          value=f"REVIEW: {reason}"[:80])
            stats["markers"] += 1

        # ---- SENSITIVE-AUDIO MUTING (no split; picture is untouched) ----
        # DTD content model puts audio-channel-source after anchor items/markers.
        # `mute` start/duration are in source media time, the same coordinate as
        # this asset-clip's `start`, so a clip-local second m -> msf + secs(m).
        if s["mutes"]:
            acs = ET.SubElement(clip, "audio-channel-source", srcCh="1, 2")
            for m0, m1 in s["mutes"]:
                ET.SubElement(acs, "mute",
                              start=T.frames(s["msf"] + T.secs(m0)),
                              duration=T.frames(max(1, T.secs(m1) - T.secs(m0))))
                stats["mutes"] += 1
                stats["mute_s"] += m1 - m0

        cursor += out_dur

    sequence.set("duration", T.frames(cursor))

    chapters = _finalize_chapters(chapter_pts, cursor * T.d / T.n,
                                  cfg.chapters.min_chapter_s)
    stats["chapters"] = len(chapters)
    _write_chapter_files(cfg, chapters, movie_title)
    build.stats = stats  # type: ignore[attr-defined]

    _indent(fcpxml)
    body = ET.tostring(fcpxml, encoding="unicode")
    out_path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE fcpxml>\n' + body + "\n", encoding="utf-8")
    return out_path


def validate(path: Path, version: str = "1.9") -> tuple[bool, str]:
    import shutil
    import tempfile
    tag = version.replace(".", "_")
    dtd = Path("/Applications/Final Cut Pro.app/Contents/Frameworks/"
               f"Interchange.framework/Versions/A/Resources/FCPXMLv{tag}.dtd")
    if not dtd.exists():
        return True, "(DTD not found; skipped)"
    with tempfile.NamedTemporaryFile(suffix=".dtd", delete=False) as tmp:
        shutil.copyfile(dtd, tmp.name)
        local = tmp.name
    try:
        r = subprocess.run(["xmllint", "--noout", "--dtdvalid", local, str(path)],
                           capture_output=True, text=True)
    finally:
        Path(local).unlink(missing_ok=True)
    return r.returncode == 0, (r.stderr.strip() or "valid")
