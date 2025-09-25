# Dispatcharr IPTV Checker Plugin

**Description:** Check IPTV stream status, analyze stream quality, and manage channels based on results

## Features

- **Stream Status Checking:** Verify if IPTV streams are alive or dead with smart retry logic
- **Technical Analysis:** Extract resolution, framerate, and video format information
- **Dispatcharr Integration:** Direct API communication with automatic authentication
- **Channel Management:** Automated renaming and moving of channels based on analysis results
- **Group-Based Operations:** Work with existing Dispatcharr channel groups
- **Real-Time Progress Tracking:** Live ETA calculations and completion notifications

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
| Dispatcharr URL | string | - | Full URL of your Dispatcharr instance |
| Dispatcharr Username | string | - | Username for API authentication |
| Dispatcharr Password | password | - | Password for API authentication |
| Groups to Check | string | - | Comma-separated group names, empty = all groups |
| Connection Timeout | number | 10 | Seconds to wait for stream connection |
| Dead Connection Retries | number | 3 | Number of retry attempts for failed streams |
| Dead Channel Prefix | string | - | Prefix to add to dead channel names |
| Dead Channel Suffix | string | - | Suffix to add to dead channel names |
| Move Dead Channels to Group | string | "Graveyard" | Group to move dead channels to |
| Low Framerate Prefix | string | - | Prefix for channels under 30fps |
| Low Framerate Suffix | string | " [Slow]" | Suffix for channels under 30fps |
| Move Low Framerate Group | string | "Slow" | Group to move low framerate channels to |
| Video Format Suffixes | string | "4k, FHD, HD, SD, Unknown" | Formats to add as suffixes |

## Usage Guide

### Step-by-Step Workflow

