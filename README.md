# hol_vids

Turn a folder of raw vacation clips into a **titled, review-ready Final Cut Pro
timeline** — fully local, no cloud. One chronological edit across all days, with
an opening title card, a date divider for each day, a location lower-third
whenever the place changes, obvious junk auto-trimmed, the iffy bits left as
review markers, and gentle cross-dissolves between scenes.

This is the reusable generalization of the one-off Paris 2026 edit. Everything
that was hard-coded there (the trip title, timezone offset, rotated clips,
continuous-recording seams, cut-word list, …) is now a field in a per-trip
`holvid.toml`, so a new vacation is just a new config file.

> Verified: rebuilding the Paris 2026 timeline through this tool produces a
> **byte-for-byte identical** FCPXML to the hand-built original.

## Requirements

- macOS (FCPXML import; rotation bake uses VideoToolbox) — the build step itself
  is cross-platform
- `uv`, Python ≥ 3.11 (`tomllib` is stdlib) — `uv sync` / `uv pip install -e .`
- `ffmpeg` + `ffprobe` on PATH
- Final Cut Pro installed for DTD validation (skipped gracefully if absent)
- No Python deps beyond the stdlib for the core pipeline. The **optional
  `sanitize`** step (silence sensitive/controversial speech) additionally needs
  `mlx-whisper` + `requests` and a local [Ollama](https://ollama.com) server.

## The pipeline

```
probe ──> sheets ──> (you/Claude fill review.json) ──> [sanitize] ──> [upright] ──> build
```

1. **probe** — scan the footage, read each clip's wall-clock time, fps, duration
   and embedded timecode → `_edit/clips.json` (sorted chronologically).
2. **sheets** — sample a frame every few seconds and tile them into contact
   sheets → `_edit/sheets/`. These are what you (or Claude) read to know what's
   in each clip. Tile position encodes the timecode exactly (no burned-in text
   needed).
3. **review** — fill `_edit/review.json`: a `location` label and any `dead`
   spans per clip. `holvid … review` scaffolds an empty one.
4. **sanitize** *(optional)* — transcribe each clip's audio (any language —
   English, Korean, and ~100 others, auto-detected) and ask a local LLM which
   lines are sensitive/controversial. Writes `mute` spans into `review.json`;
   the build keeps the picture and **silences just those spans**. See
   [Sanitizing sensitive audio](#sanitizing-sensitive-audio).
5. **upright** — bake pillarboxed landscape copies of any vertical clips so FCP
   never has to rotate/conform. Auto-detected from rotation metadata.
6. **build** — assemble `_edit/<event>.fcpxml`: titles, dissolves, auto-cuts,
   review markers, sensitive-audio mutes. DTD-validated.

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

Individual commands: `probe`, `sheets`, `review`, `sanitize`, `upright`,
`build`, `all`.

## Sanitizing sensitive audio

Vacation clips catch unguarded talk — a relative's medical news, money, a
political argument, anything embarrassing — that shouldn't end up in the family
edit. The optional `sanitize` step finds and silences it, in **any language**:

```sh
uv pip install mlx-whisper requests        # one-time, only for this step
ollama pull qwen3.6:35b-a3b-coding-mxfp8   # or set [sanitize].ollama_model

uv run holvid "/Users/you/Downloads/Korea 2026" sanitize
uv run holvid "/Users/you/Downloads/Korea 2026" build
```

How it works:

1. **Transcribe** every clip's audio with `mlx-whisper`. The language is
   **auto-detected per clip** (English and Korean are the tested baseline; ~100
   languages work), so a mixed-language trip needs no configuration. Cached in
   `_edit/transcripts.json`.
2. **Classify** each line with a local LLM against the `[sanitize].categories`
   list (private personal info — health/pregnancy/finances/addresses —, politics
   & religion, offensive/sexual content, anything embarrassing). It judges the
   *meaning* regardless of language, and is told to **default to OK when unsure**
   (a human still reviews).
3. **Write `mute` spans** (clip-local seconds, padded + merged) into
   `review.json`. The build emits a `<mute>` inside `<audio-channel-source>` for
   each span — the **picture is untouched, only the audio is silenced** over
   those seconds. Spans inside removed (`dead`) footage simply vanish.

Tune what counts as sensitive in `[sanitize].categories`, or force a language
with `[sanitize].language = "ko"`. Because muting never moves a frame, you can
re-run `sanitize` and `build` freely; edit the `mute` arrays in `review.json` by
hand to override any call.

> First time: confirm in FCP that a muted span is actually silent on the
> timeline. The `<mute>` element is the purpose-built, DTD-valid mechanism; if a
> future FCP build ever ignores it, the spans are still listed in `review.json`
> for a manual pass.

## Configuration

Copy [`holvid.toml.example`](holvid.toml.example) into the footage folder as
`holvid.toml` and edit. Every field is optional and documented inline; the
example holds the real Paris settings as a worked reference. With no config at
all you still get a valid edit (defaults: timezone offset 0, auto-detected
rotation, generic title). Key sections:

- `title` / `event_name` — the opening card + FCP project name
- `[timezone]` — `offset_hours` to convert camera time → local for the titles,
  with a `camera_time_clips` exception list
- `[titles]` — fonts, sizes, durations, date/stamp `strftime` formats
- `[transitions]` — dissolve length, start/end fades, `continuous_seams`
  (clip pairs that are one recording split across files → hard join)
- `[cuts]` — `cut_words` (a dead span mentioning one is removed) vs `keep_words`
  / short spans → left as review markers
- `[discovery]` — file globs and the filename→datetime regex (default fits DJI
  action cams; falls back to container `creation_time`, then file mtime)

## review.json schema

```jsonc
{
  "clips": {
    "DJI_…_0007_D.MP4": {
      "location": "Gare du Nord",          // title fires when this changes
      "summary": "Family in the arrivals hall.",  // notes only; not in the XML
      "dead": [[12.0, 14.8, "near-black"]], // clip-local seconds + reason (footage cut)
      "mute": [[31.5, 35.0, "sensitive"]]  // clip-local seconds: keep picture,
                                           // silence audio (from `sanitize`)
    }
  },
  "title": "Optional movie-title override"
}
```

A `dead` span whose reason contains a **cut-word** (`black`, `blur`, `floor`, …)
is cut from the timeline; anything else (e.g. `"dim church interior"`, or a
`keep_word` match) is kept as a `REVIEW:` marker for you to judge in FCP. Spans
shorter than `min_dead_s` are ignored as noise. A `mute` span keeps the picture
and silences the audio over those clip-local seconds — written automatically by
`sanitize`, but you can add or edit them by hand.

## How it builds the timeline

- **Chronological spine.** Every clip becomes one asset; kept ranges become
  `asset-clip` segments in time order across all days.
- **Titles.** See [`TITLES.md`](TITLES.md) for the full logic — opening (once),
  day divider (on calendar-date change), location lower-third (on label change),
  all rendered in local time.
- **Dissolves.** A 1 s cross-dissolve straddles each scene boundary where both
  neighbours are long enough to lend a handle; `continuous_seams` force a hard
  join. Gentle fade up at the start, fade to black at the end.
- **Timecode correctness.** DJI clips carry drop-frame embedded timecodes; each
  spine clip's `start`/`tcFormat` is set from them or FCP rejects the edit ("no
  respective media"). Media in/out is clamped to the asset's real frame range so
  ffprobe's float-duration rounding can't push a frame past the end.
- **Rotation.** Vertical clips are baked to a pillarboxed landscape
  `<stem>_upright.MP4` and treated as ordinary landscape — FCP's portrait+conform
  handling is unreliable.

## Files

- `holvid/config.py` — per-trip config + defaults (`Config.load`)
- `holvid/probe.py` — clip discovery, ffprobe manifest, contact sheets
- `holvid/sanitize.py` — optional: multilingual transcription + LLM detection of
  sensitive speech → `mute` spans (lazy `mlx-whisper`/`requests` import)
- `holvid/timeline.py` — FCPXML builder (titles, dissolves, cuts, audio mutes),
  rotation bake, DTD validation
- `holvid/cli.py` — `holvid <project_dir> <command>`
- `holvid.toml.example` — annotated config (real Paris values)
- `TITLES.md` — the title-sequence logic, written up

## Relation to `pt_vids` / `pt_cutlist`

`pt_vids` is a sibling tool for **personal-training** sessions: it transcribes a
mic, syncs it to the camera, and classifies speech KEEP/CUT. That's
audio-driven. `hol_vids` is **picture-driven** (contact-sheet review, no
transcription) because vacation footage has no coaching narration to cut on. The
two share the same FCPXML conventions and the contact-sheet review idea.
