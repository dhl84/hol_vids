---
name: plan-vacation-video
description: Turn a folder of raw family-vacation clips into a titled, review-ready Final Cut Pro timeline using the hol_vids tool. Probes and contact-sheets the footage, then READS the sheets to fill review.json (location label + dead spans per clip) — because locations are editorial knowledge the picture can't supply and only the contact sheets reveal what each clip contains — optionally transcribes the audio in any language (English/Korean/…) to detect and silence sensitive/controversial speech, then bakes any vertical clips upright and builds the FCPXML. Use when editing vacation/holiday/trip footage into a movie, when the user mentions cutting a trip video, or when they want to mute/remove sensitive audio from one.
---

# Plan & produce a vacation-video edit

Turn a folder of raw trip clips into one chronological, titled FCPXML for Final
Cut Pro with the `hol_vids` tool (`holvid/probe.py` / `timeline.py` / `cli.py`).
Pipeline: probe → contact sheets → **read the sheets to fill `review.json`** →
*(optional)* sanitize sensitive audio → bake vertical clips upright → build.
Locations and dead spans are editorial calls the audio/metadata can't make, so
the contact-sheet read is the heart of this.

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

## Phase 2.5 — (Optional) Sanitize sensitive audio

Run this when the user wants private/controversial talk silenced (medical news,
pregnancy, finances, a political argument, anything embarrassing). It is
**audio-driven and language-agnostic** — the complement to the picture-driven
review above. Skip it entirely if they don't ask; it isn't part of the default
flow.

1. **Deps + model.** Needs `mlx-whisper` + `requests` (`uv pip install
   mlx-whisper requests`) and a local Ollama server with the classifier model
   (`ollama pull qwen3.6:35b-a3b-coding-mxfp8`, or whatever `[sanitize].ollama_model`
   names). Confirm both before the slow transcription step — don't burn minutes
   transcribing to fail at classification.
2. **Run** `uv run holvid "<folder>" sanitize`. It transcribes every clip
   (`_edit/transcripts.json`, **language auto-detected per clip** — English and
   Korean are the tested baseline, ~100 others work; force one with
   `[sanitize].language` only if auto-detect mis-fires on a clip), classifies
   each line against `[sanitize].categories`, and writes padded+merged `mute`
   spans into `review.json`.
3. **Tune what counts as sensitive** in `[sanitize].categories` (it's fed to the
   LLM verbatim, so it works in any language). The classifier is told to
   **default to OK when unsure** — bias is to under-mute, because you still
   review. Read the printed per-clip span counts and skim the `mute` arrays.
4. **Muting keeps the picture.** Each `mute` span becomes a `<mute>` inside
   `<audio-channel-source>` on that segment — audio silenced for those seconds,
   not a frame removed. Spans landing inside `dead` (cut) footage just vanish.
   Because nothing moves, you can re-run `sanitize`/`build` freely and hand-edit
   `mute` arrays to override any call.
5. **Both flows compose.** `dead` removes footage; `mute` silences audio over
   kept footage. A clip can have both. The audio is the camera's own track
   (there are no separate lavs as in `pt_vids`), so one `<mute>` per span covers
   it — no mic-gating to worry about.

## Phase 3 — Upright, build, validate

1. `uv run holvid "<folder>" upright` — bakes `<stem>_upright.MP4` for vertical
   clips (auto-detected). Skip if none. Required before build, or build aborts
   with a clear message.
2. `uv run holvid "<folder>" build` → `_edit/<event>.fcpxml`. Read the printed
   stats (segments, dissolves, clips cut + seconds removed, review markers, muted
   spans + seconds of sensitive audio) and the `DTD valid` line. If `INVALID`,
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
