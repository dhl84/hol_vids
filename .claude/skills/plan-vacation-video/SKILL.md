---
name: plan-vacation-video
description: Turn a folder of raw family-vacation clips into a titled, review-ready Final Cut Pro timeline using the hol_vids tool. Probes and contact-sheets the footage, then READS the sheets to fill review.json (location label + dead spans per clip) — because locations are editorial knowledge the picture can't supply and only the contact sheets reveal what each clip contains. Optional local auto-analysis can additionally: mute sensitive speech AND arguments in any language (English/Korean/…), cut brief camera glitches (covered/knocked lens, frozen frames), speed up boring transit (walking/driving/eating), name YouTube chapters per event with a vision model, and derive place names from GoPro GPS (GPMF) for captions and day-divider cities. The build emits FCP chapter markers plus M:SS timestamps to paste into the YouTube description. Then it bakes any vertical clips upright and builds the FCPXML. Use when editing vacation/holiday/trip footage into a movie, when the user mentions cutting a trip video, when they want to mute/remove sensitive audio, cut camera mishaps, or speed up boring sections, or when they want YouTube chapters/timestamps for the upload.
---

# Plan & produce a vacation-video edit

Turn a folder of raw trip clips into one chronological, titled FCPXML for Final
Cut Pro with the `hol_vids` tool (`holvid/probe.py` / `timeline.py` / `cli.py`).
Pipeline: probe → contact sheets → **read the sheets to fill `review.json`** →
*(optional)* auto-analysis (sanitize / glitch / pace / chapters) → bake vertical
clips upright → build. Locations and dead spans are editorial calls the
audio/metadata can't make, so the contact-sheet read is the heart of this. The
build also writes a YouTube chapter index (`_edit/chapters.txt` +
`youtube_description.txt`) from the chapter/location labels.

## Operating principle (probe, then ask)

- **If the files can answer it, probe — don't ask.** fps, durations, timezone of
  the camera clock (from `creation_time` vs filename), which clips are vertical,
  which consecutive clips are one continuous recording: read these from the media
  and *tell* the user what you found.
- **For genuine editorial forks, lead with a recommendation.** The trip title,
  how aggressively to cut, whether to dissolve or hard-cut — recommend first.
- **Front-load decisions before the slow steps.** Contact-sheet generation and
  upright baking take minutes; settle the config first.

## Phase 1 — Locate footage & settle the project config

1. Find the footage folder (the user names it, e.g. `~/Downloads/Italy 2027`).
   Note whether it's one subfolder per day or loose files — discovery globs
   handle both.
2. Run `uv run holvid "<folder>" probe` and read `_edit/clips.json`: clip count,
   total runtime, fps, date span, embedded timecode, any vertical clips.
3. Ask the editorial questions in a **single `AskUserQuestion`**, recommendation
   first:

```
Q1 header:"Title"    "Movie title for the opening card?"
   - <propose from folder name + people>   (Recommended)
   - Just the place + year
   - No opening title

Q2 header:"Cuts"     "How aggressively should I trim?"
   - Conservative — only obvious junk (black/blurred/floor); rest as markers  (Recommended)
   - Tighter — also trim slow/repetitive spans
   - None — keep everything, just add titles

Q3 header:"Transitions" "Cross-dissolve between scenes, or hard cuts?"
   - Subtle 1s dissolves at scene/day boundaries   (Recommended)
   - Hard cuts throughout
```

4. **Timezone — probe, then confirm.** Compare each clip's filename time to its
   container `creation_time` to infer the camera's offset from local. Propose
   `offset_hours` (and any `camera_time_clips` for a different-country leg) and
   confirm — the titles render local time, so this must be right.
5. Write `holvid.toml` in the footage folder with the agreed values (start from
   `holvid.toml.example`).

## Phase 2 — Contact sheets, then READ them (non-negotiable)

`review.json` cannot be guessed from metadata. Generate the sheets and read every
one.

1. `uv run holvid "<folder>" sheets` → `_edit/sheets/` + `sheets_index.json`.
2. `uv run holvid "<folder>" review` to scaffold an empty `_edit/review.json`.
3. **Read the contact sheets** (use Read on each `_edit/sheets/*.jpg`). For each
   clip fill in:
   - `location` — a human place label ("Gare du Nord", "Montmartre by night",
     "Dinner · Pink Fizz"). This is what fires the location lower-third **on
     change**, so be consistent: same place → same exact string; a meaningfully
     different scene → a new string. Group sensibly so you don't get a title
     every clip nor one title for the whole trip.
   - `dead` — `[start_s, end_s, "reason"]` for junk spans, in **clip-local
     seconds** (a tile's time = `(sheet_idx*cols*rows + tile_idx) * interval`,
     geometry in `sheets_index.json`). Use a **cut-word** in the reason
     (`black`, `blur`, `floor`, `ground`, `lens`, `ceiling`, …) for spans to
     remove; describe anything you're unsure about plainly so it stays as a
     review marker instead.
   - `summary` — optional free-text note (not used in the XML).
