# Dispatcharr Timezone Source — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline). Steps use `- [ ]`.

**Goal:** Remove the plugin's `scheduler_timezone` dropdown; source the scheduler timezone from Dispatcharr (General Settings → Time Zone), falling back to `UTC`.

**Architecture:** Add a `_dispatcharr_timezone()` resolver (lazy `core.models.CoreSettings`) + pure `_coerce_timezone()` validator; replace every `settings.get('scheduler_timezone', …)` with the resolver; delete the dropdown field and the orphan `zone1970.tab`; `DEFAULT_TIMEZONE` → `UTC`.

**Tech Stack:** Python 3, Django ORM (stubbed in tests), pytz, pytest, ruff.

## Global Constraints

- No type hints in plugin code (tests exempt). No reformatting; ruff errors-only.
- Lazy `from core.models import CoreSettings` INSIDE the resolver (module must import outside the container / in tests).
- All edits are anchor-based (match code text); do not trust line numbers.
- Version bump only via `python bump_version.py`. `plugin.json` is UTF-8 (read with `encoding='utf-8'` on Windows).
- Validation gate after each task: `python -m py_compile iptv_checker/plugin.py && python -m pytest tests -q && python -m ruff check . && python -c "import json,io; json.load(io.open('iptv_checker/plugin.json', encoding='utf-8'))"`
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

**Call sites to convert (verified on this branch):** `_setup_window_state`, `_apply_pending_resume_to_loaded_channels` (saved_tz), `_maybe_resume_after_restart`, `_start_background_scheduler`, the `scheduler_loop` reload block, `validate_settings_action`, `_generate_csv_header_comments` (×2), `update_schedule_action`, `check_scheduler_status_action`.

---

### Task 1: Add the resolver + validator (with tests)

**Files:** Modify `iptv_checker/plugin.py` (insert before `def _setup_window_state`); change `DEFAULT_TIMEZONE`. Create `tests/test_timezone.py`.

- [ ] **Step 1: Write failing tests** — `tests/test_timezone.py`:

```python
"""Timezone sourced from Dispatcharr: _coerce_timezone + _dispatcharr_timezone."""
import sys
import types


def test_coerce_valid(pmod):
    assert pmod.Plugin._coerce_timezone("America/New_York") == "America/New_York"
    assert pmod.Plugin._coerce_timezone("  Europe/London  ") == "Europe/London"


def test_coerce_invalid_blank_none(pmod):
    P = pmod.Plugin
    default = pmod.PluginConfig.DEFAULT_TIMEZONE
    assert P._coerce_timezone("Not/AZone") == default
    assert P._coerce_timezone("") == default
    assert P._coerce_timezone("   ") == default
    assert P._coerce_timezone(None) == default
    assert P._coerce_timezone(12345) == default


def test_dispatcharr_timezone_fallback_when_core_absent(plugin, pmod):
    # No core.models stub installed -> lazy import raises -> fallback default.
    sys.modules.pop("core.models", None)
    assert plugin._dispatcharr_timezone() == pmod.PluginConfig.DEFAULT_TIMEZONE


def test_dispatcharr_timezone_reads_coresettings(plugin, pmod, monkeypatch):
    mod = types.ModuleType("core.models")

    class CoreSettings:
        @staticmethod
        def get_system_time_zone():
            return "Asia/Tokyo"

    mod.CoreSettings = CoreSettings
    monkeypatch.setitem(sys.modules, "core.models", mod)
    assert plugin._dispatcharr_timezone() == "Asia/Tokyo"


def test_dispatcharr_timezone_coerces_bad_value(plugin, pmod, monkeypatch):
    mod = types.ModuleType("core.models")

    class CoreSettings:
        @staticmethod
        def get_system_time_zone():
            return "garbage/zone"

    mod.CoreSettings = CoreSettings
    monkeypatch.setitem(sys.modules, "core.models", mod)
    assert plugin._dispatcharr_timezone() == pmod.PluginConfig.DEFAULT_TIMEZONE
```

