"""Optional pipeline step: name the movie's chapters (YouTube-style index).

    sample frames (ffmpeg) ─▶ "what event is this?" (local vision model) ─▶ chapter labels

Looks at a few frames from every clip and asks a local multimodal Ollama model
for a short viewer-facing chapter title ("Eiffel Tower at Night", "Breakfast at
the Hotel"), telling it the previous clip's chapter so consecutive clips at the
same event share one label. Writes a per-clip `chapter` field into review.json.

The build then opens a new chapter wherever the label changes: it emits an FCP
<chapter-marker> on the timeline and writes _edit/chapters.txt plus
_edit/youtube_description.txt with `M:SS Title` lines ready to paste into the
YouTube description (first chapter forced to 0:00, sub-10s chapters merged —
YouTube's rules). Even without this step the build derives chapters from the
`location` labels and day changes; this step just gives them better names.

No extra Python deps: ffmpeg samples the frames and the Ollama call is stdlib.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from datetime import datetime, timedelta

from .config import Config
from .sanitize import ollama_generate

CHAPTER_PROMPT = """\
You are indexing a personal holiday video into named chapters (like YouTube \
chapters). You see {n} frame(s) sampled evenly across ONE clip.{context}

The previous clip belongs to the chapter: {prev}.

Decide whether this clip CONTINUES that same event/outing or STARTS a new one, \
and give a short viewer-facing chapter title (2-5 words, Title Case) naming the \
event, place or activity — e.g. "Eiffel Tower at Night", "Breakfast at the \
Hotel", "Walking the Old Town". Never mention cameras, clips or filenames.

Answer ONLY as JSON: {{"same_event": true|false, "title": "<chapter title>"}}"""


def _sample_frames(cfg: Config, c: dict) -> list[bytes]:
    """A few JPEG frames spread evenly across the clip, downscaled, in memory."""
    n = max(1, cfg.chapters.frames_per_clip)
    out = []
    for i in range(n):
        t = c["duration"] * (i + 0.5) / n
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
             "-i", c["path"], "-frames:v", "1",
             "-vf", f"scale={cfg.chapters.frame_px}:-2",
             "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
            capture_output=True)
        if r.stdout:
            out.append(r.stdout)
    return out


def _local_dt(cfg: Config, c: dict) -> datetime | None:
    if not c["datetime"]:
        return None
    dt = datetime.fromisoformat(c["datetime"])
    if c["name"] in set(cfg.timezone.camera_time_clips):
        return dt
    return dt + timedelta(hours=cfg.timezone.offset_hours)


def _clean(title) -> str:
    t = re.sub(r"\s+", " ", str(title)).strip().strip('"“”')
    return t[:60]


def _name_clip(cfg: Config, c: dict, prev_label: str, loc: str) -> str:
    """One vision call -> a chapter label for this clip ('' on any failure)."""
    imgs = _sample_frames(cfg, c)
    if not imgs:
        return ""
    hints = []
    ldt = _local_dt(cfg, c)
    if ldt:
        hints.append(f"It was shot around {ldt.strftime('%H:%M on %A %-d %B')}.")
    if loc:
        hints.append(f'The clip\'s location label is "{loc}".')
    prompt = CHAPTER_PROMPT.format(
        n=len(imgs),
        context=(" " + " ".join(hints)) if hints else "",
        prev=(f'"{prev_label}"' if prev_label
              else "(none — this clip starts the first chapter of the day)"))
    try:
        resp = ollama_generate(
            cfg.chapters.ollama_url, cfg.chapters.vision_model, prompt,
            images=[base64.b64encode(i).decode("ascii") for i in imgs],
            timeout=180, num_predict=128)
        d = json.loads(resp)
        if prev_label and bool(d.get("same_event")):
            return prev_label
        return _clean(d.get("title") or "")
    except Exception as e:                       # network / model / JSON error
        print(f"  [warn] chapter naming failed for {c['name']}: {e}")
        return ""


def detect(cfg: Config, clips: list[dict]) -> dict:
    """Label every clip with a chapter title and write the `chapter` field into
    review.json. Returns {clip_name: label}."""
    review = (json.loads(cfg.review_json.read_text())
              if cfg.review_json.exists() else {"clips": {}})
    rclips = review.get("clips", {})
    labels: dict[str, str] = {}
    prev_label, prev_day, runs = "", None, 0
    for c in clips:
        day = c["datetime"][:10] if c["datetime"] else c["day"]
        if day != prev_day:
            prev_label = ""                      # a new day never continues a chapter
            prev_day = day
        loc = (rclips.get(c["name"], {}).get("location") or "").strip()
        label = _name_clip(cfg, c, prev_label, loc) or loc
        if label.lower() == prev_label.lower():
            label = prev_label                   # normalise capitalisation drift
        if label:
            cont = label == prev_label
            runs += 0 if cont else 1
            print(f'[chapters] {c["name"]}: "{label}"' + (" (cont.)" if cont else ""))
            prev_label = label
        else:
            print(f"[chapters] {c['name']}: (no label — will fall back to "
                  f"location/day at build)")
        labels[c["name"]] = label
    _merge_into_review(cfg, labels)
    print(f"[chapters] {runs} chapter(s) across {len(clips)} clips -> review.json; "
          f"run `build` to emit chapters.txt + youtube_description.txt")
    return labels


def _merge_into_review(cfg: Config, labels: dict) -> None:
    review = (json.loads(cfg.review_json.read_text())
              if cfg.review_json.exists() else {"clips": {}})
    clips = review.setdefault("clips", {})
    for name, label in labels.items():
        if label:                                # keep any hand-written chapter on failure
            clips.setdefault(name, {"location": "", "summary": "", "dead": []})["chapter"] = label
    cfg.edit_dir.mkdir(parents=True, exist_ok=True)
    cfg.review_json.write_text(json.dumps(review, ensure_ascii=False, indent=2))
