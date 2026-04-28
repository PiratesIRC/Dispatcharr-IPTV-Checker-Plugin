# Release Notes - v1.26.1180040

Two new features plus two correctness fixes caught in QA review before deploy.

---

## Feature 1: Only Visible Channels Filter

### What
A new boolean setting `only_visible_channels` (default off). When enabled, the plugin filters its check scope to channels that are **visible in at least one Channel Profile** — i.e. channels with at least one `ChannelProfileMembership` row where `enabled=True`. Channels with no membership rows, or whose every membership is disabled, are skipped.

### Why
Users with archived / hidden channels were forced to either delete them outright or pay the ffprobe cost to check them every run. The Channel Profile visibility flag is the natural source of truth for "what the user actually wants to see."

### Implementation
- New helper `_get_visible_channel_ids(logger)` runs a single distinct query: `ChannelProfileMembership.objects.filter(enabled=True).values_list('channel_id', flat=True).distinct()`.
- `load_groups_action` applies the filter immediately after `_get_all_channels(...)`.
- The success message and CSV summary surface the filter so runs are auditable.
- The setting is part of the windowed-schedule fingerprint (see Feature 2): drift between windows logs a warning but honors the saved channel list.

---

## Feature 2: Windowed Schedule with Resume

### What
A new scheduling mode that lets the plugin run only during a configured time window (e.g. Sun–Thu 00:00 → 04:00 CST) and **resume from the same point next window** if it didn't finish. Halts cleanly between streams when the window closes.

### Why
Prior behavior fired a single one-shot check per cron slot. For users with thousands of channels and provider concurrency limits, a full pass could exceed any practical off-peak window. There was no built-in way to say "run for 4 hours, then stop and continue tomorrow."

### Settings
| Setting | Type | Default | Notes |
|---|---|---|---|
| `schedule_window_enabled` | boolean | `false` | Reuses existing `scheduled_times` cron as the **window start**. |
| `schedule_end_mode` | select | `duration` | `time` (HH:MM) or `duration` (hours). |
| `schedule_duration_hours` | number | `4` | Used when end_mode = duration. Decimals allowed. |
| `schedule_end_time` | string | `04:00` | Used when end_mode = time. Wraps past midnight. |

Plus a **Reset Window Progress** button to clear pending state and start fresh.

### Implementation Highlights
- `_compute_window_end(now, settings, tz)` computes the absolute end-of-window. Time mode wraps past midnight (start 22:00 / end 02:00).
- Both check loops (`_process_streams_sequential`, `_process_streams_parallel`) gain a between-stream `if self._stop_event.is_set() or self._past_window_end(): break` guard. Stops are clean — a stream that has already started finishes.
- Per-stream progress is persisted to `/data/iptv_checker_pending_resume.json` (atomic writes via existing `_save_json_file`). After each stream completes, `_mark_stream_done(stream_id)` removes its id; when the list empties, the file is deleted.
- On the next cron-fire, `_apply_pending_resume_to_loaded_channels` reads the file, intersects channel ids against the live DB (drops deleted), filters `loaded_channels.json` down to remaining streams, and re-saves. A settings fingerprint stored at seed time logs a warning on drift.
- Post-actions (rename / move / delete / webhook) only run on the window that finishes the list. While `pending_resume.json` is non-empty, `_execute_scheduled_check` exits early.
- Manual "Run Scheduled Check Now" bypasses the window — useful for one-off forced runs.
- `reset_progress_action` deletes the pending file. Wired into `action_map` and exposed as a settings button.

### Verification
- Compile: `python3 -m py_compile plugin.py` clean.
- Plugin loads in container without errors: `[IPTV Checker] Plugin v1.26.1180040 initialized` followed by `Scheduler started`.
- Both root and `iptv_checker/` subfolder copies remain byte-identical.

See `SCHEDULING_LOGIC.md` §8 for the full data-flow walkthrough.

---

## Fix 1: Restart-Resume Re-Anchored Window End

### Problem (caught by QA)
`_maybe_resume_after_restart` parsed the saved `window_end_iso` to **decide whether to resume**, then threw it away. The downstream `_setup_window_state` recomputed end as `now + duration` (or `now`-anchored HH:MM). If a 00:00 → 04:00 window restarted at 03:50 in duration mode, the resumed run extended through ~07:50 instead of stopping at 04:00.

### Solution
`_execute_scheduled_check` now accepts optional `preserved_window_end` and `preserved_window_tz` parameters. `_maybe_resume_after_restart` passes the saved values through; `_setup_window_state` is bypassed when they are present. The original window boundary is honored end-to-end.

---

## Fix 2: Same-Minute Restart Double-Fire

### Problem (caught by QA)
If the container restarted within the same cron-fire minute, two paths could race:
1. `_init_scheduler` → `_maybe_resume_after_restart` spawns a daemon thread that calls `_execute_scheduled_check`.
2. `scheduler_loop`'s first 30s tick matches the cron (its in-process `last_run` map is empty after restart), sees `check_progress.status == 'running'`, and sets `_scheduler_pending_run = True`.

After the resume thread finished, the queued path would fire a second pass — issuing a fresh `load_groups_action` against an already-completed window.

### Solution
A new `self._restart_resume_active` instance flag is set **before** the resume thread spawns and cleared in its `finally`. The scheduler_loop checks the flag before queuing a duplicate fire when `check_progress.status == 'running'`, logging `Cron fire ignored: restart-resume is in progress for this window` instead.

---

## Container Verification After Deploy
```
[IPTV Checker] Plugin v1.26.1180040 initialized
[IPTV Checker] Scheduler started. Timezone: America/Chicago, Cron expressions: ['0 23 * * 1']
```
No errors / tracebacks from existing scheduled-run handling. Existing `loaded_channels.json` and `results.json` consumed without migration.
