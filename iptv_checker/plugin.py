"""
Dispatcharr IPTV Checker Plugin
Checks stream status and analyzes stream quality
"""

import logging
import requests
import subprocess
import json
import os
import re
import csv
from datetime import datetime
from urllib.parse import urlparse
import shutil
import threading
import time

# Setup logging using Dispatcharr's format
LOGGER = logging.getLogger("plugins.iptv_checker")
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    handler.setFormatter(formatter)
    LOGGER.addHandler(handler)
LOGGER.setLevel(logging.INFO)

class Plugin:
    """Dispatcharr IPTV Checker Plugin"""
    
    name = "IPTV Checker"
    version = "0.1"
    description = "Check IPTV stream status and analyze stream quality"
    
    # Settings rendered by UI
    fields = [
        {
            "id": "m3u_url",
            "label": "M3U8 Playlist URL",
            "type": "string",
            "default": "",
            "placeholder": "http://example.com/playlist.m3u8",
            "help_text": "Base name for the EPG Source(s) added to Dispatcharr.",
        },
        {
            "id": "timeout",
            "label": "Connection Timeout (seconds)",
            "type": "number",
            "default": 10,
            "help_text": "Timeout for stream connection attempts",
        },
        {
            "id": "selected_groups",
            "label": "Groups to Check (comma-separated)",
            "type": "string",
            "default": "",
            "help_text": "Specific groups to check, or leave empty to check all groups",
        },
    ]
    
    # Actions for Dispatcharr UI
    actions = [
        {
            "id": "load_playlist",
            "label": "Load Playlist",
            "description": "Load and parse the M3U8 playlist to discover available groups",
        },
        {
            "id": "preview_check",
            "label": "Preview Check",
            "description": "Preview what groups and channels will be checked based on current settings",
        },
        {
            "id": "check_streams",
            "label": "Check Streams", 
            "description": "Check stream status and analyze quality issues",
            "confirm": {
                "required": True,
                "title": "Check IPTV Streams?",
                "message": "This will check streams. Use 'Preview Check' first to see what will be checked. Continue?",
            }
        },
        {
            "id": "view_table",
            "label": "View Results Table",
            "description": "Display detailed results in table format"
        },
        {
            "id": "get_results",
            "label": "View Last Results",
            "description": "Display summary of the last stream check results"
        },
        {
            "id": "export_results",
            "label": "Export Results to CSV",
            "description": "Export the last check results to a CSV file"
        }
    ]
    
    def __init__(self):
        self.results_file = "/data/iptv_checker_results.json"
        self.channels = []
        self.check_progress = {"current": 0, "total": 0, "status": "idle"}
        self.groups = []
        self.last_check_summary = {}
        
        LOGGER.info(f"{self.name} Plugin v{self.version} initialized")

    def run(self, action, params, context):
        """Main plugin entry point"""
        LOGGER.info(f"IPTV Checker run called with action: {action}")
        
        try:
            # Get settings from context (Dispatcharr provides this)
            settings = context.get("settings", {})
            logger = context.get("logger", LOGGER)
            
            if action == "load_playlist":
                return self.load_playlist_action(settings, logger)
            elif action == "preview_check":
                return self.preview_check_action(settings, logger)
            elif action == "check_streams":
                return self.check_streams_action(settings, logger)
            elif action == "view_table":
                return self.view_table_action(settings, logger)
            elif action == "get_results":
                return self.get_results_action(settings, logger)
            elif action == "export_results":
                return self.export_results_action(settings, logger)
            else:
                return {
                    "status": "error",
                    "message": f"Unknown action: {action}",
                    "available_actions": [
                        "load_playlist", "check_streams", "get_results", 
                        "export_results"
                    ]
                }
        except Exception as e:
            LOGGER.error(f"Error in plugin run: {str(e)}")
            return {"status": "error", "message": str(e)}
    
    def preview_check_action(self, settings, logger):
        """Preview what will be checked based on current settings"""
        try:
            m3u_url = settings.get("m3u_url", "").strip()
            if not m3u_url:
                return {"status": "error", "message": "Please configure M3U8 URL in plugin settings first"}
            
            # Validate URL format
            if not m3u_url.startswith(('http://', 'https://')):
                return {"status": "error", "message": f"Invalid URL format: {m3u_url}. URL must start with http:// or https://"}
            
            # Load M3U to get current channel data
            headers = {'User-Agent': 'IPTVChecker 1.0'}
            logger.info(f"Attempting to load M3U from: {m3u_url}")
            response = requests.get(m3u_url, headers=headers, timeout=30)
            response.raise_for_status()
            
            all_channels = self.parse_m3u(response.text)
            available_groups = list(set(ch.get('group', 'No Group') for ch in all_channels))
            available_groups.sort()
            
            # Check what will be filtered
            selected_groups_str = settings.get("selected_groups", "").strip()
            if selected_groups_str:
                selected_groups = [g.strip() for g in selected_groups_str.split(',') if g.strip()]
                filtered_channels = [ch for ch in all_channels 
                                   if ch.get('group', 'No Group') in selected_groups]
                
                # Find which groups exist and which don't
                existing_groups = [g for g in selected_groups if g in available_groups]
                missing_groups = [g for g in selected_groups if g not in available_groups]
                
                message_parts = [f"Preview for 'Check Streams' action:"]
                
                if filtered_channels:
                    message_parts.extend([
                        f"• Will check: {len(filtered_channels)} channels",
                        f"• From groups: {', '.join(existing_groups)} ({len(existing_groups)} groups)"
                    ])
                    
                    # Show channel count per group
                    group_counts = {}
                    for ch in filtered_channels:
                        group = ch.get('group', 'No Group')
                        group_counts[group] = group_counts.get(group, 0) + 1
                    
                    message_parts.append("• Channels per group:")
                    for group in sorted(group_counts.keys()):
                        message_parts.append(f"  - {group}: {group_counts[group]} channels")
                else:
                    message_parts.append("• Will check: 0 channels (no matches found)")
                
                if missing_groups:
                    message_parts.extend([
                        f"• Groups not found: {', '.join(missing_groups)}",
                        f"• Available groups: {', '.join(available_groups)}"
                    ])
                    
            else:
                # Will check all groups
                group_counts = {}
                for ch in all_channels:
                    group = ch.get('group', 'No Group')
                    group_counts[group] = group_counts.get(group, 0) + 1
                
                message_parts = [
                    f"Preview for 'Check Streams' action:",
                    f"• Will check: {len(all_channels)} channels (all channels)",
                    f"• From groups: all {len(available_groups)} groups",
                    "• Channels per group:"
                ]
                
                for group in sorted(group_counts.keys()):
                    message_parts.append(f"  - {group}: {group_counts[group]} channels")
            
            # Add timing estimate
            total_channels = len(filtered_channels) if selected_groups_str else len(all_channels)
            timeout = settings.get("timeout", 10)
            estimated_minutes = (total_channels * (timeout + 5)) / 60  # rough estimate
            
            message_parts.extend([
                f"• Estimated time: {estimated_minutes:.1f} minutes",
                f"• Timeout per stream: {timeout} seconds"
            ])
            
            return {
                "status": "success",
                "message": "\n".join(message_parts)
            }
            
        except requests.RequestException as e:
            return {"status": "error", "message": f"Error loading playlist for preview: {str(e)}"}
        except Exception as e:
            logger.error(f"Unexpected error in preview_check: {str(e)}")
            return {"status": "error", "message": f"Error generating preview: {str(e)}"}

    def load_playlist_action(self, settings, logger):
        """Load playlist and discover groups - Dispatcharr action"""
        m3u_url = settings.get("m3u_url", "").strip()
        if not m3u_url:
            return {"status": "error", "message": "Please configure M3U8 URL in plugin settings first"}
        
        try:
            headers = {'User-Agent': 'IPTVChecker 1.0'}
            response = requests.get(m3u_url, headers=headers, timeout=30)
            response.raise_for_status()
            
            channels = self.parse_m3u(response.text)
            groups = list(set(channel.get('group', 'No Group') for channel in channels))
            groups.sort()
            
            # Store for later use
            self.channels = channels
            self.groups = groups
            
            # Save groups to a file for persistence
            try:
                groups_file = "/data/iptv_checker_groups.json"
                with open(groups_file, 'w') as f:
                    json.dump({"groups": groups, "channels_count": len(channels)}, f)
            except Exception as e:
                logger.warning(f"Could not save groups to file: {str(e)}")
            
            # Format groups for easy copy-paste
            groups_text = ", ".join(groups)
            
            return {
                "status": "success",
                "message": f"Loaded {len(channels)} channels from {len(groups)} groups.\n\nAvailable groups:\n{groups_text}\n\nCopy the groups you want to check into the 'Groups to Check' setting above (comma-separated), or leave empty to check all groups.",
                "channels": len(channels),
                "groups": len(groups)
            }
        except Exception as e:
            LOGGER.error(f"Error loading M3U: {str(e)}")
            return {"status": "error", "message": f"Failed to load playlist: {str(e)}"}
    
    def check_streams_action(self, settings, logger):
        """Start stream checking - Dispatcharr action"""
        try:
            m3u_url = settings.get("m3u_url", "").strip()
            if not m3u_url:
                return {"status": "error", "message": "Please configure M3U8 URL in plugin settings first"}
            
            # Load M3U first to get current channel data
            headers = {'User-Agent': 'IPTVChecker 1.0'}
            response = requests.get(m3u_url, headers=headers, timeout=30)
            response.raise_for_status()
            
            all_channels = self.parse_m3u(response.text)
            
            # Filter by selected groups
            selected_groups_str = settings.get("selected_groups", "").strip()
            if selected_groups_str:
                selected_groups = [g.strip() for g in selected_groups_str.split(',') if g.strip()]
                filtered_channels = [ch for ch in all_channels 
                                   if ch.get('group', 'No Group') in selected_groups]
                
                # Check if any of the selected groups actually exist
                available_groups = list(set(ch.get('group', 'No Group') for ch in all_channels))
                missing_groups = [g for g in selected_groups if g not in available_groups]
                
                if not filtered_channels:
                    return {"status": "error", "message": f"No channels found in selected groups: {', '.join(selected_groups)}.\n\nAvailable groups: {', '.join(sorted(available_groups))}\n\nPlease check your group names and try again."}
                
                if missing_groups:
                    logger.warning(f"Groups not found: {', '.join(missing_groups)}")
                    
                group_info = f"{len(selected_groups)} selected groups ({', '.join(selected_groups)})"
            else:
                filtered_channels = all_channels
                selected_groups = list(set(ch.get('group', 'No Group') for ch in all_channels))
                group_info = f"all {len(selected_groups)} groups"
            
            self.channels = filtered_channels
            
            # Log what we're about to check
            logger.info(f"Will check {len(filtered_channels)} channels in {group_info}")
            
            # Start checking
            config = {
                "timeout": settings.get("timeout", 10)
            }
            
            # Run synchronously for better user feedback
            results = self.check_all_streams_sync(config, logger)
            
            return {
                "status": "success",
                "message": f"Completed checking {len(self.channels)} channels in {group_info}.\n\nResults:\n• Alive: {results['alive']}\n• Dead: {results['dead']}\n• Low framerate (≤30fps): {results['low_framerate']}\n• Mislabeled resolution: {results['mislabeled']}\n\nUse 'View Last Results' for detailed breakdown or 'Export Results to CSV' to download the data.",
                "results": results
            }
            
        except requests.RequestException as e:
            LOGGER.error(f"Network error checking streams: {str(e)}")
            return {"status": "error", "message": f"Network error loading M3U playlist: {str(e)}"}
        except Exception as e:
            LOGGER.error(f"Error checking streams: {str(e)}")
            return {"status": "error", "message": f"Error during stream check: {str(e)}"}
    
    def view_table_action(self, settings, logger):
        """Display results in table format"""
        if not os.path.exists(self.results_file):
            return {"status": "error", "message": "No results available. Run 'Check Streams' first."}
        
        try:
            with open(self.results_file, 'r') as f:
                results = json.load(f)
            
            channels = results.get('channels', [])
            if not channels:
                return {"status": "error", "message": "No channel data found in results."}
            
            # Create table header
            table_lines = []
            table_lines.append("=" * 120)
            table_lines.append(f"{'Channel Name':<35} {'Group':<15} {'Status':<8} {'Resolution':<12} {'FPS':<8} {'Codec':<8} {'Issues':<15}")
            table_lines.append("=" * 120)
            
            # Process each channel
            for channel in channels:
                name = channel.get('name', 'Unknown')[:34]
                group = channel.get('group', 'None')[:14]
                status = channel.get('status', 'Unknown')
                resolution = channel.get('resolution', 'N/A')[:11]
                framerate = channel.get('framerate', 'N/A')[:7]
                codec = channel.get('codec', 'N/A')[:7]
                
                # Build issues list
                issues = []
                if channel.get('low_framerate'):
                    issues.append('Low FPS')
                if channel.get('mislabeled'):
                    issues.append('Mislabeled')
                if channel.get('status') == 'Dead':
                    issues.append('Dead')
                issues_str = ','.join(issues)[:14]
                
                # Format status with indicators
                if status == 'Alive':
                    status_display = '✓ Alive'
                elif status == 'Dead':
                    status_display = '✗ Dead'
                else:
                    status_display = status
                
                table_lines.append(f"{name:<35} {group:<15} {status_display:<8} {resolution:<12} {framerate:<8} {codec:<8} {issues_str:<15}")
            
            table_lines.append("=" * 120)
            
            # Add summary
            summary = results.get('summary', {})
            table_lines.append(f"Summary: {summary.get('total', 0)} total | {summary.get('alive', 0)} alive | {summary.get('dead', 0)} dead | {summary.get('low_framerate', 0)} low FPS | {summary.get('mislabeled', 0)} mislabeled")
            
            # Add error details for dead streams (first 5 errors)
            dead_channels = [ch for ch in channels if ch.get('status') == 'Dead']
            if dead_channels:
                table_lines.append("")
                table_lines.append("Dead Stream Errors:")
                table_lines.append("-" * 80)
                for i, channel in enumerate(dead_channels[:5]):
                    error = channel.get('error', 'Unknown error')[:60]
                    table_lines.append(f"{i+1}. {channel.get('name', 'Unknown')[:30]}: {error}")
                if len(dead_channels) > 5:
                    table_lines.append(f"... and {len(dead_channels) - 5} more dead streams")
            
            return {
                "status": "success",
                "message": "\n".join(table_lines)
            }
            
        except Exception as e:
            return {"status": "error", "message": f"Error creating table: {str(e)}"}

    def get_results_action(self, settings, logger):
        """Display results summary"""
        if not os.path.exists(self.results_file):
            return {"status": "error", "message": "No results available. Run 'Check Streams' first."}
        
        try:
            with open(self.results_file, 'r') as f:
                results = json.load(f)
            
            channels = results.get('channels', [])
            summary = results.get('summary', {})
            
            # Create detailed summary
            alive_channels = [ch for ch in channels if ch.get('status') == 'Alive']
            dead_channels = [ch for ch in channels if ch.get('status') == 'Dead']
            low_fps_channels = [ch for ch in channels if ch.get('low_framerate')]
            mislabeled_channels = [ch for ch in channels if ch.get('mislabeled')]
            
            # Top issues
            error_summary = {}
            for ch in dead_channels:
                error = ch.get('error', 'Unknown error')
                error_summary[error] = error_summary.get(error, 0) + 1
            
            message_parts = [
                f"Last check results ({summary.get('total', 0)} channels):",
                f"• Alive: {summary.get('alive', 0)}",
                f"• Dead: {summary.get('dead', 0)}",
                f"• Low framerate (<30fps): {summary.get('low_framerate', 0)}",
                f"• Mislabeled resolution: {summary.get('mislabeled', 0)}"
            ]
            
            if error_summary:
                message_parts.append("\nTop errors:")
                for error, count in sorted(error_summary.items(), key=lambda x: x[1], reverse=True)[:3]:
                    message_parts.append(f"• {error}: {count} channels")
            
            if low_fps_channels:
                message_parts.append(f"\nLow framerate channels: {', '.join([ch['name'][:30] for ch in low_fps_channels[:5]])}")
                
            if mislabeled_channels:
                message_parts.append(f"\nMislabeled channels: {', '.join([ch['name'][:30] for ch in mislabeled_channels[:5]])}")
            
            return {
                "status": "success", 
                "message": '\n'.join(message_parts),
                "results": summary
            }
        except Exception as e:
            return {"status": "error", "message": f"Error reading results: {str(e)}"}
    
    def export_results_action(self, settings, logger):
        """Export results to CSV"""
        if not os.path.exists(self.results_file):
            return {"status": "error", "message": "No results to export. Run 'Check Streams' first."}
        
        try:
            with open(self.results_file, 'r') as f:
                results = json.load(f)
            
            channels = results.get('channels', [])
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"iptv_check_results_{timestamp}.csv"
            filepath = os.path.join("/data/exports", filename)
            
            # Ensure export directory exists
            os.makedirs("/data/exports", exist_ok=True)
            
            with open(filepath, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = [
                    'name', 'group', 'url', 'status', 'error', 'codec',
                    'resolution', 'framerate', 'bitrate', 'low_framerate',
                    'mislabeled', 'checked_at'
                ]
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                
                writer.writeheader()
                for channel in channels:
                    row = {field: channel.get(field, '') for field in fieldnames}
                    writer.writerow(row)
            
            return {
                "status": "success", 
                "message": f"Results exported to {filepath}",
                "file_path": filepath,
                "total_channels": len(channels)
            }
        except Exception as e:
            LOGGER.error(f"Error exporting CSV: {str(e)}")
            return {"status": "error", "message": str(e)}
    
    def parse_m3u(self, content):
        """Parse M3U playlist content"""
        channels = []
        lines = content.split('\n')
        current_info = {}
        
        for line in lines:
            line = line.strip()
            if line.startswith('#EXTINF:'):
                # Parse channel info
                info_match = re.search(r'#EXTINF:.*?,(.*)', line)
                if info_match:
                    current_info['name'] = info_match.group(1).strip()
                
                # Extract group
                group_match = re.search(r'group-title="([^"]*)"', line)
                current_info['group'] = group_match.group(1) if group_match else 'No Group'
                
                # Extract logo
                logo_match = re.search(r'tvg-logo="([^"]*)"', line)
                current_info['logo'] = logo_match.group(1) if logo_match else ''
                
            elif line and not line.startswith('#') and current_info:
                # This is the stream URL
                current_info['url'] = line
                channels.append(current_info.copy())
                current_info = {}
        
        return channels
    
    def check_all_streams_sync(self, config, logger):
        """Check all streams synchronously and return summary"""
        try:
            timeout = config.get("timeout", 10)
            
            alive_count = 0
            dead_count = 0
            low_framerate_count = 0
            mislabeled_count = 0
            
            for i, channel in enumerate(self.channels):
                logger.info(f"Checking channel {i+1}/{len(self.channels)}: {channel['name']}")
                
                # Check stream status and get info
                stream_info = self.check_stream(channel['url'], timeout)
                
                if stream_info['status'] == 'Alive':
                    alive_count += 1
                else:
                    dead_count += 1
                
                # Detect quality issues
                low_framerate = stream_info.get('framerate_num', 0) < 30 and stream_info.get('framerate_num', 0) > 0
                mislabeled = self.detect_mislabeled(channel['name'], stream_info.get('resolution', ''))
                
                if low_framerate:
                    low_framerate_count += 1
                if mislabeled:
                    mislabeled_count += 1
                
                # Update channel with results
                channel.update({
                    'status': stream_info['status'],
                    'error': stream_info.get('error', ''),
                    'codec': stream_info.get('codec', ''),
                    'resolution': stream_info.get('resolution', ''),
                    'framerate': stream_info.get('framerate', ''),
                    'bitrate': stream_info.get('bitrate', ''),
                    'low_framerate': low_framerate,
                    'mislabeled': mislabeled,
                    'checked_at': datetime.now().isoformat()
                })
            
            # Save results
            summary = {
                "total": len(self.channels),
                "alive": alive_count,
                "dead": dead_count,
                "low_framerate": low_framerate_count,
                "mislabeled": mislabeled_count,
                "checked_at": datetime.now().isoformat()
            }
            
            results = {
                "channels": self.channels,
                "summary": summary
            }
            
            with open(self.results_file, 'w') as f:
                json.dump(results, f, indent=2)
            
            logger.info("Stream checking completed")
            return summary
            
        except Exception as e:
            logger.error(f"Error during stream checking: {str(e)}")
            return {"total": 0, "alive": 0, "dead": 0, "low_framerate": 0, "mislabeled": 0}
    
    def check_stream(self, url, timeout):
        """Check individual stream status and get info"""
        try:
            # Use ffprobe to get stream info
            cmd = [
                '/usr/local/bin/ffprobe',
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_streams',
                '-show_format',
                '-user_agent', 'IPTVChecker 1.0',
                '-timeout', str(timeout * 1000000),  # ffprobe uses microseconds
                url
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
            
            if result.returncode != 0:
                return {
                    'status': 'Dead',
                    'error': result.stderr or 'Stream not accessible'
                }
            
            # Parse ffprobe output
            probe_data = json.loads(result.stdout)
            video_stream = next((s for s in probe_data.get('streams', []) 
                               if s['codec_type'] == 'video'), None)
            audio_stream = next((s for s in probe_data.get('streams', []) 
                               if s['codec_type'] == 'audio'), None)
            
            if not video_stream:
                return {
                    'status': 'Dead',
                    'error': 'No video stream found'
                }
            
            # Extract stream info
            resolution = f"{video_stream.get('width', 0)}x{video_stream.get('height', 0)}"
            framerate = video_stream.get('r_frame_rate', '0/1')
            framerate_num = self.parse_framerate(framerate)
            
            return {
                'status': 'Alive',
                'codec': video_stream.get('codec_name', ''),
                'resolution': resolution,
                'framerate': f"{framerate_num:.2f} fps" if framerate_num > 0 else '',
                'framerate_num': framerate_num,
                'bitrate': audio_stream.get('bit_rate', '') if audio_stream else ''
            }
            
        except subprocess.TimeoutExpired:
            return {'status': 'Dead', 'error': 'Connection timeout'}
        except Exception as e:
            return {'status': 'Dead', 'error': str(e)}
    
    def parse_framerate(self, framerate_str):
        """Parse framerate string to float"""
        try:
            if '/' in framerate_str:
                num, den = framerate_str.split('/')
                return float(num) / float(den) if float(den) != 0 else 0
            return float(framerate_str)
        except:
            return 0
    
    def detect_mislabeled(self, channel_name, resolution):
        """Detect mislabeled channels"""
        if not resolution or 'x' not in resolution:
            return False
            
        try:
            width, height = map(int, resolution.split('x'))
            
            # Common resolution labels in channel names
            if '4K' in channel_name.upper():
                return width < 3840 or height < 2160
            elif '1080P' in channel_name.upper() or 'FHD' in channel_name.upper():
                return width < 1920 or height < 1080
            elif '720P' in channel_name.upper() or 'HD' in channel_name.upper():
                return width < 1280 or height < 720
            
            return False
        except:
            return False

# Export fields and actions for Dispatcharr plugin system
fields = Plugin.fields
actions = Plugin.actions