1. **Configure Authentication**
   - Enter your **Dispatcharr URL** (e.g., http://127.0.0.1:9191)
   - Enter your **Dispatcharr Username** and **Password**
   - Optionally specify **Groups to Check** (leave empty to check all)
   - Configure retry and timeout settings
   - Click **Save Settings**

2. **Load Channel Groups**
   - Click **Run** on **Load Group(s)**
   - Review available groups and channel counts
   - Note the estimated checking time (now more accurate)

3. **Check Streams**
   - Click **Run** on **Process Channels/Streams**
   - Processing runs in the background to prevent browser timeouts
   - Stream checking includes a 3-second delay between checks for better reliability

4. **Monitor Progress**
   - Use **Get Status Update** for real-time progress with ETA
   - Use **View Last Results** for summary when complete
   - Status shows format: "Checking streams X/Y - Z% complete | ETA: N min"

5. **Manage Results**
   - Use channel management actions based on results
   - Export data to CSV with detailed error categorization

# Dispatcharr IPTV Checker Plugin

**Description:** Check IPTV stream status, analyze stream quality, and manage channels based on results

## Features

- **Stream Status Checking:** Verify if IPTV streams are alive or dead with smart retry logic
- **Technical Analysis:** Extract resolution, framerate, and video format information
- **Dispatcharr Integration:** Direct API communication with automatic authentication
- **Channel Management:** Automated renaming and moving of channels based on analysis results
- **Group-Based Operations:** Work with existing Dispatcharr channel groups
- **Real-Time Progress Tracking:** Live ETA calculations and completion notifications

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
| Dispatcharr URL | string | - | Full URL of your Dispatcharr instance |
| Dispatcharr Username | string | - | Username for API authentication |
| Dispatcharr Password | password | - | Password for API authentication |
| Groups to Check | string | - | Comma-separated group names, empty = all groups |
| Connection Timeout | number | 10 | Seconds to wait for stream connection |
| Dead Connection Retries | number | 3 | Number of retry attempts for failed streams |
| Dead Channel Prefix | string | - | Prefix to add to dead channel names |
| Dead Channel Suffix | string | - | Suffix to add to dead channel names |
| Move Dead Channels to Group | string | "Graveyard" | Group to move dead channels to |
| Low Framerate Prefix | string | - | Prefix for channels under 30fps |
| Low Framerate Suffix | string | " [Slow]" | Suffix for channels under 30fps |
| Move Low Framerate Group | string | "Slow" | Group to move low framerate channels to |
| Video Format Suffixes | string | "4k, FHD, HD, SD, Unknown" | Formats to add as suffixes |

## Usage Guide

### Step-by-Step Workflow

1. **Configure Authentication**
   - Enter your **Dispatcharr URL** (e.g., http://127.0.0.1:9191)
   - Enter your **Dispatcharr Username** and **Password**
   - Optionally specify **Groups to Check** (leave empty to check all)
   - Configure retry and timeout settings
   - Click **Save Settings**

2. **Load Channel Groups**
   - Click **Run** on **Load Group(s)**
   - Review available groups and channel counts
   - Note the estimated checking time

3. **Check Streams**
   - Click **Run** on **Process Channels/Streams**
   - Processing runs in the background to prevent browser timeouts
   - Stream checking includes a 3-second delay between checks for better reliability

4. **Monitor Progress**
   - Use **Get Status Update** for real-time progress with ETA
   - Use **View Last Results** for summary when complete
   - Status shows format: "Checking streams X/Y - Z% complete | ETA: N min"

5. **Manage Results**
   - Use channel management actions based on results
   - Export data to CSV with detailed error categorization

## Channel Management Features

### Dead Channel Management
- **Rename Dead Channels:** Add configurable prefix/suffix to dead streams
- **Move Dead Channels:** Automatically relocate dead channels to the specified group

### Low Framerate Management (<30fps)
- **Rename Low FPS Channels:** Add configurable prefix/suffix to slow streams  
- **Move Low FPS Channels:** Automatically relocate slow channels to the specified group

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
- **Status:** Alive or Dead
- **Resolution:** Video resolution (e.g., 1920x1080)
- **Format:** Detected format (4K/FHD/HD/SD)
- **Framerate:** Frames per second (rounded to 1 decimal)
- **Error Type:** Categorized failure reason for dead streams
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
- **Loaded Channels:** `/data/iptv_checker_loaded_channels.json`
- **CSV Exports:** `/data/exports/iptv_check_results_YYYYMMDD_HHMMSS.csv`

## Action Reference

### Core Actions
- **Load Group(s):** Load channels from specified groups
- **Process Channels/Streams:** Check all loaded streams (background processing)
- **Get Status Update:** Real-time progress with ETA
- **View Last Results:** Summary of completed check

### Channel Management
- **Rename Dead Channels:** Apply prefixes/suffixes to dead streams
- **Move Dead Channels to Group:** Relocate dead channels
- **Rename Low Framerate Channels:** Apply prefixes/suffixes to slow streams
- **Move Low Framerate Channels to Group:** Relocate slow channels
- **Add Video Format Suffix to Channels:** Apply format tags
- **Remove [] tags:** Clean up channel names

### Data Export
- **View Results Table:** Detailed tabular format
- **Export Results to CSV:** Save analysis data

## Advanced Features

### Smart Retry System
- Timeout streams get retried after processing other streams (not immediately)
- Provides server recovery time between retry attempts
- Improves success rates for intermittent connection issues

### Real-Time Progress Tracking
- **ETA Calculation:** Updates remaining time based on actual processing speed
- **Background Processing:** Stream checking continues without browser timeout risk
- **Completion Notifications:** Clear status when checking finishes

### Performance Optimizations
- **3-Second Delays:** Built-in pause between stream checks for server stability
- **Accurate Time Estimates:** Based on real-world performance data
- **Server-Friendly Processing:** Reduces load on IPTV providers

## Troubleshooting

### First Step: Restart Container
**For any plugin issues, always try refreshing your browser (F5) and then restarting the Dispatcharr container:**
```bash
docker restart dispatcharr
```

### Common Issues

**"Plugin not found" Errors:**
- Refresh browser page
- Restart Dispatcharr container
- Check plugin folder structure

**Authentication Errors:**
- Verify Dispatcharr URL is accessible from the browser
- Ensure username and password are correct
- Check user has appropriate API access permissions
- Restart container: `docker restart dispatcharr`

**"No Groups Found" Error:**
- Check that channel groups exist in Dispatcharr
- Verify group names are spelled correctly (case-sensitive)
- Restart container: `docker restart dispatcharr`

**Stream Check Failures:**
- Increase timeout setting for slow streams
- Adjust retry count for unstable connections
- Check network connectivity from the container
- Restart container: `docker restart dispatcharr`

**Progress Stuck or Not Updating:**
- Use "Get Status Update" for real-time progress
- Stream checking continues in the background even if the browser shows a timeout
- Check container logs for actual processing status
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

**Check Processing Status:**
```bash
docker logs dispatcharr | tail -20
```

## Performance Notes

### Time Estimates
- Based on approximately 8.5 seconds per stream average
- Includes 20% buffer for reliability
- Real-time ETA updates during processing based on actual speed

### Processing Speed
- **3-Second Delays:** Built-in pause between each stream check
- **Smart Retries:** Timeout streams are retried after other streams are processed
- **Background Processing:** Continues even if the browser shows a timeout

## Limitations

- Sequential stream processing (not parallel for server stability)
- Requires valid Dispatcharr authentication
- Limited to ffprobe-supported stream formats
- Channel management operations are permanent (backup recommended)
- Processing time increases with 3-second delays (trade-off for reliability)

## Contributing

This plugin integrates deeply with Dispatcharr's API and channel management system. When reporting issues:
1. Include Dispatcharr version information
2. Provide relevant container logs
3. Test with small channel groups first
4. Document specific API error messages
5. Note if using "Get Status Update" shows different information than the browser display

## Channel Management Features

### Dead Channel Management
- **Rename Dead Channels:** Add configurable prefix/suffix to dead streams
- **Move Dead Channels:** Automatically relocate dead channels to the specified group

### Low Framerate Management (<30fps)
- **Rename Low FPS Channels:** Add configurable prefix/suffix to slow streams  
- **Move Low FPS Channels:** Automatically relocate slow channels to the specified group

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
- **Status:** Alive or Dead
- **Resolution:** Video resolution (e.g., 1920x1080)
- **Format:** Detected format (4K/FHD/HD/SD)
- **Framerate:** Frames per second (rounded to 1 decimal)
- **Error Type:** Categorized failure reason for dead streams
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
- **Loaded Channels:** `/data/iptv_checker_loaded_channels.json`
- **CSV Exports:** `/data/exports/iptv_check_results_YYYYMMDD_HHMMSS.csv`

## Action Reference

### Core Actions
- **Load Group(s):** Load channels from specified groups
- **Process Channels/Streams:** Check all loaded streams (background processing)
- **Get Status Update:** Real-time progress with ETA
- **View Last Results:** Summary of completed check

### Channel Management
- **Rename Dead Channels:** Apply prefixes/suffixes to dead streams
- **Move Dead Channels to Group:** Relocate dead channels
- **Rename Low Framerate Channels:** Apply prefixes/suffixes to slow streams
- **Move Low Framerate Channels to Group:** Relocate slow channels
- **Add Video Format Suffix to Channels:** Apply format tags
- **Remove [] tags:** Clean up channel names

### Data Export
- **View Results Table:** Detailed tabular format
- **Export Results to CSV:** Save analysis data

## Troubleshooting

### First Step: Restart Container
**For any plugin issues, always try refreshing your browser (F5) and then restarting the Dispatcharr container:**
```bash
docker restart dispatcharr
```

### Common Issues

**"Plugin not found" Errors:**
- Refresh browser page
- Restart Dispatcharr container
- Check plugin folder structure

**Authentication Errors:**
- Verify Dispatcharr URL is accessible from the browser
- Ensure username and password are correct
- Check user has appropriate API access permissions
- Restart container: `docker restart dispatcharr`

**"No Groups Found" Error:**
- Check that channel groups exist in Dispatcharr
- Verify group names are spelled correctly (case-sensitive)
- Restart container: `docker restart dispatcharr`

**Stream Check Failures:**
- Increase timeout setting for slow streams
- Adjust retry count for unstable connections
- Check network connectivity from the container
- Restart container: `docker restart dispatcharr`

**Progress Stuck or Not Updating:**
- Use "Get Status Update" for real-time progress
- Stream checking continues in the background even if the browser shows a timeout
- Check container logs for actual processing status
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

**Check Processing Status:**
```bash
docker logs dispatcharr | tail -20
```

## Performance Notes

### Time Estimates
- **More Accurate:** Based on ~8.5 seconds per stream average
- **Buffer Included:** 20% extra time added for reliability
- **Real-Time ETA:** Updates during processing based on actual speed

### Processing Speed
- **3-Second Delays:** Built-in pause between each stream check
- **Smart Retries:** Timeout streams are retried after other streams are processed
- **Background Processing:** Continues even if the browser shows a timeout

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

- Sequential stream processing (not parallel for server stability)
- Requires valid Dispatcharr authentication
- Limited to ffprobe-supported stream formats
- Channel management operations are permanent (backup recommended)
- Processing time increases with 3-second delays (trade-off for reliability)

## Contributing

This plugin integrates deeply with Dispatcharr's API and channel management system. When reporting issues:
1. Include Dispatcharr version information
2. Provide relevant container logs
3. Test with small channel groups first
4. Document specific API error messages
5. Note if using "Get Status Update" shows different information than browser display
