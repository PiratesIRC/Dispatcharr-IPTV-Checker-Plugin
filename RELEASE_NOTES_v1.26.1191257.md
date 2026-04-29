# IPTV Checker v1.26.1191257 — Release Notes

> Module-reload duplicate-thread fix and CSV-on-every-window-close.
> Supersedes v1.26.1181126; surfaced from a single overnight scheduled run that exposed two distinct bugs the v1.26.1181126 cross-process election did not cover.

## TL;DR

Last night's run revealed that the cross-process scheduler election shipped in v1.26.1181126 — while correct across processes — could be defeated *within* the elected process by Django/uwsgi re-importing the plugin module. One PID logged `Scheduler lock acquired by PID 223` three separate times in under a minute and ran ≥5 windowed checks in parallel. The provider rate-limited heavily: 444 of 775 streams in one window were skipped to HTTP 429.

A second issue was that windowed runs that closed mid-list produced **no CSV at all**, because the unconditional CSV step shipped in v1.26.1181025 was placed *after* the post-actions gate. Each new window then overwrote `results.json` with its own slice, destroying the prior window's evidence.

This release fixes both.

## What's fixed

### 1. CSV emitted on every window close (`_execute_scheduled_check`)

**Symptom:** `/data/exports/` had no `iptv_checker_results_*.csv` after a 4-hour scheduled window that scanned 775 streams. Logs showed `⏰ WINDOW: closed mid-list — post-actions deferred to next window` followed by an immediate return — the CSV step was below the gate.

**Fix:** CSV export hoisted above the post-actions gate. Each window — partial or complete — writes its own `iptv_checker_results_<timestamp>.csv` before the gate runs. Destructive post-actions (rename / move / delete / webhook) still defer to the window that drains the list. CSV failures are caught and logged; they don't abort subsequent post-actions on a complete-window run.

### 2. Module-reload duplicate scheduler threads

**Symptom:** PID 223 logged `Scheduler lock acquired by PID 223` three times at 00:51:14, 00:52:09, 00:52:09. Three `Background scheduler thread started` log lines for the same PID. Five `[Stream Check (Parallel)] Complete: NNN/6542` events fired between 11:01–11:24 UTC with different counts (727, 725, 763, 773, 775). The provider rate-limited the burst.

**Root cause:** Django/uwsgi can re-import `plugin.py` within the elected process, creating a fresh module-instance with new `_scheduler_initialized = False`, new `_bg_scheduler_thread = None`, and new `_scheduler_stop_event = threading.Event()`. `_acquire_scheduler_lock` saw its own PID in the lock file, fell through the `holder_pid != my_pid` check, "re-acquired" against itself, and spawned another thread. The orphan from the prior module-instance was still alive but waiting on a different `Event` than the new module's `_stop_background_scheduler` would signal.

**Fix:** Two protections, working together.

a. **Live-thread short-circuit.** `_init_scheduler` enumerates `threading.enumerate()` for an `iptv-checker-scheduler` thread that `is_alive()`. If found, sets `_scheduler_initialized = True`, repoints `_bg_scheduler_thread = t`, and returns without acquiring the file lock or spawning anything. `_start_background_scheduler` does the same recovery before its own start path so user-initiated restarts (e.g. settings save) work correctly across reloads.

b. **Stop-event recovery via thread attribute.** When the scheduler thread is started, the current module's `_scheduler_stop_event` is attached as `thread._iptv_stop_event`. The short-circuit recovers it (`getattr(t, "_iptv_stop_event", None)`) and rebinds the new module's `_scheduler_stop_event` global to the same `Event` object the orphan is `.wait()`ing on. After this, `_stop_background_scheduler.set()` actually reaches the orphan and the `.join()` confirms exit before clearing.

### 3. `_scheduler_initialized` semantics restored

A first-pass attempt at this fix set the flag up-front on every code path. Code review caught that this lost the original behavior where an elected-but-init-crashed process could retry on a later `Plugin()` construction. Reverted to: set the flag on lost-election (intentional no-failover, per CLAUDE.md) and on success-path-end. On bootstrap exception within the elected process, the flag stays `False` so a later `Plugin()` retries.

## How to verify

After deploying, watch the logs through the next scheduled fire:

- Exactly one `Scheduler lock acquired by PID N` per container lifetime.
- Subsequent module reloads in PID N log `Scheduler already running in this process (thread id …); skipping bootstrap.`
- Other processes log `Scheduler already owned by PID N; this process (M) will skip scheduler bootstrap.`
- Exactly one `[Stream Check (Parallel)] Complete: …` per window.
- `/data/exports/iptv_checker_results_*.csv` written on every window, including the partial windows that log `⏰ WINDOW: closed mid-list — post-actions deferred to next window`.

## Files changed

- `plugin.py` — `_init_scheduler` (live-thread short-circuit + stop-event recovery), `_start_background_scheduler` (same recovery + attach stop event to spawned thread), `_execute_scheduled_check` (CSV step moved above post-actions gate).
- `plugin.json` — version bump.
- `README.md`, `SCHEDULING_LOGIC.md` — documentation updates.

## Compatibility

- No setting changes.
- No data file format changes.
- Pre-fix orphan threads from a rolling restart will not have the `_iptv_stop_event` attribute; the new code falls back gracefully (`getattr` default `None`) but cannot stop those orphans without a container restart. Transient hazard limited to one upgrade.
