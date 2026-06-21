# Exclude Channel Groups — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline). Steps use `- [ ]`.

**Goal:** Add a `group_names_exclude` filter (applied after the include filter), update the CSV header + fingerprint + validation, and remove one benign log line.

**Tech Stack:** Python 3, Django ORM (stubbed in tests), pytest, ruff.

## Global Constraints
- No type hints in plugin code (tests exempt). No reformatting (ruff errors-only).
- Anchor-based edits (match code text). `plugin.json` is UTF-8.
- `{status,message}` action returns intact. Version bump only via `python bump_version.py`.
- Gate after each task: `python -m py_compile iptv_checker/plugin.py && python -m pytest tests -q && python -m ruff check . && python -c "import json,io; json.load(io.open('iptv_checker/plugin.json', encoding='utf-8'))"`
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

### Task 1: `_match_group_names` pure helper (TDD)

**Files:** `iptv_checker/plugin.py` (insert near `load_groups_action`), `tests/test_plugin_helpers.py`.

- [ ] **Step 1: Write failing tests** — append to `tests/test_plugin_helpers.py`:

```python
# --- _match_group_names (group include/exclude matcher) ---

def test_match_group_names_exact_and_wildcard(pmod):
    P = pmod.Plugin
    groups = {"US Sports", "US-PPV-1", "US-PPV-2", "Movies", "Graveyard"}
    assert P._match_group_names("Movies", groups) == {"Movies"}
    assert P._match_group_names("US-PPV-*", groups) == {"US-PPV-1", "US-PPV-2"}
    assert P._match_group_names("Movies, US-PPV-*", groups) == {"Movies", "US-PPV-1", "US-PPV-2"}


def test_match_group_names_no_match_and_blank(pmod):
    P = pmod.Plugin
    groups = {"Movies", "Graveyard"}
    assert P._match_group_names("Nope", groups) == set()
    assert P._match_group_names("", groups) == set()
    assert P._match_group_names("  ,  ", groups) == set()


def test_match_group_names_case_sensitive(pmod):
    P = pmod.Plugin
    groups = {"Graveyard"}
    assert P._match_group_names("graveyard", groups) == set()   # exact is case-sensitive
    assert P._match_group_names("grave*", groups) == set()      # fnmatchcase is case-sensitive
    assert P._match_group_names("Grave*", groups) == {"Graveyard"}
```

- [ ] **Step 2: Run — expect fail.** `python -m pytest tests/test_plugin_helpers.py -q -k match_group_names`

- [ ] **Step 3: Implement** — insert immediately above `def load_groups_action(self, settings, logger):`:

```python
    @staticmethod
    def _match_group_names(patterns_str, all_group_names):
        """Return the set of group names matching any comma-separated pattern.
        Wildcards (containing * ? [) use fnmatch.fnmatchcase (case-sensitive);
        literals use case-sensitive exact membership — symmetric with the include path."""
        matched = set()
        for pattern in (p.strip() for p in (patterns_str or '').split(',')):
            if not pattern:
                continue
            if any(c in pattern for c in '*?['):
                matched |= {g for g in all_group_names if fnmatch.fnmatchcase(g, pattern)}
            elif pattern in all_group_names:
                matched.add(pattern)
        return matched
```

- [ ] **Step 4: Run — expect pass.** **Step 5: Gate + commit.**

---

### Task 2: Apply exclusion in `load_groups_action`

**Files:** `iptv_checker/plugin.py`.

- [ ] **Step 1: All-groups branch** — change:
```python
                target_group_names, target_group_ids = set(group_name_to_id.keys()), set(group_name_to_id.values())
                if not target_group_ids: return {"status": "error", "message": "No groups found in Dispatcharr."}
```
to:
```python
                target_group_names = set(group_name_to_id.keys())
                if not target_group_names: return {"status": "error", "message": "No groups found in Dispatcharr."}
```

- [ ] **Step 2: Include branch** — change:
```python
                target_group_ids = {group_name_to_id[name] for name in target_group_names}

                # Log which groups are being loaded
                if target_group_names:
                    logger.info(f"✓ Loading specified groups: {', '.join(sorted(target_group_names))}")
                if unmatched_patterns:
                    logger.warning(f"⚠️ No groups matched: {', '.join(unmatched_patterns)}")

                if not target_group_ids:
                    return {"status": "error", "message": f"No groups matched: {', '.join(unmatched_patterns)}"}
```
to:
```python
                # Log which groups are being loaded
                if target_group_names:
                    logger.info(f"✓ Loading specified groups: {', '.join(sorted(target_group_names))}")
                if unmatched_patterns:
                    logger.warning(f"⚠️ No groups matched: {', '.join(unmatched_patterns)}")

                if not target_group_names:
                    return {"status": "error", "message": f"No groups matched: {', '.join(unmatched_patterns)}"}
```

- [ ] **Step 3: Insert exclusion + single id compute** — change:
```python
            channels_in_groups = self._get_all_channels(logger, group_ids=target_group_ids)
```
to:
```python
            exclude_str = settings.get("group_names_exclude", "").strip()
            if exclude_str:
                excluded = self._match_group_names(exclude_str, set(group_name_to_id.keys()))
                removed = target_group_names & excluded
                if removed:
                    logger.info(f"🚫 Excluding {len(removed)} group(s) from check: {', '.join(sorted(removed))}")
                    target_group_names = target_group_names - excluded
                if not target_group_names:
                    return {"status": "error", "message": "All target groups were excluded by the 'Group(s) to Exclude' filter. Nothing to check."}

            target_group_ids = {group_name_to_id[name] for name in target_group_names}
            channels_in_groups = self._get_all_channels(logger, group_ids=target_group_ids)
```

