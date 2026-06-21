# Channel Restore + Black/Blank Stream Flag — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-class Black/Blank stream category (own tag + own group) and a "Restore Recovered Channels" feature that strips plugin name tags and moves recovered channels back to their exact original group.

**Architecture:** Single-file Dispatcharr plugin. Decision logic is pushed into pure `@staticmethod`/`@classmethod` helpers on `Plugin` (unit-testable via `pmod.Plugin.<helper>`), while thin instance methods do the Django ORM I/O. Original group is persisted to a new atomic JSON state file; name restoration is stateless tag-stripping.

**Tech Stack:** Python 3, Django ORM (stubbed in tests via `tests/conftest.py`), pytest, ruff.

## Global Constraints

- No type hints in plugin code (tests exempt). No new docstring bloat. snake_case fns, PascalCase classes, UPPER_SNAKE_CASE constants. 4-space indent, ~120 col.
- All logs prefixed `[IPTV Checker]` via existing `PluginNameFilter`; use the passed `logger`.
- Action methods return `{"status": "ok|error", "message": "..."}` (extra keys allowed).
- Persisted writes MUST use `self._save_json_file` (atomic) — never raw `open('w')` (issue #21 EACCES).
- Do NOT add `requests`. Do NOT reorganize imports / reformat (ruff is errors-only). `re` is already imported.
- `plugin.json` is the single source of truth for settings + actions; every new action needs an `action_map` entry AND a `plugin.json` action entry.
- Version bump only via `python bump_version.py` (calver `1.26.DDDHHMM`); never hand-edit versions.
- Validation gate (run after every task): `python -m py_compile iptv_checker/plugin.py && python -m pytest tests -q && python -m ruff check .`
- Tests live only in `tests/`. New tests go in `tests/test_restore_and_black.py`.
- Commit messages end with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

**Key existing anchors (current line numbers):**
- `PluginConfig` file paths: `plugin.py:84-89`; `Plugin.__init__` file attrs: `plugin.py:289-292`.
- Helpers: `_load_json_file` 749, `_save_json_file` 763, `_get_all_channels` 1641, `_get_all_groups` 1637, `_bulk_update_channels` 1683, `_get_or_create_group` 1708, `_trigger_frontend_refresh` 1558.
- Actions: `rename_channels_action` 2229, `move_dead_channels_action` 2260, `delete_dead_channels_action` 2284, `rename_low_framerate_channels_action` 2338, `move_low_framerate_channels_action` 2370, `add_video_format_suffix_action` 2393.
- `view_results_action` 1524, `_fire_webhook` 1572, `_execute_scheduled_check` post-actions 1134-1180, `action_map` 1294-1314.
- `plugin.json`: dead settings 152-165, low-fps 173-185, format 194-199, scheduler flags 1518-1580, actions array 1604-1781.

---

### Task 1: Pure predicates + tag-derivation helpers

**Files:**
- Modify: `iptv_checker/plugin.py` (insert staticmethods just above `rename_channels_action`, ~line 2228)
- Test: `tests/test_restore_and_black.py` (create)

**Interfaces:**
- Produces:
  - `Plugin._is_dead_nonblack(result) -> bool`
  - `Plugin._is_black_screen(result) -> bool`
  - `Plugin._extract_format_tags(fmt) -> list[str]`
  - `Plugin._compile_trailing_tag_re(tags) -> re.Pattern | None`
  - `Plugin._derive_status_tags(settings) -> re.Pattern | None`
  - `Plugin._derive_strippable_tags(settings) -> re.Pattern | None`
  - Class attrs `Plugin.STANDARD_STATUS_TAGS`, `Plugin.STANDARD_QUALITY_TAGS`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_restore_and_black.py`:

```python
"""Black/blank flag + channel-restore: pure helpers and action behavior."""


# ---- Task 1: predicates + tag derivation --------------------------------

def test_is_dead_nonblack(pmod):
    P = pmod.Plugin
    assert P._is_dead_nonblack({"status": "Dead", "error_type": "Timeout"}) is True
    assert P._is_dead_nonblack({"status": "Dead", "error_type": "Black Screen"}) is False
    assert P._is_dead_nonblack({"status": "Dead"}) is True  # no error_type -> not black
    assert P._is_dead_nonblack({"status": "Alive"}) is False


def test_is_black_screen(pmod):
    P = pmod.Plugin
    assert P._is_black_screen({"status": "Dead", "error_type": "Black Screen"}) is True
    assert P._is_black_screen({"status": "Dead", "error_type": "Timeout"}) is False
    assert P._is_black_screen({"status": "Alive", "error_type": "Black Screen"}) is False


def test_extract_format_tags(pmod):
    P = pmod.Plugin
    assert P._extract_format_tags("{name} [DEAD]") == ["DEAD"]
    assert P._extract_format_tags("[X] {name} [DEAD]") == ["X", "DEAD"]
    assert P._extract_format_tags("") == []
    assert P._extract_format_tags(None) == []


def test_derive_status_tags_strips_only_status(pmod):
    P = pmod.Plugin
    settings = {
        "dead_rename_format": "{name} [DEAD]",
        "low_framerate_rename_format": "{name} [Slow]",
        "black_screen_rename_format": "{name} [Blank]",
    }
    rx = P._derive_status_tags(settings)
    assert rx.search("ESPN [DEAD]")
    assert rx.search("ESPN [Slow]")
    assert rx.search("ESPN [Blank]")
    assert not rx.search("ESPN [HD]")  # quality tag is NOT a status tag


def test_derive_strippable_tags_strips_status_and_quality(pmod):
    P = pmod.Plugin
    settings = {
        "dead_rename_format": "{name} [DEAD]",
        "low_framerate_rename_format": "{name} [Slow]",
        "black_screen_rename_format": "{name} [Blank]",
        "video_format_suffixes": "UHD, FHD, HD, SD, Unknown",
    }
    rx = P._derive_strippable_tags(settings)
    for name in ("ESPN [DEAD]", "ESPN [Slow]", "ESPN [Blank]", "ESPN [HD]", "ESPN [UHD]"):
        assert rx.sub("", name).rstrip() == "ESPN", name
    # Stacked trailing tags collapse to the clean base.
    assert rx.sub("", "ESPN [HD] [Blank]").rstrip() == "ESPN"
    # Custom label from a user-edited format is honored.
    rx2 = P._derive_strippable_tags({"dead_rename_format": "{name} [GONE]"})
    assert rx2.sub("", "ESPN [GONE]").rstrip() == "ESPN"


def test_derive_tags_case_insensitive_and_trailing_only(pmod):
    P = pmod.Plugin
    rx = P._derive_strippable_tags({"dead_rename_format": "{name} [DEAD]"})
    assert rx.sub("", "ESPN [dead]").rstrip() == "ESPN"            # case-insensitive
    assert rx.sub("", "[DEAD] ESPN Sports") == "[DEAD] ESPN Sports"  # not trailing -> untouched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_restore_and_black.py -q`
Expected: FAIL (AttributeError: type object 'Plugin' has no attribute '_is_dead_nonblack')

- [ ] **Step 3: Implement the helpers**

Insert immediately above `def rename_channels_action(self, settings, logger):` (currently line 2229):

```python
    # --- Tag taxonomy (shared by black-flag handling and restore) -----------
    # Standard labels this plugin can append to a channel name. Used as a
    # defensive floor so a previously-applied standard tag is always strippable
    # even after the user edits their rename formats (same approach as the
    # issue-#18 suffix stripper).
    STANDARD_STATUS_TAGS = ('DEAD', 'Slow', 'Blank')
    STANDARD_QUALITY_TAGS = ('UHD', 'FHD', 'HD', 'SD', 'Unknown')

    @staticmethod
    def _is_dead_nonblack(result):
        """Dead due to a probing failure, NOT a black/blank screen."""
        return result.get('status') == 'Dead' and result.get('error_type') != 'Black Screen'

    @staticmethod
    def _is_black_screen(result):
        """Marked Dead specifically because the stream is a black/blank screen."""
        return result.get('status') == 'Dead' and result.get('error_type') == 'Black Screen'

    @staticmethod
    def _extract_format_tags(fmt):
        """Pull bracketed labels out of a rename format (e.g. 'DEAD' from '{name} [DEAD]')."""
        if not fmt:
            return []
        return re.findall(r'\[([^\[\]]+)\]', fmt)

    @staticmethod
    def _compile_trailing_tag_re(tags):
        """Compile a case-insensitive regex matching one-or-more trailing ' [TAG]' groups."""
        labels = sorted({t.strip() for t in tags if t and t.strip()}, key=len, reverse=True)
        if not labels:
            return None
        pattern = r'(?:\s*\[(?:' + '|'.join(re.escape(t) for t in labels) + r')\])+\s*$'
        return re.compile(pattern, re.IGNORECASE)

    @classmethod
    def _derive_status_tags(cls, settings):
        """Compiled regex of PROBLEM tags only (DEAD/Slow/Blank + custom) — used for eligibility."""
        tags = list(cls.STANDARD_STATUS_TAGS)
        for key in ('dead_rename_format', 'low_framerate_rename_format', 'black_screen_rename_format'):
            tags.extend(cls._extract_format_tags(settings.get(key, '')))
        return cls._compile_trailing_tag_re(tags)

    @classmethod
    def _derive_strippable_tags(cls, settings):
        """Compiled regex of ALL tags this plugin can append (status + quality)."""
        tags = list(cls.STANDARD_STATUS_TAGS) + list(cls.STANDARD_QUALITY_TAGS)
        for key in ('dead_rename_format', 'low_framerate_rename_format', 'black_screen_rename_format'):
            tags.extend(cls._extract_format_tags(settings.get(key, '')))
        tags.extend(s.strip() for s in (settings.get('video_format_suffixes', '') or '').split(','))
        return cls._compile_trailing_tag_re(tags)

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_restore_and_black.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Run full validation + commit**

Run: `python -m py_compile iptv_checker/plugin.py && python -m pytest tests -q && python -m ruff check .`
Expected: all pass, no lint errors.

```bash
git add iptv_checker/plugin.py tests/test_restore_and_black.py
git commit -m "feat: add black/blank predicates + tag-derivation helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Pure planners (`_compute_restore_plan`, `_compute_capture_state`)

**Files:**
- Modify: `iptv_checker/plugin.py` (insert staticmethods directly after the Task 1 helpers)
- Test: `tests/test_restore_and_black.py`

**Interfaces:**
- Consumes: regexes from `_derive_strippable_tags` / `_derive_status_tags` (Task 1).
- Produces:
  - `Plugin._compute_restore_plan(alive_names_by_id, state, strip_re, status_re, existing_group_ids) -> dict` with keys `name_updates` (list of `{'id','name'}`), `group_updates` (list of `{'id','channel_group_id'}`), `entries_to_clear` (set of str ids), `missing_group_ids` (dict str id -> orig id).
  - `Plugin._compute_capture_state(channel_ids, current_group_by_id, group_name_by_id, managed_group_names, existing_state, now_iso) -> dict` of NEW entries to add.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_restore_and_black.py`:

```python
# ---- Task 2: pure planners ----------------------------------------------

def _settings():
    return {
        "dead_rename_format": "{name} [DEAD]",
        "low_framerate_rename_format": "{name} [Slow]",
        "black_screen_rename_format": "{name} [Blank]",
        "video_format_suffixes": "UHD, FHD, HD, SD, Unknown",
    }


def test_restore_plan_strips_tag_and_restores_group(pmod):
    P = pmod.Plugin
    strip_re = P._derive_strippable_tags(_settings())
    status_re = P._derive_status_tags(_settings())
    alive = {10: "ESPN [DEAD]", 11: "TNT [Blank]"}
    state = {"10": {"original_group_id": 5, "original_group_name": "USA"},
             "11": {"original_group_id": 7, "original_group_name": "Movies"}}
    plan = P._compute_restore_plan(alive, state, strip_re, status_re, existing_group_ids={5, 7})
    assert {"id": 10, "name": "ESPN"} in plan["name_updates"]
    assert {"id": 11, "name": "TNT"} in plan["name_updates"]
    assert {"id": 10, "channel_group_id": 5} in plan["group_updates"]
    assert plan["entries_to_clear"] == {"10", "11"}
    assert plan["missing_group_ids"] == {}


def test_restore_plan_ignores_unmarked_alive(pmod):
    P = pmod.Plugin
    strip_re = P._derive_strippable_tags(_settings())
    status_re = P._derive_status_tags(_settings())
    # Healthy channel with only a quality tag, never marked, no state -> untouched.
    plan = P._compute_restore_plan({20: "CNN [HD]"}, {}, strip_re, status_re, existing_group_ids=set())
    assert plan["name_updates"] == []
    assert plan["group_updates"] == []
    assert plan["entries_to_clear"] == set()


def test_restore_plan_eligible_by_state_even_without_tag(pmod):
    P = pmod.Plugin
    strip_re = P._derive_strippable_tags(_settings())
    status_re = P._derive_status_tags(_settings())
    # Name already clean but state exists (was moved, manually renamed) -> move back.
    plan = P._compute_restore_plan({30: "Fox"}, {"30": {"original_group_id": 9}},
                                   strip_re, status_re, existing_group_ids={9})
    assert plan["name_updates"] == []  # nothing to strip
    assert {"id": 30, "channel_group_id": 9} in plan["group_updates"]
    assert plan["entries_to_clear"] == {"30"}


def test_restore_plan_missing_group_keeps_name_drops_entry(pmod):
    P = pmod.Plugin
    strip_re = P._derive_strippable_tags(_settings())
    status_re = P._derive_status_tags(_settings())
    plan = P._compute_restore_plan({40: "ABC [DEAD]"}, {"40": {"original_group_id": 99}},
                                   strip_re, status_re, existing_group_ids={1, 2})
    assert {"id": 40, "name": "ABC"} in plan["name_updates"]
    assert plan["group_updates"] == []
    assert plan["missing_group_ids"] == {"40": 99}
    assert plan["entries_to_clear"] == {"40"}


def test_restore_plan_name_only_tag_not_emptied(pmod):
    P = pmod.Plugin
    strip_re = P._derive_strippable_tags(_settings())
    status_re = P._derive_status_tags(_settings())
    # Name that is ONLY a tag would strip to empty -> skip the rename, keep original.
    plan = P._compute_restore_plan({50: "[DEAD]"}, {}, strip_re, status_re, existing_group_ids=set())
    assert plan["name_updates"] == []


def test_capture_state_skips_existing_and_managed(pmod):
    P = pmod.Plugin
    current_group = {1: 100, 2: 200, 3: 300}
    group_names = {100: "USA Sports", 200: "Graveyard", 300: "Movies"}
    managed = ["Graveyard", "Slow", "Black Screens"]
    existing = {"3": {"original_group_id": 300}}  # already tracked
    new = P._compute_capture_state([1, 2, 3], current_group, group_names, managed, existing, "T0")
    assert "1" in new and new["1"]["original_group_id"] == 100
    assert "2" not in new   # currently in a managed group -> not recorded
    assert "3" not in new   # already tracked -> not overwritten
    assert new["1"]["original_group_name"] == "USA Sports"
    assert new["1"]["moved_at"] == "T0"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_restore_and_black.py -q -k "restore_plan or capture_state"`
Expected: FAIL (no attribute `_compute_restore_plan`)

- [ ] **Step 3: Implement the planners**

Insert directly after the Task 1 helpers (above `rename_channels_action`):

```python
    @staticmethod
    def _compute_restore_plan(alive_names_by_id, state, strip_re, status_re, existing_group_ids):
        """Pure planner for the restore action.

        A channel is eligible iff it has stored original-group state OR its current
        name carries a trailing status tag ([DEAD]/[Slow]/[Blank] or custom). Quality
        tags alone never make a healthy channel eligible. Eligible channels have ALL
        plugin tags stripped from the name and are moved back to their original group
        when it still exists.
        """
        name_updates = []
        group_updates = []
        entries_to_clear = set()
        missing_group_ids = {}

        for cid, name in alive_names_by_id.items():
            entry = state.get(str(cid))
            has_state = entry is not None
            has_status_tag = bool(status_re and name and status_re.search(name))
            if not has_state and not has_status_tag:
                continue

            if strip_re and name:
                base = strip_re.sub('', name).rstrip()
                if base and base != name:
                    name_updates.append({'id': cid, 'name': base})

            if has_state:
                orig = entry.get('original_group_id')
                if orig is not None and orig in existing_group_ids:
                    group_updates.append({'id': cid, 'channel_group_id': orig})
                else:
                    missing_group_ids[str(cid)] = orig
                entries_to_clear.add(str(cid))

        return {
            'name_updates': name_updates,
            'group_updates': group_updates,
            'entries_to_clear': entries_to_clear,
            'missing_group_ids': missing_group_ids,
        }

    @staticmethod
    def _compute_capture_state(channel_ids, current_group_by_id, group_name_by_id,
                               managed_group_names, existing_state, now_iso):
        """Pure planner for original-state capture. Returns ONLY the new entries to add.

        Skips channels already tracked, and channels currently sitting in a managed
        destination group (so a second move never records a dead/slow/black group as
        the 'original').
        """
        managed = {n.strip().lower() for n in managed_group_names if n and n.strip()}
        new_entries = {}
        for cid in channel_ids:
            key = str(cid)
            if key in existing_state:
                continue
            gid = current_group_by_id.get(cid)
            gname = group_name_by_id.get(gid, '') if gid is not None else ''
            if gname and gname.strip().lower() in managed:
                continue
            new_entries[key] = {
                'original_group_id': gid,
                'original_group_name': gname,
                'moved_at': now_iso,
            }
        return new_entries

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_restore_and_black.py -q`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Validation + commit**

Run: `python -m py_compile iptv_checker/plugin.py && python -m pytest tests -q && python -m ruff check .`

```bash
git add iptv_checker/plugin.py tests/test_restore_and_black.py
git commit -m "feat: add pure restore + state-capture planners

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: State file plumbing + capture wrapper + black actions + dead-action refactor + plugin.json

**Files:**
- Modify: `iptv_checker/plugin.py` — `PluginConfig` (~88), `__init__` (~292), `rename_channels_action` (2242), `move_dead_channels_action` (2270-2278), `move_low_framerate_channels_action` (2380-2388), insert `_capture_original_state` + two black actions (after `add_video_format_suffix_action`, ~2495), `action_map` (1313)
- Modify: `iptv_checker/plugin.json` — black settings, scheduler flags, actions
- Test: `tests/test_restore_and_black.py`

**Interfaces:**
- Consumes: `_is_dead_nonblack`/`_is_black_screen` (Task 1), `_compute_capture_state` (Task 2), `_get_all_channels`, `_get_all_groups`, `_bulk_update_channels`, `_get_or_create_group`, `_load_json_file`, `_save_json_file`.
- Produces: `self.channel_state_file`, `Plugin._capture_original_state(channel_ids, settings, logger)`, `Plugin.rename_black_screen_channels_action`, `Plugin.move_black_screen_channels_action`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_restore_and_black.py`:

```python
# ---- Task 3: capture wrapper + black actions ----------------------------

import json
import logging


def _logger():
    lg = logging.getLogger("iptv_checker.tests.t3")
    lg.addHandler(logging.NullHandler())
    lg.propagate = False
    return lg


def _write(plugin, results):
    with open(plugin.results_file, "w") as f:
        json.dump(results, f)


def test_capture_original_state_writes_file(plugin, tmp_path, monkeypatch):
    plugin.channel_state_file = str(tmp_path / "state.json")
    monkeypatch.setattr(plugin, "_get_all_channels",
                        lambda logger: [{"id": 1, "name": "ESPN", "channel_group_id": 100}])
    monkeypatch.setattr(plugin, "_get_all_groups",
                        lambda logger: [{"id": 100, "name": "USA Sports"}, {"id": 9, "name": "Graveyard"}])
    settings = {"move_to_group_name": "Graveyard", "move_low_framerate_group": "Slow",
                "move_black_screen_group": "Black Screens"}
    plugin._capture_original_state([1], settings, _logger())
    state = json.load(open(plugin.channel_state_file))
    assert state["1"]["original_group_id"] == 100
    assert state["1"]["original_group_name"] == "USA Sports"


def test_rename_dead_excludes_black(plugin, monkeypatch):
    captured = {}
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: captured.setdefault("p", payload) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)
    _write(plugin, [
        {"channel_id": 1, "channel_name": "A", "status": "Dead", "error_type": "Timeout"},
        {"channel_id": 2, "channel_name": "B", "status": "Dead", "error_type": "Black Screen"},
    ])
    res = plugin.rename_channels_action({"dead_rename_format": "{name} [DEAD]"}, _logger())
    assert res["status"] == "ok"
    ids = {p["id"] for p in captured["p"]}
    assert ids == {1}  # black channel 2 excluded


