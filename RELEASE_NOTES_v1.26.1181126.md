# IPTV Checker v1.26.1181126 — Release Notes

> Multi-process scheduler election + scheduler reliability hardening.
> Supersedes v1.26.1181025 with seven distinct fixes uncovered during a single live testing session.

## TL;DR

Prior versions running inside Dispatcharr **silently fired every scheduled cron N times in parallel** (one fire per Python process — typically 9). The 429 rate-limit guard counter also reset across reloads, the parallel-checking mode silently fell back to sequential when the DB had no value for it, and the windowed-resume mechanism could be wedged by either a stale on-disk window_end or a stale `progress.json` from a container kill.

This release fixes all of those.

## What's fixed

### 1. Cross-process scheduler election (`SCHEDULER_LOCK_FILE`)

**Symptom:** Live test logged 9 simultaneous `⏰ SCHEDULED RUN triggered` lines for one configured cron match. Every uwsgi worker, celery worker, celery beat, and daphne process was running its own `scheduler_loop` thread.

**Root cause:** Module-level globals (`_scheduler_init_lock`, `_scheduler_initialized`) live within one Python process and don't cross process boundaries. Each Dispatcharr process imported the plugin and spawned its own scheduler.

**Fix:** New `_acquire_scheduler_lock()` writes the current PID to `/data/iptv_checker_scheduler.pid` via tmp-file + `os.rename` (POSIX atomic). After the rename, every contender re-reads the file; only the last writer's PID survives, so exactly one process wins regardless of race ordering. Stale locks (dead holder PID, detected via `os.kill(pid, 0)`) are reclaimed. `PermissionError` → treat as held; other `OSError` → skip rather than steal. See `SCHEDULING_LOGIC.md` §9.

**Verified:** Live test post-deploy showed exactly 1 `Scheduler lock acquired` line; 6 other processes correctly logged `will skip scheduler bootstrap`. Cron fired exactly once.

### 2. Singleton `RateLimitGuard`

**Symptom:** "Rate-limit guard tripped: pausing checks for 60s" warnings always showed 60s, never doubling — even though 5 trips happened in 3 seconds. The doubling logic (60→120→240→...) had no observable effect.

**Root cause:** `self._rate_limit_guard = RateLimitGuard()` was created per-Plugin-instance. Each Plugin reload had its own counter, so each fresh instance's first trip was always 60s.

**Fix:** `_RATE_LIMIT_GUARD` is now a module-level singleton, eagerly initialized at module scope right below the `RateLimitGuard` class definition (runs once under the import lock — no race surface). `Plugin.__init__` simply binds `self._rate_limit_guard = _RATE_LIMIT_GUARD`. Combined with the cross-process file lock above, only the elected process now records hits, so the cooldown counter is accurate.

**Verified:** Post-deploy trip showed `pausing checks for 120s` — the doubled cooldown is now observable.

### 3. Stale `progress.json` normalization

**Symptom:** After a container restart killed an in-flight check, every subsequent cron fire logged `Scheduled run triggered but a check is already running - queuing for later`. The scheduler was effectively wedged.

**Root cause:** Container kill bypasses the `finally` block in `_process_streams_*` that flips `check_progress['status']` to `idle`. The on-disk `/data/iptv_checker_progress.json` stays in `running`. On next startup the scheduler reads `status='running'` and self-queues forever.

**Fix:** New `_normalize_stale_progress()` runs at the top of `_init_scheduler` (before file-lock acquisition). If `status=='running'` at startup, it logs a warning and writes back `idle` + a fresh `end_time` via the atomic `_save_json_file` helper. Safe because no thread can be running at `__init__` time.

### 4. Resume-window re-anchor

**Symptom:** Pending file showed `window_end_iso: 2026-04-28T03:00 CDT` 3 hours after that time had passed, with 292 streams still flagged as remaining. A container restart mid-window would refuse to resume because `_maybe_resume_after_restart` saw the past timestamp.

**Root cause:** When a fresh cron-fire opens a NEW window AND `_apply_pending_resume_to_loaded_channels` succeeds at filtering down to remaining streams, the existing pending file's `window_end_iso` was never updated. `_mark_stream_done` only mutated `remaining_stream_ids`, preserving the stale window_end.

