# Channel Restore + Black/Blank Stream Flag — Design Spec

> Date: 2026-06-21
> Plugin: IPTV Checker (Dispatcharr) — `iptv_checker/plugin.py` (single-file plugin) + `iptv_checker/plugin.json`
> Status: Approved (design), ready for implementation plan

## 1. Problem / Motivation

The plugin can mark problem channels in three ways: rename dead channels (`[DEAD]`),
move them to a group ("Graveyard"), and the equivalents for low-framerate channels
(`[Slow]` / "Slow" group). It also tags Alive channels with quality suffixes
(`[UHD]/[FHD]/[HD]/[SD]`). Black-screen streams currently collapse into the generic
`Dead` bucket (`error_type='Black Screen'`).

Two gaps:

1. **No way back.** Once a channel is renamed/moved, there is no inverse operation.
   When a stream recovers (comes back `Alive`), it keeps its `[DEAD]`/`[Slow]` tag and
   stays stranded in the Graveyard/Slow group. Nothing currently stores a channel's
   original name or original group, so an exact restore is impossible today.
2. **Black streams are indistinguishable from dead streams.** A black/blank stream is
   technically reachable but useless; the user wants it categorized separately from a
   hard-dead stream (own tag, own group), not lumped under `[DEAD]`.

## 2. Goals

- **G1** — A first-class "Black/Blank" category: own rename tag, own move-group, own
  manual actions, own scheduler flags. Parallel in every way to the Dead and Slow tracks.
- **G2** — A "Restore Recovered Channels" feature: for channels whose latest status is
  `Alive` but which were previously marked by this plugin, strip the plugin's name tags
  back to a clean base name **and** move the channel back to its **exact original group**.
- **G3** — Manual button **and** scheduled (self-healing) execution for restore, mirroring
  the existing post-action pattern.
- **G4** — Regression-tested pure helpers and updated documentation.

## 3. Non-Goals

- No new top-level stream `status` value (reuse `Alive`/`Dead`/`Skipped`). Black stays
  `status='Dead', error_type='Black Screen'`.
- No re-checking logic change. (Operational note in §10: a channel parked in Graveyard is
  only restorable if the scan scope re-includes that group.)