4. Sanity-check the location sequence reads like a story across the days before
   building.

## Phase 2.5 — (Optional) Local auto-analysis

Four independent steps that look at the footage and write proposals into
`review.json`. Run only the ones the user asks for — none are part of the default
flow. All local; all re-runnable (mutes, speed-ramps and chapter labels never
move a frame, so you can hand-edit the JSON and rebuild). They write different
fields, so they compose freely on the same clip:

| step | writes | effect at build | needs |
|------|--------|-----------------|-------|
| `sanitize` | `mute` | silence audio, keep picture | `mlx-whisper` + text LLM |
| `glitch`   | `dead` | cut footage, dissolve over gap | ffmpeg only |
| `pace`     | `speed`| play faster + muted, keep picture | ffmpeg + vision model |
| `chapters` | `chapter` | FCP chapter markers + YouTube timestamps | ffmpeg + vision model |
| `geo` | `geo` + `location` | day-divider city; fills empty location labels | exiftool + reverse_geocoder |

**`sanitize` — mute sensitive speech AND arguments (any language).** For private/
controversial talk (medical, finances, embarrassing) **and arguments/fights**
(`detect_arguments`, on by default — e.g. a couple bickering in English or
Korean). Confirm `mlx-whisper` and the Ollama model are present *before* the slow
transcription step. Run `uv run holvid "<folder>" sanitize`: it transcribes every
clip (`_edit/transcripts.json`, **language auto-detected per clip** — English and
Korean tested, ~100 work; force with `[sanitize].language` only if auto-detect
mis-fires), classifies each line against `[sanitize].categories` + the argument
category, and writes padded+merged `mute` spans. The classifier **defaults to OK
when unsure** (bias to under-mute; you still review). Tune `categories` (fed to
the LLM verbatim, any language). Each `mute` → `<mute>` inside
`<audio-channel-source>`; the camera's own audio is the only track (no lavs like
`pt_vids`), so one mute per span covers it.

**`glitch` — cut brief camera mishaps (ffmpeg only, no model).** Run
`uv run holvid "<folder>" glitch`. ffmpeg `blackdetect`+`freezedetect` find a
covered/knocked lens (black) or a dropped camera that froze, and write **short**
`dead` cuts (only anomalies ≤ `[glitch].max_glitch_s` — a deliberate night hold
is left alone). The reason carries a cut-word so the build removes the span, and
the dissolve logic **transitions over the gap**. Skim the cuts before building;
widen `max_glitch_s`/thresholds if it misses or over-cuts.

**`pace` — speed up boring transit (local vision model).** Run
`uv run holvid "<folder>" pace` with a multimodal model pulled into Ollama
(`[pace].vision_model`, e.g. gemma4). It samples a frame every `sample_s`, asks
the model if it's skippable transit (`[pace].categories`: walking, driving/
riding, eating, queueing), merges boring runs ≥ `min_span_s`, and writes `speed`
spans (default `factor` 2×). The build plays each span faster and **muted** via a
`<timeMap>` retime — no footage removed. The model **defaults to keep-normal when
unsure**. Skim the `speed` arrays; adjust `factor`/`min_span_s`/`categories` or
hand-edit. Note: retimed segments hard-cut in/out (no dissolve), so a boring
stretch snaps to/from normal speed.

**`chapters` — name YouTube chapters (local vision model).** Run
`uv run holvid "<folder>" chapters` with a multimodal model in Ollama
(`[chapters].vision_model`). It samples `frames_per_clip` frames per clip and
asks the model for a short viewer-facing event title ("Eiffel Tower at Night"),
passing the previous clip's chapter so consecutive clips at one event share a
label (a new day always starts fresh); writes a `chapter` field per clip. Skim
the labels in `review.json` — they're plain strings, trivially hand-editable,
and an empty `chapter` just continues the previous one. **The build derives
chapters even without this step** (label preference `chapter` > `location` >
day divider), so if the user only wants YouTube timestamps from their existing
location labels, skip straight to build.

