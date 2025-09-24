# Dispatcharr IPTV Checker Plugin

**Description:** Check IPTV stream status, analyze stream quality, and manage channels based on results

## Features

- **Stream Status Checking:** Verify if IPTV streams are alive or dead with retry logic
- **Technical Analysis:** Extract resolution, framerate, and video format information
- **Dispatcharr Integration:** Direct API communication with automatic authentication
- **Channel Management:** Automated renaming and moving of channels based on analysis results
- **Group-Based Operations:** Work with existing Dispatcharr channel groups

## Requirements

### System Dependencies
This plugin requires **ffmpeg** and **ffprobe** to be installed in the Dispatcharr container for stream analysis.

**Default Locations:**
- **ffprobe:** `/usr/local/bin/ffprobe` (plugin default)
- **ffmpeg:** `/usr/local/bin/ffmpeg`

**Verify Installation:**
```bash
docker exec dispatcharr which ffprobe
docker exec dispatcharr which ffmpeg
```

### Dispatcharr Setup
- Active Dispatcharr installation with configured channels and groups
- Valid Dispatcharr username and password for API access
- Channel groups containing IPTV streams to analyze

## Installation

1. Log in to Dispatcharr's web UI
2. Navigate to **Plugins**
3. Click **Import Plugin** and upload the plugin zip file
4. Enable the plugin after installation

## Settings Reference

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Dispatcharr Username | string | - | Username for API authentication |
| Dispatcharr Password | password | - | Password for API authentication |
| Groups to Check | string | - | Comma-separated group names, empty = all groups |
| Connection Timeout | number | 10 | Seconds to wait for stream connection |
| Dead Connection Retries | number | 2 | Number of retry attempts for failed streams |
| Dead Channel Prefix | string | "[DEAD] " | Prefix to add to dead channel names |
| Dead Channel Suffix | string | "" | Suffix to add to dead channel names |
| Dead Channel Target Group | string | "Graveyard" | Group to move dead channels to |
| Low FPS Prefix | string | "[SLOW] " | Prefix to add to low framerate channel names |
| Low FPS Suffix | string | "" | Suffix to add to low framerate channel names |
| Low FPS Target Group | string | "Slow" | Group to move low framerate channels to |

## Usage Guide

### Step-by-Step Workflow

1. **Configure Authentication**
   - Enter your **Dispatcharr Username** and **Password**
   - Optionally specify **Groups to Check** (leave empty to check all)
   - Configure retry and timeout settings
   - Click **Save Settings**

2. **Load Channel Groups**
   - Click **Run** on **Load Group(s)**
   - Review available groups and channel counts
   - Note the estimated checking time

3. **Preview Check (Optional)**
   - Click **Run** on **Preview Check** 
   - Verify which channels will be analyzed
   - Check estimated completion time

4. **Check Streams**
   - Click **Run** on **Check Streams**
   - Monitor live progress with **Check Status/View Last Results**
   - Wait for analysis completion

5. **Manage Results**
   - Use channel management actions based on results
   - Export data to CSV if needed

## Channel Management Features

### Dead Channel Management
- **Rename Dead Channels:** Add configurable prefix/suffix to dead streams
- **Move Dead Channels:** Automatically relocate dead channels to specified group

### Low Framerate Management (<30fps)
- **Rename Low FPS Channels:** Add configurable prefix/suffix to slow streams  
- **Move Low FPS Channels:** Automatically relocate slow channels to specified group

### Video Format Management
- **Add Format Suffixes:** Add [4K], [FHD], [HD], [SD] tags based on resolution
- **Remove Existing Tags:** Clean up channels by removing text within square brackets []

### Smart Features
- **Duplicate Prevention:** Avoids adding prefixes/suffixes that already exist
- **Auto Group Creation:** Creates target groups if they don't exist
- **GUI Refresh:** Automatically updates Dispatcharr interface after changes

## Output Data

### Stream Analysis Results
- **Name:** Channel name from Dispatcharr
- **Group:** Current channel group
- **Status:** Alive or Dead (with retry attempts)
- **Resolution:** Video resolution (e.g., 1920x1080)
- **Format:** Detected format (4K/FHD/HD/SD)
- **Framerate:** Frames per second
- **Error Details:** Specific failure reasons for dead streams
- **Checked At:** Analysis timestamp

### Quality Detection Rules
- **Low Framerate:** Streams with <30fps
- **Format Detection:** 
  - **4K:** 3840x2160+
  - **FHD:** 1920x1080+
  - **HD:** 1280x720+
  - **SD:** Below HD resolution

## File Locations

- **Results:** `/data/iptv_checker_results.json`
- **CSV Exports:** `/data/exports/iptv_check_results_YYYYMMDD_HHMMSS.csv`

## Troubleshooting

### First Step: Restart Container
**For any plugin issues, always try refreshing your browser (F5) and then restarting the Dispatcharr container:**
```bash
docker restart dispatcharr
```

### Common Issues

**Authentication Errors:**
- Verify Dispatcharr username and password are correct
- Ensure user has appropriate API access permissions
- Restart container: `docker restart dispatcharr`

**"No Groups Found" Error:**
- Check that channel groups exist in Dispatcharr
- Verify group names are spelled correctly (case-sensitive)
- Restart container: `docker restart dispatcharr`

**Stream Check Failures:**
- Increase timeout setting for slow streams
- Adjust retry count for unstable connections
- Check network connectivity from container
- Restart container: `docker restart dispatcharr`

**Channel Management Not Working:**
- Verify channels exist in specified groups
- Check for API permission issues
- Ensure group names don't contain special characters
- Restart container: `docker restart dispatcharr`

**Progress Not Updating:**
- Refresh browser page during long operations
- Check container logs for processing status
- Restart container: `docker restart dispatcharr`

### Debugging Commands

**Check Plugin Status:**
```bash
docker exec dispatcharr ls -la /data/plugins/iptv_checker/
```

**Monitor Plugin Activity:**
```bash
docker logs dispatcharr | grep -i iptv
```

**Test ffprobe Installation:**
```bash
docker exec dispatcharr /usr/local/bin/ffprobe -version
```

## Version History

**v0.2** (Major Update)
- Direct Dispatcharr API integration with automatic authentication
- Channel group-based input instead of M3U URLs  
- Comprehensive channel management features (rename, move, format tagging)
- Live progress tracking and time estimation
- Connection retry logic for improved reliability
- Smart duplicate prevention and auto-group creation
- Automatic GUI refresh after channel modifications

**v0.1** (Initial Release)  
- Basic stream status checking
- M3U playlist parsing
- Technical analysis and CSV export
- Group filtering and preview mode

## Limitations

- Single-threaded stream checking (sequential processing)
- Requires valid Dispatcharr authentication
- Limited to ffprobe-supported stream formats
- Channel management operations are permanent (backup recommended)

## Contributing

This plugin integrates deeply with Dispatcharr's API and channel management system. When reporting issues:
1. Include Dispatcharr version information
2. Provide relevant container logs
3. Test with small channel groups first
4. Document specific API error messages
