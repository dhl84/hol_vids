# hol_vids

Turn a folder of raw vacation clips into a **titled, review-ready Final Cut Pro
timeline** — fully local, no cloud. One chronological edit across all days, with
an opening title card, a date divider for each day, a location lower-third
whenever the place changes (all titles gently fading in and out), obvious junk
auto-trimmed, the iffy bits left as review markers, gentle cross-dissolves
between scenes, a dip through black between days (the classic "time has passed"
cue), an optional low background-music bed, a closing card with the trip's date
range, and a YouTube-ready chapter index (FCP chapter markers + `M:SS Title`
timestamps for the video description).

This is the reusable generalization of the one-off Paris 2026 edit. Everything
that was hard-coded there (the trip title, timezone offset, rotated clips,
continuous-recording seams, cut-word list, …) is now a field in a per-trip
`holvid.toml`, so a new vacation is just a new config file.

> Verified: rebuilding the Paris 2026 timeline through this tool produced a
> **byte-for-byte identical** FCPXML to the hand-built original (verified before
> the chapters feature). A rebuild today additionally emits `<chapter-marker>`
> elements, title fade-ins/outs, day-boundary dips to black, and a closing date
> card — set `[titles].fade_s = 0`, `closing_s = 0` and
> `[transitions].day_dip_s = 0` to reproduce the original look.

## Requirements

- macOS (FCPXML import; rotation bake uses VideoToolbox) — the build step itself
  is cross-platform
