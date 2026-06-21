# Black→Blank GUI Rename + Settings Reorganization — Design Spec

> Date: 2026-06-21
> Plugin: IPTV Checker (Dispatcharr) — `iptv_checker/plugin.json` (+ minor `plugin.py` doc/text, docs)
> Status: Approved (design QA-reviewed twice; APPROVED)

## 1. Problem / Motivation

(1) The GUI uses "Black Screen" terminology; the user wants "Blank Screen" everywhere
the user reads it in settings/actions. (2) The settings tab (~40 fields, 11 sections)
has grown unwieldy: the two ⬛ sections are split apart, the Restore info section is
orphaned mid-list, and the Scheduling section is a wall of toggles.

## 2. Goals

- **G1** — Rename GUI vocabulary "Black Screen"/"Black-Screen"/"black screen" →
  "Blank Screen"/"Blank-Screen"/"blank screen" in all user-facing settings/action text.
- **G2** — Reorganize the `fields` array into a clear lifecycle order.
- **G3** — Zero functional change: keep all ids, `error_type='Black Screen'`, the
  `move_black_screen_group` default `"Black Screens"`, and detection logic stable.
- **G4** — A guard test + green suite + deploy.

## 3. Non-Goals / Keep-Stable (QA-confirmed)

- **All field/action `id`s unchanged** (`black_screen_*`, `move_black_screen_group`,
  `rename_black_screen_channels`, `move_black_screen_channels`, scheduler toggles, …) —
  the DB keys settings by id; changing them drops saved values.
- **`error_type='Black Screen'` unchanged** in `plugin.py` (set ~L3721, consumed by
  `_is_black_screen`/`_is_dead_nonblack`). Results-table/CSV continue to show
  "Black Screen" — an intentional, contained trade-off (documented, see §6).
- **`move_black_screen_group` default `"Black Screens"` unchanged** in plugin.json AND
  its matching `.get(..., "Black Screens")` code fallbacks — avoids orphaning an
  already-created group / default drift.
- Detection logic, the `[Blank]` rename tag (already "Blank"), action dispatch.

## 4. Part 1 — Rename (GUI vocabulary)

In `plugin.json`, change the black-screen wording in field `label`s, section
`description`s, action `label`/`button_label`/`confirm.message`, and `help_text` —
with these carve-outs:

- **R1 carve-out:** `black_screen_detection.help_text` keeps the literal `Black Screen`
  token where it documents the error_type value (reword cleanly, e.g. "…marked Dead
  and reported under the `Black Screen` error type…"). Must stay factually aligned with
  the code that writes `error_type='Black Screen'`.
- **R6a:** `_section_black.label` "⬛ Black / Blank Screen Handling" → "⬛ Blank-Screen
  Handling" (clean, not "Blank / Blank…"); description reworded cleanly.
- "Black group" category references (`_section_restore.description`,
  `scheduler_restore_channels.help_text`: "Graveyard/Slow/Black group") → reword to "the
  blank-screen group" (conceptual category; the literal default group is still
  "Black Screens", which users may rename).

Action buttons `⬛ Rename Black` → `⬛ Rename Blank`, `⬛ Move Black` → `⬛ Move Blank`.

## 5. Part 2 — Reorganize (field reorder + `info` dividers only)

New `fields` order (no ids added/removed except the new umbrella divider
`_section_post_check`; no logic touched):

```
📥 Group Selection         group_names, group_names_exclude, check_alternative_streams, only_visible_channels
🔎 Check Behavior          timeout, probe_timeout, dead_connection_retries, enable_parallel_checking,
                           parallel_workers, stream_check_delay
⬛ Blank-Screen Detection   black_screen_detection, black_screen_sample_seconds,
                           black_screen_min_black_seconds, black_screen_ffmpeg_timeout
🏷️ Post-Check Actions       (_section_post_check umbrella), then contiguous:
   💀 Dead                 dead_rename_format, move_to_group_name
   ⬛ Blank                 black_screen_rename_format, move_black_screen_group
   🐌 Low Framerate        low_framerate_rename_format, move_low_framerate_group
   🎬 Format Suffixes      video_format_suffixes
   ♻️ Restore              (info, end of block)
🔗 Webhook                 webhook_url
🚨 Scheduling & Automation scheduled_times, schedule_window_enabled, schedule_end_mode,
                           schedule_duration_hours, schedule_end_time,
   ⚙️ Auto-run after checks (sub-divider): scheduler_export_csv, scheduler_restore_channels,
                           scheduler_rename_dead_channels, scheduler_rename_black_screen_channels,
                           scheduler_rename_low_framerate_channels, scheduler_add_video_format_suffix,
                           scheduler_move_dead_channels, scheduler_move_black_screen_channels,
                           scheduler_move_low_framerate_channels, scheduler_delete_dead_channels,
                           auto_delete_confirmation, scheduler_fire_webhook
🔧 Advanced                ffprobe_flags, ffprobe_analysis_duration, streamlink_hosts,
                           ffprobe_path, ffmpeg_path
```

Constraints (QA-verified positional safety):
- `scheduled_times` stays BEFORE `schedule_window_enabled` ("Scheduled Times above").
- Webhook section stays BEFORE Scheduling ("Webhook URL above").
- **R3d fix:** `schedule_end_time.help_text` "…in the Scheduler Timezone above…" →
  "…in Dispatcharr's timezone…" (that field was removed in the timezone feature).
- Don't normalize emoji escaping — keep each line's existing raw/`\uXXXX` form.
- `auto_delete_confirmation` wording stays generic (gates both manual + scheduled delete).

Implementation note: rebuild the `fields` array wholesale in the new order (a reorder is
error-prone via many small edits). Then verify the frozen id-set is unchanged.

## 6. Documentation

CLAUDE.md/README/release notes: note that `error_type='Black Screen'` and the
"Black Screens" group default are intentionally retained, so the results-table/CSV
terminology divergence is a known, contained trade-off (not a missed rename).

## 7. Testing

New `tests/test_settings_schema.py` guard:
- **(a)** the exact set of field `id`s and action `id`s equals a frozen expected set
  (catches drop/rename/dup, incl. the new `_section_post_check`).
- **(b)** no "Black Screen"/"Black-Screen" substring in any field `label`, section
  `description`, action `label`/`button_label`/`confirm.message`. `help_text` is
  EXCLUDED (the L129 error_type reference is legitimate).
- JSON validity; existing 134-test suite stays green (cosmetic change).

Gate: `python -m py_compile iptv_checker/plugin.py && python -m pytest tests -q &&
python -m ruff check . && python -c "import json,io; json.load(io.open('iptv_checker/plugin.json', encoding='utf-8'))"`.

## 8. Rollout

Version bump via `python bump_version.py`; release notes; README; `.wolf/`; deploy to
container (verify version + the renamed labels render + no errors).
