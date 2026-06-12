"""Optional pipeline step: find and silence sensitive/controversial speech.

    transcribe (mlx-whisper, any language) ─▶ classify (local LLM) ─▶ mute spans

Writes a per-clip `mute` list into review.json (clip-local seconds); the build
keeps the picture but silences those spans. Works in any language Whisper
supports — English and Korean tested, plus ~100 others — because Whisper
auto-detects per clip and the LLM judges the transcript text directly.

mlx-whisper is imported lazily (and the Ollama call uses the stdlib), so the
rest of the tool stays stdlib-only; you only need mlx-whisper to run
`holvid <proj> sanitize`.

All times here are clip-local seconds (0 = the clip's first frame), matching the
`dead` ranges in review.json.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from .config import Config


def ollama_generate(url: str, model: str, prompt: str, system: str | None = None,
                    images: list[str] | None = None, timeout: float = 600,
                    num_predict: int | None = None) -> str:
    """POST to Ollama's /api/generate and return the `response` string. Uses the
    stdlib only (no `requests`), so the tool needs no extra HTTP dep. `images`
    are base64-encoded for multimodal models. JSON-formatted, temperature 0.
    Always pass a `num_predict` cap for short structured replies — without one,
    a model stuck in a repetition loop generates until its context fills, which
    holds the Ollama slot for many minutes and stalls every queued request."""
    payload: dict = {"model": model, "prompt": prompt, "stream": False,
                     "format": "json", "options": {"temperature": 0}}
    if num_predict is not None:
        payload["options"]["num_predict"] = num_predict
    if system is not None:
        payload["system"] = system
    if images:
        payload["images"] = images
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")).get("response", "")

SYSTEM_PROMPT = """\
You review the transcript of a personal holiday video to flag speech the family \
would not want in the final cut. The transcript may be in ANY language \
(English, Korean, etc.) — judge the meaning regardless of language.

Flag a line as SENSITIVE if it contains:
{categories}

Do NOT flag ordinary holiday talk, place names, reactions, or directions.
When unsure, do NOT flag it (default OK) — it is worse to mute innocent talk \
than to miss the rare sensitive line, which a human still reviews.