- `uv`, Python ≥ 3.11 (`tomllib` is stdlib) — `uv sync` / `uv pip install -e .`
- `ffmpeg` + `ffprobe` on PATH
- Final Cut Pro installed for DTD validation (skipped gracefully if absent)
- No Python deps beyond the stdlib for the core pipeline. The optional analysis
  steps need a local [Ollama](https://ollama.com) server and:
  - **`sanitize`** (mute sensitive speech + arguments): `mlx-whisper` + a text
    LLM. **`pace`** (speed up boring footage): a multimodal model (e.g. gemma4)
    — no pip install. **`glitch`** (cut camera mishaps): nothing beyond ffmpeg.
    **`chapters`** (name YouTube chapters): a multimodal model.

## The pipeline

```
probe ──> sheets ──> (you/Claude fill review.json) ──┐
                                                      ├─ [sanitize] [glitch] [pace] [chapters] ──> [upright] ──> build
              (optional auto-analysis, any order) ────┘
```

1. **probe** — scan the footage, read each clip's wall-clock time, fps, duration
   and embedded timecode → `_edit/clips.json` (sorted chronologically).
2. **sheets** — sample a frame every few seconds and tile them into contact
   sheets → `_edit/sheets/`. These are what you (or Claude) read to know what's
   in each clip. Tile position encodes the timecode exactly (no burned-in text
   needed).
3. **review** — fill `_edit/review.json`: a `location` label and any `dead`
   spans per clip. `holvid … review` scaffolds an empty one.
4. **sanitize / glitch / pace** *(optional, any order)* — automatic analysis
   that writes proposals into `review.json`. See
   [Optional auto-analysis](#optional-auto-analysis):
   - **sanitize** — mute sensitive speech **and arguments** (any language).
   - **glitch** — cut brief camera mishaps (covered lens / dark / frozen).
   - **pace** — speed up boring transit (walking/driving/eating), muted.
   - **chapters** — name each clip's event with a vision model → `chapter`
     labels (YouTube chapter titles).
5. **upright** — bake pillarboxed landscape copies of any vertical clips so FCP
   never has to rotate/conform. Auto-detected from rotation metadata.
6. **build** — assemble `_edit/<event>.fcpxml`: titles, dissolves, auto-cuts,
   review markers, audio mutes, speed-ramps, and chapter markers.
   DTD-validated. Also writes `_edit/chapters.txt` + `youtube_description.txt`
   — `M:SS Title` timestamps to paste into the YouTube description so viewers
   can flick between events (see [chapters](#chapters--name-youtube-chapters-local-vision-model)).

## Usage

```sh
# one-shot prep (probe + sheets + scaffold review):
uv run holvid "/Users/you/Downloads/Italy 2027" all

# ... read the contact sheets, fill in _edit/review.json ...

uv run holvid "/Users/you/Downloads/Italy 2027" upright   # if any vertical clips
uv run holvid "/Users/you/Downloads/Italy 2027" build
```

Then in Final Cut Pro: **File ▸ Import ▸ XML**. It creates a new project and
touches nothing else. Review markers show in **Timeline Index ▸ Tags**.

Individual commands: `probe`, `sheets`, `review`, `sanitize`, `glitch`, `pace`,
`chapters`, `upright`, `build`, `all`.

## Optional auto-analysis

Four optional steps look at the footage and write proposals into `review.json`
(`mute` / `dead` / `speed` / `chapter`). They're independent — run any subset, in any order —
and all local. Re-run freely: muting and speed-ramps never move a frame, so you
can tweak the JSON by hand and rebuild. Each is off by default; enable in
`holvid.toml` or just run the command (it runs when invoked explicitly).

### `sanitize` — mute sensitive speech **and arguments** (any language)

Catches unguarded talk — a relative's medical news, money, anything embarrassing
— **and arguments/fights** (e.g. a couple bickering), in any language:

```sh
uv pip install mlx-whisper                 # one-time, only for this step
ollama pull qwen3.6:35b-a3b-coding-mxfp8   # or set [sanitize].ollama_model
uv run holvid "/Users/you/Downloads/Korea 2026" sanitize
```

1. **Transcribe** every clip with `mlx-whisper`, **language auto-detected per
   clip** (English and Korean tested; ~100 work). Cached in `transcripts.json`.
2. **Classify** each line with a local LLM against `[sanitize].categories` plus —
   when `detect_arguments` is on (default) — heated arguments/conflict. It judges
   *meaning* regardless of language and **defaults to OK when unsure**. Progress
   prints per batch (`N/M lines classified, K flagged`); generation is capped
   (`num_predict`) and each batch times out in ~2 min, so a misbehaving model
   skips a batch (kept OK, logged) instead of stalling the run.
3. **Write `mute` spans** into `review.json`. The build emits a `<mute>` inside
   `<audio-channel-source>` — **picture untouched, audio silenced** over those
   seconds. Spans inside removed (`dead`) footage vanish.

> **Pick a model that fits your RAM with headroom.** A model near the machine's
> memory ceiling makes Ollama evict/reload and prompt-processing crawl into
> timeouts (measured: a 37.7 GB model on a 48 GB Mac ≈ unusable; a ~10 GB model
> classified the same Korean transcript at ~28 s per 12-line batch with sound
> judgment). Set `[sanitize].ollama_model` per project. Whatever the model
> flags, **skim the spans in `review.json` before building** — smaller models
> occasionally over-flag playful family talk as conflict, and deleting a span
> + rebuilding takes seconds.

### `glitch` — cut brief camera mishaps (ffmpeg only)

```sh
uv run holvid "/Users/you/Downloads/Korea 2026" glitch
```

Uses ffmpeg `blackdetect` + `freezedetect` to find the lens being covered /
knocked to point at something dark (black frames) and a dropped camera that
freezes — but only **short** anomalies (`[glitch].max_glitch_s`, so a deliberate
night hold is left alone). Each becomes a short `dead` cut; the build removes it
and the **dissolve logic transitions over the gap**.

### `pace` — speed up boring transit (local vision model)

```sh
ollama pull gemma4                         # any multimodal model; set [pace].vision_model
uv run holvid "/Users/you/Downloads/Korea 2026" pace
```

Samples a frame every few seconds and asks a **local multimodal model** whether
it's skippable transit (`[pace].categories`: walking, driving/riding, eating,
queueing). Boring runs ≥ `min_span_s` become `speed` spans. The build keeps the
picture but plays them `factor`× faster and **muted**, via a DTD `<timeMap>`
retime + volume to -96dB — no footage removed.

### `chapters` — name YouTube chapters (local vision model)

```sh
ollama pull gemma4                         # any multimodal model; set [chapters].vision_model
uv run holvid "/Users/you/Downloads/Korea 2026" chapters
```

Samples a few frames from every clip and asks a **local multimodal model** for a
short viewer-facing event title ("Eiffel Tower at Night", "Breakfast at the
Hotel"), telling it the previous clip's chapter so consecutive clips at the same
event share one label (a new day always starts fresh). Writes a `chapter` field
per clip into `review.json` — edit the labels freely and rebuild.

The **build** then opens a chapter wherever the label changes (falling back to
the `location` label, then a day divider, so you get chapters even without this
step) and writes:

- an FCP `<chapter-marker>` at each chapter start (visible in FCP; survives
  into exports),
- `_edit/chapters.txt` — bare `M:SS Title` lines,
- `_edit/youtube_description.txt` — a paste-ready description block.

Paste the timestamps into the YouTube video description and YouTube shows the
chapter bar. Its rules are applied automatically: the first chapter is forced
to `0:00`, a chapter shorter than `[chapters].min_chapter_s` (default 10 s,
YouTube's minimum) loses its slot to the next label, and the build warns if
fewer than 3 chapters remain (YouTube's minimum for the chapter bar).

> First time: confirm in FCP that mutes are silent and speed-ramps play at the
> right rate. `<mute>` and `<timeMap>` are the purpose-built, DTD-valid
> mechanisms; the spans are also listed in `review.json` for a manual pass.

## Configuration

Copy [`holvid.toml.example`](holvid.toml.example) into the footage folder as
`holvid.toml` and edit. Every field is optional and documented inline; the
example holds the real Paris settings as a worked reference. With no config at
all you still get a valid edit (defaults: timezone offset 0, auto-detected
rotation, generic title). Key sections:

- `title` / `event_name` — the opening card + FCP project name
- `[timezone]` — `offset_hours` to convert camera time → local for the titles,
  with a `camera_time_clips` exception list
- `[titles]` — fonts, sizes, durations, title fade (`fade_s`), closing card
  (`closing_s` / `closing_text`), date/stamp `strftime` formats
- `[transitions]` — dissolve length, day dip-to-black (`day_dip_s`), start/end
  fades, `continuous_seams` (clip pairs that are one recording split across
  files → hard join)
- `[music]` — optional background-music bed (`files`, `volume_db`, fades)
- `[cuts]` — `cut_words` (a dead span mentioning one is removed) vs `keep_words`
  / short spans → left as review markers
- `[discovery]` — file globs and the filename→datetime regex (default fits DJI
  action cams; falls back to container `creation_time`, then file mtime)
- `[sanitize]` — sensitive-speech + argument muting (model, language,
  categories, `detect_arguments`)
- `[glitch]` — camera-mishap cutting (black/freeze thresholds, `max_glitch_s`)
- `[pace]` — boring-transit speed-up (`vision_model`, `factor`, `min_span_s`,
  categories)
- `[chapters]` — YouTube-chapter naming (`vision_model`, `frames_per_clip`,
  `min_chapter_s`, `day_format`)

## review.json schema

```jsonc
{
  "clips": {
    "DJI_…_0007_D.MP4": {
      "location": "Gare du Nord",          // title fires when this changes
      "chapter": "Arrival in Paris",       // YouTube chapter opens when this changes
      "summary": "Family in the arrivals hall.",  // notes only; not in the XML
      "dead":  [[12.0, 14.8, "near-black"]],       // cut footage (cut-word reason)
      "mute":  [[31.5, 35.0, "argument"]],         // keep picture, silence audio
      "speed": [[40.0, 70.0, 2.0, "boring transit"]] // keep picture, 2x + muted
    }
  },
  "title": "Optional movie-title override"
}
```

All spans are clip-local seconds. A `dead` span whose reason contains a
**cut-word** (`black`, `blur`, `floor`, …) is cut from the timeline at any
length; an ambiguous reason (e.g. `"dim church interior"`, or a `keep_word`
match) is kept as a `REVIEW:` marker, and ambiguous spans shorter than
`min_dead_s` are ignored as noise. A `mute` span keeps the picture and silences
the audio. A `speed` span keeps the picture and plays it `factor`× faster and
muted. A `chapter` label opens a new YouTube chapter when it differs from the
previous clip's (empty = continue; falls back to `location`, then the day).
All four are written automatically by the analysis steps, but you can add or
edit them by hand.

## How it builds the timeline

- **Chronological spine.** Every clip becomes one asset; kept ranges become
  `asset-clip` segments in time order across all days.
- **Titles.** See [`TITLES.md`](TITLES.md) for the full logic — opening (once),
  day divider (on calendar-date change), location lower-third (on label change),
  closing card (the trip's date range, over the final fade-out), all rendered in
  local time and each fading gently in/out (`[titles].fade_s`).
- **Dissolves & day dips.** A 1 s cross-dissolve straddles each scene boundary
  where both neighbours are long enough to lend a handle; `continuous_seams`
  force a hard join. A **day boundary dips through black** instead
  (`[transitions].day_dip_s`) — fade out the old day, fade in the new — the
  conventional "time has passed" cue. Gentle fade up at the start, fade to
  black at the end. A removed glitch mid-clip is bridged by a dissolve too.
- **Music bed.** `[music].files` lie in order under the whole edit on a
  connected lane at `volume_db` (default −18 dB — the camera audio stays the
  foreground), fading in at the start and out at the movie's end (or the
  music's own end if it runs short; no looping).
- **Speed-ramps.** A `speed` span becomes its own segment with a `<timeMap>`
  retime (output time = source ÷ factor) and `adjust-volume=-96dB` (muted).
  Retimed segments hard-cut in/out (the handle math is 1:1), so they don't take
  dissolves.
- **Chapters.** On a clip's first kept segment, a label change (`chapter` >
  `location` > day) emits an FCP `<chapter-marker>` and records the segment's
  output-timeline time — so the YouTube timestamps stay correct through cuts,
  dissolve handles, and speed-ramps. After the spine is laid out, YouTube's
  rules are applied (first chapter at 0:00, ≥ 10 s each) and the
  `chapters.txt` / `youtube_description.txt` files are written.
- **Timecode correctness.** DJI clips carry drop-frame embedded timecodes; each
  spine clip's `start`/`tcFormat` is set from them or FCP rejects the edit ("no
  respective media"). Media in/out is clamped to the asset's real frame range so
  ffprobe's float-duration rounding can't push a frame past the end.
- **Mixed cameras / frame rates.** The timeline adopts the dominant source
  format (by total duration); every other source keeps its native rate via its
  own `<format>` resource and FCP conforms it (e.g. a 29.97 GoPro inside a
  59.94 DJI edit). Cut points snap to each clip's own frame grid first, so
  media in/out always lands on a real source frame; a non-integer rate ratio
  (e.g. 25 in 29.97) is rounded to the nearest timeline frame with a warning.
- **Rotation.** Vertical clips are baked to a pillarboxed landscape
  `<stem>_upright.MP4` and treated as ordinary landscape — FCP's portrait+conform
  handling is unreliable.

## Files

- `holvid/config.py` — per-trip config + defaults (`Config.load`)
- `holvid/probe.py` — clip discovery, ffprobe manifest, contact sheets
- `holvid/sanitize.py` — optional: multilingual transcription + LLM detection of
  sensitive speech & arguments → `mute` spans (lazy `mlx-whisper`; stdlib Ollama
  call shared via `ollama_generate`)
- `holvid/glitch.py` — optional: ffmpeg black/freeze detection → `dead` cuts
- `holvid/pace.py` — optional: local vision model classifies boring transit →
  `speed` spans
- `holvid/chapters.py` — optional: local vision model names each clip's event →
  `chapter` labels (YouTube chapters)
- `holvid/timeline.py` — FCPXML builder (titles, dissolves, cuts, audio mutes,
  speed-ramps), rotation bake, DTD validation
- `holvid/cli.py` — `holvid <project_dir> <command>`
- `holvid.toml.example` — annotated config (real Paris values)
- `TITLES.md` — the title-sequence logic, written up

## Relation to `pt_vids` / `pt_cutlist`

`pt_vids` is a sibling tool for **personal-training** sessions: it transcribes a
mic, syncs it to the camera, and classifies speech KEEP/CUT. That's
audio-driven. `hol_vids` is **picture-driven** (contact-sheet review, no
transcription) because vacation footage has no coaching narration to cut on. The
two share the same FCPXML conventions and the contact-sheet review idea.
