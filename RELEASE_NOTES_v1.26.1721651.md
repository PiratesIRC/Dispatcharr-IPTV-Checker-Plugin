# IPTV Checker v1.26.1721651 — Release Notes

## Scheduler now uses Dispatcharr's timezone

The plugin no longer ships its own **Scheduler Timezone** dropdown. The scheduler
reads the timezone you've already configured in **Dispatcharr → Settings →
General → Time Zone**, so there's a single source of truth and nothing to keep in
sync. This matches the Event-Channel-Managarr plugin.

### What changed

- **Removed** the `🌍 Scheduler Timezone` setting (a 416-option dropdown,
  ~1,250 lines of `plugin.json`).
- Added `_dispatcharr_timezone()` — reads `core.models.CoreSettings.get_system_time_zone()`
  and validates it; falls back to **`UTC`** only when Dispatcharr's timezone
  genuinely can't be read (e.g. during a DB migration, or running outside the
  container).
- All scheduled-check, windowed-schedule, validation, status, and CSV timestamps
  now resolve their timezone from Dispatcharr.

### ⚠️ Behavior change

The effective scheduler timezone is now **whatever is set in Dispatcharr**. If you
previously selected a plugin timezone that differed from your Dispatcharr time
zone, your scheduled run times will shift to follow Dispatcharr's zone. To keep
the old behavior, set the same zone in **Dispatcharr → Settings → General → Time
Zone**.

Any old `scheduler_timezone` value still stored in the plugin's settings is simply
ignored — no migration needed.

### Note on in-flight windowed runs

A windowed run that was mid-window across the upgrade resumes against the timezone
already saved in `/data/iptv_checker_pending_resume.json` (written under the old
default) until that pending file clears — this is by design, so the original
window boundary is honored. New windows use Dispatcharr's timezone.

## Tests

- New `tests/test_timezone.py`: `_coerce_timezone` (valid / blank / whitespace /
  `None` / non-string / invalid) and `_dispatcharr_timezone` (reads CoreSettings,
  coerces bad values, and falls back to `UTC` when `core.models` is absent or the
  accessor raises). Full suite: **103 passing**, ruff clean.

## Upgrade notes

No migration. Set your timezone in Dispatcharr's General Settings if you haven't
already. Deploy both `plugin.py` and `plugin.json` from the `iptv_checker/` folder
(hot-reload fires on `plugin.json` mtime), then restart the container.
