# IPTV Checker user guide

Everything the plugin does, what each setting means, and what to do when something looks wrong.
The [README](../README.md) is the short version.

---

## Contents

- [How a stream is judged](#how-a-stream-is-judged)
- [How a channel is judged](#how-a-channel-is-judged)
- [Choosing which groups to check](#choosing-which-groups-to-check)
- [Running a check](#running-a-check)
- [Detection modes](#detection-modes)
- [Acting on the results](#acting-on-the-results)
- [The HTML report and email](#the-html-report-and-email)
- [Scheduling](#scheduling)
- [Windowed runs](#windowed-runs)
- [Provider connection limits](#provider-connection-limits)
- [Every setting](#every-setting)
- [Every button](#every-button)
- [File locations](#file-locations)
- [Troubleshooting](#troubleshooting)

---

## How a stream is judged

Every stream ends up in one of three states.

### Alive

`ffprobe` connected and found a video track. The plugin records resolution, framerate, codecs,
pixel format, audio details and bitrate, and writes them back into Dispatcharr so the channel menu
can display them.

Bitrate deserves a note. Live MPEG-TS and HLS streams almost never publish a bitrate, so the plugin
measures it from the packets it read. A probe that captures fewer than 30 video packets, roughly one
second at 30fps, leaves the bitrate unset rather than storing a noisy number. Very short samples
produce wildly inflated figures.

### Dead

Either the stream did not connect, or it connected and delivered nothing worth watching. The second
group is only detected when you turn the matching option on:

| Reason | What it means |
|---|---|
| Timeout, connection refused, 404, 403 and similar | The stream did not connect |
| **Blank Screen** | Connected and decoded to a pure black picture |
| **Frozen Video** | Connected and the picture never changes |
| **Silent Audio** | Connected, the audio track is healthy, and it is completely silent |
| **Placeholder File** | Connected, but it is a fixed-length file rather than a live stream |

### Skipped

**The checker could not judge it, which is not the same as a fault.** Three unrelated situations
produce this, and none of them is evidence of a problem:

| Reason | What it means | What to do |
|---|---|---|
| **Rate Limited** | The provider answered with HTTP 429. The stream itself may be perfect. | Re-run later, or lower the worker count |
| **No Video Stream** | Audio with no video. This is a working radio station. | Nothing |
| **Skipped** | The host needs Streamlink, which `ffprobe` cannot read | Nothing |

**No destructive action ever touches a Skipped stream.** This matters most for rate limiting: if a
throttled stream counted as dead, a scheduled run could delete a large part of your lineup on a
night the provider was busy.

---

## How a channel is judged

**A channel is only as broken as its best stream.**

Most channels carry a primary stream and one or more backups, and Dispatcharr fails over between
them. So the plugin judges a channel by **all** of its streams together:

- **Any stream Alive** means the channel works. It is never reported dead, whatever the others did.
- **Any stream Skipped, with none Alive** means the channel is **unproven**. A rate-limited stream
  might be fine, so nothing destructive touches it.
- **Every stream Dead** is the only case that counts as dead.

The same rule applies to the other categories. A channel is only reported as low framerate when
every stream that actually plays is slow: if one plays at full rate, Dispatcharr can use it.

---

## Choosing which groups to check

Two settings work together.

**Channel Groups** is a comma-separated list. Leave it empty to check every group.

**Channel Groups Mode** decides how that list is read:

- **Check only the groups listed above** processes just those groups. A group you create later is
  skipped until you add it.
- **Check all groups except those listed above** processes everything else. A group you create later
  is picked up automatically.

Wildcards work in both modes:

| Pattern | Matches |
|---|---|
| `US-*` | `US-Movies`, `US-Sports`, `US-News` |
| `*Sports*` | any group with "Sports" anywhere in the name |
| `Movies-??` | `Movies-US`, `Movies-UK` |
| `US-*, UK-*, *Sports*` | any of the three |

Matching is case-sensitive. An empty list means every group in **both** modes.

**A pattern that matches nothing is reported as an error in both modes.** In exclude mode a typo
would quietly leave a group being checked and acted on, which is the outcome you were avoiding.

> **Upgrading from an older version.** These two settings replaced separate "Group(s) to Check" and
> "Group(s) to EXCLUDE" boxes. Your old values are read automatically until you save the new
> setting, so nothing changes on upgrade. An install that used **both** old boxes keeps both applied,
> because one list plus a mode cannot express "these, except those". Save the new setting to finish
> the move.

---

## Running a check

1. **Validate** confirms the plugin can reach the database and tells you **how many groups will be
   checked**, so a filter that selects far more or far fewer than you expected is obvious before you
   start.
2. **Load Groups** gathers the channels and their streams. Large lists load in the background.
3. **Start Check** runs the probes in a background thread and returns immediately with an estimate.
4. **View Progress** shows a live percentage and estimated finish time. It stays honest through
   retry passes rather than jumping to 100% early.
5. **Cancel Check** stops cleanly. Already-probed results are kept.

Streams that fail with a transient error are queued and retried after the rest of the list, giving
the provider time to recover, rather than being hammered immediately.

---

## Detection modes

All four are **off by default**. Each one costs time, and three of them cost an extra provider
connection per stream.

**A zero from a detector that is switched off means nobody looked.** That is not the same as
"measured and found none", and the HTML report says which detectors actually ran.

### Placeholder file

**Costs nothing extra.** A live stream never reports a fixed duration; a finite file does. Providers
commonly serve a dead channel as a short looping file, and this catches it from data the normal
probe already collected.

Turn it off if any of your channels is legitimately fed by a finite file you want to keep.

### Blank screen

Decodes a few seconds of each alive stream and flags a pure black picture. Catches streams that
report a perfectly valid signal but display nothing.

Only pure black is detected. A dark grey "no signal" card is not. A channel that is legitimately
black for a few seconds, such as a fade from black, can trip it: raise the required continuous run
or the sample length.

### Frozen video

Flags a picture that has stopped moving but is not black, which blank-screen detection cannot see.
Shares the same decode pass, so enabling it alongside blank-screen detection costs nothing extra.

Blank screen takes precedence: a black picture is also a still picture, and it is reported as blank
rather than frozen.

### Silent audio

Flags a healthy audio track carrying no sound, at or below the configured level. The default of
-70 dBFS sits between digitally silent audio, which measures about -91 dB, and the quietest real
content measured, about -44 dB.

**This marks a channel dead even though its picture is fine**, so the delete action would remove a
channel that still shows video. Streams with no audio track at all are skipped, because they cannot
be silent.

### Fail-open

If `ffmpeg` is missing, errors, or exceeds its timeout, **the stream is left Alive**. A tooling
problem never kills a working channel.

---

## Acting on the results

Every action reads the last check. None of them re-probes anything.

| Action | Acts on |
|---|---|
| **Rename Dead** / **Move Dead** | Channels where every stream failed, excluding blank screens |
| **Rename Blank** / **Move Blank** | Channels where every stream decoded to a blank picture |
| **Rename Slow** / **Move Slow** | Channels where every playable stream is below 24fps |
| **Add Format Suffix** | Alive channels, tagging `[UHD]`, `[FHD]`, `[HD]`, `[SD]` |
| **Delete Dead** | Channels where every stream is Dead, including blank screens |
| **Restore Channels** | Channels that are Alive again but still carry a plugin tag |

Blank-screen channels are their own category with their own tag and group, so they are not
double-tagged by the dead actions. They remain Dead, so **Delete Dead removes them**.

**Low framerate triggers below 24fps.** Film at 24 and PAL at 25 are not flagged.

### Deletion

Permanent deletion has two gates: you must type `DELETE` into the confirmation setting **and**
confirm the dialog. It also refuses to delete anything outside the groups your last load actually
covered, which protects you from a stale results file.

### Restore

When a channel that was tagged and exiled comes back to life, **Restore Channels** strips the
plugin's tags and moves it back to the **exact** group it came from, recorded at the moment it was
first moved. A healthy channel that merely carries a `[HD]` suffix is never touched.

One thing to know: **a channel parked in your Graveyard group is only re-checked, and therefore only
restorable, if your scan scope includes that group.** Add the managed groups to your scope so
self-healing actually happens.

---

## The HTML report and email

**Email Report** builds a single self-contained HTML file and, if delivery is configured, queues it
for email.

The report is written to **`/config/iptv_checker/report.html`**, which is a real folder on the host
under your Dispatcharr config directory. Dated copies are kept alongside it, pruned to the last
eight, with a matching CSV.

It groups channels by **what you should do about them**, not by error code:

| Section | What to do |
|---|---|
| **Confirmed dead** | Rename, move or delete |
| **Dead at the provider** | Hide them or raise it upstream. Every mirror serves the same dead content, so there is nothing local to retry |
| **Not judged** | Re-run later. These have not been shown to be broken |
| **Working on a backup stream** | Nothing. Listed so a failed stream is visible without the channel looking broken |
| **Low framerate** | Tag or move if choppy playback matters to you |
| **Audio only** | Nothing. Radio stations |

Channels with nothing wrong are counted but not listed, and the report states how many, so the
numbers add up.

The page is one self-contained file: no fonts, images or scripts are fetched from the internet, so
it renders the same on a television browser with no route out as it does in a mail client. It
follows the same visual style as the Dustarr report, and adapts to a light or dark theme on its own.
The footer credits Newsflasharr for the emailed copy.

### Getting it by email

Delivery goes through the **Newsflasharr** plugin. Three things must be in place:

1. Newsflasharr installed, with its SMTP settings filled in.
2. A **routing rule** in Newsflasharr matching this plugin:

   ```json
   {"match": {"source": "iptv_checker", "event": "usage_report"},
    "channels": ["smtp"], "exclusive": true}
   ```

   Put it **before** any catch-all rule, or the catch-all claims the event first.
3. **Email Report After Scheduled Check** switched on, if you want it automatically.

**Without the routing rule the report is delivered somewhere else**, most likely as a push
notification, and everything still reports success. The plugin checks the rule first and refuses,
naming what is missing, rather than telling you it worked.

**The report is always written to disk first**, so a delivery problem never costs you the report.
The button reports success as "queued for delivery" rather than "sent", because delivery happens
afterwards on Newsflasharr's own retry schedule.

---

## Scheduling

Set **Scheduled Times** to a standard cron expression, then click **Save Schedule**.

```
0 4 * * *     every day at 04:00
0 2 * * 0     Sundays at 02:00
0 0 * * 0-4   Sunday through Thursday at midnight
```

**The timezone comes from Dispatcharr**, at Settings, General, Time Zone. There is no separate
plugin timezone to keep in sync.

The scheduler starts on its own when the container boots. **Check Scheduler** reports whether it is
running and which worker process owns it.

Anything you enable in the scheduled-run settings happens after each check, in this order: restore,
CSV export, email report, rename, move, delete.

---

## Windowed runs

For large lineups, **Use Windowed Schedule** turns the cron time into a window **start**. The check
runs until the window closes, stops cleanly between streams, and **resumes where it left off** the
next time the window opens.

Example, midnight to 04:00 Sunday through Thursday:

| Setting | Value |
|---|---|
| Scheduled Times | `0 0 * * 0-4` |
| Use Windowed Schedule | on |
| Window End Mode | `duration` |
| Window Duration | `4` |

- Progress persists, so a container restart mid-window resumes against the **original** window end
  rather than starting a new four hours.
- **The CSV and the report are written every window**, including one that closes part way through,
  so each window leaves its own record.
- Rename, move and delete are **deferred** until the window that finishes the list.
- **Reset Progress** wipes the pending state and starts fresh next window.
- If you change which groups are checked, pending progress is discarded rather than resumed against
  a scope it was not built for.

---

## Provider connection limits

Most IPTV accounts cap concurrent connections, often between one and four. Checking uses those
connections, so a check can interrupt someone watching.

- **Parallel Workers** should stay **below** your cap. With a four-connection account, two workers
  leaves room to watch while a check runs.
- **Stream Check Delay** makes each worker wait after finishing before taking the next stream, so
  the connection has time to release. Retry passes wait three times as long.

If many streams fail with "Server Error" or "Stream Unreachable" and then succeed on retry, raise
the delay or lower the worker count.

Enabling blank-screen, frozen-video or silent-audio detection adds a second connection per alive
stream. The placeholder-file check adds none.

---

## Every setting

### Scope

| Setting | Default | What it does |
|---|---|---|
| Channel Groups | *(empty)* | Comma-separated list, wildcards supported. Empty means every group |
| Channel Groups Mode | Check only those listed | Whether the list names groups to check or groups to skip |
| Check Alternative Streams | on | Check backup streams as well as the primary |
| Only Visible Channels | off | Limit to channels enabled in a channel profile |

### Check behaviour

| Setting | Default | What it does |
|---|---|---|
| Connection Timeout | 10 | Seconds to wait for a connection |
| Probe Timeout | 20 | Seconds to wait for analysis |
| Dead Connection Retries | 3 | Retry attempts for a failed stream |
| Enable Parallel Checking | on | Check several streams at once |
| Parallel Workers | 2 | How many at once. Keep below your provider's cap |
| Stream Check Delay | 3 | Seconds a worker waits between streams |

### Detection

| Setting | Default | What it does |
|---|---|---|
| Detect Placeholder-File Streams | off | Flags a stream reporting a fixed duration. Costs nothing extra |
| Detect Blank-Screen Streams | off | Flags a pure black picture |
| Blank-Screen Sample | 6 | Seconds of video to decode |
| Continuous Blank Required | 3 | Seconds of continuous black needed to flag |
| Blank-Screen ffmpeg Timeout | 20 | Hard cap on the decode |
| Detect Frozen-Video Streams | off | Flags a still picture that is not black |
| Frozen-Video Minimum | 4 | Seconds the picture must stay identical |
| Detect Silent-Audio Streams | off | Flags a healthy audio track with no sound |
| Silence Threshold | -70 | dBFS at or below which audio counts as silent |

### Naming and grouping

| Setting | Default |
|---|---|
| Dead Channel Rename Format | `{name} [DEAD]` |
| Move Dead Channels to Group | `Graveyard` |
| Blank-Screen Rename Format | `{name} [Blank]` |
| Move Blank-Screen Group | `Black Screens` |
| Low Framerate Rename Format | `{name} [Slow]` |
| Move Low Framerate Group | `Slow` |
| Video Format Suffixes | `UHD, FHD, HD, SD, Unknown` |

### Scheduling

| Setting | Default | What it does |
|---|---|---|
| Scheduled Times | *(empty)* | Cron expression. Empty disables the scheduler |
| Use Windowed Schedule | off | Turns the cron time into a window start |
| Window End Mode | `duration` | Run for N hours, or until a set time |
| Window Duration | 4 | Hours, decimals allowed |
| Window End Time | `04:00` | Used when the mode is `time` |

### After a scheduled check

All default to off.

| Setting | What it does |
|---|---|
| Export CSV | Write a CSV of the results |
| **Email Report After Scheduled Check** | Build the HTML report and queue it for delivery |
| Restore Recovered Channels | Un-tag and move back channels that recovered. Runs first |
| Rename Dead / Slow / Blank | Apply the matching rename format |
| Add Video Format Suffix | Tag alive channels with their quality |
| Move Dead / Slow / Blank | Move to the matching group |
| Delete Dead Channels | Permanently delete. Also needs the confirmation setting |

### Advanced

| Setting | Default | What it does |
|---|---|---|
| Auto-Delete Confirmation | *(empty)* | Must contain `DELETE` for any deletion to run |
| FFprobe Path | `/usr/local/bin/ffprobe` | |
| FFmpeg Path | `/usr/local/bin/ffmpeg` | Needed only for the decode-based detectors |
| FFprobe Analysis Flags | `-show_streams,-show_packets,-loglevel error` | **Do not add `-show_frames`.** It makes ffprobe merge packets and frames into one array, which silently breaks the bitrate measurement |
| FFprobe Analysis Duration | 8 | Seconds of stream to analyse |
| Streamlink-Only Hosts | `youtube.com, youtu.be, twitch.tv, kick.com` | Hosts `ffprobe` cannot read. Streams on them are Skipped |

---

## Every button

**Setup**

- **Validate** checks the settings and reports how many groups will be checked
- **Save Schedule** applies the schedule and restarts the scheduler
- **Check Scheduler** reports whether the scheduler is running and which process owns it
- **Reset Progress** clears pending windowed progress

**Checking**

- **Load Groups**, **Start Check**, **View Progress**, **Cancel Check**, **View Results**

**Acting**

- **Rename Dead**, **Move Dead**, **Rename Blank**, **Move Blank**, **Rename Slow**, **Move Slow**,
  **Add Format Suffix**, **Restore Channels**, **Delete Dead**

**Output**

- **View Table** shows the results as text you can copy
- **Export CSV** writes results plus a full settings preamble to `/data/exports/`
- **Email Report** builds the HTML report and queues it for delivery
- **Clear CSV Exports** deletes the exported files
- **Cleanup Orphaned Tasks** clears stale background state

---

## File locations

| What | Where |
|---|---|
| HTML report, dated copies and report CSV | `/config/iptv_checker/` |
| CSV exports | `/data/exports/` |
| Last results | `/data/iptv_checker_results.json` |
| Loaded channels | `/data/iptv_checker_loaded_channels.json` |
| Progress state | `/data/iptv_checker_progress.json` |
| Windowed resume state | `/data/iptv_checker_pending_resume.json` |
| Original groups, for restore | `/data/iptv_checker_channel_state.json` |
| Scheduler election lock | `/data/iptv_checker_scheduler.pid` |
| Record of which cron minutes already ran | `/data/iptv_checker_fire_claims/` |

`/config/` is a folder on your host, so the report opens by double-clicking it. `/data/` is inside
the container.

The fire-claim folder holds one small file per scheduled run that has already started, named after
the schedule and the minute. It is how a second copy of the plugin loaded into the same Dispatcharr
process is prevented from running the same nightly check twice. Files from earlier days are removed
automatically. Deleting the folder is harmless, but do not delete it while a scheduled run is
starting.

---

## Troubleshooting

### Try this first

Refresh the browser, then restart the container:

```bash
docker restart dispatcharr
```

### The plugin does not appear, or a button does nothing

Refresh the page, then restart the container. Dispatcharr caches plugin code until it reloads.

### The scheduler is not running

- It starts on its own at container boot. No button press is needed.
- Confirm the cron expression has five fields.
- Set your timezone in Dispatcharr, at Settings, General, Time Zone.
- Use **Check Scheduler**. It reports the owning process, so "not running" from one worker is not
  proof.
- Check the log:

  ```bash
  docker logs dispatcharr | grep -i "IPTV Checker"
  ```

### Many streams fail and then work on retry

You are probably hitting your provider's connection cap. Lower **Parallel Workers** or raise
**Stream Check Delay**. See [provider connection limits](#provider-connection-limits).

### Lots of results are "Skipped, Rate Limited"

The provider is throttling. Nothing is wrong with those streams and nothing destructive will touch
them. Re-run later with fewer workers.

### A channel was marked dead but it plays

Check whether the report puts it under **Working on a backup stream**. If it is genuinely listed as
dead, the last check found every one of its streams failing. Re-run before acting, especially if
the report warns that the run was rate limited.

### The email never arrives

The plugin refuses and names the cause rather than reporting a false success. The usual reasons are
a missing Newsflasharr routing rule or incomplete SMTP settings. See
[the HTML report and email](#the-html-report-and-email). The report itself is still written to
`/config/iptv_checker/report.html` regardless.

### Bitrate is missing for some streams

A probe that captured fewer than 30 video packets leaves it unset on purpose, because short samples
produce wildly wrong numbers. Raising **FFprobe Analysis Duration** gives slow-starting streams more
room.

### Reporting a problem

Include your Dispatcharr version, the output of
`docker logs dispatcharr | grep "IPTV Checker"`, and the exact error text.
[Open an issue](https://github.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/issues).
