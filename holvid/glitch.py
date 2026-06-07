"""Optional pipeline step: detect brief camera mishaps and cut them.

    ffmpeg blackdetect + freezedetect ─▶ short anomaly spans ─▶ review.json `dead`

Catches the lens being covered or the camera knocked to point at something dark
(black frames) and a dropped/bumped camera that freezes (frozen frames). Only
*short* anomalies are treated as glitches — a long black or frozen stretch is
probably intentional (a night shot, a deliberate hold) and is left alone.

The spans are written into review.json's `dead` list with a reason containing a
`cut_words` token, so the build removes them; the existing dissolve logic then
transitions over each gap. Pure stdlib + ffmpeg — no model needed.
"""
from __future__ import annotations

import json
import re
import subprocess

from .config import Config

# blackdetect logs:  [blackdetect @ ..] black_start:12.3 black_end:13.1 black_duration:0.8
_BLACK = re.compile(r"black_start:(\d+\.?\d*)\s+black_end:(\d+\.?\d*)")
# freezedetect logs freeze_start / freeze_end on separate lines, in order.
_FRZ_START = re.compile(r"freeze_start:\s*(\d+\.?\d*)")
_FRZ_END = re.compile(r"freeze_end:\s*(\d+\.?\d*)")


def _detect_clip(cfg: Config, path: str) -> list[tuple[float, float]]:
    g = cfg.glitch
    vf = (f"blackdetect=d={g.black_min_s}:pic_th={g.black_pic_th}:"
          f"pix_th={g.black_pix_th},"
          f"freezedetect=n={g.freeze_noise_db}dB:d={g.freeze_min_s}")
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-vf", vf, "-an", "-f", "null", "-"],
        capture_output=True, text=True)
    err = r.stderr
    spans = [(float(a), float(b)) for a, b in _BLACK.findall(err)]
    # pair freeze starts with ends in order; a start with no end runs to EOF
    # (likely an intentional hold) and is dropped.
    starts = [float(x) for x in _FRZ_START.findall(err)]
    ends = [float(x) for x in _FRZ_END.findall(err)]
    spans.extend(zip(starts, ends))
    return spans


def detect(cfg: Config, clips: list[dict]) -> dict:
    """Scan every clip for brief black/frozen glitches and write them into
    review.json `dead`. Returns {clip_name: [[s0,s1,reason], …]}."""
    g = cfg.glitch
    out: dict[str, list] = {}
    total = 0.0
    for c in clips:
        print(f"[glitch] scanning {c['name']} …")
        spans = []
        for s, e in _detect_clip(cfg, c["path"]):
            s = max(0.0, s - g.pad_s)
            e = min(c["duration"], e + g.pad_s)
            if 0 < e - s <= g.max_glitch_s:        # ignore long (intentional) holds
                spans.append((s, e))
        spans.sort()
        merged = []
        for a, b in spans:
            if merged and a <= merged[-1][1] + 0.1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        if merged:
            out[c["name"]] = [[round(a, 2), round(b, 2), g.reason] for a, b in merged]
            total += sum(b - a for a, b in merged)
            print(f"[glitch] {c['name']}: {len(merged)} glitch(es) to cut")
    _merge_into_dead(cfg, out)
    print(f"[glitch] cut {total:.1f}s of camera glitches across "
          f"{len(out)} clip(s) -> review.json")
    return out


def _merge_into_dead(cfg: Config, glitches: dict) -> None:
    review = (json.loads(cfg.review_json.read_text())
              if cfg.review_json.exists() else {"clips": {}})
    clips = review.setdefault("clips", {})
    for name, spans in glitches.items():
        entry = clips.setdefault(name, {"location": "", "summary": "", "dead": []})
        dead = entry.setdefault("dead", [])
        seen = {(round(float(d[0]), 1), round(float(d[1]), 1)) for d in dead}
        for sp in spans:
            key = (round(sp[0], 1), round(sp[1], 1))
            if key not in seen:                    # don't duplicate on re-run
                dead.append(sp)
                seen.add(key)
        dead.sort(key=lambda d: float(d[0]))
    cfg.edit_dir.mkdir(parents=True, exist_ok=True)
    cfg.review_json.write_text(json.dumps(review, ensure_ascii=False, indent=2))
