# Exclude Channel Groups + Log-Line Cleanup — Design Spec

> Date: 2026-06-21
> Plugin: IPTV Checker (Dispatcharr) — `iptv_checker/plugin.py` + `iptv_checker/plugin.json`
> Status: Approved (design QA-reviewed and APPROVED)

## 1. Problem / Motivation

The plugin can only *include* groups ("Group(s) to Check", comma-separated,
wildcards). There's no way to say "check everything *except* these" or "check
`US-*` but skip `US-PPV`". Also, a benign INFO log fires on every restart when a
stale windowed-resume file is discarded — the user wants it gone.

## 2. Goals

- **G1** — A new **exclude** filter for channel groups, applied *after* the
  include filter, with the same wildcard semantics.
- **G2** — The CSV audit header reports the exclude setting.
- **G3** — Remove the benign discard log line (behavior unchanged).
- **G4** — Windowed-resume scope-drift detection accounts for the exclude filter.
- **G5** — Regression-tested; docs updated.

## 3. Decision: new text field, not a boolean

Chosen a **new text-input field** over a boolean that would invert "Group(s) to
Check". A boolean makes the existing label contradict behavior and is
all-or-nothing; a separate exclude string is clearer and **composable**
(include + exclude). Exclude is applied after include; if a group matches both,
**exclude wins**.

## 4. Architecture

### 4.1 New setting (`plugin.json`)
- `group_names_exclude` — string, default `""`. Inserted **between** `group_names`
  and `check_alternative_streams` in the Group Selection section.
- Label: `📂 Group(s) to EXCLUDE (comma-separated, wildcards supported)`.
- help_text: "Groups to skip even if they match 'Group(s) to Check'. Wildcards
  supported (e.g. `US-PPV-*`). Applied AFTER the include filter; with a blank
  include this means 'all groups except these'. If a group matches both fields,
  exclude wins. Blank = exclude nothing."

### 4.2 Shared matcher (new pure staticmethod)
```python
@staticmethod
def _match_group_names(patterns_str, all_group_names):
    """Return the set of group names matching any comma-separated pattern.
    Wildcard patterns (containing * ? [) use fnmatch.fnmatchcase (case-sensitive);
    literals use case-sensitive exact membership — symmetric with the include path."""
```
Used by the exclude step in `load_groups_action` AND the exclude validation in
`validate_settings_action`. The existing include path (per-pattern matching +
unmatched tracking + per-pattern logging) is **intentionally left as-is** to
preserve its diagnostics and limit risk; its duplication is pre-existing.

### 4.3 `load_groups_action`
- Both branches compute `target_group_names`; their empty checks change from
  `if not target_group_ids` to `if not target_group_names` (1:1 with ids via
  `group_name_to_id`, so emptiness is preserved). This keeps both existing
  distinct messages: "No groups found in Dispatcharr." (all-groups branch) and
  "No groups matched: <unmatched>" (include branch).
- The in-branch `target_group_ids` computations are removed.
- After the if/else: read `group_names_exclude`; if set,
  `excluded = self._match_group_names(exclude_str, set(group_name_to_id.keys()))`,
  `removed = target_group_names & excluded`; if `removed`, log
  `🚫 Excluding N group(s) from check: …` and `target_group_names -= excluded`;
  then if `not target_group_names` return a NEW distinct error: "All target
  groups were excluded by the 'Group(s) to Exclude' filter. Nothing to check."
- Compute `target_group_ids = {group_name_to_id[n] for n in target_group_names}`
  ONCE, before the existing `self._get_all_channels(group_ids=target_group_ids)`.

### 4.4 `validate_settings_action`
Add an exclude-validation block using `_match_group_names`: show which current
groups the exclude matches ("✅ Exclude filter matches N group(s): …" or
"ℹ️ Exclude filter matches no current groups"). The authoritative all-excluded
check stays at load time (documented). Include validation unchanged.

### 4.5 CSV header (`_generate_csv_header_comments`)
Add under "Group(s) Checked:":
`#   Group(s) Excluded: {settings.get('group_names_exclude', '') or '(none)'}`.

### 4.6 Fingerprint (`_settings_fingerprint`)
Add `'group_names_exclude': settings.get('group_names_exclude', '')`. A mid-run
change trips the windowed-resume scope-drift guard, consistent with `group_names`.

### 4.7 Success message (`_build_load_success_message`)
When the exclude field is non-empty, append "(excluding: …)" to the group
description (ordered before the visible-channels suffix).

### 4.8 Log-line removal
Delete the benign `LOGGER.info("⏰ WINDOW: pending state exists but its window
already closed — discarding dead pending state")` (currently plugin.py:717),
keeping the immediately-following `self._clear_pending_resume()` + `return`.

## 5. Error Handling
- Exclude pattern matching nothing → harmless (empty subtraction).
- Exclude removing everything → the new distinct all-excluded error (load time).
- Exclude with blank include → exclude-from-all-groups (the headline use case).

## 6. Testing
- New tests for `_match_group_names`: exact, wildcard, multiple patterns, no
  match, case-sensitivity (`graveyard` does NOT match `Graveyard`).
- Exclusion outcome (exclude-wins; all-excluded).
- **Fingerprint:** update `tests/test_scheduler_window.py::test_fingerprint_tracks_scope_settings`
  expected dict to include `"group_names_exclude": ""`; add
  `test_fingerprint_differs_on_exclude_change`. Other `_settings_fingerprint`
  call sites in that file (lines ~128-129, 139, 149) are self-consistent and
  unaffected (both sides default the key to `''`).

Validation gate: `python -m py_compile iptv_checker/plugin.py && python -m pytest
tests -q && python -m ruff check . && python -c "import json,io;
json.load(io.open('iptv_checker/plugin.json', encoding='utf-8'))"`.

## 7. Behavior Change / Upgrade Note
- New, opt-in field; blank = no change to existing behavior.
- One-time effect: a `pending_resume.json` written before the upgrade lacks the
  `group_names_exclude` fingerprint key, so the first windowed resume after
  upgrade reports scope drift and discards pending state (one forfeited resume →
  fresh load; self-heals on the next seed).

## 8. Rollout
- Version bump via `python bump_version.py`; release notes; README/DEVELOPMENT
  updates. `.wolf/` per OpenWolf protocol. Deploy to the container.
