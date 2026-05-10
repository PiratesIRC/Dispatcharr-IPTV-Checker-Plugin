# Dispatcharr IPTV Checker Plugin

## Check IPTV stream status, analyze stream quality, and manage channels based on results

[![Dispatcharr plugin](https://img.shields.io/badge/Dispatcharr-plugin-8A2BE2)](https://github.com/Dispatcharr/Dispatcharr)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)
[![Workflow Guide](https://img.shields.io/badge/%F0%9F%93%96-Workflow_Guide-1F6FEB?style=flat)](https://piratesirc.github.io/Dispatcharr-Plugin-Workflow/workflow/01-iptv-checker/)
[![Discord](https://img.shields.io/badge/Discord-Discussion-5865F2?logo=discord&logoColor=white)](https://discord.gg/Sp45V5BcxU)

[![GitHub Release](https://img.shields.io/github/v/release/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin?include_prereleases&logo=github)](https://github.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/releases)
[![Downloads](https://img.shields.io/github/downloads/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/total?color=success&label=Downloads&logo=github)](https://github.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/releases)

![Top Language](https://img.shields.io/github/languages/top/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)
![Repo Size](https://img.shields.io/github/repo-size/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)
![Last Commit](https://img.shields.io/github/last-commit/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)
![License](https://img.shields.io/github/license/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)


## Warning: Backup Your Database
Before installing or using this plugin, it is **highly recommended** that you create a backup of your Dispatcharr database. This plugin makes significant changes to your channel and stream assignments.

**[Click here for instructions on how to back up your database.](https://dispatcharr.github.io/Dispatcharr-Docs/troubleshooting/?h=backup#how-can-i-make-a-backup-of-the-database)**

## Features

- **Stream Status Checking:** Verify if IPTV streams are alive or dead with smart retry logic
- **Wildcard Group Matching:** Target groups using patterns like `US-*`, `*Sports*`, or `Movies-??`
- **Automated Scheduler:** Schedule stream checks using cron expressions with timezone support
- **Post-Check Automation:** Automatically rename, move, delete, export, and webhook after scheduled checks
- **Metadata Synchronization:** Sync technical stream data (codecs, bitrate, sample rate) back to Dispatcharr
- **Background Processing:** Stream checks run in background threads with cancellation support
- **Alternative Streams:** Option to check backup/alternative streams associated with channels
- **Technical Analysis:** Extract resolution, framerate, and video format information
- **Configurable FFprobe:** Custom path, analysis flags, and analysis duration settings
- **Direct ORM Integration:** Runs inside Dispatcharr with direct database access — no API credentials needed
- **Channel Management:** Automated renaming, moving, and deletion of channels based on results
- **Group-Based Operations:** Work with existing Dispatcharr channel groups
- **Smart Loading:** Asynchronous loading for large channel lists to prevent interface timeouts
- **Real-Time Progress Tracking:** Live ETA calculations with adaptive WebSocket notifications
- **Smart Retry System:** Timeout streams queued and retried after other streams for better success rates
- **Enhanced Error Categorization:** Detailed error types (Timeout, 404, 403, Connection Refused, etc.)
- **Webhook Notifications:** Send HTTP POST notifications after scheduled checks complete
- **Auto-Delete Dead Channels:** Permanently remove dead channels with safety confirmation gate
- **CSV Exports:** Export results with comprehensive statistics and URL masking. Scheduled sessions emit a CSV every time a run ends — including windowed runs that close mid-list — so each window has its own audit record (v1.26.1191257+; the hoist regressed via the subfolder/root sync drift and was re-applied in v1.26.1212238).
- **Video Bitrate Reporting:** Per-stream `video_bitrate` (kbps) captured via ffprobe per-packet data and stored in Dispatcharr's `stream_stats`, so the channel-menu UI can display it. Live MPEG-TS / HLS streams almost never expose container-level `bit_rate`, so the plugin computes the average from `packets[].size / packets[].duration_time` for the video stream. Rounded to the nearest whole kbps before write (v1.26.1220052+). Probes that capture fewer than 30 video packets (≈1s of 30fps video) leave `video_bitrate` unset rather than persist a noisy average — short samples were producing wildly inflated values (observed: 22924 kbps from 2 packets) that polluted the channel-menu display (v1.26.1221035+). The default `ffprobe_analysis_duration` was bumped from 5 s → 8 s in v1.26.1221101 to give slow-start streams enough room to clear the 30-packet trust gate; verified runs jumped from 97% to 100% bitrate coverage on alive streams with median packet count rising from 200–400 → 662 (default-only change, existing deployments keep their saved value). The default `ffprobe_flags` was changed to `-show_streams,-show_packets,-loglevel error` in v1.26.1211342 — passing both `-show_frames` and `-show_packets` makes ffprobe emit a combined `packets_and_frames` array instead of separate `packets[]`, which silently breaks the bitrate calc. If you've customized `ffprobe_flags`, do **not** include `-show_frames`.
- **Window-Aware Retry Pass:** When a windowed run closes mid-list, the parallel/sequential retry passes now also bail on `_past_window_end()` so transient-error retries cannot overshoot the window boundary (v1.26.1212238+; previously observed up to 14 minutes of overrun).
- **Adaptive Rate-Limit Guard:** Detects upstream HTTP 429 responses, classifies them as **Skipped (Rate Limited)** instead of Dead so destructive actions never act on a throttled stream, and applies an exponentially-doubling cooldown when 429s spike (v1.26.1181025+). The cooldown counter is shared across the whole container — Dispatcharr's multiple worker processes can no longer reset it independently (v1.26.1181126+).
- **Single-Scheduler Election:** Dispatcharr runs ~9 separate Python processes; a file-based PID lock at `/data/iptv_checker_scheduler.pid` ensures exactly one of them hosts the cron scheduler. Prior versions could fire each cron N times in parallel (v1.26.1181126+). Module-reload duplicate-thread protection added in v1.26.1191257 (Django/uwsgi could re-import the plugin module within the elected process and spawn additional scheduler threads, defeating the PID lock). Cross-worker UI-restart protection added in v1.26.1220951: any UI button click landed in whichever uwsgi worker the load balancer picked, and `update_schedule_action` / `Plugin.run()` previously called `_start_background_scheduler` directly without checking the PID lock — so non-owner workers spawned rogue scheduler threads (observed: `'59 23 * * *'` fired twice 27 ms apart on 2026-05-02). Non-owners now write a `/data/iptv_checker_scheduler_reload.flag` file that the owner's scheduler loop polls every 30 s; the owner re-reads settings via `_fresh_settings` and swaps its cron expressions in place.

## Requirements

### System Dependencies
This plugin requires **ffmpeg** and **ffprobe** to be installed in the Dispatcharr container for stream analysis. The scheduler feature requires **pytz** (usually included).

**Default Locations:**
- **ffprobe:** `/usr/local/bin/ffprobe` (plugin default, configurable)
- **ffmpeg:** `/usr/local/bin/ffmpeg`

**Verify Installation:**
```bash
docker exec dispatcharr which ffprobe
docker exec dispatcharr which ffmpeg
```

### Dispatcharr Setup
- Active Dispatcharr installation (v0.20.0+) with configured channels and groups
- Channel groups containing IPTV streams to analyze

No API credentials are needed — the plugin runs inside Dispatcharr with direct database access.

## Installation

1. Log in to Dispatcharr's web UI
2. Navigate to **Plugins**
3. Click **Import Plugin** and upload the plugin zip file
4. Enable the plugin after installation

### Updating the Plugin

To update the plugin:

1. **Remove Old Plugin**
   * Navigate to **Plugins** in Dispatcharr
   * Click the trash icon next to the old plugin
   * Confirm deletion

2. **Restart Dispatcharr**
   * Log out of Dispatcharr
   * Restart the Docker container:
     ```bash
     docker restart dispatcharr
     ```

3. **Install Updated Plugin**
   * Log back into Dispatcharr
   * Navigate to **Plugins**
   * Click **Import Plugin** and upload the new plugin zip file
   * Enable the plugin after installation

4. **Verify Installation**
   * Check that the plugin appears in the plugin list
   * Reconfigure your settings if needed

## Settings Reference

### Core Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Group(s) to Check | string | *(empty = all)* | Comma-separated group names. Supports wildcards: `US-*`, `*Sports*` |
| Check Alternative Streams | boolean | true | Check all alternative/backup streams for each channel |
| Connection Timeout | number | 10 | Seconds to wait for stream connection |
| Probe Timeout | number | 20 | Seconds to wait for FFprobe stream analysis |
| Dead Connection Retries | number | 3 | Number of retry attempts for failed streams |

### Channel Management Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Dead Channel Rename Format | string | `{name} [DEAD]` | Format for renaming dead channels |
| Move Dead Channels to Group | string | `Graveyard` | Group to move dead channels to |
| Low Framerate Rename Format | string | `{name} [Slow]` | Format for renaming low FPS channels (<30fps) |
| Move Low Framerate Group | string | `Slow` | Group to move low framerate channels to |
| Video Format Suffixes | string | `UHD, FHD, HD, SD, Unknown` | Formats to add as suffixes |

### FFprobe Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| FFprobe Path | string | `/usr/local/bin/ffprobe` | Full path to the ffprobe executable |
| FFprobe Analysis Flags | string | `-show_streams,-show_frames,...` | Comma-separated FFprobe flags |
| FFprobe Analysis Duration | number | 5 | Seconds of stream to analyze |
| Streamlink-Only Hosts | string | `youtube.com, youtu.be, twitch.tv, kick.com` | Comma-separated host suffixes ffprobe cannot validate (served via Streamlink). Streams matching these hosts are marked **Skipped** instead of **Dead**, so rename/move/delete actions leave them alone. Blank falls back to defaults. |

### Parallel Checking

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Enable Parallel Checking | boolean | true | Check multiple streams simultaneously |
| Number of Parallel Workers | number | 2 | How many streams to check at once. **Keep below your provider's concurrent-connection limit.** |
| Per-Stream Cooldown (seconds) | number | 2 | Each worker waits this long after finishing a check before picking up the next. Prevents provider rate-limiting / slot-reuse errors. Retry passes wait 3× this value. |

### Webhook

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Webhook URL | string | *(empty)* | HTTP POST URL for notifications after scheduled checks |

### Scheduler Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Scheduled Check Times | string | *(empty)* | Cron expression (e.g., `0 4 * * *` for daily at 4 AM). When **Use Windowed Schedule** is on, this becomes the window **start** trigger. |
| Scheduler Timezone | select | `America/Chicago` | Timezone for the scheduler |
| Use Windowed Schedule | boolean | false | When on, each cron-fire opens a run window. The check runs until the configured end-of-window, then halts cleanly between streams. The next time the window opens, the run **resumes** from where it left off — already-checked streams are skipped. |
| Window End Mode | select | `duration` | `duration` = run for N hours; `time` = run until a specific HH:MM (wraps past midnight if earlier than the start). |
| Window Duration (hours) | number | 4 | Used when Window End Mode = duration. Decimals allowed (e.g. 3.5). |
| Window End Time | string | `04:00` | Used when Window End Mode = time. 24-hour format in the Scheduler Timezone above. |
| Export CSV for Scheduled Checks | boolean | false | Auto-export results to CSV after scheduled checks |
| Rename Dead Channels | boolean | false | Auto-rename dead channels after scheduled checks |
| Rename Low Framerate Channels | boolean | false | Auto-rename slow channels after scheduled checks |
| Add Video Format Suffix | boolean | false | Auto-add format suffix after scheduled checks |
| Move Dead Channels | boolean | false | Auto-move dead channels after scheduled checks |
| Move Low Framerate Channels | boolean | false | Auto-move slow channels after scheduled checks |
| Delete Dead Channels | boolean | false | Auto-delete dead channels after scheduled checks |
| Send Webhook Notification | boolean | false | Send webhook after scheduled checks |

### Destructive Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Auto-Delete Confirmation | string | *(empty)* | Type `DELETE` to enable auto-delete of dead channels |

## Usage Guide

### Step-by-Step Workflow

1. **Configure Preferences**
   - Set your **Group(s) to Check** (supports wildcards like `US-*`)
   - Configure checking preferences (Alternative Streams, Timeouts, Retries)
   - Optionally enable **Parallel Checking** for faster processing
   - Click **Save Settings**

2. **Validate Settings** *(Recommended)*
   - Click **Run** on **Validate Settings**
   - Verifies group names, FFprobe path, and configuration

3. **Configure Schedule** *(Optional)*
   - Set **Scheduled Check Times** using cron format
   - Select your **Scheduler Timezone**
   - Enable post-check automation options as desired
   - Click **Run** on **Update Schedule** to activate

4. **Load Channel Groups**
   - Click **Run** on **Load Group(s)**
   - Review available groups and channel counts
   - Large lists (>100 channels) load in the background

5. **Check Streams**
   - Click **Run** on **Start Stream Check**
   - Processing runs in the background
   - Returns immediately with estimated completion time
   - Metadata is automatically synced to the database during checks

6. **Monitor Progress**
   - Click **View Check Progress** for real-time status with ETA
   - Use **Cancel Stream Check** to stop a running check
   - Progress updates continue even if browser times out

7. **View Results**
   - Click **View Last Results** for summary when complete
   - Shows alive/dead counts and format distribution
   - Use **View Results Table** for detailed tabular format

8. **Manage Channels**
   - Use channel management actions based on results
   - All destructive operations include confirmation dialogs
   - GUI automatically refreshes after changes

9. **Export Data**
   - Click **Export Results to CSV** to save analysis data
   - CSV includes comprehensive header comments with settings and stats

## Action Reference

### Setup & Validation
- **Validate Settings:** Verify configuration, group names, and FFprobe path
- **Update Schedule:** Apply schedule settings and restart the scheduler
- **Check Scheduler Status:** View current scheduler state and next run time

### Core Stream Checking
- **Load Group(s):** Load channels from specified groups (async for large lists)
- **Start Stream Check:** Begin checking all loaded streams in background thread
- **View Check Progress:** View current progress and ETA of the running check
- **Cancel Stream Check:** Stop the currently running stream check (confirmation dialog; queued and in-flight streams abort, already-probed results are kept)
- **View Last Results:** View summary of the last completed stream check

### Channel Management
- **Rename Dead Channels:** Apply rename format to dead streams
- **Move Dead Channels to Group:** Relocate dead channels
- **Delete Dead Channels:** Permanently remove dead channels (requires confirmation)
- **Rename Low Framerate Channels:** Apply rename format to slow streams (<30fps)
- **Move Low Framerate Channels:** Relocate slow channels
- **Add Video Format Suffix:** Apply format tags ([UHD], [FHD], [HD], [SD])

### Data & Maintenance
- **View Results Table:** Detailed tabular format for copy/paste
- **Export Results to CSV:** Save analysis data with comprehensive statistics
- **Clear CSV Exports:** Delete all CSV files in /data/exports/
- **Cleanup Orphaned Tasks:** Clean up stale background tasks

## Advanced Features

### Wildcard Group Matching
Use shell-style wildcards in the Group(s) to Check field:
- `US-*` — matches US-Movies, US-Sports, US-News, etc.
- `*Sports*` — matches any group containing "Sports"
- `Movies-??` — matches Movies-US, Movies-UK, etc.
- Multiple patterns: `US-*, UK-*, *Sports*` (comma-separated)

### Automated Scheduling
- **Cron Support:** Configure checks using standard cron syntax (e.g., `0 4 * * *`)
- **Timezone Aware:** Schedules run according to your local timezone
- **Post-Check Automation:** Chain any combination of rename, move, delete, export, and webhook actions
- **Conflict Prevention:** Scheduler queues jobs if a manual check is already running

#### Windowed Schedules with Resume

For overnight or off-peak runs, enable **Use Windowed Schedule**. The cron expression becomes the window **start**; the check halts cleanly when the window closes and resumes from the same place the next time the window opens.

**Example — Sun–Thu, 00:00 → 04:00 CST:**

| Setting | Value |
|---|---|
| Scheduled Check Times | `0 0 * * 0-4` |
| Scheduler Timezone | `America/Chicago` |
| Use Windowed Schedule | ✅ on |
| Window End Mode | `duration` |
| Window Duration (hours) | `4` |

What happens:

- The window opens at midnight Sun–Thu and runs for 4 hours.
- Per-stream progress is persisted to `/data/iptv_checker_pending_resume.json`.
- If the window closes before the channel list is finished, post-check actions (rename / move / delete / webhook) are **deferred** to the window that completes the list.
- If the container restarts mid-window, the original window end is preserved and the check resumes immediately rather than waiting for the next cron fire.
- Click **Reset Window Progress** to wipe pending state and start fresh on the next window.

### Metadata Synchronization
- **Database Sync:** Automatically updates Dispatcharr with technical stream details from FFprobe analysis
- **Synced Fields:** Video/Audio Codecs, Resolution, Bitrates, Sample Rates, Audio Channels, Stream Types

### Smart Retry System
- Timeout streams queued and retried after processing other streams (not immediately)
- Provides server recovery time between retry attempts
- Retry queue processes every 4 streams to balance throughput and recovery
- Multiple retry attempts per stream based on configured retry count
- **Retry-aware ETA:** `View Check Progress` keeps the percentage and ETA honest through retry passes — it no longer snaps to 100% at the end of the first pass.

### Provider Concurrency Limits
Most IPTV providers cap concurrent connections per account (often 1–4). Two settings let you stay under the cap while still running checks in parallel:

- **Number of Parallel Workers** — keep this **below** your account's cap. For a 4-stream account, 2 workers leaves headroom for viewing while a check runs.
- **Per-Stream Cooldown** — 2 s by default. Each worker waits this long after finishing before picking up the next stream, so the upstream slot has time to release. Retry passes wait `3×` this value.

If you see a lot of "Server Error" or "Stream Unreachable" results that turn alive on retry, raise the cooldown or drop the worker count.

### Auto-Delete Dead Channels
- Permanently deletes channels with dead streams from the database
- **Safety gates:** Requires typing `DELETE` in the confirmation field AND confirming via dialog
- Can be automated via scheduler with the same confirmation gate

### Webhook Notifications
- Sends HTTP POST with JSON payload after scheduled checks complete
- Configure any URL — works with Discord, Slack, custom endpoints
- No additional dependencies (uses Python's built-in `urllib`)

## Troubleshooting

### First Step: Restart Container
**For any plugin issues, try refreshing your browser (F5) and then restarting the Dispatcharr container:**
```bash
docker restart dispatcharr
```

### Common Issues

**"Plugin not found" Errors:**
- Refresh browser page (F5)
- Restart Dispatcharr container

**Scheduler Not Running:**
- The scheduler starts automatically on container boot — no UI action needed
- Verify `pytz` is installed in the container
- Check cron syntax (5 fields required: minute hour day month weekday)
- Use **Check Scheduler Status** to verify state
- Check logs: `docker logs dispatcharr | grep -i scheduler`
- Confirm scheduler started on boot: `docker logs dispatcharr | grep "Background scheduler thread started"`

**Stream Check Failures:**
- Increase connection timeout and/or probe timeout for slow streams
- Adjust retry count for unstable connections
- Try enabling parallel mode for better timeout handling
- Restart container: `docker restart dispatcharr`

**Progress Stuck or Not Updating:**
- Stream checking runs in background and continues even if browser times out
- Use **View Check Progress** to check current status
- Use **Cancel Stream Check** if needed
- Check container logs for actual processing status

## File Locations

- **Results:** `/data/iptv_checker_results.json`
- **Loaded Channels:** `/data/iptv_checker_loaded_channels.json`
- **Progress State:** `/data/iptv_checker_progress.json`
- **Settings:** `/data/iptv_checker_settings.json`
- **CSV Exports:** `/data/exports/iptv_checker_results_YYYYMMDD_HHMMSS.csv`

## Versioning

This plugin uses calver `1.26.{DDD}{HHMM}` (UTC day-of-year + UTC hour-minute), matching the Lineuparr / Channel-Mapparr / EPG-Janitor cohort. Releases prior to `1.26.1081815` used semver (`0.X.Y`). See the [release notes](https://github.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/releases) for full changelogs.

## Contributing

When reporting issues:
1. Include Dispatcharr version information
2. Provide relevant container logs (`docker logs dispatcharr | grep "IPTV Checker"`)
3. Test with small channel groups first
4. Document specific error messages and error types
5. Note current progress from **View Last Results**

Pull requests welcome. To submit changes:

### To this repo (PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)

1. Bump version: `python3 bump_version.py` (auto-stamps with current UTC day-of-year + HHMM).
2. Commit, push, tag, and release:

```bash
git tag <version> && git push origin <version>
gh release create <version> --title "v<version>" --notes "..."
gh release upload <version> iptv_checker.zip
```

### To the upstream marketplace (Dispatcharr/Plugins)

Updates also need to be PR'd to `Dispatcharr/Plugins` so the plugin updates in users' Dispatcharr UIs. The repo's GitHub Actions validator enforces strict rules — failing any blocks the merge:

| Check | Requirement |
|-------|-------------|
| PR title | Must match `[iptv-checker]: <description>`. The `validate-title` job fails on any other format. Most common trip-up. |
| Version bump | `plugin.json` version must be greater than the version on upstream `main` for any code/asset change. Metadata-only edits are exempt. |
| Required `plugin.json` fields | `name`, `version`, `description`, `author`, `license` (SPDX). |
| Authorship | PR author's GitHub username must appear in `author` or `maintainers`, or the `close-unauthorized` job auto-closes the PR. |
| Folder name | `plugins/iptv-checker/` (lowercase-kebab) — note this differs from the `iptv_checker/` snake_case used inside this repo's zip. |

Workflow:

```bash
# In your fork of Dispatcharr/Plugins:
git fetch upstream && git checkout main && git merge upstream/main --ff-only && git push origin main
git checkout -b iptv-checker-v<version>
cp <this-repo>/plugin.{py,json} plugins/iptv-checker/
git commit -am "[iptv-checker]: ..."
git push -u origin iptv-checker-v<version>
gh pr create --repo Dispatcharr/Plugins --base main \
    --title "[iptv-checker]: Bump to v<version> — <summary>" \
    --body "..."
```

On merge, upstream automation builds the zip + checksums and updates `manifest.json` on the `releases` branch — do not touch that branch manually.
