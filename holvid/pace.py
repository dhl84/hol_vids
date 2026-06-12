"""Optional pipeline step: speed up boring transit footage.

    sample frames (ffmpeg) ─▶ "is this boring?" (local vision model) ─▶ speed spans

Long stretches of walking, driving/riding, eating, or queueing show the view but
drag. This samples a frame every few seconds, asks a local multimodal Ollama
model (e.g. gemma4) whether it's skippable transit, groups the boring runs, and
writes `speed` spans into review.json. The build keeps the picture but plays
those spans `factor`x faster and muted (DTD `<timeMap>` retime + volume to
-96dB) — no footage is removed.

No extra Python deps: ffmpeg does the sampling and the Ollama call goes through
the stdlib. You only need ffmpeg and a multimodal model pulled into Ollama to
run `holvid <proj> pace`.

All times here are clip-local seconds (0 = the clip's first frame).
"""
from __future__ import annotations

import base64
import json
import subprocess

from .config import Config
from .sanitize import ollama_generate

VISION_PROMPT = """\
You are trimming a personal holiday video. Look at this single frame and decide \
if it is part of a BORING, SKIPPABLE stretch that a viewer would want sped up — \
specifically any of:
{categories}

It is NOT boring if something notable is happening: a landmark or scenic view \
worth lingering on, people interacting/celebrating, a clear subject or activity.
When unsure, answer false (keep it at normal speed).

Answer ONLY as JSON: {{"boring": true|false, "kind": "<short, e.g. walking>"}}"""


def _sample_frame(cfg: Config, path: str, t: float) -> bytes | None:
    """Extract one JPEG frame at time t (seconds), downscaled, to memory."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
         "-i", path, "-frames:v", "1", "-vf", f"scale={cfg.pace.frame_px}:-2",
         "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
        capture_output=True)
    return r.stdout or None


def _classify_frame(cfg: Config, prompt: str, img: bytes) -> bool:
    b64 = base64.b64encode(img).decode("ascii")
    try:
        resp = ollama_generate(cfg.pace.ollama_url, cfg.pace.vision_model,
                               prompt, images=[b64], timeout=120,
                               num_predict=128)
        return bool(json.loads(resp).get("boring"))
    except Exception as e:                          # network / model / JSON error
        print(f"  [warn] vision classify failed: {e}")
        return False                                # default: keep normal speed


def detect(cfg: Config, clips: list[dict]) -> dict:
    """Find boring runs in every clip and write `speed` spans into review.json.
    Returns {clip_name: [[s0,s1,factor,reason], …]}."""
    p = cfg.pace
    prompt = VISION_PROMPT.format(
        categories="\n".join(f"- {c}" for c in p.categories))
    speeds: dict[str, list] = {}
    total_saved = 0.0
    for c in clips:
        dur = c["duration"]
        if dur < p.min_span_s:
            continue
        # sample at the centre of each sample_s window; each sample stands for its
        # whole [t-half, t+half] window.
        half = p.sample_s / 2.0
        times, flags = [], []
        t = half
        while t < dur:
            img = _sample_frame(cfg, c["path"], t)
            flags.append(bool(img) and _classify_frame(cfg, prompt, img))
            times.append(t)
            t += p.sample_s
        # boring sample window -> [t-half, t+half]; collect, merge, threshold.
        windows = [(max(0.0, ti - half), min(dur, ti + half))
                   for ti, f in zip(times, flags) if f]
        windows.sort()
        merged = []
        for a, b in windows:
            if merged and a <= merged[-1][1] + p.merge_gap_s:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        spans = [(a, b) for a, b in merged if b - a >= p.min_span_s]
        if spans:
            speeds[c["name"]] = [[round(a, 2), round(b, 2), p.factor, "boring transit"]
                                 for a, b in spans]
            saved = sum((b - a) * (1 - 1 / p.factor) for a, b in spans)
            total_saved += saved
            print(f"[pace] {c['name']}: {len(spans)} boring span(s) -> "
                  f"{p.factor:g}x (~{saved:.0f}s shorter)")
    _merge_into_review(cfg, speeds)
    print(f"[pace] sped up boring footage in {len(speeds)} clip(s); "
          f"~{total_saved:.0f}s shorter overall -> review.json")
    return speeds


def _merge_into_review(cfg: Config, speeds: dict) -> None:
    review = (json.loads(cfg.review_json.read_text())
              if cfg.review_json.exists() else {"clips": {}})
    clips = review.setdefault("clips", {})
    for name, spans in speeds.items():
        clips.setdefault(name, {"location": "", "summary": "", "dead": []})["speed"] = spans
    cfg.edit_dir.mkdir(parents=True, exist_ok=True)
    cfg.review_json.write_text(json.dumps(review, ensure_ascii=False, indent=2))
