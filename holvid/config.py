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
    closing_s: float = 4.0          # closing card over the end fade (0 to disable)
    closing_text: str = ""          # "" = auto: the trip's date range ("28 May – 4 June 2026")
    fade_s: float = 0.5             # every title fades in/out over this (0 = pop on/off)
    opening_font_size: int = 84
    day_font_size: int = 96
    location_font_size: int = 60
    closing_font_size: int = 72
    location_y: float = -360.0      # lower-third vertical position (0 = centre)
    font: str = "Helvetica Neue"
    # strftime patterns ("%-d" = no leading zero, macOS/Linux):
    date_format: str = "%A %-d %B %Y"          # day divider, e.g. "Thursday 28 May 2026"
    location_stamp_format: str = "%-d %b · %H:%M"  # appended under a location title
    closing_range_format: str = "%-d %B %Y"    # auto closing text renders the trip's
                                               # first/last day with this (month/year
                                               # dropped from the first when shared)


@dataclass
class Transitions:
    enabled: bool = True
    dissolve_s: float = 1.0     # scene-boundary cross-dissolve length
    day_dip_s: float = 1.5      # day boundaries dip through black for this long in
                                # total (the "time has passed" cue; 0 = plain dissolve)
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
class Sanitize:
    """Find and silence sensitive/controversial speech (any language).

    Pipeline: transcribe each clip's audio (mlx-whisper auto-detects the
    language — English, Korean, and ~100 others) -> classify each line with a
    local multilingual LLM (Ollama) -> write `mute` spans into review.json. The
    build keeps the picture but silences those spans (DTD `<mute>` element).

    Needs one extra dep only for the `sanitize` step: mlx-whisper (the Ollama
    call uses the stdlib).
    """
    enabled: bool = False
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    language: str = ""               # "" = auto-detect; or "en", "ko", …
    ollama_model: str = "qwen3.6:35b-a3b-coding-mxfp8"
    ollama_url: str = "http://localhost:11434/api/generate"
    batch_lines: int = 12            # transcript lines per classification call
    min_mute_s: float = 0.4          # ignore detected spans shorter than this
    pad_s: float = 0.25              # widen each muted span by this on both sides
    # Also mute arguments/fights between people (any language). On by default —
    # the LLM understands the conflict regardless of language (English, Korean…).
    detect_arguments: bool = True
    argument_category: str = (
        "a heated argument, fight, raised-voice quarrel, or tense interpersonal "
        "conflict between people (in any language, e.g. English or Korean)")
    # What counts as sensitive — fed to the LLM, language-agnostic. Tune per trip.
    categories: list[str] = field(default_factory=lambda: [
        "private personal information (health, medical, pregnancy, finances, "
        "relationships, home address, a private individual's full name)",
        "politics, religion, or other controversial/divisive opinions",
        "offensive, discriminatory, hateful, or sexual content",
        "anything embarrassing or that a person would not want shared publicly",
    ])


@dataclass
class Glitch:
    """Detect brief camera mishaps and CUT them (the dissolve logic transitions
    over the gap). Pure ffmpeg signal analysis — no model needed.

    Catches: lens covered / pointed at something dark (black frames), and a
    dropped/knocked camera that freezes (frozen frames). Only *short* anomalies
    are treated as glitches; a long black/frozen stretch is probably intentional
    (night shot, a deliberate hold) and is left alone.
    """
    enabled: bool = False
    black_min_s: float = 0.15        # min black interval to flag (blackdetect d=)
    black_pic_th: float = 0.97       # blackdetect picture blackness threshold
    black_pix_th: float = 0.10       # blackdetect per-pixel black threshold
    freeze_min_s: float = 0.6        # min frozen interval to flag
    freeze_noise_db: int = -55       # freezedetect noise floor (dB)
    max_glitch_s: float = 5.0        # ignore anomalies longer than this (likely intentional)
    pad_s: float = 0.1               # widen each cut span by this on both sides
    # reason written into review.json `dead`; must contain a `cut_words` token so
    # the build removes it rather than leaving a review marker.
    reason: str = "camera glitch (black/blank)"


@dataclass
class Pace:
    """Speed up boring transit footage (walking, driving, eating, queueing) so it
    shows the view without dragging — muted, at `factor`x. Uses a local
    multimodal Ollama model (e.g. gemma4) to look at sampled frames and decide
    what is skippable; writes `speed` spans into review.json.

    Needs no extra Python deps — just ffmpeg (frame sampling) and a multimodal
    model pulled into your local Ollama.
    """
    enabled: bool = False
    vision_model: str = "gemma4:latest"   # a multimodal Ollama model
    ollama_url: str = "http://localhost:11434/api/generate"
    sample_s: float = 5.0            # seconds between sampled/classified frames
    frame_px: int = 320              # downscale sampled frames to this width
    factor: float = 2.0              # default speed-up for boring spans
    min_span_s: float = 6.0          # only speed runs at least this long (else not worth it)
    merge_gap_s: float = 3.0         # bridge boring runs separated by < this
    # What counts as boring/skippable — fed to the vision model, tune per trip.
    categories: list[str] = field(default_factory=lambda: [
        "walking or strolling from place to place with nothing notable happening",
        "riding in or driving a car, bus, train or boat (transit/commute)",
        "sitting and eating a meal with little visible activity",
        "waiting or queuing",
    ])


