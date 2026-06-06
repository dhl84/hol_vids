"""Step 3 of the pipeline: build the master FCPXML timeline.

Consumes:
  _edit/clips.json   ordered clip manifest (from probe.py)
  _edit/review.json  visual review: per-clip location + dead ranges (schema below)

Produces a single chronological timeline (all clips, all days) with:
  * an opening movie title over the first clip
  * a day-divider title at each day's first clip (calendar-date change)
  * a location lower-third whenever the location label changes
  * obvious-junk dead spans removed; ambiguous ones left as REVIEW markers
  * subtle cross-dissolves at scene/day boundaries + fade in/out

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
        "summary": "...",                   # free text (notes only; unused in XML)
        "dead": [[start_s, end_s, "reason"], ...],  # clip-local seconds (footage removed)
        "mute": [[start_s, end_s, "reason"], ...]   # clip-local seconds: keep the
                                                    # picture, silence the audio
                                                    # (sensitive speech; see sanitize.py)
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
    ambiguous spans to leave as review markers. Returns (kept[(s0,s1)], marks)."""
    obvious, marks = [], []
    for span in dead:
        s0, s1 = max(0.0, float(span[0])), min(dur, float(span[1]))
        reason = span[2] if len(span) > 2 else "boring"
        if s1 - s0 < cfg.cuts.min_dead_s:
            continue
        if _is_obvious_dead(reason, cfg):
            obvious.append((s0, s1))
        else:
            marks.append((s0, s1, reason))
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
    # DTD order inside <title>: param* , text* , text-style-def* , ...
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
    return t


# --- builder --------------------------------------------------------------