def test_rename_black_targets_only_black(plugin, monkeypatch):
    captured = {}
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: captured.setdefault("p", payload) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)
    _write(plugin, [
        {"channel_id": 1, "channel_name": "A", "status": "Dead", "error_type": "Timeout"},
        {"channel_id": 2, "channel_name": "B", "status": "Dead", "error_type": "Black Screen"},
    ])
    res = plugin.rename_black_screen_channels_action({"black_screen_rename_format": "{name} [Blank]"}, _logger())
    assert res["status"] == "ok"
    assert captured["p"] == [{"id": 2, "name": "B [Blank]"}]


def test_move_black_captures_then_moves(plugin, tmp_path, monkeypatch):
    plugin.channel_state_file = str(tmp_path / "state.json")
    calls = {}
    monkeypatch.setattr(plugin, "_capture_original_state",
                        lambda ids, s, lg: calls.setdefault("captured", set(ids)))
    monkeypatch.setattr(plugin, "_get_or_create_group",
                        lambda name, logger: type("G", (), {"id": 77})())
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: calls.setdefault("moved", payload) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)
    _write(plugin, [{"channel_id": 2, "channel_name": "B", "status": "Dead", "error_type": "Black Screen"}])
    res = plugin.move_black_screen_channels_action({"move_black_screen_group": "Black Screens"}, _logger())
    assert res["status"] == "ok"
    assert calls["captured"] == {2}
    assert calls["moved"] == [{"id": 2, "channel_group_id": 77}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_restore_and_black.py -q -k "capture or black or excludes"`
Expected: FAIL (channel_state_file/_capture_original_state/black actions missing)

- [ ] **Step 3a: Add the state-file path**

`plugin.py` — after line 89 (`SCHEDULER_RELOAD_FLAG = ...`), add inside `PluginConfig`:

```python
    CHANNEL_STATE_FILE = "/data/iptv_checker_channel_state.json"
```

`plugin.py` — after line 292 (`self.pending_resume_file = ...`), add in `__init__`:

```python
        self.channel_state_file = PluginConfig.CHANNEL_STATE_FILE
```

- [ ] **Step 3b: Add the capture wrapper + black actions**

Insert after `add_video_format_suffix_action` (after its final `except Exception as e: return ...`, currently line 2495):

```python
    def _capture_original_state(self, channel_ids, settings, logger):
        """Persist each channel's current group as its 'original' before a move so
        restore can return it later. Never overwrites an existing entry and never
        records a managed destination group. Best-effort: never aborts the move."""
        try:
            channel_ids = list(channel_ids)
            if not channel_ids:
                return
            current_group_by_id = {c['id']: c.get('channel_group_id') for c in self._get_all_channels(logger)}
            group_name_by_id = {g['id']: g['name'] for g in self._get_all_groups(logger)}
            managed_group_names = [
                settings.get('move_to_group_name', ''),
                settings.get('move_low_framerate_group', ''),
                settings.get('move_black_screen_group', ''),
            ]
            state = self._load_json_file(self.channel_state_file) or {}
            now_iso = datetime.utcnow().isoformat() + 'Z'
            new_entries = self._compute_capture_state(
                channel_ids, current_group_by_id, group_name_by_id,
                managed_group_names, state, now_iso,
            )
            if new_entries:
                state.update(new_entries)
                self._save_json_file(self.channel_state_file, state, indent=2)
                logger.info(f"Captured original group for {len(new_entries)} channel(s) before move.")
        except Exception as e:
            logger.warning(f"Could not capture original channel state (continuing): {e}")

    def rename_black_screen_channels_action(self, settings, logger):
        """Rename channels marked Dead specifically because they are a black/blank screen."""
        rename_format = settings.get("black_screen_rename_format", "{name} [Blank]").strip()
        if not rename_format:
            return {"status": "error", "message": "Please configure a Black-Screen Channel Rename Format."}
        if "{name}" not in rename_format:
            return {"status": "error", "message": "Black-Screen Channel Rename Format must contain {name} placeholder."}

        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first."}

        black_channels = {r['channel_id']: r['channel_name'] for r in results if self._is_black_screen(r)}
        if not black_channels:
            return {"status": "ok", "message": "No black-screen channels found in the last check."}

        payload = []
        for cid, name in black_channels.items():
            new_name = rename_format.replace('{name}', name)
            if new_name != name:
                payload.append({'id': cid, 'name': new_name})

        if not payload:
            return {"status": "ok", "message": "No channels needed renaming."}
        try:
            count = self._bulk_update_channels(payload, ['name'], logger)
            self._trigger_frontend_refresh(settings, logger)
            return {"status": "ok", "message": f"Successfully renamed {count} black-screen channels. GUI refresh triggered."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def move_black_screen_channels_action(self, settings, logger):
        """Move black/blank-screen channels to a dedicated group (captures original group first)."""
        group_name = settings.get("move_black_screen_group", "Black Screens").strip()
        if not group_name:
            return {"status": "error", "message": "Please enter a destination group name for black-screen channels."}

        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first."}

        black_channel_ids = {r['channel_id'] for r in results if self._is_black_screen(r)}
        if not black_channel_ids:
            return {"status": "ok", "message": "No black-screen channels found to move."}
        try:
            self._capture_original_state(black_channel_ids, settings, logger)
            dest_group = self._get_or_create_group(group_name, logger)
            payload = [{'id': cid, 'channel_group_id': dest_group.id} for cid in black_channel_ids]
            moved_count = self._bulk_update_channels(payload, ['channel_group_id'], logger)
            self._trigger_frontend_refresh(settings, logger)
            return {"status": "ok", "message": f"Successfully moved {moved_count} black-screen channels to group '{group_name}'. GUI refresh triggered."}
        except Exception as e:
            return {"status": "error", "message": str(e)}
```

- [ ] **Step 3c: Refactor dead rename/move to exclude black + capture state**

`plugin.py:2242` — change:
```python
        dead_channels = {r['channel_id']: r['channel_name'] for r in results if r['status'] == 'Dead'}
```
to:
```python
        dead_channels = {r['channel_id']: r['channel_name'] for r in results if self._is_dead_nonblack(r)}
```

`plugin.py:2270` — change:
```python
        dead_channel_ids = {r['channel_id'] for r in results if r['status'] == 'Dead'}
```
to:
```python
        dead_channel_ids = {r['channel_id'] for r in results if self._is_dead_nonblack(r)}
```

`plugin.py:2273-2275` — in `move_dead_channels_action`, change the `try:` block start from:
```python
        try:
            dest_group = self._get_or_create_group(move_to_group_name, logger)
            new_group_id = dest_group.id
```
to:
```python
        try:
            self._capture_original_state(dead_channel_ids, settings, logger)
            dest_group = self._get_or_create_group(move_to_group_name, logger)
            new_group_id = dest_group.id
```

`plugin.py:2383-2385` — in `move_low_framerate_channels_action`, change:
```python
        try:
            dest_group = self._get_or_create_group(group_name, logger)
            new_group_id = dest_group.id
```
to:
```python
        try:
            self._capture_original_state(low_fps_channel_ids, settings, logger)
            dest_group = self._get_or_create_group(group_name, logger)
            new_group_id = dest_group.id
```

- [ ] **Step 3d: Register black actions in action_map**

`plugin.py:1313` — after `"delete_dead_channels": self.delete_dead_channels_action,` add:
```python
                "rename_black_screen_channels": self.rename_black_screen_channels_action,
                "move_black_screen_channels": self.move_black_screen_channels_action,
```

- [ ] **Step 3e: Add black settings to plugin.json**

`plugin.json` — after the `move_to_group_name` object (closing `}` at line 165), insert:
```json
    {
      "id": "_section_black",
      "label": "⬛ Black / Blank Screen Handling",
      "type": "info",
      "description": "How to rename and relocate channels detected as a pure black/blank screen. Requires 'Detect Black-Screen Streams' to be ON. These channels are handled separately from [DEAD] channels."
    },
    {
      "id": "black_screen_rename_format",
      "label": "⬛ Black-Screen Channel Rename Format",
      "type": "string",
      "default": "{name} [Blank]",
      "placeholder": "{name} [Blank]",
      "help_text": "Format for renaming black/blank-screen channels. Use {name} as the placeholder. Black-screen channels are excluded from the Dead rename/move actions so they are not double-tagged."
    },
    {
      "id": "move_black_screen_group",
      "label": "⬛ Move Black-Screen Channels to Group",
      "type": "string",
      "default": "Black Screens",
      "help_text": "Enter the name for the group to move black/blank-screen channels into."
    },
```

- [ ] **Step 3f: Add black scheduler flags to plugin.json**

`plugin.json` — after the `scheduler_rename_low_framerate_channels` object (closing `}` at line 1538), insert:
```json
    {
      "id": "scheduler_rename_black_screen_channels",
      "label": "⬛ Rename Black-Screen Channels After Scheduled Checks",
      "type": "boolean",
      "default": false,
      "help_text": "Automatically rename black/blank-screen channels after scheduled checks complete. Requires 'Detect Black-Screen Streams' to be ON."
    },
