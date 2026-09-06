# Changelog

Newest first. Versions are calver Major.YY.DDDHHMM (UTC).

## 1.26.2481600 (2026-09-05)

Five changes. Two of them stop the plugin acting on stale or incomplete
evidence, one makes a whole class of unusable schedule impossible to save, one
is housekeeping for exported files, and one is a pass over the interface that
removed four statements that were not true.

Read the behaviour changes at the end before upgrading. One setting has been
removed, and several buttons have changed colour.

### A scan that was cut short no longer applies its verdicts

The scan runs in its own thread; the scheduled check only waits for it. That
wait ended either when the scan finished or when the scheduler was told to stop,
and the two were treated identically. Stopping or restarting the scheduler
therefore let the plugin carry on as though the scan had completed.

It did not act on partial data, which would have been the obvious guess. The
results file is written once, at the end of a scan, so while a scan is in flight
that file still holds the previous run's complete results. The plugin was
therefore re-applying an earlier run's verdicts at an arbitrary later time,
which can delete a channel that has since recovered.

A session that did not write new results now writes its CSV, which is the record
of what was probed, and stops there. No report is emailed, and no rename, move,
restore or delete runs.

**This happens more often than it sounds.** Opening the Dispatcharr Plugins page
causes a plugin discovery pass, which unloads every plugin, which stops the
scheduler. On the day this was released the guard fired for real twenty minutes
after deployment, on a scan 275 streams into 5,275.

### Schedules that can never run are refused, and described when they can

The schedule field checked only that an expression had five fields. It did not
look at what was in them. These were all accepted, reported as saved, and then
never ran:

| What you type | What used to happen |
|---|---|
| `0 25 * * *` | Saved. Hour 25 matches no hour, so it never ran. |
| `"0 22 * * 0,2,4"` | Saved with the quotation marks as part of the fields. Never ran. |
| `0 22 * * SUN,TUE,THU` | Saved. This plugin matches numbers, not day names. Never ran. |
| `0 22 * * 0,2,4,` | Saved. The trailing comma leaves an empty value. Never ran. |
| `0 22 * * 0, 2, 4` | Saved, and ran **on Sundays only**. The spaces split it and the rest was dropped silently. |

Each is now refused when you save it, with a message naming what is wrong.

Saving a schedule also shows what it means:

```
Cron Schedules: 0 22 * * 0,2,4 (Sun, Tue and Thu at 10:00 PM)
```

Anything the plugin cannot describe with certainty is shown as written rather
than guessed at.

**Day of week 7 now means Sunday**, as in standard cron. It previously matched
nothing, so a schedule written that way never fired.

### Old CSV exports can be deleted automatically

A new setting, **Delete CSV Exports Older Than (Days)**, removes this plugin's
own exports from `/data/exports/` once they pass that age. It runs straight
after each export rather than on a schedule, because files only accumulate when
one is written.

It defaults to **0, meaning keep everything**, so nothing is deleted unless you
ask for it.

Four rules apply, and each has a test:

- Only files named `iptv_checker_results_*.csv` are ever removed. That directory
  is shared with other plugins, and their files are not touched.
- The file just written is never deleted.
- At least one file always survives, so a small number cannot empty the
  directory.
- A failure to tidy up never turns a successful export into a reported error.

### The settings form, the buttons and the CSV record

An interface pass found four statements that were not true and corrected them.

**A setting that did nothing has been removed.** `scheduler_export_csv` had a
label, help text and a default of off, and no code read it. The scheduled CSV
export is unconditional by design, because that file is the record of what was
probed when a destructive action follows. The control promised the opposite of
the behaviour in both directions.

**The Scheduling heading advertised webhook actions.** The webhook feature was
removed from this plugin a month earlier and no code mentions it.

**The group selection panel described the danger backwards.** It said a typo
narrows a run. In exclude mode a pattern that matches nothing excludes nothing,
so the run covers every group, and all of them become eligible for the
scheduled rename, move and delete. The panel now says so.

**Seven confirmation dialogs claimed their action was irreversible.** Renaming
and moving are undone by Restore Recovered Channels. Those dialogs now say how
to undo the action instead, with one exception stated plainly: Restore does not
strip the video format suffix, because it acts on channels that failed rather
than on channels that play.

The form is now grouped under 17 headings rather than 15. The block of twelve
automatic actions split into three: what a run leaves behind, the channel
changes that can be undone, and deletion, which cannot.

The CSV preamble now says what the file is and that the hash lines are a
preamble a spreadsheet should skip, states what the run did before how it was
configured, renders settings as Yes and No rather than as Python values, and
**records which detectors ran and which automatic changes were armed**. All four
detectors are optional, so a report listing no blank screens previously meant
either that none were found or that nobody looked, and the file did not say
which. It also no longer fails when the results list is empty.

