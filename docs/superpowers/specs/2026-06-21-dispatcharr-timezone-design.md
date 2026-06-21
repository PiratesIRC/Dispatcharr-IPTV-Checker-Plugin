# Use Dispatcharr's Timezone Instead of the Plugin's Own — Design Spec

> Date: 2026-06-21
> Plugin: IPTV Checker (Dispatcharr) — `iptv_checker/plugin.py` + `iptv_checker/plugin.json`
> Status: Approved (design), UTC fallback confirmed
> Reference: Event-Channel-Managarr (commits e2179da / 1c89adb / bc5bc13)

## 1. Problem / Motivation

The plugin ships its own `scheduler_timezone` setting — a `select` dropdown with
416 hard-coded IANA options (~1,250 lines of `plugin.json`) defaulting to
`America/Chicago`. This duplicates a value Dispatcharr already owns (General
Settings → Time Zone) and forces users to keep two timezones in sync. The
sibling plugin Event-Channel-Managarr already migrated to sourcing Dispatcharr's
timezone; this aligns IPTV Checker with that pattern.

## 2. Goals

- **G1** — Remove the plugin's `scheduler_timezone` field; the scheduler uses
  Dispatcharr's configured timezone as the single source of truth.
- **G2** — Degrade gracefully (to `UTC`) when Dispatcharr's timezone cannot be
  read (running outside the container, DB unavailable during migration, etc.).
- **G3** — Preserve all existing scheduler/windowed-resume correctness — only the
  *source* of the timezone string changes, not how it's used.
- **G4** — Regression-tested resolver + updated docs.

## 3. Non-Goals

- No change to cron parsing, window math, election, reload-flag, or
  pending-resume mechanics beyond swapping the timezone source.
- No per-plugin override field (the user explicitly wants Dispatcharr to be the
  only source — matching the reference, which fully removed its field).
- No migration of the old saved value (stale `scheduler_timezone` left in the DB
  is simply ignored — harmless).

## 4. Design

### 4.1 Two new helpers (ported from the reference)

- **`_dispatcharr_timezone(self)`** — instance method:
  ```python
  def _dispatcharr_timezone(self):
      """Resolve the effective timezone from Dispatcharr's global setting
      (General Settings -> Time Zone, core.models.CoreSettings). Falls back to
      PluginConfig.DEFAULT_TIMEZONE ('UTC') when unreadable/invalid or when
      running outside Dispatcharr."""
      try:
          from core.models import CoreSettings
          return self._coerce_timezone(CoreSettings.get_system_time_zone())
      except Exception as e:
          LOGGER.debug(f"{LOG_PREFIX} Could not read Dispatcharr timezone, using {PluginConfig.DEFAULT_TIMEZONE}: {e}")
          return PluginConfig.DEFAULT_TIMEZONE
  ```
  Lazy `from core.models import CoreSettings` inside the function so the module
  still imports outside the container (and in tests, where the import raises and
  the fallback returns).

- **`_coerce_timezone(value)`** — pure `@staticmethod`, unit-testable:
  ```python
  @staticmethod
  def _coerce_timezone(value):
      """Return a valid IANA timezone name, or PluginConfig.DEFAULT_TIMEZONE as a
      safe fallback. Accepts None / blank / non-string / invalid -> default."""
      if not isinstance(value, str) or not value.strip():
          return PluginConfig.DEFAULT_TIMEZONE
      candidate = value.strip()
      try:
          import pytz
          pytz.timezone(candidate)
      except Exception:
          return PluginConfig.DEFAULT_TIMEZONE
      return candidate
  ```

### 4.2 Replace every call site

Every `settings.get('scheduler_timezone', PluginConfig.DEFAULT_TIMEZONE)` →
`self._dispatcharr_timezone()`. Call sites (anchor-based — re-read actual code,
line numbers shift): `_setup_window_state`, `_start_background_scheduler`, the
`scheduler_loop` reload block, `_maybe_resume_after_restart`,
`_apply_pending_resume_to_loaded_channels`, `validate_settings_action`,
`update_schedule_action`, `check_scheduler_status_action`,
`_generate_csv_header_comments`. The windowed-resume fallback
`pending.get("tz") or settings.get('scheduler_timezone', …)` →
`pending.get("tz") or self._dispatcharr_timezone()`.

`pending_resume.json` continues to persist the resolved `tz` so restart-resume
honors the original window boundary. Only the *fallback source* changes.

### 4.3 plugin.json

- Delete the entire `scheduler_timezone` `select` field (label `🌍 Scheduler
  Timezone`, default `America/Chicago`, 416 options) — the whole object.
- No new field added.

### 4.4 PluginConfig.DEFAULT_TIMEZONE

`"America/Chicago"` → `"UTC"`. Now purely the last-resort fallback.

### 4.5 UI text

`validate_settings_action`, `update_schedule_action`,
`check_scheduler_status_action` stop validating a dropdown and instead *display*
the resolved zone (e.g. "Scheduler uses Dispatcharr's timezone: `<tz>`"). The
`update_schedule_action` timezone-validation error branch is removed (the
resolver always returns a valid zone).

### 4.6 Orphan cleanup

Delete `iptv_checker/zone1970.tab` — referenced nowhere in code; it only ever
documented/sourced the now-deleted dropdown.

## 5. Components & Interfaces

| Symbol | Kind | Purpose |
|--------|------|---------|
| `_coerce_timezone(value)` | pure staticmethod | validate IANA name → default |
| `_dispatcharr_timezone()` | method | read CoreSettings → coerce → default |

Changed: all timezone call sites listed in §4.2; `PluginConfig.DEFAULT_TIMEZONE`;
`plugin.json` (field removed); `zone1970.tab` removed.

## 6. Error Handling

- `CoreSettings` import error / DB error / missing value → `_dispatcharr_timezone`
  returns `PluginConfig.DEFAULT_TIMEZONE` (`UTC`), logged at debug.
- Invalid IANA name from Dispatcharr → `_coerce_timezone` returns the default.
- Existing `pytz.timezone(...)` call sites keep their own
  `UnknownTimeZoneError → DEFAULT_TIMEZONE` guards (belt and suspenders).

## 7. Testing

New `tests/test_timezone.py` (no container needed):
- `_coerce_timezone`: valid name, blank, whitespace-only, `None`, non-string,
  invalid name → all the right result (valid passthrough vs `UTC` fallback).
- `_dispatcharr_timezone`: returns `UTC` when `core.models` is absent (the test
  harness has no such stub → ImportError path); returns the stubbed value when a
  `core.models.CoreSettings.get_system_time_zone` stub is installed; returns
  `UTC` when the stub returns an invalid/blank value.

Validation gate: `python -m py_compile iptv_checker/plugin.py && python -m pytest
tests -q && python -m ruff check . && python -c "import json,io;
json.load(io.open('iptv_checker/plugin.json', encoding='utf-8'))"`.

## 8. Behavior Change (document in release notes)

The effective scheduler timezone becomes **Dispatcharr → Settings → Time Zone**.
A user whose old plugin dropdown differed from their Dispatcharr timezone will
see scheduled run times shift to Dispatcharr's zone after upgrade. Old saved
`scheduler_timezone` values remain in the DB but are ignored. Fallback is `UTC`
only when Dispatcharr's timezone is genuinely unreadable.

## 9. Rollout

- Version bump via `python bump_version.py`.
- Release notes documenting the source change + behavior change + UTC fallback.
- README / DEVELOPMENT / CLAUDE updates. `.wolf/` per OpenWolf protocol.
