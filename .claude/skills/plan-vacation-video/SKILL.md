---
name: plan-vacation-video
description: Turn a folder of raw family-vacation clips into a titled, review-ready Final Cut Pro timeline using the hol_vids tool. Probes and contact-sheets the footage, then READS the sheets to fill review.json (location label + dead spans per clip) — because locations are editorial knowledge the picture can't supply and only the contact sheets reveal what each clip contains. Optional local auto-analysis can additionally: mute sensitive speech AND arguments in any language (English/Korean/…), cut brief camera glitches (covered/knocked lens, frozen frames), and speed up boring transit (walking/driving/eating). Then it bakes any vertical clips upright and builds the FCPXML. Use when editing vacation/holiday/trip footage into a movie, when the user mentions cutting a trip video, or when they want to mute/remove sensitive audio, cut camera mishaps, or speed up boring sections.
---

# Plan & produce a vacation-video edit

Turn a folder of raw trip clips into one chronological, titled FCPXML for Final
Cut Pro with the `hol_vids` tool (`holvid/probe.py` / `timeline.py` / `cli.py`).
Pipeline: probe → contact sheets → **read the sheets to fill `review.json`** →
*(optional)* auto-analysis (sanitize / glitch / pace) → bake vertical clips
upright → build. Locations and dead spans are editorial calls the audio/metadata
can't make, so the contact-sheet read is the heart of this.

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

Three independent steps that look at the footage and write proposals into
`review.json`. Run only the ones the user asks for — none are part of the default
flow. All local; all re-runnable (mutes and speed-ramps never move a frame, so
you can hand-edit the JSON and rebuild). They write different fields, so they
compose freely on the same clip:

| step | writes | effect at build | needs |
|------|--------|-----------------|-------|
| `sanitize` | `mute` | silence audio, keep picture | `mlx-whisper` + text LLM |
| `glitch`   | `dead` | cut footage, dissolve over gap | ffmpeg only |
| `pace`     | `speed`| play faster + muted, keep picture | ffmpeg + vision model |

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

## Phase 3 — Upright, build, validate

1. `uv run holvid "<folder>" upright` — bakes `<stem>_upright.MP4` for vertical
   clips (auto-detected). Skip if none. Required before build, or build aborts
   with a clear message.
2. `uv run holvid "<folder>" build` → `_edit/<event>.fcpxml`. Read the printed
   stats (segments, dissolves, clips cut + seconds removed, review markers, muted
   spans, speed-ups + seconds saved) and the `DTD valid` line. If `INVALID`,
   report the xmllint message — don't ship it.
3. Tell the user to import via **File ▸ Import ▸ XML** (new project, nothing
   destructive) and to check the `REVIEW:` markers in **Timeline Index ▸ Tags**.

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
