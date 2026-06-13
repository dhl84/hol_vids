"""Command-line entry point.

    holvid <project_dir> <command>

where <project_dir> holds the footage (one subfolder per day is typical) and an
optional holvid.toml. Commands:

    probe     scan + probe clips            -> _edit/clips.json
    sheets    contact sheets for review     -> _edit/sheets/, sheets_index.json
              (implies probe)
    review    scaffold an empty review.json from the manifest (won't overwrite)
    sanitize  transcribe audio (any language) + flag sensitive/controversial
              speech AND arguments with a local LLM -> writes `mute` spans into
              review.json. The build keeps the picture and silences those spans.
              Needs mlx-whisper (Ollama call is stdlib); see holvid.toml [sanitize].
    glitch    detect brief camera mishaps (covered lens / dark / frozen) with
              ffmpeg -> writes short `dead` cuts; the build transitions over them.
    pace      speed up boring transit (walking/driving/eating) using a local
              vision model -> writes `speed` spans; the build plays them faster
              and muted. Needs ffmpeg + a multimodal Ollama model. See [pace].
    chapters  name each clip's event with a local vision model -> writes
              `chapter` labels into review.json. The build turns label changes
              into FCP chapter markers + YouTube `M:SS Title` timestamps
              (_edit/chapters.txt, youtube_description.txt). See [chapters].
    geo       read each clip's GoPro GPS (GPMF) with exiftool, reverse-geocode
              it (offline city/country; opt-in online landmark) -> writes a
              `geo` field + fills empty `location` labels in review.json. Needs
              exiftool + reverse_geocoder. See [geo].
    upright   bake pillarboxed copies of rotated clips (run before build)
    build     assemble the titled FCPXML    -> _edit/<event>.fcpxml
              (+ chapters.txt / youtube_description.txt when labels exist)
    all       probe + sheets + scaffold review (the prep before you fill it in)

Run with no command for usage. Example:

    uv run holvid "~/Downloads/Paris 2026" sheets
    uv run holvid "~/Downloads/Paris 2026" build
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import probe, timeline
from .config import Config

COMMANDS = ("probe", "sheets", "review", "sanitize", "glitch", "pace",
            "chapters", "geo", "upright", "build", "all")


def _scaffold_review(cfg: Config, clips: list[dict]) -> None:
    if cfg.review_json.exists():
        print(f"[review] {cfg.review_json} exists — not overwriting")
        return
    out = {"clips": {c["name"]: {"location": "", "chapter": "", "summary": "",
                                 "dead": []}
                     for c in clips},
           "title": cfg.title}
    cfg.edit_dir.mkdir(parents=True, exist_ok=True)
    cfg.review_json.write_text(json.dumps(out, indent=2))
    print(f"[review] scaffolded {cfg.review_json} ({len(clips)} clips) — fill in "
          f"location/dead by reading the contact sheets")


def _load_clips(cfg: Config) -> list[dict]:
    if cfg.clips_json.exists():
        return json.loads(cfg.clips_json.read_text())
    return probe.build_manifest(cfg)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    project_dir = Path(argv[0]).expanduser()
    cmd = argv[1] if len(argv) > 1 else "all"
    if cmd not in COMMANDS:
        print(f"unknown command {cmd!r}; choose one of: {', '.join(COMMANDS)}")
        return 2
    if not project_dir.is_dir():
        print(f"project dir not found: {project_dir}")
        return 2

    cfg = Config.load(project_dir)
    print(f"[holvid] project={cfg.project_dir.name}  root={cfg.root}  cmd={cmd}")

    if cmd == "probe":
        probe.build_manifest(cfg)
    elif cmd == "sheets":
        probe.make_sheets(cfg, probe.build_manifest(cfg))
    elif cmd == "review":
        _scaffold_review(cfg, _load_clips(cfg))
    elif cmd == "sanitize":
        from . import sanitize
        if not cfg.sanitize.enabled:
            print("[sanitize] [sanitize].enabled is false in holvid.toml — "
                  "running anyway since you asked for it explicitly")
        sanitize.detect(cfg, _load_clips(cfg))
    elif cmd == "glitch":
        from . import glitch
        if not cfg.glitch.enabled:
            print("[glitch] [glitch].enabled is false in holvid.toml — "
                  "running anyway since you asked for it explicitly")
        glitch.detect(cfg, _load_clips(cfg))
    elif cmd == "pace":
        from . import pace
        if not cfg.pace.enabled:
            print("[pace] [pace].enabled is false in holvid.toml — "
                  "running anyway since you asked for it explicitly")
        pace.detect(cfg, _load_clips(cfg))
    elif cmd == "chapters":
        from . import chapters
        if not cfg.chapters.enabled:
            print("[chapters] [chapters].enabled is false in holvid.toml — "
                  "running anyway since you asked for it explicitly")
        chapters.detect(cfg, _load_clips(cfg))
    elif cmd == "geo":
        from . import geo
        if not cfg.geo.enabled:
            print("[geo] [geo].enabled is false in holvid.toml — "
                  "running anyway since you asked for it explicitly")
        geo.detect(cfg, _load_clips(cfg))
    elif cmd == "upright":
        timeline.bake_upright(cfg, _load_clips(cfg))
    elif cmd == "all":
        clips = probe.build_manifest(cfg)
        probe.make_sheets(cfg, clips)
        _scaffold_review(cfg, clips)
    elif cmd == "build":
        clips = _load_clips(cfg)
        review = (json.loads(cfg.review_json.read_text())
                  if cfg.review_json.exists() else {"clips": {}})
        if not cfg.review_json.exists():
            print("[build] no review.json — building with locations/cuts empty")
        out = timeline.build(cfg, clips, review, cfg.out_fcpxml)
        ok, msg = timeline.validate(out, cfg.fcpxml_version)
        s = getattr(timeline.build, "stats", {})
        print(f"[fcpxml] {out.name}: {len(clips)} clips -> {s.get('segments')} "
              f"segments, {s.get('transitions')} dissolves, "
              f"{s.get('day_dips', 0)} day dips, cut {s.get('cuts')} "
              f"clips (~{s.get('cut_s', 0):.0f}s removed), "
              f"{s.get('markers')} review markers, "
              f"{s.get('mutes', 0)} muted spans (~{s.get('mute_s', 0):.0f}s), "
              f"{s.get('speedups', 0)} speed-ups "
              f"(~{s.get('speed_saved_s', 0):.0f}s shorter), "
              f"{s.get('chapters', 0)} chapters, "
              f"~{s.get('music_s', 0):.0f}s music bed")
        print(f"[fcpxml] DTD {'valid' if ok else 'INVALID: ' + msg}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
