# IPTV Checker v1.26.1721554 — Release Notes

Two linked features: a first-class **Black/Blank stream category**, and
**Restore Recovered Channels** — automatic "un-marking" of channels that come
back to life.

## 1. Restore Recovered Channels (self-healing)

Once a channel was renamed `[DEAD]`/`[Slow]`/`[Blank]` and exiled to a
Graveyard/Slow/Black group, there was no automatic way back when its stream
recovered. It stayed tagged and stranded. Now there is.

### What it does

For every channel whose **latest status is Alive** but which was previously
marked by this plugin (it has a stored original group **or** its name still
carries a plugin status tag), the **Restore Recovered Channels** action:

1. **Strips all plugin name tags** — `[DEAD]`, `[Slow]`, `[Blank]`, and quality
   tags `[UHD]/[FHD]/[HD]/[SD]` (including custom labels parsed from your
   configured rename formats) — back to the clean base name.
2. **Moves it back to its exact original group**, remembered from the moment it
   was first moved.

A healthy channel that merely has a `[HD]` suffix and was **never** marked is
left untouched — eligibility is deliberately conservative.

### How the original group is remembered

Each **Move** action (dead / low-framerate / black) now records a channel's
current group to `/data/iptv_checker_channel_state.json` *before* relocating it.
It never overwrites an existing capture and never records a managed destination
group (Graveyard/Slow/Black Screens) as the "original".

### Manual or scheduled

- **Manual:** the new **♻️ Restore Recovered** action button.
- **Scheduled:** the new **Restore Recovered Channels After Scheduled Checks**
  toggle. It runs **first** in the post-check sequence — heal, then re-mark.

### Edge cases

- Original group deleted in the meantime → name is still restored, the move is
  skipped, and a warning is logged.
- Deleting a dead channel prunes its stored state.
- **Operational note:** a channel parked in a Graveyard/Slow/Black group is only
  re-checked — and therefore only restorable — if your scan scope **includes**
  that group. Add managed groups to your scheduled scan scope (or run a
  full-scope scan) so self-healing fires.

## 2. Black / Blank streams are now their own category

Previously, black/blank streams detected by black-screen detection were
classified `Dead` and tagged `[DEAD]` / moved to Graveyard with every other dead
channel. They are now a **first-class category** with their own tag and group.

### New actions & settings

| Setting | Default | Notes |
|---|---|---|
| Black-Screen Channel Rename Format | `{name} [Blank]` | Tag applied by **Rename Black-Screen Channels**. |
| Move Black-Screen Channels to Group | `Black Screens` | Destination for **Move Black-Screen Channels**. |

New action buttons: **⬛ Rename Black**, **⬛ Move Black**. New scheduler toggles:
**Rename Black-Screen Channels** and **Move Black-Screen Channels**.

### ⚠️ Behavior change

The Dead **rename** and **move** actions now **exclude** black/blank channels
(`error_type = Black Screen`) so they aren't double-tagged. **Delete Dead
Channels is unchanged** — black streams are genuinely dead and remain deletable.

If you have black-screen *detection* enabled and relied on black streams getting
`[DEAD]`/Graveyard, either enable the new black rename/move toggles, or set the
**Black-Screen Channel Rename Format** to `{name} [DEAD]` and the black group to
`Graveyard` to reproduce the old behavior.

## Reporting

- The scheduled **webhook** payload gains a `restored` count (Discord hides it
  when zero; the generic JSON payload always includes the key for machine
  consumers).
- **View Last Results** shows a black/blank sub-count on the Dead line.
- The CSV audit header lists the new black-screen rename format and group; the
  Black Screen count already appears in the error-type distribution.

## Tests

- New `tests/test_restore_and_black.py`: pure predicates/tag-derivation,
  `_compute_restore_plan` / `_compute_capture_state` planners, the black
  rename/move actions, the restore action, webhook `restored`, and delete state
  hygiene. Full suite: **119 passing**, ruff clean.

## Upgrade notes

No migration. The new state file is created on first use. Black handling is only
active when black-screen detection is on **and** you enable the black toggles;
restore only acts when you run its action or enable its toggle. Deploy both
`plugin.py` and `plugin.json` from the `iptv_checker/` folder (hot-reload fires
on `plugin.json` mtime), then restart the container.