- [ ] **Step 4: Gate** (existing tests must stay green — no behavior change when exclude is blank). **Commit.**

---

### Task 3: Fingerprint + tests, CSV header, success message, validation, log removal

**Files:** `iptv_checker/plugin.py`, `tests/test_scheduler_window.py`.

- [ ] **Step 1: Fingerprint** — change `_settings_fingerprint` return dict to add:
```python
            'group_names_exclude': settings.get('group_names_exclude', ''),
```
(append after the `only_visible_channels` line, before the closing `}`).

- [ ] **Step 2: Update fingerprint test** — `tests/test_scheduler_window.py` `test_fingerprint_tracks_scope_settings`, change the expected dict:
```python
    assert fp == {
        "group_names": "STL",
        "check_alternative_streams": False,
        "only_visible_channels": True,
        "group_names_exclude": "",
    }
```
And add after `test_fingerprint_differs_on_group_change`:
```python
def test_fingerprint_differs_on_exclude_change(plugin):
    a = plugin._settings_fingerprint({"group_names": "STL"})
    b = plugin._settings_fingerprint({"group_names": "STL", "group_names_exclude": "US-PPV-*"})
    assert a != b
```

- [ ] **Step 3: CSV header** — after `lines.append(f"#   Group(s) Checked: {settings.get('group_names', 'All groups')}")` insert:
```python
        lines.append(f"#   Group(s) Excluded: {settings.get('group_names_exclude', '') or '(none)'}")
```

- [ ] **Step 4: Success message** — in `_build_load_success_message`, change:
```python
        group_msg = "all groups" if not group_names_str else f"group(s): {', '.join(target_group_names)}"
```
to:
```python
        group_msg = "all groups" if not group_names_str else f"group(s): {', '.join(target_group_names)}"
        exclude_str = settings.get("group_names_exclude", "").strip()
        if exclude_str:
            group_msg += f" (excluding: {exclude_str})"
```

- [ ] **Step 5: Exclude validation** — in `validate_settings_action`, immediately after the include-validation block (after the `if group_names_str:` block that ends with the unmatched warnings), insert:
```python
            exclude_str = settings.get("group_names_exclude", "").strip()
            if exclude_str:
                all_group_names = {g['name'] for g in self._get_all_groups(logger)}
                ex_matched = self._match_group_names(exclude_str, all_group_names)
                if ex_matched:
                    validation_results.append(f"✅ Exclude filter matches {len(ex_matched)} group(s): {', '.join(sorted(ex_matched))}")
                else:
                    validation_results.append("ℹ️ Exclude filter matches no current groups")
```
(Place it inside the same `if self._is_scheduler_configured...`/scheduler-validation scope as the include validation — match the existing indentation. If `all_groups` is already fetched above in that scope, reuse it instead of re-fetching.)

- [ ] **Step 6: Remove the benign log line** — delete:
```python
            LOGGER.info("⏰ WINDOW: pending state exists but its window already closed — discarding dead pending state")
```
(keep the following `self._clear_pending_resume()` and `return`).

- [ ] **Step 7: Gate + commit.**

---

### Task 4: plugin.json field

- [ ] Insert the `group_names_exclude` field object between `group_names` and `check_alternative_streams`:
```json
    {
      "id": "group_names_exclude",
      "label": "📂 Group(s) to EXCLUDE (comma-separated, wildcards supported)",
      "type": "string",
      "default": "",
      "help_text": "Groups to skip even if they match 'Group(s) to Check'. Wildcards supported (e.g. US-PPV-*). Applied AFTER the include filter; with a blank include this means 'all groups except these'. If a group matches both fields, exclude wins. Blank = exclude nothing."
    },
```
- [ ] Gate (JSON valid; full suite green). Commit.

---

### Task 5: Docs, version, deploy

- [ ] Dry-run: throwaway script importing the plugin via stubs; call `_match_group_names` + exercise the load exclusion path on synthetic groups; confirm exclude-wins + all-excluded. Delete it.
- [ ] `python bump_version.py`.
- [ ] README (Group Selection settings table: add the exclude row; usage note), DEVELOPMENT.md gotcha if useful, `RELEASE_NOTES_v<NEW>.md`. `.wolf/` per protocol.
- [ ] Final gate incl. `test_version_sync.py`. Commit.
- [ ] Deploy to container (`/deploy` steps: validate → docker cp both files → restart → verify logs: version, single scheduler election, no traceback). Confirm the removed log line no longer appears.

## Self-Review
- G1 exclude filter → Tasks 1-2,4. G2 CSV → Task 3 Step 3. G3 log removal → Task 3 Step 6. G4 fingerprint → Task 3 Steps 1-2. G5 tests/docs → Tasks 1,3,5.
- Distinct empty-set messages preserved (Task 2 Steps 1-2) + new all-excluded message (Step 3). `target_group_ids` computed once before its sole use. No placeholders. Names↔ids 1:1 emptiness equivalence holds.
