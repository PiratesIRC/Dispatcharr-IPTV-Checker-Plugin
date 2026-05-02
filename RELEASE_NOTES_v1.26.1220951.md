# IPTV Checker v1.26.1220951 — Release Notes

> Single-fix release. Closes the cross-worker rogue-thread bug that allowed multiple uwsgi workers to each spawn an independent scheduler thread, producing duplicate cron fires.

## TL;DR

On 2026-05-02 at 23:59:25 CDT the scheduled cron `'59 23 * * *'` fired **twice within 27 ms**, producing two parallel 971-stream check runs and two CSV exports (`iptv_checker_results_20260502_065241.csv` and `iptv_checker_results_20260502_065702.csv`, both 971/971). Root cause: UI requests land in whatever uwsgi worker the load balancer picks, and the prior code spawned a scheduler thread in *every* worker that handled `update_schedule_action` or *any* `Plugin.run()` call. Each worker's thread independently fired the cron when the time matched.

This release moves all scheduler-thread lifecycle inside the elected scheduler-owner process. UI workers now signal config changes via a flag file that the owner consumes within 30 s.

## What was actually broken

`_bg_scheduler_thread` is a module-level global, which means it is **process-local** in Dispatcharr's multi-process layout (~9 Python processes: 4 uwsgi web workers, celery worker, celery beat, daphne ASGI, supervisors). The cross-process PID lock from v1.26.1181126 (`_acquire_scheduler_lock`) correctly prevented multiple processes from owning the scheduler at module-import time.

But two code paths bypassed that lock entirely:

1. **`Plugin.run()` line ~1119** called `self._start_background_scheduler(settings)` on **every action invocation** — including `view_progress`, `load_groups`, anything. So any plugin button click in a non-elected worker spawned a scheduler thread.

2. **`update_schedule_action`** called `self._start_background_scheduler(settings)` directly to apply UI schedule changes. Even though the user thought they were editing one schedule, each click landed in a possibly-different worker and started a fresh thread there.

In both cases, the local `_stop_background_scheduler` call inside `_start_background_scheduler` saw `_bg_scheduler_thread = None` (process-local; never set in this worker before) and no-op'd. A new thread started. Net effect: each UI interaction added one scheduler thread to whichever worker handled the request.

### The smoking gun

```
2026-05-02 00:50:09,708 Scheduler started. Cron: ['59 23 * * *']
2026-05-02 00:50:15,405 Scheduler started. Cron: ['59 23 * * *']
2026-05-02 00:50:15,412 Scheduler started. Cron: ['59 23 * * *']
2026-05-02 00:53:19,396 Scheduler started. Cron: ['59 23 * * *']
2026-05-02 00:53:24,493 Scheduler started. Cron: ['59 23 * * *']
2026-05-02 00:53:24,515 Scheduler started. Cron: ['59 23 * * *']
```

Six "Scheduler started" lines for the same cron in three minutes — **with zero corresponding "Stopping scheduler thread..." log lines** between them. Each came from a different worker process whose `_stop_background_scheduler` saw a `None` thread global and skipped the stop branch.