- [ ] **Step 2: Run — expect fail** (`AttributeError: _coerce_timezone`):
`python -m pytest tests/test_timezone.py -q`

- [ ] **Step 3: Change the default.** `plugin.py` — `DEFAULT_TIMEZONE = "America/Chicago"` → `DEFAULT_TIMEZONE = "UTC"`.

- [ ] **Step 4: Add the helpers** — insert immediately before `def _setup_window_state(self, settings):`:

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

    def _dispatcharr_timezone(self):
        """Resolve the effective timezone from Dispatcharr's global setting
        (General Settings -> Time Zone, core.models.CoreSettings). Falls back to
        PluginConfig.DEFAULT_TIMEZONE ('UTC') when unreadable/invalid or when
        running outside Dispatcharr. Lazy import so the module loads in tests."""
        try:
            from core.models import CoreSettings
            return self._coerce_timezone(CoreSettings.get_system_time_zone())
        except Exception as e:
            LOGGER.debug(f"{LOG_PREFIX} Could not read Dispatcharr timezone, using {PluginConfig.DEFAULT_TIMEZONE}: {e}")
            return PluginConfig.DEFAULT_TIMEZONE

```

- [ ] **Step 5: Run — expect pass.** `python -m pytest tests/test_timezone.py -q`

- [ ] **Step 6: Validation + commit** (gate above).

---

### Task 2: Convert all scheduler call sites

**Files:** Modify `iptv_checker/plugin.py`.

- [ ] **Step 1: Mechanical replacements** (each anchor unique):
  - `_setup_window_state`: `tz_str = settings.get('scheduler_timezone', PluginConfig.DEFAULT_TIMEZONE)` → `tz_str = self._dispatcharr_timezone()`
  - `_apply_pending_resume_to_loaded_channels`: `saved_tz = pytz.timezone(pending.get('tz') or PluginConfig.DEFAULT_TIMEZONE)` → `saved_tz = pytz.timezone(pending.get('tz') or self._dispatcharr_timezone())`
  - `_maybe_resume_after_restart`: `tz_str = pending.get("tz") or settings.get('scheduler_timezone', PluginConfig.DEFAULT_TIMEZONE)` → `tz_str = pending.get("tz") or self._dispatcharr_timezone()`
  - `_start_background_scheduler`: `tz_str = settings.get('scheduler_timezone', PluginConfig.DEFAULT_TIMEZONE)` → `tz_str = self._dispatcharr_timezone()`
  - `scheduler_loop` reload: `new_tz_str = fresh.get("scheduler_timezone", PluginConfig.DEFAULT_TIMEZONE)` → `new_tz_str = self._dispatcharr_timezone()`
  - CSV ×2: `tz {settings.get('scheduler_timezone', PluginConfig.DEFAULT_TIMEZONE)})` → `tz {self._dispatcharr_timezone()})` (both the `duration` and `until` lines)
  - `check_scheduler_status_action`: `tz_name = settings.get("scheduler_timezone", PluginConfig.DEFAULT_TIMEZONE)` → `tz_name = self._dispatcharr_timezone()`

- [ ] **Step 2: `validate_settings_action` — replace the validation block:**

Old:
```python
            # Validate timezone
            scheduler_timezone = settings.get("scheduler_timezone", PluginConfig.DEFAULT_TIMEZONE)
            if PYTZ_AVAILABLE:
                try:
                    pytz.timezone(scheduler_timezone)
                    validation_results.append(f"✅ Timezone valid: {scheduler_timezone}")
                except pytz.exceptions.UnknownTimeZoneError:
                    validation_results.append(f"❌ Unknown timezone: {scheduler_timezone}")
                    has_errors = True
            else:
                validation_results.append("⚠️ pytz not available - scheduler timezone cannot be validated")
```
New:
```python
            # Timezone comes from Dispatcharr's global setting (General Settings -> Time Zone).
            if PYTZ_AVAILABLE:
                validation_results.append(f"✅ Using Dispatcharr timezone: {self._dispatcharr_timezone()}")
            else:
                validation_results.append("⚠️ pytz not available - scheduler cannot run")