### A setting that is off is now read as off

Every boolean setting was read with plain truthiness. A value stored as the
string `false` is a non-empty string, so it was read as on. Nine of the
thirty affected reads gate a scheduled rename, move or delete.

All of them now use one parser, and the CSV record renders through that same
parser, so the file and the behaviour cannot disagree about what a setting says.

On the installation where this was found, Dispatcharr stores these as real
booleans, so the fault was not occurring there. It is a correctness fix for
anyone whose values are stored differently.

### Behaviour changes

> **A setting has been removed.** `scheduler_export_csv` is gone from the form.
> It never did anything: the scheduled CSV export always ran and still does.
> Your stored value remains in the database, unread.

> **Button colours have changed.** Red now marks only Delete Dead Channels, the
> one action that removes channels. The rename and move actions are orange,
> because Restore Recovered Channels undoes them. Clear CSV Exports is orange
> rather than red, because it deletes this plugin's own export files and no
> channel data.

> **A schedule that cannot run is now refused at save time.** If you have one
> saved, it is still loaded, because refusing it outright would leave an
> installation running nothing at all. You will see the error the next time you
> press Save Schedule, which is the point at which you can fix it.

> **A schedule written with day of week 7 now fires on Sundays.** It previously
> fired on nothing.

> **A boolean setting stored as the string `false` is now read as off.** If any
> of your settings were stored that way, an action you had switched off may have
> been running, and will now stop.

> **A scan stopped part way no longer emails a report or applies channel
> changes.** A window that closes normally still does both.

### Upgrading

Delete the old plugin on the Plugins page, restart the container, then import
the new zip. Your settings are preserved.

Nothing in this release requires a configuration change. The new export
retention setting defaults to keeping every file.

## 1.26.2402308 (2026-08-28)

- See the GitHub release for v1.26.2402308.

## 1.26.2201040 (2026-08-08)

- See the GitHub release for v1.26.2201040.

## 1.26.2191412 (2026-08-07)

- See the GitHub release for v1.26.2191412.

## 1.26.2191411 (2026-08-07)

- See the GitHub release for v1.26.2191411.

## 1.26.2191335 (2026-08-07)

- See the GitHub release for v1.26.2191335.

## 1.26.2191151 (2026-08-07)

- See the GitHub release for v1.26.2191151.

## 1.26.2181303 (2026-08-06)

- See the GitHub release for v1.26.2181303.

## 1.26.2171636 (2026-08-05)

- See the GitHub release for v1.26.2171636.

## 1.26.1851155 (2026-07-04)

- See the GitHub release for v1.26.1851155.

## 1.26.1841039 (2026-07-04)

- See the GitHub release for v1.26.1841039.

## 1.26.1741204 (2026-06-23)

Five fixes surfaced by reviewing the nightly CSV audit export. Two are
user-visible behavior changes (radio handling, low-framerate threshold); the
rest are correctness/audit fixes.

### Audio-only / radio streams are now Skipped, not Dead

Streams that ffprobe validates but that carry **no video track** - e.g. radio
stations like BBC Radio 1 - were being classified **Dead** (`No Video Stream`),
which meant the dead-channel rename/move/delete actions would tag, relocate, or
remove perfectly working audio streams. They are now classified **Skipped**, so
destructive actions leave them alone.

`Skipped` now has **three** triggers: Streamlink-only hosts, HTTP 429
rate-limiting, and audio-only streams.

> **Behavior change:** if you previously *wanted* audio-only channels removed,
> they will now persist as Skipped. Exclude those groups from the scan if you
> don't want them checked at all.

### PAL-safe low-framerate threshold (30 -> 24 fps)

The low-framerate flag previously triggered below **30 fps**, which mis-tagged
every 25 fps PAL/European broadcast (ITV, BBC, S4C, ...) and 24 fps film-rate feed
as `[Slow]`. The threshold is now **24 fps** (`PluginConfig.LOW_FRAMERATE_THRESHOLD`),
so only genuinely choppy streams qualify. NTSC 23.976 is safe too - `framerate_num`
is stored rounded to 1 decimal, so 23.976 -> 24.0.

> **Behavior change:** ~10% of alive streams in a typical run (the 24/25 fps
> population) are no longer eligible for the `[Slow]` rename / Slow group.

### CSV export fixes

- The export header no longer duplicates the `ffprobe_monitoring_seconds`
  column. It was hardcoded in the base columns **and** re-collected by the
  `ffprobe_`-prefix auto-collector, so it appeared twice (identical values, so
  no data loss, but it broke `DictReader`-based consumers).
- The audit preamble's `FFprobe Flags:` line now reports the flags actually
  used - both the probe and the preamble share `PluginConfig.DEFAULT_FFPROBE_FLAGS`
  instead of disagreeing fallbacks.