Each thread polls every 30 s with its own `last_run = {}` dict (closure-local, so it can't dedupe across threads). Two of the six threads happened to sample within the 23:59 minute window:

```
2026-05-02 04:59:25,372 ⏰ SCHEDULED RUN triggered at 2026-05-01 23:59:25 for cron: 59 23 * * *
2026-05-02 04:59:25,399 ⏰ SCHEDULED RUN triggered at 2026-05-01 23:59:25 for cron: 59 23 * * *
```

Both ran 971-stream parallel check loops; both produced a CSV.

## What's fixed

### 1. `Plugin.run()` no longer touches the scheduler

The unconditional `self._start_background_scheduler(settings)` call at the top of `run()` was removed and replaced with a comment. Scheduler bootstrap is solely the responsibility of `_init_scheduler` (called from `Plugin.__init__` once per process), which honors the cross-process PID lock. Non-owner processes simply don't run a scheduler thread.

### 2. Reload-flag pattern for UI-driven schedule changes

Two new helpers:

- `_owns_scheduler_lock()` — read-only check: is `os.getpid()` the PID written in `SCHEDULER_LOCK_FILE`? Used by UI code paths to decide whether they can touch the thread directly.
- `_request_scheduler_reload()` — writes a tiny timestamp to a new `SCHEDULER_RELOAD_FLAG = /data/iptv_checker_scheduler_reload.flag` file. Tells the elected process "re-read settings from DB."

`update_schedule_action` now branches:

- **Owner process**: same in-process restart as before (works because the owner's `_bg_scheduler_thread` global is correctly populated, so `_stop_background_scheduler` actually stops the old loop).
- **Non-owner process**: writes the reload flag and returns success. Schedule takes effect within ≤30 s (one `SCHEDULER_CHECK_INTERVAL`).

The "schedule cleared" branch always uses the reload flag — even on the owner — because tearing the loop down would leave no consumer for any future re-add flag. Empty cron just makes the loop's `for cron_expr in scheduled_times:` body a no-op each iteration; the thread stays alive and idle.

### 3. Scheduler loop reads its own config dynamically

The `scheduler_loop` closure inside `_start_background_scheduler` previously captured `scheduled_times` / `tz_str` / `local_tz` once at start time. It now polls `SCHEDULER_RELOAD_FLAG` at the top of each iteration; on detection, it deletes the flag, re-reads settings via `self._fresh_settings(settings)`, parses the new cron expressions and timezone, and reassigns the (now `nonlocal`-declared) closure variables. `last_run` resets so the new schedule isn't suppressed by a stale match record.

## How to verify

After deploy + container restart, expect exactly one "Scheduler started" line per cron change, exactly one "SCHEDULED RUN triggered" line per fire, and a `/data/iptv_checker_scheduler_reload.flag` file that appears briefly when a non-owner worker handles the UI save and disappears within 30 s.

### Verified post-deploy

Test fire on 2026-05-02 05:10 CDT with `10 5 * * *`:

```
2026-05-02 10:10:05,604 ⏰ SCHEDULED RUN triggered at 2026-05-02 05:10:05 for cron: 10 5 * * *
```

Single fire (vs. two on the prior `'59 23'` run). Window closed cleanly at 05:20:00 with `[Stream Check (Parallel)] Complete: 94/6680 in 10m 8s`, CSV exported (`iptv_checker_results_20260502_102016.csv` — 76 Alive, 18 Dead, 0 Skipped), `pending_resume.json` written, and `scheduler_reload.flag` was absent throughout.

## Notes for the next maintainer

- **Reload latency is a feature, not a bug.** A UI schedule change can take up to 30 s to apply (the next `SCHEDULER_CHECK_INTERVAL` poll on the owner). Users won't notice this for cron edits, and removing the latency would require a more complex IPC mechanism (file-based watch, signal, etc.) that adds a lot of code for very little benefit.

- **`_bg_scheduler_thread` is still process-local and that's still correct.** The fix doesn't make the global cross-process — it just stops non-owner processes from manipulating it. The PID lock continues to be the source of truth for "who owns the scheduler."

- **The reload flag survives container restart harmlessly.** On startup the new owner's `_init_scheduler` calls `_start_background_scheduler` with current DB settings. The leftover flag (if any) triggers an immediate redundant reload (no-op since settings already match). The flag is then removed.

- **Subfolder/root copy drift remains.** This commit modifies `iptv_checker/plugin.py` (the canonical/deployed copy) and bumps the version string in both `iptv_checker/plugin.json` and the root `plugin.json` / `plugin.py`. The root `plugin.py` is still significantly out of sync with the subfolder — it does not yet have this fix. Don't deploy from the root copy. A future cleanup commit should either delete the root copy or sync it from the subfolder.