```
`plugin.json` — after the `scheduler_move_low_framerate_channels` object (closing `}` at line 1559), insert:
```json
    {
      "id": "scheduler_move_black_screen_channels",
      "label": "⬛ Move Black-Screen Channels After Scheduled Checks",
      "type": "boolean",
      "default": false,
      "help_text": "Automatically move black/blank-screen channels to the configured group after scheduled checks complete."
    },
```

- [ ] **Step 3g: Add black actions to plugin.json actions array**

`plugin.json` — after the `move_dead_channels` action object (closing `}` at line 1701), insert:
```json
    {
      "id": "rename_black_screen_channels",
      "label": "⬛ Rename Black-Screen Channels",
      "description": "Rename all channels detected as a black/blank screen in the last check using the configured format.",
      "button_color": "red",
      "confirm": {
        "message": "This will rename black-screen channels. This action is irreversible. Continue?"
      },
      "button_variant": "filled",
      "button_label": "⬛ Rename Black"
    },
    {
      "id": "move_black_screen_channels",
      "label": "⬛ Move Black-Screen Channels to Group",
      "description": "Moves all channels detected as a black/blank screen in the last check to the specified group.",
      "button_color": "red",
      "confirm": {
        "message": "This will move black-screen channels to the configured group. This action is irreversible. Continue?"
      },
      "button_variant": "filled",
      "button_label": "⬛ Move Black"
    },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_restore_and_black.py -q`
Expected: PASS (Task 1-3 tests). Also validate plugin.json parses: `python -c "import json; json.load(open('iptv_checker/plugin.json'))"`

- [ ] **Step 5: Validation + commit**

Run: `python -m py_compile iptv_checker/plugin.py && python -m pytest tests -q && python -m ruff check . && python -c "import json,sys; json.load(open('iptv_checker/plugin.json')); print('json ok')"`

```bash
git add iptv_checker/plugin.py iptv_checker/plugin.json tests/test_restore_and_black.py
git commit -m "feat: black/blank stream flag (separate tag + group) with original-group capture

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Restore action + registration + plugin.json

