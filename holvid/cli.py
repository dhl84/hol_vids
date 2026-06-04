"""Command-line entry point.

    holvid <project_dir> <command>

where <project_dir> holds the footage (one subfolder per day is typical) and an
optional holvid.toml. Commands:

    probe     scan + probe clips            -> _edit/clips.json
    sheets    contact sheets for review     -> _edit/sheets/, sheets_index.json
              (implies probe)
    review    scaffold an empty review.json from the manifest (won't overwrite)
    upright   bake pillarboxed copies of rotated clips (run before build)
    build     assemble the titled FCPXML    -> _edit/<event>.fcpxml
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

COMMANDS = ("probe", "sheets", "review", "upright", "build", "all")


def _scaffold_review(cfg: Config, clips: list[dict]) -> None:
    if cfg.review_json.exists():
        print(f"[review] {cfg.review_json} exists — not overwriting")
        return
    out = {"clips": {c["name"]: {"location": "", "summary": "", "dead": []}
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
              f"segments, {s.get('transitions')} dissolves, cut {s.get('cuts')} "
              f"clips (~{s.get('cut_s', 0):.0f}s removed), "
              f"{s.get('markers')} review markers")
        print(f"[fcpxml] DTD {'valid' if ok else 'INVALID: ' + msg}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
