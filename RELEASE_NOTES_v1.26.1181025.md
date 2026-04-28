# Release Notes — v1.26.1181025

## Why this release

Diagnosing the overnight scheduled run on 2026-04-28 revealed that the upstream provider (`pia.cx`) was rate-limiting the checker with HTTP 429 responses after a few thousand requests. Those 429s were misclassified as `Stream Unreachable` / `Server Error`, which would have caused destructive actions (rename/move/delete) to wipe out roughly 3,000 healthy streams once the window completed. A live re-probe of 10 random "dead" URLs returned HTTP 301 — the streams were never dead. This release fixes the classification, adds adaptive backoff, fixes a missing-sleep bug in the sequential retry loop, and makes the scheduled-session CSV unconditional so there is always an audit trail before destructive steps run.

## Changes

### Rate-limit handling
- New `RateLimitGuard` class with sliding-window 429 hit counter and exponentially-doubling cooldown:
  - 5 hits in 60 s trips a 60 s cooldown.
  - Each successive trip doubles the cooldown, capped at 600 s.
  - Cooldown decays back to 60 s baseline after 5 minutes of clean operation.
- HTTP 429 / "too many requests" / "rate limit" stderr is now classified as `error_type='Rate Limited'` with `status='Skipped'`. Destructive actions (rename/move/delete) ignore Skipped, so a throttled stream is never mistaken for a dead one.
- Substring detection uses `re.search(r'\b429\b', ...)` plus phrase matching to avoid false positives on stream IDs containing the literal substring "429".
- `wait_if_throttled()` is invoked at the **top of `check_stream`** so every probe (sequential, parallel, retry) honors the cooldown without each call site needing its own hook. The cooldown loop re-reads `_cooldown_until` under lock each iteration so a fresh trip extending the cooldown is honored mid-sleep — TOCTOU-safe for parallel workers.

### Sequential retry pacing fix
- Both the inline retry path (every-4-streams) and the end-of-list final-flush retry path in `_process_streams_sequential` now `time.sleep(delay * 3)` before the retry probe — matches the parallel path's `backoff = delay * 3` and the help text in `plugin.json` ("Retry passes use 3x this value"). Prior versions ran retries with no sleep, which amplified rate-limit errors at end-of-window.

### Scheduled-session CSV is now unconditional
- The post-actions step that exports `/data/exports/iptv_checker_results_<YYYYMMDD_HHMMSS>.csv` runs at the end of every completed scheduled session, regardless of `scheduler_export_csv`. Wrapped in try/except so a CSV failure logs and the rest of the post-actions chain still executes. The CSV is the authoritative audit record when destructive actions follow.
- The `scheduler_export_csv` setting is still present in `plugin.json` but is no longer consulted for scheduled runs.

## Verification

- `python -m py_compile plugin.py` — clean.
- Code review by `code-reviewer` agent — two Major findings raised and fixed in this release (substring false-positive risk on `'429'`, TOCTOU in `wait_if_throttled`).

## Operational notes

- After upgrading, run **Reset Window Progress** before the next scheduled fire if a prior windowed run was interrupted by 429-driven failures — otherwise the resume may be working off a stale list.
- If you have many providers behind a shared `streamlink_hosts` cohort, the guard is global (not per-host); if one provider trips it, all subsequent probes pause. This is intentional for v1; per-host guarding is a future enhancement.
