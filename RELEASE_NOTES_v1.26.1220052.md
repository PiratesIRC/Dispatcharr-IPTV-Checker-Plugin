# IPTV Checker v1.26.1220052 — Release Notes

> Three-fix consolidated release surfacing from a single morning of investigation:
> 1. video bitrate is now actually captured and stored,
> 2. windowed runs no longer overshoot `window_end` via the retry pass,
> 3. partial-window CSV export was regressed by a subfolder/root sync drift and is restored.
>
> Versions covered: **v1.26.1211342** (ffprobe flags), **v1.26.1212238** (window-aware retry + CSV-hoist re-apply), **v1.26.1220052** (round bitrate to int).

## TL;DR

The Apr 30 → May 1 overnight cron fired **0 of expected 1** times because the cron expression `0 0 * * 0-4` (Sun–Thu) excludes Friday. That's working as designed — but it freed up the May 1 morning to investigate why no scheduled runs since Apr 27 had populated `video_bitrate` in Dispatcharr's channel-menu UI. Three distinct bugs surfaced; all three are fixed in this release.

The earlier May 1 07:00 "test" run produced a CSV but had `video_bitrate=null` for all 439 alive streams. The 09:10 run produced `video_bitrate` for 450 of 451 alive streams **but no CSV** because the CSV-hoist had silently regressed when re-deploying from the subfolder copy of `plugin.py`. The 18:20 run was killed at 18:50:47 by an unidentified internal process restart (not me, not OOM, not the docker daemon — likely a uwsgi-internal reload), so it did not validate the window-aware retry fix end-to-end. The 23:59 → 03:59 window will be the first verification.

## What's fixed

### 1. Video bitrate now captured (v1.26.1211342)

**Symptom:** 0 of 439 alive streams in a scheduled-run snapshot had `video_bitrate` populated. `dispatcharr_channels_stream.stream_stats` had `audio_bitrate` for 3,747 of 4,038 stat-bearing rows but `video_bitrate` for only 2.

**Root cause:** Live MPEG-TS / HLS streams almost never expose `bit_rate` at the per-stream OR `format` level — confirmed by direct ffprobe against the provider. The plugin has a packet-based fallback that sums `packets[].size / packets[].duration_time` for the video stream, but `ffprobe_data` was empty `{}` on every alive result, so the fallback never ran.

When `-show_frames` AND `-show_packets` are both passed to ffprobe, the JSON output emits a single combined `packets_and_frames` array instead of separate top-level `packets[]` and `frames[]` keys. The plugin's parser only checks `probe_data.get('packets')` and `probe_data.get('frames')`, both of which return `None` against the combined output, so `ffprobe_extra_data` stays `{}` and no bitrate calc fires.

**Fix:** Default `ffprobe_flags` changed from `'-show_streams,-show_frames,-show_packets,-loglevel error'` to `'-show_streams,-show_packets,-loglevel error'`. Without `-show_frames`, ffprobe emits `packets[]` as a top-level key and the existing parser path works as designed. `plugin.json` `help_text` and `placeholder` updated to warn against re-adding `-show_frames`.

**Verified:** post-fix run at 09:10 CDT — 450 of 451 alive streams had `video_bitrate` populated, all 451 had `packet_count`. Sample values: FXX 3,983 kbps · National Geographic 3,047–4,567 kbps. DB query confirmed 364 of 427 stream rows updated within the run window had `video_bitrate` in `stream_stats`.

### 2. Video bitrate rounded to whole kbps (v1.26.1220052)

The packet-based calc emits floats like `4849.984` kbps. Dispatcharr's channel-menu UI displays bitrate as an integer, so the fractional precision was just adding noise to `stream_stats` jsonb and CSV exports. Now `int(round(video_bitrate))` is applied in `check_stream` immediately after the bitrate is resolved (whether from per-stream `bit_rate`, `format.bit_rate`, or the packet sum), before the metadata dict is built. Existing rows keep their floats until the next probe overwrites them.

### 3. Window-end aware retry pass (v1.26.1212238)

**Symptom:** May 1 09:10–10:24 scheduled run logged `⏰ WINDOW: end-of-window reached — cancelling remaining stream checks` at 10:10:10 (correctly, on schedule), then **continued running for 14 minutes** before logging `[Stream Check (Parallel)] Complete: 526/1789 in 1h 14m`. The window was nominally 60 minutes; the actual elapsed time was 74.

**Root cause:** When `_past_window_end()` triggers in the main parallel `for future in as_completed(...)` loop, the loop breaks correctly. Execution then **falls through** to the retry-pass section in `_process_streams_parallel` (the `if retries > 0:` block at ~line 1940). That section only checks `self._stop_event.is_set()` — it has no `_past_window_end()` check. With `dead_connection_retries=3` and `stream_check_delay=3` defaults, that's up to 3 retry passes × (9s backoff + parallel re-probe of every retryable error) running well past the window boundary.