**`geo` — place names from GoPro GPS (exiftool + reverse geocoding).** Run
`uv run holvid "<folder>" geo` (needs `exiftool` on PATH + `uv pip install
reverse_geocoder`). Reads each clip's GoPro GPMF GPS track, takes the median of
the valid fixes, reverse-geocodes it, writes a `geo` field per clip and fills
**empty** `location` labels (never overwrites yours). `[geo].day_includes_city`
prefixes day dividers with the city ("Paris · …"). **Coverage is sparse by
nature** — indoor / no-fix clips get nothing and keep their visual label, so
treat GPS as fill + verification, not a replacement for the contact-sheet read.
Offline mode (default) is fully local (city/country, good for telling trip legs
apart). `[geo].online = true` is **opt-in** and **sends coordinates to
OpenStreetMap Nominatim** for landmark names — only enable with the user's
nod, and prefer offline for sensitive/residential locations (homes, a child's
nursery). The step prints a coordinate→place report; verify labels before
build. Note the older the camera, the less likely a fix: pre-Hero-5 and most
DJI action cams have no GPS at all, and even Hero 11 needs GPS enabled + an
outdoor sky view.

## Phase 3 — Upright, build, validate

1. `uv run holvid "<folder>" upright` — bakes `<stem>_upright.MP4` for vertical
   clips (auto-detected). Skip if none. Required before build, or build aborts
   with a clear message.
2. `uv run holvid "<folder>" build` → `_edit/<event>.fcpxml`. Read the printed
   stats (segments, dissolves, clips cut + seconds removed, review markers, muted
   spans, speed-ups + seconds saved, chapters) and the `DTD valid` line. If
   `INVALID`, report the xmllint message — don't ship it.
3. Tell the user to import via **File ▸ Import ▸ XML** (new project, nothing
   destructive) and to check the `REVIEW:` markers in **Timeline Index ▸ Tags**.
4. If chapter labels existed, build also wrote `_edit/chapters.txt` and
   `_edit/youtube_description.txt` — tell the user to paste the latter into the
   YouTube video description to get chapter navigation. YouTube's rules are
   pre-applied (first chapter 0:00, each ≥ 10s); if the build warned about
   fewer than 3 chapters, YouTube won't show the chapter bar — add more labels.
   The timestamps are computed from the *output* timeline, so they remain
   correct after cuts and speed-ups — but they go stale if the user re-edits
   the timeline inside FCP before exporting (rebuild or hand-fix then).

## The title-sequence logic (so you can explain/tune it)

Titles fire only on a clip's **first kept segment**, on three lanes:
opening card (first clip only), **day divider** (when the local **calendar date**
changes — survives shoots past midnight), **location lower-third** (when the
`location` label changes; a new day re-announces its first location). All render
**local** time. Full write-up in `TITLES.md`; timing/fonts/formats live in
`[titles]` of `holvid.toml`; the firing logic itself is pass 3 of
`build()` in `holvid/timeline.py`.

## Gotchas baked in from the Paris edit

- **Drop-frame embedded timecodes** (DJI): each spine clip's `start`/`tcFormat`
  must come from them or FCP rejects the edit. `parse_timecode` handles it.
- **Spaces in the folder name** ("Paris 2026"): the media URL is percent-encoded
  exactly once — never double-quote it, or FCP reports Missing File.
- **Vertical clips**: bake upright; don't rely on FCP's portrait+conform.
- **Timezone**: the camera clock is rarely local time — verify `offset_hours` or
  the on-screen times will be wrong.
- **Conservative cuts**: when unsure whether a span is junk, leave it as a
  marker, not a cut — it's easier to delete one extra shot in FCP than to notice
  a missing moment.
- **Sensitive-audio muting** uses the DTD `<mute>` element (source-media-time
  coordinate, same as the asset-clip `start`), not a clip split — picture stays
  intact. On the **first** run, confirm in FCP that a muted span is genuinely
  silent on the timeline; the spans are also listed in `review.json` for a manual
  pass if ever needed. Whisper can hallucinate text over music/noise — harmless,
  since hallucinated lines are almost never classified sensitive.
- **Speed-ramps** use a `<timeMap>` (output time = source ÷ factor) + muted via
  `adjust-volume=-96dB`. The timeMap `value` axis is clip-start-relative (0 =
  the clip's `start`). Confirm on first import that a sped span plays at the
  right rate. Retimed segments are excluded from dissolves (handle math is 1:1).
- **Glitch cuts vs `min_dead_s`**: a `dead` span whose reason has a **cut-word**
  is removed at *any* length (glitches are brief); `min_dead_s` only filters
  *ambiguous* spans down to review markers. So glitch reasons must contain a
  cut-word (the default `[glitch].reason` does — "black/blank").
- **The Ollama call is stdlib** (`sanitize.ollama_generate`), so neither
  `sanitize` nor `pace` needs `requests`. `sanitize` needs `mlx-whisper`;
  `pace` needs only ffmpeg + a multimodal model in Ollama.