**Files:**
- Modify: `iptv_checker/plugin.py` — insert `restore_channels_action` (after `move_black_screen_channels_action`), `action_map` (1313 area)
- Modify: `iptv_checker/plugin.json` — restore info section, restore action, `scheduler_restore_channels` flag
- Test: `tests/test_restore_and_black.py`

**Interfaces:**
- Consumes: `_derive_strippable_tags`/`_derive_status_tags` (Task 1), `_compute_restore_plan` (Task 2), `_get_all_channels`, `_get_all_groups`, `_bulk_update_channels`, `_trigger_frontend_refresh`, `self.channel_state_file` (Task 3).
- Produces: `Plugin.restore_channels_action(settings, logger)` returning `{"status","message","restored": int}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_restore_and_black.py`:

```python
# ---- Task 4: restore action ---------------------------------------------

def test_restore_action_strips_and_moves_back(plugin, tmp_path, monkeypatch):
    plugin.channel_state_file = str(tmp_path / "state.json")
    with open(plugin.channel_state_file, "w") as f:
        json.dump({"1": {"original_group_id": 100, "original_group_name": "USA"}}, f)
    _write(plugin, [
        {"channel_id": 1, "channel_name": "ESPN", "status": "Alive", "error_type": None},
        {"channel_id": 2, "channel_name": "Dead1", "status": "Dead", "error_type": "Timeout"},
    ])
    monkeypatch.setattr(plugin, "_get_all_channels",
                        lambda logger: [{"id": 1, "name": "ESPN [DEAD]"}, {"id": 2, "name": "Dead1 [DEAD]"}])
    monkeypatch.setattr(plugin, "_get_all_groups", lambda logger: [{"id": 100, "name": "USA"}])
    payloads = []
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: payloads.append((fields[0], payload)) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)

    settings = {"dead_rename_format": "{name} [DEAD]", "video_format_suffixes": "UHD, FHD, HD, SD, Unknown"}
    res = plugin.restore_channels_action(settings, _logger())
    assert res["status"] == "ok"
    assert res["restored"] == 1
    by_field = dict(payloads)
    assert by_field["name"] == [{"id": 1, "name": "ESPN"}]
    assert by_field["channel_group_id"] == [{"id": 1, "channel_group_id": 100}]
    # State entry cleared after restore.
    assert json.load(open(plugin.channel_state_file)) == {}


def test_restore_action_no_recovered(plugin, tmp_path, monkeypatch):
    plugin.channel_state_file = str(tmp_path / "state.json")
    _write(plugin, [{"channel_id": 9, "channel_name": "CNN", "status": "Alive"}])
    monkeypatch.setattr(plugin, "_get_all_channels", lambda logger: [{"id": 9, "name": "CNN [HD]"}])
    monkeypatch.setattr(plugin, "_get_all_groups", lambda logger: [])
    monkeypatch.setattr(plugin, "_bulk_update_channels", lambda payload, fields, logger: len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)
    res = plugin.restore_channels_action({"video_format_suffixes": "UHD, FHD, HD, SD, Unknown"}, _logger())
    assert res["status"] == "ok"
    assert res["restored"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_restore_and_black.py -q -k restore_action`