**Fix:** Added `if self._past_window_end(): break` at two sites in the parallel retry-pass section — one at the top of the `for retry_pass in range(retries):` loop (skips remaining retry passes once the window closes), one inside the per-pass `as_completed` consumer (cancels in-flight retry probes when window-end is detected mid-pass). The sequential `_process_streams_sequential` final-flush retry while-loop got an equivalent guard so it can't overshoot either. Logs distinguish the new exit paths:
- `⏰ WINDOW: end-of-window reached — skipping remaining retry passes` (between passes)
- `⏰ WINDOW: end-of-window reached — cancelling in-flight retry probes` (mid-pass)
- `⏰ WINDOW: end-of-window reached — abandoning final-flush retries` (sequential)

Retryable streams not retried in this window roll into the next window's resume — same lifecycle as rate-limited streams.

### 4. CSV-per-window hoist regression — re-applied (v1.26.1212238)

**Symptom:** May 1 09:10 run probed 526 streams, populated `video_bitrate` correctly, but `/data/exports/` had no new CSV. Logs showed `⏰ WINDOW: closed mid-list — post-actions deferred to next window` followed by `_execute_scheduled_check` returning, with no `Exporting results to CSV` line.

**Root cause:** This repo has long-standing subfolder/root drift (two copies of `plugin.py` — `iptv_checker/plugin.py` is canonical and deployed; `plugin.py` at repo root is a duplicate that `bump_version.py` writes to). The v1.26.1191257 CSV-hoist fix was applied to the **root** copy only. The subfolder copy had been pinned at v1.26.1181126 the entire time (its `version =` line never advanced). When v1.26.1211342's ffprobe fix was deployed from the subfolder copy on May 1, the deploy regressed the CSV-hoist along with it.

**Fix:** CSV export block moved above the `is_window and self._has_pending_resume(): return` gate in `_execute_scheduled_check` — same logical fix as v1.26.1191257, this time applied directly to the subfolder copy that actually ships. Both copies are now at the same version and will stay in sync going forward.

## Related setting

- `ffprobe_flags` default now `-show_streams,-show_packets,-loglevel error`. If you've customized this in the UI, **remove `-show_frames`** — adding it back will silently break video-bitrate capture again. The `help_text` and `placeholder` in plugin.json have been updated to call this out.

## How to verify

After the next scheduled fire (or a manual Run Now):

- Probed alive streams have `video_bitrate` as an integer (no fractional part) in both `/data/iptv_checker_results.json` and Dispatcharr's `stream_stats` jsonb.
- The channel-menu UI in Dispatcharr displays the bitrate.
- A windowed run that closes mid-list logs `Exporting results to CSV...` → `Results exported to /data/exports/iptv_checker_results_<timestamp>.csv` → `WINDOW: closed mid-list — post-actions deferred to next window`, in that order. CSV file timestamp is within seconds of the window-close log line.
- If retryable errors exist near window-end, you should see one of: `skipping remaining retry passes`, `cancelling in-flight retry probes`, or `abandoning final-flush retries` — and the next "Stream Check Complete" log line within ~30s of the window-end log line, not 14+ minutes later.

## Files changed

- `iptv_checker/plugin.py` — `check_stream` (ffprobe_flags default updated; `int(round(video_bitrate))` before metadata build); `_process_streams_parallel` (two `_past_window_end()` checks added in the retry-pass section); `_process_streams_sequential` (one `_past_window_end()` check in the final-flush while-loop); `_execute_scheduled_check` (CSV step hoisted above post-actions gate).
- `iptv_checker/plugin.json` — version bump; `ffprobe_flags` default + `help_text` + `placeholder` updated.
- `plugin.py`, `plugin.json` (root copies) — synced.
- `CLAUDE.md`, `README.md`, `SCHEDULING_LOGIC.md` — documentation updates.

## Compatibility

- No data file format changes.
- Existing `stream_stats.video_bitrate` floats are not migrated; they'll be overwritten with integers on the next probe of each stream.
- If a user has manually set `ffprobe_flags` in the UI, the new code-side default does not touch that value; their setting wins. They must remove `-show_frames` themselves to benefit from the bitrate fix.
- The window-end guards add new log lines only when triggered. No setting changes.

## Open thread (carried into the 23:59 → 03:59 window)

The May 1 18:20 run was killed at 18:50:47 by an internal process restart of the dispatcharr container's Python tree (uwsgi master + celery + daphne all respawned with new PIDs, but `docker inspect` showed `RestartCount=0`, `OOMKilled=false`, `StartedAt=18:22:51` — the container itself never restarted). This kept the v1.26.1212238 fixes from being end-to-end validated. The 23:59 fire on v1.26.1220052 is the next opportunity. If the same restart pattern recurs near the 03:59 window-close, it warrants a separate investigation — possibly a uwsgi internal reload signal, Docker Desktop process management, or a Dispatcharr health-check.
