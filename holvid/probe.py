"""Step 1 of the pipeline: probe clips and build contact sheets for review.

  probe   -> _edit/clips.json   ordered clip manifest: wall-clock datetime,
                                duration, fps, frames, embedded timecode.
  sheets  -> _edit/sheets/      one or more contact sheets per clip. Frames are
                                sampled every `sheets.interval_s` and tiled
                                cols x rows. Tile time is deterministic: tile i
                                (row-major, 0-based) on sheet n is at clip-local
                                (n*cols*rows + i) * interval seconds. (Many
                                ffmpeg builds lack drawtext, so we encode time in
                                tile *position* rather than burning it in.)
            _edit/sheets_index.json   maps each clip to its sheet files + the
                                grid geometry needed to turn a tile position back
                                into a clip-local timecode.

The contact sheets are what a human (or Claude) reads to write review.json.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .config import Config


def _discover(cfg: Config) -> list[Path]:
    seen: dict[str, Path] = {}
    for pat in cfg.discovery.patterns:
        for p in cfg.root.glob(pat):
            if not p.is_file():
                continue
            # skip our own outputs and baked-upright copies
            if "_edit" in p.parts or p.stem.endswith("_upright"):
                continue
            seen.setdefault(p.name, p)   # dedupe across overlapping globs
    return list(seen.values())


def _datetime_from(path: Path, cfg: Config, creation_time: str) -> tuple[datetime | None, int]:
    """Best wall-clock datetime + in-day sequence for a clip.

    Filename regex first (most reliable for action cams), then the container's
    creation_time tag, then file mtime. seq breaks ties between same-second files.
    """
    m = re.search(cfg.discovery.filename_regex, path.name)
    if m and all(m.group(i) for i in range(1, 7)):
        dt = datetime(*(int(m.group(i)) for i in range(1, 7)))
        seq = int(m.group(7)) if (m.lastindex or 0) >= 7 and m.group(7) else 0
        return dt, seq
    if creation_time:
        try:
            return datetime.fromisoformat(creation_time.replace("Z", "+00:00")).replace(tzinfo=None), 0
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime), 0
    except OSError:
        return None, 0


def probe_one(path: Path, cfg: Config) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
         "-show_entries", "stream_tags=timecode",
         "-show_entries", "format=duration:format_tags=creation_time,timecode",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    st = d["streams"][0]
    fmt = d["format"]
    num, den = (int(x) for x in st["r_frame_rate"].split("/"))
    duration = float(fmt["duration"])
    frames = (int(st["nb_frames"]) if str(st.get("nb_frames", "")).isdigit()
              else round(duration * num / den))
    tc = st.get("tags", {}).get("timecode") or fmt.get("tags", {}).get("timecode", "")
    creation_time = fmt.get("tags", {}).get("creation_time", "")
    dt, seq = _datetime_from(path, cfg, creation_time)
    return {
        "name": path.name,
        "path": str(path),
        "day": path.parent.name,
        "datetime": dt.isoformat() if dt else "",
        "seq": seq,
        "duration": round(duration, 3),
        "fps_num": num, "fps_den": den,
        "frames": frames,
        "width": st["width"], "height": st["height"],
        "timecode": tc,
        "creation_time": creation_time,
    }


def build_manifest(cfg: Config) -> list[dict]:
    files = _discover(cfg)
    if not files:
        raise SystemExit(f"[probe] no clips found under {cfg.root} "
                         f"(patterns: {cfg.discovery.patterns})")
    clips = [probe_one(f, cfg) for f in files]
    clips.sort(key=lambda c: (c["datetime"], c["seq"], c["name"]))  # chronological
    cfg.edit_dir.mkdir(parents=True, exist_ok=True)
    cfg.clips_json.write_text(json.dumps(clips, indent=2))
    total = sum(c["duration"] for c in clips)
    print(f"[probe] {len(clips)} clips, {total / 3600:.0f}h{total % 3600 / 60:02.0f}m "
          f"-> {cfg.clips_json}")
    return clips


def make_sheets(cfg: Config, clips: list[dict]) -> None:
    """One or more contact sheets per clip, grid sized to the clip so short clips
    don't waste a fixed grid. Writes sheets_index.json; a tile's clip-local time:

        seconds = (sheet_idx * cols * rows + tile_idx) * interval
    where sheet_idx and tile_idx are 0-based and tile_idx is row-major.
    """
    s = cfg.sheets
    cfg.sheets_dir.mkdir(parents=True, exist_ok=True)
    index, failed = [], []
    for c in clips:
        stem = Path(c["name"]).stem
        dur = c["duration"]
        # Short clips: shrink the interval so the fps filter's first (centred)
        # sample still lands inside the clip, guaranteeing >=1 frame.
        interval = s.interval_s if dur >= s.interval_s else max(0.5, dur / 2)
        n_frames = max(1, int(dur / interval) + 1)
        rows = min(s.max_rows, max(1, math.ceil(n_frames / s.cols)))
        vf = (f"fps=1/{interval},scale={s.tile_w}:-2,"
              f"tile={s.cols}x{rows}:padding=4:margin=4:color=black")
        pattern = str(cfg.sheets_dir / f"{stem}_sheet%02d.jpg")
        for old in cfg.sheets_dir.glob(f"{stem}_sheet*.jpg"):
            old.unlink()
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-hwaccel", "videotoolbox",
                 "-i", c["path"], "-vf", vf, "-an", "-q:v", "4", pattern],
                check=True)
        except subprocess.CalledProcessError:
            # last-ditch: a single thumbnail at the clip midpoint
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", f"{dur / 2:.2f}",
                 "-i", c["path"], "-frames:v", "1", "-vf", f"scale={s.tile_w}:-2",
                 "-q:v", "4", str(cfg.sheets_dir / f"{stem}_sheet00.jpg")])
            interval, rows = dur, 1
        made = sorted(p.name for p in cfg.sheets_dir.glob(f"{stem}_sheet*.jpg"))
        if not made:
            failed.append(stem)
        index.append({
            "name": c["name"], "day": c["day"], "datetime": c["datetime"],
            "duration": dur, "interval": round(interval, 3),
            "cols": s.cols, "rows": rows, "sheets": made,
        })
        print(f"[sheet] {stem}: {len(made)} sheet(s) ({dur:.0f}s, {s.cols}x{rows})")
    cfg.sheets_index_json.write_text(json.dumps(index, indent=2))
    print(f"[sheet] wrote {cfg.sheets_index_json.name} ({len(index)} clips, "
          f"{len(failed)} failed: {failed})")