Expected: FAIL (no attribute `restore_channels_action`)

- [ ] **Step 3a: Implement the restore action**

Insert after `move_black_screen_channels_action` (before `view_table_action`, ~line 2497):

```python
    def restore_channels_action(self, settings, logger):
        """Restore recovered channels: strip plugin name tags and move back to the
        original group for channels that are Alive again but were previously marked."""
        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first.", "restored": 0}

        alive_ids = {r['channel_id'] for r in results if r.get('status') == 'Alive'}
        if not alive_ids:
            return {"status": "ok", "message": "No alive channels in the last check to restore.", "restored": 0}

        state = self._load_json_file(self.channel_state_file) or {}
        strip_re = self._derive_strippable_tags(settings)
        status_re = self._derive_status_tags(settings)

        alive_names_by_id = {c['id']: c['name'] for c in self._get_all_channels(logger) if c['id'] in alive_ids}
        existing_group_ids = {g['id'] for g in self._get_all_groups(logger)}

        plan = self._compute_restore_plan(alive_names_by_id, state, strip_re, status_re, existing_group_ids)

        affected = {u['id'] for u in plan['name_updates']} | {u['id'] for u in plan['group_updates']}
        if not affected and not plan['entries_to_clear']:
            return {"status": "ok", "message": "No recovered channels needed restoring.", "restored": 0}

        try:
            renamed = self._bulk_update_channels(plan['name_updates'], ['name'], logger)
            moved = self._bulk_update_channels(plan['group_updates'], ['channel_group_id'], logger)

            if plan['missing_group_ids']:
                logger.warning(
                    f"Restore: original group no longer exists for {len(plan['missing_group_ids'])} channel(s); "
                    f"name restored but left in current group: {sorted(plan['missing_group_ids'])}"
                )

            for key in plan['entries_to_clear']:
                state.pop(key, None)
            self._save_json_file(self.channel_state_file, state, indent=2)

            self._trigger_frontend_refresh(settings, logger)
            return {"status": "ok", "restored": len(affected),
                    "message": f"Restored {len(affected)} recovered channel(s): {renamed} renamed, {moved} moved back to original group. GUI refresh triggered."}
        except Exception as e:
            return {"status": "error", "message": str(e), "restored": 0}
```