def build(cfg: Config, clips: list[dict], review: dict, out_path: Path) -> Path:
    fps_num, fps_den = clips[0]["fps_num"], clips[0]["fps_den"]
    T = _T(fps_num, fps_den)
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
    fmt_id = "r1"
    ET.SubElement(resources, "format", id=fmt_id,
                  name=f"FFVideoFormat{clips[0]['height']}p{fps_num // 1000}",
                  frameDuration=T.frame_dur(),
                  width=str(clips[0]["width"]), height=str(clips[0]["height"]),
                  colorSpace="1-1-1 (Rec. 709)")
    ET.SubElement(resources, "effect", id="rTitle", name="Basic Title",
                  uid=BASIC_TITLE_UID)
    ET.SubElement(resources, "effect", id="rDis", name="Cross Dissolve",
                  uid=CROSS_DISSOLVE_UID)
    ET.SubElement(resources, "effect", id="rAud", name="Audio Crossfade",
                  uid="FFAudioTransition")

    # one asset per clip
    for i, c in enumerate(clips, 1):
        c["ref"] = f"v{i}"
        c["daykey"] = c["datetime"][:10] if c["datetime"] else c["day"]
        c["rotated"] = c["name"] in rotated
        if c["rotated"]:
            src_path = _upright_path(c)
            if not src_path.exists():
                raise SystemExit(
                    f"[fcpxml] {c['name']} is rotated but {src_path.name} is "
                    f"missing — run `holvid <project> upright` first.")
            c["frames"] = _probe_frames(src_path) or c["frames"]
            c["duration"] = c["frames"] * fps_den / fps_num
            c["media_start_f"], c["tcfmt"] = 0, "NDF"
        else:
            sf, drop = (parse_timecode(c["timecode"], fps_num, fps_den)
                        if c["timecode"] else (0, False))
            c["media_start_f"], c["tcfmt"] = sf, ("DF" if drop else "NDF")
            src_path = Path(c["path"])
        asset = ET.SubElement(resources, "asset", id=c["ref"],
                              name=Path(c["name"]).stem,
                              start=T.frames(c["media_start_f"]),
                              duration=T.frames(c["frames"]),
                              hasVideo="1", hasAudio="1", format=fmt_id,
                              videoSources="1", audioSources="1",
                              audioChannels="2", audioRate="48000")
        ET.SubElement(asset, "media-rep", kind="original-media",
                      src=_file_url(src_path))

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
             "transitions": 0, "mutes": 0, "mute_s": 0.0}

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
        cut_total = c["duration"] - sum(b - a for a, b in kept)
        if cut_total > 0.05:
            stats["cuts"] += 1
            stats["cut_s"] += cut_total
        # clamp media in/out to the asset's real frame range: ffprobe's float
        # duration can round 1-2 frames past c["frames"], which FCP rejects.
        fr = c["frames"]
        for si, (s0, s1) in enumerate(kept):
            in_f = msf + min(T.secs(s0), fr)
            out_f = msf + min(T.secs(s1), fr)
            # clip each mute span to this kept range (spans inside removed footage
            # vanish automatically); store as clip-local seconds.
            seg_mutes = []
            for m0, m1 in mute_spans:
                a, b = max(s0, m0), min(s1, m1)
                if b - a > 0.02:
                    seg_mutes.append((a, b))
            segs.append({
                "ref": c["ref"], "name": Path(c["name"]).stem, "cname": c["name"],
                "in_f": in_f, "out_f": out_f, "msf": msf, "mend": msf + fr,
                "tcfmt": c["tcfmt"], "rotated": c["rotated"],
                "daykey": c["daykey"], "ldt": local_dt(c),
                "loc": (rc.get("location") or "").strip(), "clip_first": si == 0,
                "marks": [(a, b, r) for (a, b, r) in marks if s0 <= a < s1],
                "mutes": seg_mutes,
            })

    # ---- pass 2: decide dissolves + handle trims ----
    D = T.secs(cfg.transitions.dissolve_s)         # dissolve length (frames)
    H = T.secs(cfg.transitions.dissolve_s / 2) + 3  # handle trimmed per dissolving edge
    elig = [(s["out_f"] - s["in_f"]) >= 2 * D + 2 * H for s in segs]
    trans = [False] * len(segs)                    # trans[i] = dissolve between i and i+1
    if cfg.transitions.enabled:
        for i in range(len(segs) - 1):
            if (segs[i]["cname"], segs[i + 1]["cname"]) in seams:
                continue                            # one continuous recording -> hard join
            if elig[i] and elig[i + 1]:
                trans[i] = True
    for i, s in enumerate(segs):
        s["lh"] = i > 0 and trans[i - 1]
        s["rh"] = i < len(segs) - 1 and trans[i]
        s["vin"] = s["in_f"] + (H if s["lh"] else 0)
        s["vout"] = s["out_f"] - (H if s["rh"] else 0)

    # ---- pass 3: emit spine ----
    cursor = 0
    prev_day = prev_loc = None
    for i, s in enumerate(segs):
        is_first = (i == 0)
        is_last = (i == len(segs) - 1)
        vdur = max(1, s["vout"] - s["vin"])
        vdur = min(vdur, s["mend"] - s["vin"])     # never exceed available media

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
                             duration=T.frames(vdur), start=T.frames(s["vin"]),
                             tcFormat=s["tcfmt"], audioRole="dialogue")
        stats["segments"] += 1

        fade = None
        if is_first and cfg.transitions.start_fade_s > 0:
            fade = ("fadeIn", "easeIn", T.secs(cfg.transitions.start_fade_s))
        if is_last and cfg.transitions.end_fade_s > 0:
            fade = ("fadeOut", "easeOut", T.secs(cfg.transitions.end_fade_s))
        if fade:
            ab = ET.SubElement(clip, "adjust-blend", amount="1")
            p = ET.SubElement(ab, "param", name="amount", value="1")
            ET.SubElement(p, fade[0], type=fade[1], duration=T.frames(fade[2]))

        # ---- TITLE SEQUENCE LOGIC (only on a clip's first kept segment) ----
        if s["clip_first"]:
            tt = cfg.titles
            ldt = s["ldt"]
            if is_first:                            # opening movie title, lane 3
                _title(clip, T, cfg, "rTitle", 3, s["vin"], T.secs(tt.opening_s),
                       next_ts(), movie_title, tt.opening_font_size, None)
            if s["daykey"] != prev_day and ldt is not None:   # day divider, lane 2
                day_off = s["vin"] + (T.secs(tt.opening_s) if is_first else 0)
                _title(clip, T, cfg, "rTitle", 2, day_off, T.secs(tt.day_title_s),
                       next_ts(), ldt.strftime(tt.date_format),
                       tt.day_font_size, None)
                prev_day = s["daykey"]
                prev_loc = None                     # re-announce location each new day
            if s["loc"] and s["loc"] != prev_loc:   # location lower-third, lane 1
                stamp = ldt.strftime(tt.location_stamp_format) if ldt else ""
                text = f"{s['loc']}\n{stamp}" if stamp else s["loc"]
                _title(clip, T, cfg, "rTitle", 1, s["vin"], T.secs(tt.location_title_s),
                       next_ts(), text, tt.location_font_size, tt.location_y)
                prev_loc = s["loc"]

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

        cursor += vdur

    sequence.set("duration", T.frames(cursor))
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
