"""Per-vacation project configuration.

Everything that was hard-coded in the one-off Paris scripts lives here as a
field with a sensible default, so a new trip is just a new `holvid.toml`.

A project is a folder of footage (usually one subfolder per day) plus a
`holvid.toml` next to it. Load order:

    Config.load(project_dir)
      -> reads <project_dir>/holvid.toml if present (else all defaults)
      -> resolves `root` relative to the config file

All durations are seconds; all offsets are hours.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

# Conventional Motion-template UIDs that FCP resolves internally on import.
BASIC_TITLE_UID = (".../Titles.localized/Bumper:Opener.localized/"
                   "Basic Title.localized/Basic Title.moti")
# The genuine built-in Cross Dissolve (lifted from an FCP-authored FCPXML). It
# is an FxPlug, not a Motion template — the .motr path conventions never resolve.
CROSS_DISSOLVE_UID = "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"


@dataclass
class Sheets:
    interval_s: float = 6.0     # seconds between sampled frames on a contact sheet
    cols: int = 6               # tiles per row (fixed, so tile position -> time is exact)
    max_rows: int = 12          # tall-clip cap; longer clips spill onto extra sheets
    tile_w: int = 384           # thumbnail width (px); height keeps aspect


@dataclass
class Titles:
    opening_s: float = 5.0          # opening movie-title duration over the first clip
    day_title_s: float = 4.0        # day-divider title duration
    location_title_s: float = 4.0   # location lower-third duration
    opening_font_size: int = 84
    day_font_size: int = 96
    location_font_size: int = 60
    location_y: float = -360.0      # lower-third vertical position (0 = centre)
    font: str = "Helvetica Neue"
    # strftime patterns ("%-d" = no leading zero, macOS/Linux):
    date_format: str = "%A %-d %B %Y"          # day divider, e.g. "Thursday 28 May 2026"
    location_stamp_format: str = "%-d %b · %H:%M"  # appended under a location title


@dataclass
class Transitions:
    enabled: bool = True
    dissolve_s: float = 1.0     # day/scene-boundary cross-dissolve length
    start_fade_s: float = 1.0   # gentle fade up at the very start (0 to disable)
    end_fade_s: float = 2.0     # fade to black at the very end (0 to disable)
    # clip pairs that are ONE continuous recording split across files -> hard join
    # (no dissolve). Each entry is [first_filename, second_filename].
    continuous_seams: list[list[str]] = field(default_factory=list)


@dataclass
class Cuts:
    min_dead_s: float = 3.0     # ignore flagged dead spans shorter than this (noise)
    # A dead span whose reason contains a cut-word is removed from the timeline.
    # Anything else (e.g. "dim church interior") only gets a REVIEW marker.
    cut_words: list[str] = field(default_factory=lambda: [
        "ground", "feet", "floor", "blur", "black", "wall", "face",
        "backpack", "ceiling", "underexposed", "lens", "blank", "pavement",
    ])
    # keep-words win over cut-words: a span matching one stays as a marker.
    keep_words: list[str] = field(default_factory=lambda: ["dark interior"])


@dataclass
class Timezone:
    """The camera clock rarely matches local time. Apply a single offset to get
    local wall-clock time for the titles, with an exception list for clips shot
    in a different zone (e.g. the leg before you crossed a border)."""
    offset_hours: float = 0.0          # added to every clip's filename time
    camera_time_clips: list[str] = field(default_factory=list)  # filenames: use camera time as-is


@dataclass
class Discovery:
    """How to find clips and read their wall-clock time."""
    # globs (relative to root) matched recursively for video files:
    patterns: list[str] = field(default_factory=lambda: [
        "*/*.MP4", "*/*.mp4", "*/*.MOV", "*/*.mov",
        "*.MP4", "*.mp4", "*.MOV", "*.mov",
    ])
    # filename -> datetime regex. Must capture, in order, 6 groups
    # (Y, M, D, h, m, s); an optional 7th group is the in-day sequence number.
    # Default matches DJI Osmo / Action ("DJI_20260528144150_0001_D.MP4").
    # Falls back to the file's creation_time, then mtime, when it doesn't match.
    filename_regex: str = r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2}).*?(\d+)?"


@dataclass
class Config:
    # --- identity ---
    title: str = "Our Holiday"          # opening movie title
    event_name: str = "Holiday"         # FCP event + project name
    # --- paths ---
    project_dir: Path = field(default_factory=Path)   # where holvid.toml lives
    root: Path = field(default_factory=Path)          # footage root (holds day folders/clips)
    # --- behaviour ---
    # clips shot vertical (stored landscape + a 90° rotation flag). FCP mishandles
    # portrait + conform, so `holvid upright` bakes them into a pillarboxed
    # landscape "<stem>_upright.MP4" the timeline treats as an ordinary clip.
    # Leave empty to auto-detect from rotation metadata at build time.
    rotated_clips: list[str] = field(default_factory=list)
    auto_detect_rotation: bool = True
    fcpxml_version: str = "1.9"
    # nested sections
    discovery: Discovery = field(default_factory=Discovery)
    timezone: Timezone = field(default_factory=Timezone)
    titles: Titles = field(default_factory=Titles)
    transitions: Transitions = field(default_factory=Transitions)
    cuts: Cuts = field(default_factory=Cuts)
    sheets: Sheets = field(default_factory=Sheets)

    # --- derived paths ---
    @property
    def edit_dir(self) -> Path:
        return self.project_dir / "_edit"

    @property
    def sheets_dir(self) -> Path:
        return self.edit_dir / "sheets"

    @property
    def clips_json(self) -> Path:
        return self.edit_dir / "clips.json"

    @property
    def sheets_index_json(self) -> Path:
        return self.edit_dir / "sheets_index.json"

    @property
    def review_json(self) -> Path:
        return self.edit_dir / "review.json"

    @property
    def out_fcpxml(self) -> Path:
        safe = "".join(ch if ch.isalnum() else "_" for ch in self.event_name).strip("_")
        return self.edit_dir / f"{safe or 'holiday'}.fcpxml"

    @classmethod
    def load(cls, project_dir: str | Path) -> "Config":
        project_dir = Path(project_dir).resolve()
        toml_path = project_dir / "holvid.toml"
        data = tomllib.loads(toml_path.read_text()) if toml_path.exists() else {}
        cfg = _from_dict(cls, data)
        cfg.project_dir = project_dir
        # `root` in TOML is relative to the config file; default = project_dir.
        raw_root = data.get("root", ".")
        cfg.root = (project_dir / raw_root).resolve()
        return cfg


def _from_dict(klass, data: dict):
    """Shallow/nested dataclass builder that ignores unknown keys (forward-compat)
    and recurses into nested dataclass sections."""
    kwargs = {}
    for f in fields(klass):
        if f.name not in data:
            continue
        if is_dataclass(f.type) if not isinstance(f.type, str) else False:
            kwargs[f.name] = _from_dict(f.type, data[f.name])
        else:
            kwargs[f.name] = data[f.name]
    # nested sections are typed by annotation name; handle them explicitly so we
    # don't depend on `from __future__ import annotations` turning types to str.
    nested = {"discovery": Discovery, "timezone": Timezone, "titles": Titles,
              "transitions": Transitions, "cuts": Cuts, "sheets": Sheets}
    for name, sub in nested.items():
        if name in data and isinstance(data[name], dict):
            kwargs[name] = _from_dict(sub, data[name])
    # scalar paths
    if "project_dir" in kwargs:
        kwargs["project_dir"] = Path(kwargs["project_dir"])
    return klass(**kwargs)