@dataclass
class Chapters:
    """Index the movie into named chapters (YouTube-style).

    Labels come from review.json: an explicit per-clip `chapter` field (written
    by the optional `chapters` vision step, or by hand), falling back to the
    `location` label, then to a day divider. The build emits an FCP
    <chapter-marker> at each chapter start and writes _edit/chapters.txt +
    _edit/youtube_description.txt with `M:SS Title` lines ready to paste into
    the YouTube description. The chapter files are always written by `build`
    when any labels exist; `enabled` only gates the vision *naming* step.
    """
    enabled: bool = False
    vision_model: str = "gemma4:latest"   # multimodal Ollama model (naming step)
    ollama_url: str = "http://localhost:11434/api/generate"
    frames_per_clip: int = 3         # frames sampled per clip for the model
    frame_px: int = 320              # downscale sampled frames to this width
    # YouTube rules: first chapter at 0:00, each chapter >= 10s, >= 3 chapters.
    min_chapter_s: float = 10.0      # shorter chapters merge into the previous one
    day_format: str = "%A %-d %B"    # label for a new day with no chapter/location


@dataclass
class Geo:
    """Derive place names from GoPro GPS (GPMF telemetry) for captions/titles.

    GoPro Hero 5+ embeds a GPS track in a `gpmd` data stream. This step pulls a
    representative coordinate per clip with `exiftool` (median of valid fixes),
    reverse-geocodes it, and writes a `geo` field per clip into review.json
    (optionally filling empty `location` labels). Only clips with a satellite
    fix get a coordinate — indoor/no-fix clips are skipped and keep their visual
    labels. The build can prefix each day divider with the day's city.

    Offline geocoding (default) stays fully local: `reverse_geocoder` maps the
    coordinate to the nearest city + country — great for telling trip legs apart
    (Seoul vs Paris), not for landmarks within a city. Online geocoding
    (`online = true`, opt-in) additionally asks OpenStreetMap Nominatim for a
    landmark/street name — richer captions, but it SENDS YOUR COORDINATES to a
    third-party server, so it is off by default.

    Needs `exiftool` on PATH, and `reverse_geocoder` (pip) for the offline step.
    """
    enabled: bool = False
    fill_empty_location: bool = True   # auto-fill blank `location` labels (never overwrites)
    day_includes_city: bool = False    # prefix day dividers with the day's city
    # --- online landmark enrichment (opt-in; sends coordinates off-machine) ---
    online: bool = False
    online_zoom: int = 16              # Nominatim zoom: 16≈building/landmark, 14≈suburb
    nominatim_url: str = "https://nominatim.openstreetmap.org/reverse"
    user_agent: str = "holvid-geo (personal video tool)"  # Nominatim requires a UA
    request_pause_s: float = 1.1      # Nominatim policy: <= 1 request/second


@dataclass
class Music:
    """Optional background-music bed under the whole edit.

    The listed files play in order on a connected audio lane at a low, constant
    volume (the camera audio stays the foreground), fading in at the start and
    out at the end of the movie (or at the music's own end if it runs short).
    No looping: bring enough music, or let it end early — both read fine.
    """
    files: list[str] = field(default_factory=list)  # audio files, relative to the
                                                    # project dir (or absolute)
    volume_db: float = -18.0         # bed level; camera audio remains foreground
    fade_in_s: float = 3.0           # music fade-in at the start
    fade_out_s: float = 4.0          # music fade-out at the end


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
    music: Music = field(default_factory=Music)
    geo: Geo = field(default_factory=Geo)
    cuts: Cuts = field(default_factory=Cuts)
    sheets: Sheets = field(default_factory=Sheets)
    sanitize: Sanitize = field(default_factory=Sanitize)
    glitch: Glitch = field(default_factory=Glitch)
    pace: Pace = field(default_factory=Pace)
    chapters: Chapters = field(default_factory=Chapters)

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
    def gps_json(self) -> Path:
        return self.edit_dir / "gps.json"

    @property
    def chapters_txt(self) -> Path:
        return self.edit_dir / "chapters.txt"

    @property
    def youtube_description(self) -> Path:
        return self.edit_dir / "youtube_description.txt"

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
              "transitions": Transitions, "music": Music, "geo": Geo,
              "cuts": Cuts, "sheets": Sheets, "sanitize": Sanitize,
              "glitch": Glitch, "pace": Pace, "chapters": Chapters}
    for name, sub in nested.items():
        if name in data and isinstance(data[name], dict):
            kwargs[name] = _from_dict(sub, data[name])
    # scalar paths
    if "project_dir" in kwargs:
        kwargs["project_dir"] = Path(kwargs["project_dir"])
    return klass(**kwargs)