**Fix:** After a successful resume-load, `_apply_pending_resume_to_loaded_channels` now overwrites `pending['window_end_iso']` and `pending['tz']` with the active window. `settings_fingerprint` is intentionally **not** overwritten so drift detection still works against the original run.

**Verified:** Post-deploy, the pending file now correctly tracks the live window (e.g. `2026-04-28T07:14:14 CDT` for a 06:14 fire with default 1h duration).

### 5. Parallel-checking default fix

**Symptom:** Despite `parallel_workers=2` and `enable_parallel_checking` defaulting to `True` in `plugin.json`, a live run was sequential. Log output showed "Estimated time: ~108 min (sequential mode)".

**Root cause:** `_process_streams_background` had `settings.get("enable_parallel_checking", False)`. When the DB had no value, the `False` default won over plugin.json's `True` default.

**Fix:** Changed the dispatch default to `True`, matching plugin.json.

**Verified:** Post-deploy, runs immediately log `Starting parallel stream checking with 2 workers`.

### 6. Within-process scheduler dedup (also v1.26.1181126+)

**Symptom:** Same as #1 but within a single process — Django can construct multiple Plugin instances during plugin reload, each calling `_init_scheduler`, each starting its own thread.

**Fix:** Wrapped `_init_scheduler`'s body in a module-level `threading.Lock` + `_scheduler_initialized` flag. First instance in the process bootstraps; later instances no-op. This layer sits below the file lock and handles the per-process sub-cohort.

### 7. UI/UX polish

- **`Scheduled Check Times` field** now ends its help text with a `💾 Save Schedule` reminder.
- **`Check Scheduler` button** output is now compact (5 lines) instead of ~25 lines of dependency / thread metadata, fitting in a single Dispatcharr toast notification.
- **Cron expressions are humanized** in the status output: `0 4 * * *` → `at 4:00 AM daily`, `30 14 * * 1-5` → `at 2:30 PM on Mon–Fri`, `*/15 * * * *` → `every 15 minutes daily`. Out-of-range / malformed expressions fall back to the raw text.

## Operational notes

### File: `/data/iptv_checker_scheduler.pid`

A new persistent file. Contains a single line: the PID of the process currently hosting the scheduler. Safe to delete manually; the next process to call `_init_scheduler` will reclaim. Survives container restarts (the dead-PID check prunes it on next startup).

### Constraint: `/data` must be local

POSIX rename atomicity weakens on NFS. Standard Docker bind-mounts and named volumes are fine.

### No mid-lifetime failover

If the process holding the scheduler lock dies (e.g. SIGKILL, OOM-killer), the lock file remains with the dead PID until next container restart. Within the current container lifetime, the scheduler will simply not run. This is acceptable for Dispatcharr's deployment model where all candidate processes (uwsgi/celery/daphne) are long-lived.

## Verification checklist for upgraders

After upgrading and restarting:

```bash
# Should show ONE PID in the lock file
docker exec dispatcharr cat /data/iptv_checker_scheduler.pid

# Should show 1 "Scheduler lock acquired" + N "will skip scheduler bootstrap"
docker logs --since 1m <container> | grep -E "Scheduler lock acquired|will skip scheduler"

# Should show 1 fire per cron match (was N before)
docker logs --since 24h <container> | grep "SCHEDULED RUN triggered" | wc -l
```

If the second command shows zero "Scheduler lock acquired" lines, the elected process is hosting silently — that is also healthy. The "will skip" lines confirm the other processes saw a live holder.

## Files changed

- `plugin.py` — all seven fixes plus version bump to `1.26.1181126`
- `plugin.json` — version bump + scheduled_times help text update
- `iptv_checker/plugin.py`, `iptv_checker/plugin.json` — mirror copies (kept in sync)
- `CLAUDE.md` — architecture entries + version line
- `README.md` — features bullet for single-scheduler election + rate-limit-guard sharing note
- `SCHEDULING_LOGIC.md` — new §9 documenting cross-process election
- `RELEASE_NOTES_v1.26.1181126.md` — this file

## Coexistence with prior versions

If you're running multiple iptv_checker instances against the same Dispatcharr install (don't), only one will host the scheduler at a time — the lock is keyed to a single fixed path. The others will skip silently. This is the same property that fixes the multi-process bug; it's not a new constraint, just a documented one.