- [ ] **Step 3b: Register the restore action**

`plugin.py` action_map — after the two black entries added in Task 3d, add:
```python
                "restore_channels": self.restore_channels_action,
```

- [ ] **Step 3c: Add restore scheduler flag + info section + action to plugin.json**

`plugin.json` — after the `scheduler_export_csv` object (closing `}` at line 1524), insert:
```json
    {
      "id": "scheduler_restore_channels",
      "label": "♻️ Restore Recovered Channels After Scheduled Checks",
      "type": "boolean",
      "default": false,
      "help_text": "After each scheduled check, channels that are Alive again but were previously marked by this plugin have their name tags ([DEAD]/[Slow]/[Blank]/quality) stripped and are moved back to their original group. Runs first, before re-marking. NOTE: a channel parked in a Graveyard/Slow/Black group is only re-checked (and thus restorable) if your scan scope includes that group."
    },
```

`plugin.json` — after the `_section_black` / black settings block added in Task 3e (i.e. after the `move_black_screen_group` object's closing `}`), insert a restore info section:
```json
    {
      "id": "_section_restore",
      "label": "♻️ Restore Recovered Channels",
      "type": "info",
      "description": "Channels that come back Alive can be auto-cleaned: plugin name tags are stripped and the channel is moved back to the original group captured when it was first moved. Use the 'Restore Recovered Channels' action or the scheduler toggle above."
    },
```

`plugin.json` — after the `move_black_screen_channels` action object added in Task 3g, insert:
```json
    {
      "id": "restore_channels",
      "label": "♻️ Restore Recovered Channels",
      "description": "For channels that are Alive again but were previously marked: strip plugin name tags and move them back to their original group.",
      "button_color": "green",
      "confirm": {
        "message": "This will strip plugin tags from recovered channel names and move them back to their original groups. Continue?"
      },
      "button_variant": "filled",
      "button_label": "♻️ Restore Recovered"
    },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_restore_and_black.py -q && python -c "import json; json.load(open('iptv_checker/plugin.json'))"`
Expected: PASS, JSON valid.

- [ ] **Step 5: Validation + commit**

Run: `python -m py_compile iptv_checker/plugin.py && python -m pytest tests -q && python -m ruff check .`

```bash
git add iptv_checker/plugin.py iptv_checker/plugin.json tests/test_restore_and_black.py
git commit -m "feat: Restore Recovered Channels action (strip tags + restore original group)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Scheduler orchestration + delete hygiene + reporting

**Files:**
- Modify: `iptv_checker/plugin.py` — `_execute_scheduled_check` (1134-1180), `delete_dead_channels_action` (~2324, after delete), `_fire_webhook` signature + payload (1572-1611), `view_results_action` summary (1545-1556)
- Test: `tests/test_restore_and_black.py` (+ `tests/test_webhook.py` left untouched unless broken)

**Interfaces:**
- Consumes: `restore_channels_action`, `rename_black_screen_channels_action`, `move_black_screen_channels_action` (Tasks 3-4), `self.channel_state_file`.
- Produces: `_fire_webhook(settings, logger, restored=None)` (added optional param, backward compatible).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_restore_and_black.py`:

```python
# ---- Task 5: webhook restored + delete hygiene --------------------------

def test_webhook_includes_restored_json(plugin, monkeypatch):
    _write(plugin, [{"channel_id": 1, "status": "Alive"}, {"channel_id": 2, "status": "Dead"}])
    sent = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=10):
        sent["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(plugin, "version", "9.9.9", raising=False)
    monkeypatch.setattr(plugin, "key", "iptv_checker", raising=False)
    import iptv_checker.plugin as pm
    monkeypatch.setattr(pm.urllib.request, "urlopen", _fake_urlopen)
    res = plugin._fire_webhook({"webhook_url": "https://example.com/hook"}, _logger(), restored=4)
    assert res["status"] == "ok"
    assert sent["body"]["restored"] == 4


def test_webhook_omits_restored_when_none(plugin, monkeypatch):
    _write(plugin, [{"channel_id": 1, "status": "Alive"}])
    sent = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(plugin, "version", "9.9.9", raising=False)
    monkeypatch.setattr(plugin, "key", "iptv_checker", raising=False)
    import iptv_checker.plugin as pm
    monkeypatch.setattr(pm.urllib.request, "urlopen",
                        lambda req, timeout=10: (sent.update(body=json.loads(req.data.decode())) or _Resp()))
    plugin._fire_webhook({"webhook_url": "https://example.com/hook"}, _logger())
    assert "restored" not in sent["body"]


def test_delete_prunes_restore_state(plugin, tmp_path, monkeypatch):
    plugin.channel_state_file = str(tmp_path / "state.json")
    with open(plugin.channel_state_file, "w") as f:
        json.dump({"2": {"original_group_id": 5}, "3": {"original_group_id": 6}}, f)
    _write(plugin, [{"channel_id": 2, "channel_name": "B", "status": "Dead", "error_type": "Timeout"}])
    with open(plugin.loaded_channels_file, "w") as f:
        json.dump([{"id": 2}], f)
    import iptv_checker.plugin as pm
    monkeypatch.setattr(pm.Channel.objects, "filter",
                        lambda **k: type("Q", (), {"delete": lambda self: (1, {})})(), raising=False)
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)
    res = plugin.delete_dead_channels_action({"auto_delete_confirmation": "DELETE"}, _logger())
    assert res["status"] == "ok"
    state = json.load(open(plugin.channel_state_file))
    assert "2" not in state and "3" in state  # only deleted id pruned
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_restore_and_black.py -q -k "webhook or delete_prunes"`
Expected: FAIL (restored param / pruning not implemented)

- [ ] **Step 3a: Thread `restored` through the webhook**

`plugin.py:1572` — change signature:
```python
    def _fire_webhook(self, settings, logger):
```
to:
```python
    def _fire_webhook(self, settings, logger, restored=None):
```

`plugin.py:1596-1611` — replace the `if is_discord: ... else: ...` payload block with:
```python
        if is_discord:
            content = (
                f"**IPTV Checker — check complete**\n"
                f"Total: {len(results)}  •  ✅ Alive: {alive}  •  ❌ Dead: {dead}  •  ⏭️ Skipped: {skipped}"
            )
            if restored:
                content += f"  •  ♻️ Restored: {restored}"
            payload = json.dumps({"content": content}).encode('utf-8')
        else:
            body = {
                "plugin": self.key,
                "event": "check_complete",
                "total": len(results),
                "alive": alive,
                "dead": dead,
                "skipped": skipped,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
            if restored is not None:
                body["restored"] = restored
            payload = json.dumps(body).encode('utf-8')
```

- [ ] **Step 3b: Wire restore-first + black steps into the scheduler**

`plugin.py:1133-1134` — between the window-gate `return` block and `# Step 4: Rename dead channels`, insert:
```python
            # Step 3b: Restore recovered channels FIRST (heal before re-marking)
            restored_count = 0
            if settings.get('scheduler_restore_channels', False):
                LOGGER.info("⏰ SCHEDULED: Restoring recovered channels...")
                restore_result = self.restore_channels_action(settings, scheduled_logger)
                restored_count = restore_result.get('restored', 0)
                LOGGER.info(f"⏰ SCHEDULED: {restore_result.get('message')}")

```

`plugin.py` — after the low-framerate rename block (`# Step 5: Rename low framerate channels`, ends at line 1144), insert:
```python
            # Step 5b: Rename black-screen channels if enabled
            if settings.get('scheduler_rename_black_screen_channels', False):
                LOGGER.info("⏰ SCHEDULED: Renaming black-screen channels...")
                rename_black_result = self.rename_black_screen_channels_action(settings, scheduled_logger)
                LOGGER.info(f"⏰ SCHEDULED: {rename_black_result.get('message')}")

```

`plugin.py` — after the move-low-framerate block (`# Step 8: Move low framerate channels`, ends at line 1162), insert:
```python
            # Step 8b: Move black-screen channels if enabled
            if settings.get('scheduler_move_black_screen_channels', False):
                LOGGER.info("⏰ SCHEDULED: Moving black-screen channels to group...")
                move_black_result = self.move_black_screen_channels_action(settings, scheduled_logger)
                LOGGER.info(f"⏰ SCHEDULED: {move_black_result.get('message')}")

```

`plugin.py:1176` — in the webhook step, change:
```python
                webhook_result = self._fire_webhook(settings, scheduled_logger)
```
to:
```python
                webhook_result = self._fire_webhook(settings, scheduled_logger, restored=restored_count)
```

- [ ] **Step 3c: Prune restore-state on delete**

`plugin.py` — in `delete_dead_channels_action`, inside the `try:` after `logger.warning(f"DELETED {deleted_count} ...")` and before `self._trigger_frontend_refresh(...)` (around line 2329), insert:
```python
            # Hygiene: drop original-state entries for channels we just deleted.
            try:
                state = self._load_json_file(self.channel_state_file) or {}
                if state:
                    for cid in dead_channel_ids:
                        state.pop(str(cid), None)
                    self._save_json_file(self.channel_state_file, state, indent=2)
            except Exception as e:
                logger.warning(f"Could not prune restore-state after delete: {e}")
```

- [ ] **Step 3d: Show restore hint in view_results summary**

`plugin.py:1545-1549` — change the `summary = [...]` list to append a black-screen count line. Replace:
```python
        summary = [
            f"📊 Last Check Results ({len(results)} streams):",
            f"✅ Alive: {alive}",
            f"❌ Dead: {dead}",
            f"⤼ Skipped: {skipped}\n",
            "📺 Alive Stream Formats:"
        ]
```
with:
```python
        black = sum(1 for r in results if self._is_black_screen(r))
        summary = [
            f"📊 Last Check Results ({len(results)} streams):",
            f"✅ Alive: {alive}",
            f"❌ Dead: {dead}" + (f"  (⬛ {black} black/blank)" if black else ""),
            f"⤼ Skipped: {skipped}\n",
            "📺 Alive Stream Formats:"
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests -q`
Expected: PASS (including untouched `tests/test_webhook.py`).

- [ ] **Step 5: Validation + commit**

Run: `python -m py_compile iptv_checker/plugin.py && python -m pytest tests -q && python -m ruff check .`

```bash
git add iptv_checker/plugin.py tests/test_restore_and_black.py
git commit -m "feat: scheduler restore-first + black steps, webhook restored count, delete state hygiene

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Dry-run, docs, version bump, .wolf

**Files:**
- Create: `RELEASE_NOTES_v<new>.md` (root)
- Modify: `README.md`, `DEVELOPMENT.md`, `CLAUDE.md` (architecture bullet — via bump it updates Current Version; add a manual architecture note)
- Modify: `.wolf/anatomy.md`, `.wolf/memory.md`, `.wolf/cerebrum.md`, `.wolf/buglog.json`
- Run: `python bump_version.py`

- [ ] **Step 1: Logic dry-run (no container)**

Write a temporary scratch script that imports the plugin via the test stubs and exercises the planners end-to-end on synthetic results to print the computed name/group payloads (sanity check beyond unit tests). Run it, eyeball output, delete it. Do NOT commit the scratch file.

Run: `python -m pytest tests -q -k "restore or black or capture"` and confirm the dry-run matches expectations (recovered `[DEAD]`→clean name + original group; black `[Blank]` separated from dead).

- [ ] **Step 2: Version bump**

Run: `python bump_version.py`
Expected: updates `iptv_checker/plugin.json`, `iptv_checker/plugin.py`, and the `CLAUDE.md` Current Version line in sync. Capture the new version string `<NEW>`.

- [ ] **Step 3: Documentation**

- `README.md`: add the three new actions (Rename Black, Move Black, Restore Recovered), the three new settings (`black_screen_rename_format`, `move_black_screen_group`, restore), the three new scheduler toggles, and the §10 operational note about scan scope. Document the dead/black rename-move split as a behavior change.
- `DEVELOPMENT.md`: note the new state file `/data/iptv_checker_channel_state.json` and that move actions now capture original group.
- `CLAUDE.md`: add an architecture bullet describing the restore feature + black-flag split + state file (mirror the style of the existing bullets).
- Create `RELEASE_NOTES_v<NEW>.md` summarizing the feature, settings, actions, and the behavior change.

- [ ] **Step 4: Update .wolf**

- `.wolf/anatomy.md`: add `tests/test_restore_and_black.py`, the two new spec/plan docs, and refresh the `iptv_checker/plugin.py` description.
- `.wolf/memory.md`: append session entries.
- `.wolf/cerebrum.md`: add a Key Learning (black-flag split + restore state file) and a Decision Log entry (stateful group restore, stateless name restore).
- `.wolf/buglog.json`: only if a bug was found/fixed during implementation.

- [ ] **Step 5: Final validation + commit**

Run: `python -m py_compile iptv_checker/plugin.py && python -m pytest tests -q && python -m ruff check . && python -m pytest tests/test_version_sync.py -q`
Expected: all pass, version in sync.

```bash
git add -A
git commit -m "docs: channel restore + black/blank flag (README, DEVELOPMENT, CLAUDE, release notes, version bump)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- G1 Black category → Task 3 (settings, actions, filter split, scheduler flags) ✓
- G2 Restore → Tasks 1-4 (helpers, planners, action) ✓
- G3 Manual + scheduled → Task 4 (action/button) + Task 5 (scheduler flag) ✓
- G4 Tests + docs → all tasks include tests; Task 6 docs ✓
- Original-state capture (§5.2) → Task 2 planner + Task 3 wrapper + move-action wiring ✓
- Reporting (§5.5) → Task 5 (webhook restored, view_results black count); CSV error-type distribution already counts Black Screen ✓
- Delete unchanged but black still deletable (§5.1) → delete keeps `status=='Dead'`; Task 5 only adds state pruning ✓
- Error handling (§8): missing/corrupt state → `or {}`; missing group → name kept, entry dropped, warn; capture/prune best-effort try/except ✓

**Placeholder scan:** No TBD/TODO; all code blocks complete; all test bodies concrete. ✓

**Type consistency:** `_compute_restore_plan` keys (`name_updates`, `group_updates`, `entries_to_clear`, `missing_group_ids`) used identically in Task 2 tests and Task 4 action. `restored` int key consistent across action return, scheduler read, and webhook param. `_is_dead_nonblack`/`_is_black_screen` names consistent across Tasks 1/3/5. State entry shape `{original_group_id, original_group_name, moved_at}` consistent across capture (Task 2/3) and restore (Task 4). ✓

## Execution Handoff

Plan complete and saved. This will be executed inline in this session (subagent-driven review is provided separately via the QA/simplify agent steps the user requested).