### "View Last Results" shows the check date

The summary now includes a `Checked: <date>` line (the check's completion
time, falling back to the results-file timestamp).

### Tests

- New `tests/test_csv_and_status_fixes.py` (no-dup header, PAL-safe low-fps,
  ffprobe-flags default, View-Last-Results date).
- `test_bitrate_calc.py::test_no_video_stream_is_dead` -> `..._is_skipped`.
- Full suite: **146 passing**, ruff clean. QA'd by a code-reviewer subagent.

### Upgrade notes

No migration, no saved-setting changes. Deploy both `plugin.py` and `plugin.json`
from `iptv_checker/` and restart the container. Verified live on real streams
(BBC Radio 1/1Xtra -> Skipped; ITV/WDIV -> Alive).

## 1.26.1721834 (2026-06-21)

Cosmetic / UX release: clearer terminology and a reorganized settings screen. No
functional behavior changes.

### "Black Screen" -> "Blank Screen" in the UI

Every user-facing reference to "Black Screen" / "Black-Screen" in the settings and
actions now reads **"Blank Screen" / "Blank-Screen"** (the detection toggle, the
sample/required/timeout fields, the handling section, the rename/move actions and
their buttons, and the scheduler toggles).

**Kept stable on purpose** (so nothing breaks): the internal setting/action IDs, the
detection logic, the `[Blank]` rename tag, and the destination group default
**"Black Screens"**. The classification value is still `error_type = 'Black Screen'`
in code - so the **results table and CSV still show "Black Screen"** (it's the stored
error code). That divergence is intentional, not a missed rename.

### Settings screen reorganized

The settings tab was getting unwieldy. Fields are now grouped by lifecycle:

```
Group Selection -> Check Behavior -> Blank-Screen Detection
-> Post-Check Actions  (Dead, Blank, Low-Framerate, Format, Restore - all together)
-> Webhook -> Scheduling & Automation  (+ an "Auto-run after scheduled checks" sub-section)
-> Advanced  (ffprobe flags / analysis duration / streamlink hosts moved here)
```

The two scattered black-screen sections are now distinct and sensibly placed (detection up in
the scan area, handling in Post-Check Actions); the previously-orphaned Restore
section sits at the end of the Post-Check block; and the ~11 scheduler toggles are
grouped under their own sub-header. Reordering is purely cosmetic - Dispatcharr keys
settings by id, so saved values are unaffected.

Also fixed a stale help-text reference ("Scheduler Timezone above" -> "Dispatcharr's
timezone"; that field was removed in v1.26.1721651).

### Tests

- New `tests/test_settings_schema.py`: freezes the field/action id set (a reorder can
  never silently drop/rename an id) and asserts no "Black Screen"/"Black-Screen"
  vocabulary leaks back into a user-facing label/description/button. Full suite:
  **141 passing**, ruff clean.

### Upgrade notes

No migration, no saved-setting changes. Deploy both `plugin.py` and `plugin.json`
from `iptv_checker/` and restart the container.

## 1.26.1582047 (2026-06-07)

- See the GitHub release for v1.26.1582047.

## 1.26.1421301 (2026-05-22)

- See the GitHub release for v1.26.1421301.

## 1.26.1362003 (2026-05-16)

- See the GitHub release for v1.26.1362003.

## 1.26.1221101 (2026-05-02)

- See the GitHub release for v1.26.1221101.

## 1.26.1161403 (2026-04-26)

- See the GitHub release for 1.26.1161403.

## 1.26.1081815 (2026-04-18)

- See the GitHub release for 1.26.1081815.

## 0.8.0 (2026-04-04)

- See the GitHub release for 0.8.0.

## 0.7.0 (2026-03-29)

- See the GitHub release for 0.7.0.

## 0.6.0c (2026-03-13)

- See the GitHub release for 0.6.0c.

## 0.6.0b (2026-03-13)

- See the GitHub release for 0.6.0b.

## 0.6.0a (2026-03-09)

- See the GitHub release for 0.6.0a.

## 0.5.1 (2025-12-21)

- See the GitHub release for 0.5.1.

## 0.5.0 (2025-12-17)

- See the GitHub release for 0.5.0.

## 0.4.0 (2025-12-08)

- See the GitHub release for 0.4.0.

## 0.3.1 (2025-11-11)

- See the GitHub release for 0.3.1.

## 0.3.0 (2025-11-10)

- See the GitHub release for 0.3.0.

## 0.2.1 (2025-09-25)

- See the GitHub release for 0.2.1.

### 0.2 (2025-09-24)

- See the GitHub release for 0.2.

### 0.1 (2025-09-19)

- See the GitHub release for v0.1.