- No per-channel original *name* storage — name restoration is stateless tag-stripping
  (more robust, matches the existing issue-#18 suffix stripper). Only the original *group*
  is persisted, because it cannot be recovered any other way.

## 4. Design Decisions (confirmed with user)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Group restore mechanism | **Stateful** — persist original group id/name on move; restore exact group (fallback if deleted). |
| D2 | Black handling | **Separate tag + separate group**; excluded from `[DEAD]` rename/move to avoid double-tagging. |
| D3 | Restore name scope | **Strip all enabled plugin tags** (status + quality) → clean base name. |
| D4 | Restore trigger | **Manual button + scheduler flag.** |

## 5. Architecture

### 5.1 Black/Blank category (G1)

New `plugin.json` settings:
- `black_screen_rename_format` — string, default `"{name} [Blank]"`, must contain `{name}`.
- `move_black_screen_group` — string, default `"Black Screens"`.

New scheduler flags (`plugin.json`, booleans, default `false`):
- `scheduler_rename_black_screen_channels`
- `scheduler_move_black_screen_channels`

New actions (`action_map` + `plugin.json` actions array, with confirm dialogs & buttons):
- `rename_black_screen_channels` → `rename_black_screen_channels_action`
- `move_black_screen_channels` → `move_black_screen_channels_action`

**Filter split (behavior change — documented in release notes):**
- Dead rename/move predicate becomes `status=='Dead' AND error_type != 'Black Screen'`.
- Black rename/move predicate is `status=='Dead' AND error_type=='Black Screen'`.
- `delete_dead_channels` is **unchanged** (`status=='Dead'`, includes black) — black streams
  are genuinely dead and remain deletable.

Refactor these predicates into pure module-level helpers so they're unit-testable:
- `_is_dead_nonblack(result)`
- `_is_black_screen(result)`

### 5.2 Original-state capture (B / enables G2)

New state file `/data/iptv_checker_channel_state.json`, written atomically via the
existing `_save_json_file`. Path added to `PluginConfig` and bound in `Plugin.__init__`
as `self.channel_state_file`.

Schema (object keyed by stringified channel id):
```json
{
  "12": {
    "original_group_id": 5,
    "original_group_name": "USA Sports",
    "moved_at": "2026-06-21T05:00:12Z"
  }
}
```

New helper `_capture_original_state(channel_ids, settings, logger)`:
1. Load current state (default `{}` on missing/corrupt).
2. Resolve each channel's *current* `channel_group_id` (+ group name) from the DB.
3. Build the set of **managed destination group names** from settings
   (`move_to_group_name`, `move_low_framerate_group`, `move_black_screen_group`), lower-cased.
4. For each channel id **not already in state** whose current group name is **not** a
   managed destination: record `{original_group_id, original_group_name, moved_at}`.
5. Persist. (Channels already tracked, or already sitting in a managed group, are skipped —
   so a second move never clobbers the true original.)

Called at the top of each move action (dead/slow/black) **before** the bulk group update.

**Hygiene:** `delete_dead_channels_action` removes state entries for the channel ids it
deletes (best-effort, wrapped so a state failure never aborts the delete).

### 5.3 Restore action (G2/G3)

New `plugin.json` setting:
- Scheduler flag `scheduler_restore_channels` (bool, default `false`).
- (No restore-specific string settings — tags are derived; group is from state.)

New action `restore_channels` → `restore_channels_action` (+ confirm dialog & button).

New pure helper `_derive_strippable_tags(settings)` → returns a compiled
case-insensitive regex matching one-or-more trailing ` [TAG]` groups. Tag labels =
bracket labels parsed from `dead_rename_format` + `low_framerate_rename_format` +
`black_screen_rename_format` + comma-split `video_format_suffixes`, **unioned with** the
standard set `{DEAD, Slow, Blank, UHD, FHD, HD, SD, Unknown}` (defensive — same approach
as the existing suffix stripper so a previously-applied standard tag is always strippable).

New pure helper `_derive_status_tags(settings)` → the subset of tags that mark a channel
as a *problem* (from the three problem-formats + `{DEAD, Slow, Blank}`), used only for
eligibility.

`restore_channels_action` logic:
1. Load results; collect `alive_ids = {r.channel_id : r.channel_name for status=='Alive'}`.
2. Load channel-state file.
3. Build the strip regex and the status-tag detector.
4. For each alive channel, it is **eligible** iff it has a state entry **OR** its current
   DB name carries a trailing *status* tag. (A healthy `[HD]`-only channel that was never
   marked is **not** eligible.)
5. For each eligible channel:
   - **Name**: `new_name = strip_regex.sub('', current_name).rstrip()`; queue a name update
     if changed.
   - **Group**: if a state entry exists and `original_group_id`'s group still exists, queue a
     `channel_group_id` update back to it; remove the state entry. If the group was deleted,
     keep the name restore, skip the move, log a warning, drop the stale entry.
6. Bulk-update names + groups (reuse `_bulk_update_channels`), persist trimmed state, refresh
   frontend. Return a count summary.

### 5.4 Scheduler orchestration (G3)

In `_execute_scheduled_check`, post-action order (after the always-on CSV export):
1. **Restore recovered channels** — if `scheduler_restore_channels` *(NEW, runs first: heal
   before marking)*.
2. Rename dead (excl. black) — if `scheduler_rename_dead_channels`.
3. Rename low framerate — if `scheduler_rename_low_framerate_channels`.
4. **Rename black** — if `scheduler_rename_black_screen_channels` *(NEW)*.
5. Add video format suffix — if `scheduler_add_video_format_suffix`.
6. Move dead (excl. black) — if `scheduler_move_dead_channels` *(captures state)*.
7. Move low framerate — if `scheduler_move_low_framerate_channels` *(captures state)*.
8. **Move black** — if `scheduler_move_black_screen_channels` *(NEW, captures state)*.
9. Delete dead (incl. black) — if `scheduler_delete_dead_channels`.
10. Webhook — if `scheduler_fire_webhook`.

Restore acts on `Alive` channels; rename/move/delete act on `Dead` channels — disjoint sets,
so ordering is safe. Restore-first is the logical "heal then mark" sequence.

### 5.5 Reporting (E)

- `_fire_webhook` payload gains a `restored` integer (Discord text line + JSON key). The
  scheduled path threads the restore action's count into the webhook call.
- CSV header comments (`_generate_csv_header_comments`) and `view_results_action` summary gain
  a Restored line. Black streams already appear in the CSV error-type distribution via
  `error_type='Black Screen'` — no extra work there, but the dead/black split is noted.

## 6. Components & Interfaces (new/changed)

| Symbol | Kind | Purpose |
|--------|------|---------|
| `_is_dead_nonblack(r)` | pure fn | predicate: dead but not black |
| `_is_black_screen(r)` | pure fn | predicate: dead + black screen |
| `_derive_strippable_tags(settings)` | pure fn | compiled regex of all strippable trailing tags |
| `_derive_status_tags(settings)` | pure fn | compiled regex of problem-only trailing tags (eligibility) |
| `_capture_original_state(channel_ids, settings, logger)` | method | persist original group before a move |
| `rename_black_screen_channels_action` | action | tag black channels |
| `move_black_screen_channels_action` | action | move black channels (captures state) |
| `restore_channels_action` | action | strip tags + restore original group for recovered channels |
| `self.channel_state_file` | attr | `/data/iptv_checker_channel_state.json` |

Changed: `rename_channels_action`, `move_dead_channels_action`,
`move_low_framerate_channels_action` (state capture + black exclusion),
`delete_dead_channels_action` (state hygiene), `_execute_scheduled_check`,
`_fire_webhook`, `_generate_csv_header_comments`, `view_results_action`, `action_map`,
`PluginConfig`, `Plugin.__init__`.

## 7. Data Flow

```
check run → results.json (status/error_type per channel)
   ├─ move_* actions ──> _capture_original_state ──> channel_state.json (original group)
   │                      then bulk set channel_group_id = managed group
   ├─ rename_* actions ─> bulk set name = format.replace({name})
   └─ restore action ───> for Alive+marked channels:
                            strip tags from name; read channel_state.json;
                            set channel_group_id back; drop state entry
```

## 8. Error Handling

- State file missing/corrupt → treat as `{}` (mirrors `_load_json_file` behavior elsewhere).
- Original group deleted → name restored, move skipped, warning logged, stale entry dropped.
- State capture/hygiene failures are caught and logged; they never abort the primary
  rename/move/delete operation.
- All new actions return the standard `{"status": "ok|error", "message": ...}` shape.
- New persisted writes use `_save_json_file` exclusively (never raw `open('w')` — see the
  issue-#21 EACCES learning).

## 9. Testing

New pure-helper tests in `tests/` (no container needed; `Plugin.__new__` pattern where a
method is exercised):
- `_derive_strippable_tags` / `_derive_status_tags`: parsing of default and custom formats;
  trailing-only stripping; multi-tag stacks; case-insensitivity.
- `_is_dead_nonblack` / `_is_black_screen`: the error_type split, including missing
  `error_type`.
- Restore eligibility: alive+`[DEAD]` → eligible; alive+`[HD]`-only, never marked →
  not eligible; alive+state-entry → eligible.
- Name restoration: `"ESPN [DEAD]"` → `"ESPN"`; `"ESPN [HD] [Blank]"` → `"ESPN"`;
  untouched when no tag.
- `_capture_original_state` guards (pure-ish, with stubbed group resolution): never overwrite
  existing entry; never record a managed destination group.

Validation gate: `python -m py_compile iptv_checker/plugin.py && python -m pytest tests -q &&
python -m ruff check .`

## 10. Operational Note (documented in README)

A channel moved to "Graveyard"/"Slow"/"Black Screens" is only re-probed — and therefore only
eligible for auto-restore — if the scheduled scan scope **includes** that group. Recommend
users either add the managed groups to their scan scope or run restore after a full-scope scan
so self-healing actually fires.

## 11. Rollout

- Version bump via `python bump_version.py` (calver `1.26.DDDHHMM`).
- Release notes documenting the new actions/settings and the dead/black rename-move split as a
  behavior change.
- `.wolf/` anatomy/memory/cerebrum/buglog updates per OpenWolf protocol.