```

- [ ] **Step 3: `update_schedule_action` — three edits:**
  - `scheduler_timezone = settings.get("scheduler_timezone", PluginConfig.DEFAULT_TIMEZONE)` → `scheduler_timezone = self._dispatcharr_timezone()`
  - Replace the timezone-validation block:
    Old:
    ```python
            # Validate timezone
            if PYTZ_AVAILABLE:
                try:
                    pytz.timezone(scheduler_timezone)
                except pytz.exceptions.UnknownTimeZoneError:
                    return {
                        "status": "error",
                        "message": f"❌ Unknown timezone: {scheduler_timezone}\n\nPlease select a valid timezone from the dropdown."
                    }
            else:
                return {
                    "status": "error",
                    "message": "❌ Scheduler requires pytz library but it is not installed.\n\nPlease install pytz to use scheduling features."
                }
    ```
    New:
    ```python
            # Timezone comes from Dispatcharr's global setting; only pytz is required.
            if not PYTZ_AVAILABLE:
                return {
                    "status": "error",
                    "message": "❌ Scheduler requires pytz library but it is not installed.\n\nPlease install pytz to use scheduling features."
                }
    ```
  - `message += f"Timezone: {scheduler_timezone}\n"` → `message += f"Timezone (from Dispatcharr): {scheduler_timezone}\n"`

- [ ] **Step 4: Validation + commit.** Confirm no `scheduler_timezone` remains in plugin.py: `grep -n scheduler_timezone iptv_checker/plugin.py` (Grep tool) should return nothing.

---

### Task 3: Remove the plugin.json field

**Files:** `iptv_checker/plugin.json`. (NOTE: `iptv_checker/zone1970.tab` was already removed earlier on this branch in commit `0405745` — do NOT attempt to `git rm` it; it no longer exists.)

- [ ] **Step 1:** Delete the entire `scheduler_timezone` field object from `plugin.json` (the `select` with label `🌍 Scheduler Timezone`, default `America/Chicago`, and its full options array). It is mid-array: preceded by `scheduled_times` and followed by `schedule_window_enabled`. Remove exactly one object (and its trailing comma).
- [ ] **Step 2: Validation** — JSON parses; `scheduler_timezone` returns nothing in plugin.json; full gate green. Commit.

---

### Task 4: Docs, version, .wolf

- [ ] Dry-run: a throwaway script that imports the plugin via stubs, monkeypatches a fake `core.models.CoreSettings`, and prints `_dispatcharr_timezone()` for valid/invalid/missing — confirm UTC fallback. Delete the script (don't commit).
- [ ] `python bump_version.py`.
- [ ] README: replace "timezone support" wording + the Scheduler Settings table row (remove the Scheduler Timezone row; note times use Dispatcharr's General Settings → Time Zone). DEVELOPMENT.md / CLAUDE.md: note the timezone source + UTC fallback. Create `RELEASE_NOTES_v<NEW>.md` (source change + behavior change + UTC fallback). Also note the M1 nuance: an **in-flight windowed run** resumes against the `tz` already persisted in `pending_resume.json` (written under the old default) until that pending file clears — by design, to preserve the original window boundary.
- [ ] `.wolf/` anatomy/memory/cerebrum per protocol.
- [ ] Final gate: `python -m pytest tests -q && python -m ruff check . && python -m pytest tests/test_version_sync.py -q`. Commit.

## Self-Review

- G1 (remove field) → Task 3. G2 (UTC fallback) → Task 1 (`_coerce_timezone`/`_dispatcharr_timezone` + DEFAULT_TIMEZONE='UTC'). G3 (preserve scheduler/window correctness) → Task 2 swaps only the source; pending_resume `tz` persistence untouched; existing `pytz` guards kept. G4 (tests+docs) → Tasks 1 & 4.
- Placeholder scan: none. Type/name consistency: `_dispatcharr_timezone` / `_coerce_timezone` used identically across tasks.
- Completeness: Task 2 Step 4 + Task 3 Step 3 both assert zero remaining `scheduler_timezone` occurrences — the key risk (a missed call site) is gated.