Return ONLY a JSON object with a "results" array, one entry per input line, in
order — never a bare single object, even for one line:
{{"results": [{{"i": <int>, "sensitive": true|false, "category": "<short>"}}, …]}}"""


def _transcripts_path(cfg: Config) -> Path:
    return cfg.edit_dir / "transcripts.json"


def transcribe_clips(cfg: Config, clips: list[dict], force: bool = False) -> dict:
    """Transcribe every clip's audio (auto-detected language). Cached in
    _edit/transcripts.json — re-run with force=True to redo. Returns
    {clip_name: {"language": str, "lines": [{"start","end","text"}, …]}}."""
    import mlx_whisper

    out_path = _transcripts_path(cfg)
    cache = json.loads(out_path.read_text()) if out_path.exists() else {}
    lang = cfg.sanitize.language or None
    for c in clips:
        if c["name"] in cache and not force:
            continue
        print(f"[sanitize] transcribing {c['name']} …")
        res = mlx_whisper.transcribe(
            c["path"], path_or_hf_repo=cfg.sanitize.whisper_model,
            language=lang, verbose=False)
        cache[c["name"]] = {
            "language": res.get("language", ""),
            "lines": [{"start": round(s["start"], 2), "end": round(s["end"], 2),
                       "text": s["text"].strip()}
                      for s in res["segments"] if s["text"].strip()],
        }
        cfg.edit_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    return cache


def _ollama(cfg: Config, prompt: str, system: str) -> str:
    # a classification batch is a short reply: cap it hard so a bad batch fails
    # in ~2 min (kept OK + logged) instead of holding the server for 20.
    return ollama_generate(cfg.sanitize.ollama_url, cfg.sanitize.ollama_model,
                           prompt, system=system, timeout=120, num_predict=2048)


def _parse_array(text: str):
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, list):
                    return v
    except json.JSONDecodeError:
        pass
    a, b = text.find("["), text.rfind("]")
    if a != -1 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except json.JSONDecodeError:
            return None
    return None


def _classify_lines(cfg: Config, lines: list[dict],
                    categories: list[str]) -> list[str | None]:
    """Return a short reason per line (the matched category) or None if OK.
    Defaults all to OK on any failure."""
    sys_p = SYSTEM_PROMPT.format(
        categories="\n".join(f"- {c}" for c in categories))
    reasons: list[str | None] = [None] * len(lines)
    for i in range(0, len(lines), cfg.sanitize.batch_lines):
        batch = lines[i:i + cfg.sanitize.batch_lines]
        numbered = "\n".join(f"{i + j}: {ln['text']}" for j, ln in enumerate(batch))
        # the explicit count matters: without it some models (e.g. gemma) judge
        # only the first line and stop.
        prompt = (f'Classify ALL {len(batch)} lines below. Your "results" array '
                  f"must contain exactly {len(batch)} entries, i = {i} through "
                  f"{i + len(batch) - 1}.\n\nLines:\n{numbered}")
        parsed = None
        for _ in range(2):                       # one retry on bad JSON
            try:
                parsed = _parse_array(_ollama(cfg, prompt, sys_p))
            except Exception as e:               # network / model error
                print(f"  [warn] classify failed: {e}")
                parsed = None
            if parsed is not None:
                break
        if parsed is None:
            print(f"  [warn] lines {i}-{i + len(batch) - 1}: unparseable, kept OK")
            continue
        for item in parsed:
            if isinstance(item, dict) and item.get("sensitive") is True:
                idx = item.get("i")
                if isinstance(idx, int) and 0 <= idx < len(reasons):
                    cat = str(item.get("category") or "sensitive").strip()
                    reasons[idx] = cat[:40] or "sensitive"
        done = min(i + len(batch), len(lines))
        flagged = sum(1 for r in reasons if r)
        print(f"  [sanitize] {done}/{len(lines)} lines classified, "
              f"{flagged} flagged", flush=True)
    return reasons


def detect(cfg: Config, clips: list[dict], force: bool = False) -> dict:
    """Transcribe + classify, then write `mute` spans into review.json (keeping
    any existing dead/location fields). Silences both sensitive speech and (when
    enabled) arguments. Returns {clip_name: [[s0,s1,reason], …]}."""
    categories = list(cfg.sanitize.categories)
    if cfg.sanitize.detect_arguments:
        categories.append(cfg.sanitize.argument_category)
    transcripts = transcribe_clips(cfg, clips, force=force)
    pad, min_s = cfg.sanitize.pad_s, cfg.sanitize.min_mute_s
    mutes: dict[str, list] = {}
    total = 0.0
    for c in clips:
        tr = transcripts.get(c["name"])
        if not tr or not tr["lines"]:
            continue
        reasons = _classify_lines(cfg, tr["lines"], categories)
        spans = []
        for ln, reason in zip(tr["lines"], reasons):
            if not reason:
                continue
            s0 = max(0.0, ln["start"] - pad)
            s1 = min(c["duration"], ln["end"] + pad)
            if s1 - s0 >= min_s:
                spans.append((s0, s1, reason))
        spans.sort()
        merged = []
        for a, b, r in spans:                    # merge touching/overlapping
            if merged and a <= merged[-1][1] + 0.1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b), merged[-1][2])
            else:
                merged.append((a, b, r))
        if merged:
            mutes[c["name"]] = [[round(a, 2), round(b, 2), r] for a, b, r in merged]
            total += sum(b - a for a, b, _ in merged)
            print(f"[sanitize] {c['name']} ({tr['language']}): "
                  f"{len(merged)} span(s) muted "
                  f"[{', '.join(sorted({r for *_, r in merged}))}]")
    _merge_into_review(cfg, mutes)
    print(f"[sanitize] muted {total:.0f}s across {len(mutes)} clip(s) "
          f"(sensitive speech{' + arguments' if cfg.sanitize.detect_arguments else ''})"
          f" -> review.json")
    return mutes


def _merge_into_review(cfg: Config, mutes: dict) -> None:
    review = (json.loads(cfg.review_json.read_text())
              if cfg.review_json.exists() else {"clips": {}})
    clips = review.setdefault("clips", {})
    for name, spans in mutes.items():
        clips.setdefault(name, {"location": "", "summary": "", "dead": []})["mute"] = spans
    cfg.edit_dir.mkdir(parents=True, exist_ok=True)
    cfg.review_json.write_text(json.dumps(review, ensure_ascii=False, indent=2))
