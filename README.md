# Dispatcharr-IPTV-Checker-Plugin
A Dispatcharr Plugin that goes through a playlist to check IPTV channels
Based on https://github.com/NewsGuyTor/IPTVChecker

**Description:** Check IPTV streams' status and analyze each stream's quality

## Features
- **Stream Status Checking:** Verify if IPTV streams are alive or dead
- **Technical Analysis:** Extract codec, resolution, framerate, and bitrate information
- **Quality Detection:** Identify low framerate streams (<30fps) and mislabeled channels
- **Group Filtering:** Check specific groups within M3U8 playlists
- **Multiple Output Formats:** View results as a summary, a table, or export to CSV
- **Preview Mode:** See what will be checked before running analysis

## Requirements
### System Dependencies
This plugin requires **ffmpeg** and **ffprobe** to be installed in the Dispatcharr container for stream analysis.

**Default Locations:**
- **ffprobe:** `/usr/local/bin/ffprobe` (plugin default)
- **ffmpeg:** `/usr/local/bin/ffmpeg` (used for future features)

The plugin assumes these tools are available at the standard Dispatcharr installation paths. If ffprobe is installed in a different location, the plugin will fail with "ffprobe not found" errors.

**Verify Installation:**
```bash
docker exec dispatcharr which ffprobe
docker exec dispatcharr which ffmpeg
```

Both commands should return the full path to the respective tools.

## Installation
1. Log on to Dispatcharr's web UI
2. Go to **Plugins**
3. In the upper right, click **Import Plugin** and upload the zip file


## Usage Guide
### Current Best Practice Workflow
Even with the UI limitations of Dispatcharr's plugin system, users should follow this step-by-step pattern for best results:
1. **Enter M3U8 URL** in settings → **Save Settings**
2. **Load Playlist** → **Run** (shows available groups)  
3. **Enter desired groups** in settings → **Save Settings**
4. **Preview Check** → **Run** (verify what will be checked)
5. **Check Streams** → **Run** (perform analysis)
6. **View Results** using preferred format

Full Guide
### Step 1: Configure Settings
1. Navigate to **Plugins > IPTV Checker**
2. Enter your **M3U8 Playlist URL**
3. Set **Connection Timeout** (default: 10 seconds)
4. Optionally specify **Groups to Check** (comma-separated)
5. Click **Save Settings**

### Step 2: Load Playlist
1. Click **Run** on **Load Playlist**
2. Review the success message showing available groups
3. Copy desired group names to the "Groups to Check" setting if needed
4. Save settings again if you made changes

### Step 3: Preview Check (Optional)
1. Click **Run** on **Preview Check**
2. Review what channels will be checked
3. Verify the estimated time and channel counts

### Step 4: Check Streams
1. Click **Run** on **Check Streams**
2. Confirm the action when prompted
3. Wait for completion (monitor container logs for progress)

### Step 5: View Results
Choose your preferred result format:
- **View Results Table:** Formatted text table with stream details
- **View Last Results:** Summary with error breakdown
- **Export Results to CSV:** Download detailed data file

## Settings Reference

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| M3U8 Playlist URL | string | - | URL to your M3U8 playlist file |
| Connection Timeout | number | 10 | Seconds to wait for stream connection |
| Groups to Check | string | - | Comma-separated group names, empty = all groups |

## Output Data
### Stream Analysis Fields
- **Name:** Channel name from playlist
- **Group:** Channel group/category
- **URL:** Stream URL
- **Status:** Alive or Dead
- **Error:** Error message for dead streams
- **Codec:** Video codec (h264, h265, etc.)
- **Resolution:** Video resolution (1920x1080, etc.)
- **Framerate:** Frames per second
- **Bitrate:** Audio bitrate
- **Low Framerate:** Boolean, true if <30fps
- **Mislabeled:** Boolean, true if resolution doesn't match channel name
- **Checked At:** Timestamp of analysis

### Quality Detection Rules
**Low Framerate Detection:**
- Identifies streams with <30fps
- Useful for finding poor quality feeds

**Mislabeled Channel Detection:**
- **4K channels:** Should be 3840x2160 or higher
- **1080p/FHD channels:** Should be 1920x1080 or higher  
- **720p/HD channels:** Should be 1280x720 or higher

## File Locations
- **Results:** `/data/iptv_checker_results.json`
- **Groups Cache:** `/data/iptv_checker_groups.json`
- **CSV Exports:** `/data/exports/iptv_check_results_YYYYMMDD_HHMMSS.csv`

## Troubleshooting
### First Step: Restart Container
**For any plugin issues, always try restarting the Dispatcharr container first:**
```bash
docker restart dispatcharr
```
This resolves most plugin loading, configuration, and caching issues.

### Common Issues
**"No connection adapters" Error:**
- Verify M3U URL starts with `http://` or `https://`
- Check the URL is accessible from the container
- Ensure settings are saved before running actions
- Restart container: `docker restart dispatcharr`

**"ffprobe not found" Error:**
- Plugin uses `/usr/local/bin/ffprobe` (standard Dispatcharr location)
- Verify ffprobe is installed: `docker exec dispatcharr which ffprobe`
- Restart container: `docker restart dispatcharr`

**No Groups Found:**
- Check the M3U file contains `group-title` attributes
- Verify playlist loads successfully with the Load Playlist action
- Clear browser cache and restart container: `docker restart dispatcharr`

**Timeout Errors:**
- Increase timeout setting for slow streams
- Check network connectivity from the container
- Restart container: `docker restart dispatcharr`

**Plugin Not Appearing:**
- Check plugin files are in the correct location: `/data/plugins/iptv_checker/`
- Verify file permissions are correct
- Restart container: `docker restart dispatcharr`

**Settings Not Saving:**
- Click the "Save Settings" button after making changes
- Refresh browser page
- Restart container: `docker restart dispatcharr`

**Actions Not Working:**
- Verify plugin loaded successfully in logs
- Check for JavaScript errors in the browser console
- Restart container: `docker restart dispatcharr`

**General Plugin Issues:**
- Always restart the container first: `docker restart dispatcharr`
- Wait 30-60 seconds for full startup
- Refresh browser page
- Check container logs for errors

### Debugging
**Enable Verbose Logging:**
```bash
docker logs dispatcharr | grep -i iptv
```

**Check Plugin Status:**
```bash
docker exec dispatcharr ls -la /data/plugins/iptv_checker/
```

**Test Stream Manually:**
```bash
docker exec dispatcharr /usr/local/bin/ffprobe -v quiet -print_format json -show_streams "http://your-stream-url"
```

## Limitations
- No custom UI components (uses text-based results)
- Single-threaded checking (processes streams sequentially)
- Limited to ffprobe-supported stream formats
- No persistent stream history

