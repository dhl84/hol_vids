"""Optional pipeline step: find the best moments (audio excitement + vision).

    ffmpeg ebur128 momentary loudness ─▶ bursts above the clip's baseline
    ─▶ confirm + title (local vision model) ─▶ `highlight` spans

Family highlights announce themselves on the soundtrack: laughter, cheering,
a shout, a splash. This measures each clip's momentary loudness (EBU R128,
10 Hz), finds sustained bursts well above the clip's own median, and asks a
local multimodal Ollama model to look at the frame under the loudest instant —
confirming the moment and giving it a short title. Writes `highlight` spans
into review.json; the build emits a ★ marker at each one (Timeline Index ▸
Tags) plus _edit/highlights.txt with `M:SS ★ Title` lines for the YouTube
description.

If Ollama is unreachable the bursts are kept with a generic "loud moment"
label — the audio signal alone is still a good treasure map.

No extra Python deps: ffmpeg measures the loudness, the Ollama call is stdlib.
All times here are clip-local seconds (0 = the clip's first frame).
"""
from __future__ import annotations

import base64
import json
import re
import statistics
import subprocess

from .config import Config
from .sanitize import ollama_generate

VISION_PROMPT = """\
You are picking the best moments of a personal holiday video. At this instant \
the soundtrack spikes {above:.0f} dB above the clip's baseline — laughter, \
cheering, a shout, a splash, music. Look at the frame and decide if this is a \
HIGHLIGHT a family would want to jump to: people laughing, celebrating or \
playing, action (a wave, a ride, an animal), a performance, a striking view, \
a toast or a treat.

It is NOT a highlight if the noise is mundane: traffic, wind, a passing crowd, \
the camera being handled. When unsure, answer false.

Answer ONLY as JSON: {{"highlight": true|false, "title": "<2-4 words, Title Case>"}}"""

# ebur128 logs one line per 100ms:  ... t: 2.1 TARGET:-23 LUFS M: -27.5 S: ...
_EBUR = re.compile(r"t:\s*(\d+\.?\d*)\s.*?M:\s*(-?\d+\.?\d*)")


def _loudness(path: str) -> list[tuple[float, float]]:
    """Momentary loudness series [(t, M_lufs), …] at 10 Hz. Empty if no audio."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-vn", "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True)
    return [(float(t), float(m)) for t, m in _EBUR.findall(r.stderr)]


def _bursts(series: list[tuple[float, float]], dur: float,
            cfg: Config) -> list[tuple[float, float, float]]:
    """Sustained loud bursts [(s0, s1, peak_lufs), …]: momentary loudness at
    least `threshold_db` above the clip's median AND above `min_peak_lufs`,
    lasting >= min_span_s, merged across gaps < merge_gap_s, loudest
    `top_per_clip` kept, padded by pad_s and clamped to the clip."""
    h = cfg.highlight
    if len(series) < 10:                         # <1s of audio: nothing to rank
        return []
    thr = statistics.median(m for _, m in series) + h.threshold_db
    runs: list[list[float]] = []                 # [s0, s1, peak]
    for t, m in series:
        if m < thr or m < h.min_peak_lufs:
            continue
        if runs and t - runs[-1][1] < h.merge_gap_s:
            runs[-1][1] = t
            runs[-1][2] = max(runs[-1][2], m)
        else:
            runs.append([t, t, m])
    runs = [r for r in runs if r[1] - r[0] >= h.min_span_s]
    runs = sorted(runs, key=lambda r: -r[2])[:max(1, h.top_per_clip)]
    return sorted((max(0.0, s0 - h.pad_s), min(dur, s1 + h.pad_s), peak)
                  for s0, s1, peak in runs)


def _sample_frame(cfg: Config, path: str, t: float) -> bytes | None:
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
         "-i", path, "-frames:v", "1", "-vf", f"scale={cfg.highlight.frame_px}:-2",
         "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
        capture_output=True)
    return r.stdout or None


def _confirm(cfg: Config, path: str, t: float, above: float) -> str | None:
    """Vision check at the burst peak: a short title, "loud moment" when the
    model is unreachable (keep — audio alone is useful), None to drop."""
    img = _sample_frame(cfg, path, t)
    if not img:
        return "loud moment"
    try:
        resp = ollama_generate(
            cfg.highlight.ollama_url, cfg.highlight.vision_model,
            VISION_PROMPT.format(above=above),
            images=[base64.b64encode(img).decode("ascii")],
            timeout=120, num_predict=128)
        d = json.loads(resp)
        if not d.get("highlight"):
            return None
        title = re.sub(r"\s+", " ", str(d.get("title") or "")).strip().strip('"“”')
        return title[:60] or "highlight"
    except Exception as e:                       # network / model / JSON error
        print(f"  [warn] highlight vision check failed: {e}")
        return "loud moment"


def detect(cfg: Config, clips: list[dict]) -> dict:
    """Find the loudest, best moments in every clip and write `highlight` spans
    into review.json. Returns {clip_name: [[s0,s1,title], …]}."""
    out: dict[str, list] = {}
    for c in clips:
        series = _loudness(c["path"])
        med = statistics.median(m for _, m in series) if len(series) >= 10 else 0.0
        spans = []
        for s0, s1, peak in _bursts(series, c["duration"], cfg):
            title = _confirm(cfg, c["path"], (s0 + s1) / 2, peak - med)
            if title:
                spans.append([round(s0, 2), round(s1, 2), title])
        if spans:
            out[c["name"]] = spans
            print(f"[highlight] {c['name']}: "
                  + ", ".join(f'"{t}" @{s0:.0f}s' for s0, _, t in spans))
    _merge_into_review(cfg, out)
    n = sum(len(v) for v in out.values())
    print(f"[highlight] {n} highlight(s) across {len(out)} clip(s) -> review.json; "
          f"`build` adds ★ markers + highlights.txt")
    return out


def _merge_into_review(cfg: Config, highlights: dict) -> None:
    review = (json.loads(cfg.review_json.read_text())
              if cfg.review_json.exists() else {"clips": {}})
    clips = review.setdefault("clips", {})
    for name, spans in highlights.items():
        clips.setdefault(name, {"location": "", "summary": "", "dead": []})["highlight"] = spans
    cfg.edit_dir.mkdir(parents=True, exist_ok=True)
    cfg.review_json.write_text(json.dumps(review, ensure_ascii=False, indent=2))


if __name__ == "__main__":                       # self-check: burst logic
    class _H:  # noqa: N801
        threshold_db, min_peak_lufs, min_span_s = 8.0, -35.0, 1.0
        merge_gap_s, pad_s, top_per_clip = 4.0, 2.0, 3

    class _C:  # noqa: N801
        highlight = _H()

    quiet = [(i / 10, -40.0) for i in range(600)]
    assert _bursts(quiet, 60.0, _C()) == []      # flat clip: no bursts
    loud = list(quiet)
    for i in range(100, 130):                    # 3s burst at t=10..13, -20 LUFS
        loud[i] = (i / 10, -20.0)
    b = _bursts(loud, 60.0, _C())
    assert len(b) == 1 and abs(b[0][0] - 8.0) < 0.2 and abs(b[0][1] - 14.9) < 0.2, b
    soft = list(quiet)
    for i in range(100, 130):                    # loud vs baseline but < -35 LUFS
        soft[i] = (i / 10, -36.0)
    assert _bursts(soft, 60.0, _C()) == []       # absolute floor holds
    print("highlight self-check OK")
