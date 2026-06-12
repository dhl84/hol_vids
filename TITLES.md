# Title-sequence logic

This is the reasoning behind *how the editor decides where titles go* and *what
each says* — extracted from the one-off Paris 2026 edit and now generalized in
`holvid/timeline.py` (`build()`, pass 3) and `holvid/config.py` (`Titles`).

There are **three kinds of title**, stacked on three lanes above the picture.
They are emitted **only on the first kept segment of a clip** (`clip_first`) — a
clip that gets split by an internal cut still only considers titles at its head,
so a title never re-fires mid-clip.

| lane | title | trigger | text | position | default font |
|------|-------|---------|------|----------|--------------|
| 3 | **Opening** | first clip of the whole edit only | `cfg.title` (or `review.json["title"]`) | centre | 84 |
| 2 | **Day divider** | the clip's **calendar date** differs from the previous title's | `local_dt.strftime(date_format)` → "Thursday 28 May 2026" | centre | 96 |
| 1 | **Location** | the clip's **location label** differs from the previous one | `"<place>\n<stamp>"` → "Gare du Nord\n28 May · 18:48" | lower third (`location_y = -360`) | 60 |
| 3 | **Closing** | last segment of the edit (emitted on any segment, not just a clip's first) | `closing_text`, or auto: the trip's date range → "28 May – 4 June 2026" | centre | 72 |

Every title **fades in and out** over `[titles].fade_s` (default 0.5 s, capped
at a third of the title's duration) rather than popping on/off — an
`adjust-blend` fade on the title itself, no extra effects. Set `fade_s = 0` for
the old hard on/off.

The **closing card** sits over the final fade-to-black and ends with the
picture. Its auto text elides the shared month/year from the first date
("3 – 9 August 2026", "28 May – 4 June 2026"); set `closing_text` to override
or `closing_s = 0` to disable. It is skipped on a movie too short to give it
clear air after the opening (both live on lane 3).

## Why each trigger is what it is

- **Opening — once.** It's the movie's title card. Fires on `is_first` only, and
  the day/location titles that also land on clip 1 are pushed *after* it
  (`day_off = vin + opening_s`) so they don't overlap the opener.

- **Day divider — by calendar date, not by folder.** The trigger is
  `clip["datetime"][:10]` (the wall-clock **local** day), compared to the
  previous divider. This matters because a single evening's shooting often
  spills past midnight into the next file-system folder, and a download can put
  one day's clips across two folders. Grouping on the actual local date keeps
  "one divider per real day" regardless of how the files are foldered. When the
  day changes we also reset the remembered location (`prev_loc = None`) so the
  **first place of a new day is always re-announced**, even if you happen to wake
  up in the same neighbourhood you went to sleep in.

- **Location — on change of a human/AI label.** The picture can't tell you
  "Gare du Nord"; that's editorial knowledge. So `location` comes from
  `review.json` (filled by reading the contact sheets). A new lower-third fires
  only when the label *changes*, so a run of ten clips at the same place gets one
  title, not ten. The label is free text — `"Montmartre"`, `"Montmartre by
  night"`, `"Dinner · Pink Fizz"` are all distinct and each re-fires.

## Local time is the source of truth for the text

Both the day divider and the location stamp render **local** wall-clock time, not
the camera clock. The camera's filename/timecode is in whatever zone the camera
was set to; `cfg.timezone.offset_hours` is added to get local time, with
`camera_time_clips` as a per-clip exception list (e.g. the leg shot in a
different country before you travelled). See `local_dt()` in `timeline.py`. Get
this wrong and a 18:48 dinner reads as 17:48 on screen.

## Styling

All titles use the built-in **Basic Title** Motion template (`BASIC_TITLE_UID`)
so FCP resolves them on import with no external assets. Style (font, size,
colour, drop shadow, alignment) is set per title in `_title()`; sizes and the
lower-third Y are config (`[titles]`). Durations are config too
(`opening_s` / `day_title_s` / `location_title_s`, default 5/4/4 s).

## To change the behaviour

- **Restyle / retime:** edit `[titles]` in the project's `holvid.toml`.
- **Different date wording:** `date_format` / `location_stamp_format` (strftime;
  `%-d` = no leading zero on macOS/Linux).
- **Change *when* titles fire** (the logic itself): edit pass 3 of
  `build()` in `holvid/timeline.py` — e.g. to add a per-day *closing* card, or to
  fire a location title on a time gap rather than a label change.
