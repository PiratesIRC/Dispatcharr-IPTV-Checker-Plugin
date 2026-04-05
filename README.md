# Dispatcharr IPTV Checker Plugin

## Check IPTV stream status, analyze stream quality, and manage channels based on results

[![Dispatcharr plugin](https://img.shields.io/badge/Dispatcharr-plugin-8A2BE2)](https://github.com/Dispatcharr/Dispatcharr)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)

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
- **CSV Exports:** Export results with comprehensive statistics and URL masking

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

### Parallel Checking

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Enable Parallel Checking | boolean | true | Check multiple streams simultaneously |
| Number of Parallel Workers | number | 2 | How many streams to check at once |

### Webhook

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Webhook URL | string | *(empty)* | HTTP POST URL for notifications after scheduled checks |

### Scheduler Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Scheduled Check Times | string | *(empty)* | Cron expression (e.g., `0 4 * * *` for daily at 4 AM) |
| Scheduler Timezone | select | `America/Chicago` | Timezone for the scheduler |
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
- **Cancel Stream Check:** Stop the currently running stream check
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

### Metadata Synchronization
- **Database Sync:** Automatically updates Dispatcharr with technical stream details from FFprobe analysis
- **Synced Fields:** Video/Audio Codecs, Resolution, Bitrates, Sample Rates, Audio Channels, Stream Types

### Smart Retry System
- Timeout streams queued and retried after processing other streams (not immediately)
- Provides server recovery time between retry attempts
- Retry queue processes every 4 streams to balance throughput and recovery
- Multiple retry attempts per stream based on configured retry count

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
- Verify `pytz` is installed in the container
- Check cron syntax (5 fields required: minute hour day month weekday)
- Use **Check Scheduler Status** to verify state
- Check logs: `docker logs dispatcharr | grep -i scheduler`

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

## Contributing

When reporting issues:
1. Include Dispatcharr version information
2. Provide relevant container logs (`docker logs dispatcharr | grep "IPTV Checker"`)
3. Test with small channel groups first
4. Document specific error messages and error types
5. Note current progress from **View Last Results**
