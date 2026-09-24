"""Optional step: fold the trip's photos into the film.

Photos are grouped by capture time (anything within `[stills].group_gap_s` of
its neighbour belongs to the same montage) and each group is rendered to a
single short clip at the project's format, with a slow Ken Burns push on every
still and a crossfade between them. A burst — nine near-identical frames of the
same moment — becomes one quick-fire montage rather than nine separate shots.

The rendered clip carries the group's *capture* time as its container
`creation_time`, exactly like a camera would, so `probe` places it
chronologically among the video with no special handling anywhere downstream:
to the rest of the pipeline a montage is just another clip.

    probe -> stills -> probe (again, to pick the montages up) -> sheets -> ...

HEIC is decoded with macOS `sips`, not ffmpeg: ffmpeg's HEIC path here returns
the container's small preview tile (512x512), not the full-resolution image.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config

_STILL_PREFIX = "STILLS_"


def _capture_utc(path: Path) -> datetime:
    """The photo's capture instant in UTC.

    EXIF DateTimeOriginal is local wall-clock with a separate OffsetTimeOriginal;
    we normalise to UTC so the montage's creation_time matches the convention
    every camera in the folder already uses (and so [timezone].offset_hours
    applies to it identically).
    """
    if shutil.which("exiftool"):
        out = subprocess.run(
            ["exiftool", "-s3", "-d", "%Y-%m-%dT%H:%M:%S",
             "-DateTimeOriginal", "-OffsetTimeOriginal", str(path)],
            capture_output=True, text=True).stdout.splitlines()
        if out and out[0].strip():
            try:
                dt = datetime.fromisoformat(out[0].strip())
                off = out[1].strip() if len(out) > 1 else ""
                if off.startswith(("+", "-")) and len(off) == 6:
                    sign = -1 if off[0] == "-" else 1
                    dt -= sign * timedelta(hours=int(off[1:3]), minutes=int(off[4:6]))
                return dt
            except ValueError:
                pass
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(tzinfo=None)


def _discover(cfg: Config) -> list[tuple[datetime, Path]]:
    seen: dict[str, Path] = {}
    for pat in cfg.stills.patterns:
        for p in cfg.root.glob(pat):
            if p.is_file() and "_edit" not in p.parts:
                seen.setdefault(p.name, p)
    return sorted(((_capture_utc(p), p) for p in seen.values()), key=lambda t: (t[0], t[1].name))


def _group(shots: list[tuple[datetime, Path]], gap_s: float) -> list[list[tuple[datetime, Path]]]:
    groups: list[list[tuple[datetime, Path]]] = []
    for shot in shots:
        if groups and (shot[0] - groups[-1][-1][0]).total_seconds() <= gap_s:
            groups[-1].append(shot)
        else:
            groups.append([shot])
    return groups


def _to_jpeg(src: Path, dst: Path) -> Path:
    """Full-resolution JPEG. macOS `sips` handles HEIC; everything else passes
    through untouched (ffmpeg already applies the EXIF orientation on decode)."""
    if src.suffix.lower() in (".heic", ".heif"):
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "best",
                        str(src), "--out", str(dst)],
                       check=True, capture_output=True)
        return dst
    return src


def _project_format(clips: list[dict]) -> tuple[int, int, int, int]:
    """Dominant source format by total duration — the same rule the timeline uses
    to pick its sequence format, so montages never become the odd one out."""
    weights: dict[tuple[int, int, int, int], float] = {}
    for c in clips:
        if c["name"].startswith(_STILL_PREFIX):
            continue                       # don't let a previous run vote
        key = (c["fps_num"], c["fps_den"], c["width"], c["height"])
        weights[key] = weights.get(key, 0.0) + c["duration"]
    return max(weights, key=weights.__getitem__) if weights else (60000, 1001, 3840, 2160)


def _filtergraph(n: int, w: int, h: int, fps: float, per_s: float,
                 xfade_s: float, zoom: float) -> tuple[str, float]:
    """Ken Burns each still, then crossfade the chain together.

    A portrait photo is centred over a blurred, darkened copy of itself rather
    than black bars. The zoom runs on a 2x-upscaled frame so the push is smooth
    instead of stepping a pixel at a time.
    """
    d = max(2, round(per_s * fps))                 # frames held per still
    parts = []
    for i in range(n):
        # alternate the push direction so a montage doesn't pulse in one rhythm
        z = (f"1+{zoom - 1:.4f}*on/{d}" if i % 2 == 0
             else f"{zoom:.4f}-{zoom - 1:.4f}*on/{d}")
        parts.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},gblur=sigma=30,eq=brightness=-0.2[bg{i}];"
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease[fg{i}];"
            f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,scale={w * 2}:{h * 2},"
            f"zoompan=z='{z}':d={d}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={w}x{h}:fps={fps},setsar=1,format=yuv420p[v{i}]")
    if n == 1:
        return ";".join(parts) + ";[v0]null[out]", d / fps
    chain, cur = [], "v0"
    total = d / fps
    for i in range(1, n):
        nxt = "out" if i == n - 1 else f"x{i}"
        off = total - xfade_s
        chain.append(f"[{cur}][v{i}]xfade=transition=fade:duration={xfade_s}"
                     f":offset={off:.4f}[{nxt}]")
        total = off + d / fps          # each xfade overlaps the pair by xfade_s
        cur = nxt
    return ";".join(parts + chain), total


def render(cfg: Config, clips: list[dict]) -> list[Path]:
    st = cfg.stills
    shots = _discover(cfg)
    if not shots:
        print(f"[stills] no photos found under {cfg.root} (patterns: {st.patterns})")
        return []
    fps_num, fps_den, w, h = _project_format(clips)
    fps = fps_num / fps_den
    groups = _group(shots, st.group_gap_s)
    print(f"[stills] {len(shots)} photo(s) -> {len(groups)} montage(s) "
          f"at {w}x{h} {fps:.2f}fps")

    made: list[Path] = []
    for gi, group in enumerate(groups, 1):
        out = cfg.root / f"{_STILL_PREFIX}{gi:02d}.MP4"
        # a burst of near-identical frames gets quick-fire timing; a lone photo
        # gets room to breathe.
        per_s = st.burst_photo_s if len(group) >= st.burst_threshold else st.per_photo_s
        xfade = min(st.xfade_s, per_s / 2)
        with tempfile.TemporaryDirectory() as td:
            inputs = []
            for i, (_, p) in enumerate(group):
                inputs += ["-loop", "1", "-t", f"{per_s + 1:.3f}", "-i",
                           str(_to_jpeg(p, Path(td) / f"{i:03d}.jpg"))]
            fg, dur = _filtergraph(len(group), w, h, fps, per_s, xfade, st.zoom)
            stamp = group[0][0].strftime("%Y-%m-%dT%H:%M:%S.000000Z")
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", *inputs,
                 "-filter_complex", fg, "-map", "[out]",
                 "-c:v", "h264_videotoolbox", "-b:v", st.bitrate,
                 "-r", f"{fps_num}/{fps_den}", "-t", f"{dur:.4f}",
                 "-metadata", f"creation_time={stamp}",
                 "-movflags", "+faststart", str(out)],
                check=True)
        names = ", ".join(p.stem for _, p in group)
        print(f"[stills] {out.name}  {dur:5.1f}s  {group[0][0]:%H:%M:%S}Z  "
              f"{len(group)} photo(s): {names}")
        made.append(out)
    print(f"[stills] re-run `probe` to fold {len(made)} montage(s) into the manifest")
    return made


if __name__ == "__main__":            # self-check: grouping + montage duration
    from datetime import datetime as _dt

    def _at(s):
        return (_dt(2026, 8, 12, 19, 0, 0) + timedelta(seconds=s), Path(f"{s}.jpg"))

    # a burst plus two far-apart singles -> three groups
    shots = [_at(0), _at(2), _at(3), _at(90), _at(400)]
    assert [len(g) for g in _group(shots, 60.0)] == [3, 1, 1]
    # the gap is measured against the group's LAST photo, so a steady drip of
    # sub-gap intervals stays one group however long it runs
    assert len(_group([_at(i * 50) for i in range(5)], 60.0)) == 1
    assert [len(g) for g in _group([], 60.0)] == []

    # n stills of per_s each, overlapping by xfade_s at every join
    fps, per_s, xf = 60.0, 4.0, 0.5
    for n, want in ((1, 4.0), (2, 7.5), (3, 11.0)):
        _, dur = _filtergraph(n, 3840, 2160, fps, per_s, xf, 1.12)
        assert abs(dur - want) < 1e-6, (n, dur, want)
    # a single still needs no xfade chain but must still expose [out]
    fg, _ = _filtergraph(1, 3840, 2160, fps, per_s, xf, 1.12)
    assert fg.endswith("[out]") and "xfade" not in fg
    print("stills self-check OK")
