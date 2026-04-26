"""
Dispatcharr IPTV Checker Plugin
Checks stream status and analyzes stream quality
"""

import logging
import subprocess
import json
import os
import re
import csv
import fnmatch
import time
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Django ORM imports (plugins run inside the Django backend process)
from apps.channels.models import Channel, ChannelGroup, Stream, ChannelStream
from django.db import transaction
from core.utils import send_websocket_update

# Scheduler imports
try:
    import pytz
    PYTZ_AVAILABLE = True
except ImportError:
    PYTZ_AVAILABLE = False
    # Will log warning later when scheduler is attempted to be used

# Django/Dispatcharr imports for metadata updates
try:
    from apps.proxy.ts_proxy.services.channel_service import ChannelService
    DISPATCHARR_INTEGRATION_AVAILABLE = True
except ImportError:
    DISPATCHARR_INTEGRATION_AVAILABLE = False

# Setup logging with plugin name for Dispatcharr's logging system
class PluginNameFilter(logging.Filter):
    """Filter that adds [IPTV Checker] prefix to all log messages"""
    def filter(self, record):
        if not record.getMessage().startswith('[IPTV Checker]'):
            record.msg = f'[IPTV Checker] {record.msg}'
        return True

LOGGER = logging.getLogger("plugins.iptv_checker")
LOGGER.addFilter(PluginNameFilter())

# --- Scheduler Globals ---
_bg_scheduler_thread = None
_scheduler_stop_event = threading.Event()
_scheduler_pending_run = False  # Flag to queue a run if check already in progress

LOG_PREFIX = "[IPTV Checker]"


class PluginConfig:
    # --- File Paths ---
    DATA_DIR = "/data"
    EXPORTS_DIR = "/data/exports"
    RESULTS_FILE = "/data/iptv_checker_results.json"
    LOADED_CHANNELS_FILE = "/data/iptv_checker_loaded_channels.json"
    PROGRESS_FILE = "/data/iptv_checker_progress.json"

    # --- Scheduler ---
    DEFAULT_TIMEZONE = "America/Chicago"
    SCHEDULER_CHECK_INTERVAL = 30  # Check every 30 seconds
    SCHEDULER_TIME_WINDOW = 30  # ±30 second window to trigger
    SCHEDULER_ERROR_WAIT = 60  # Wait 60s if error occurs
    SCHEDULER_STOP_TIMEOUT = 5  # Max wait for thread to stop

    # --- ETA Estimation ---
    # Fallback only; _estimate_check_seconds models a realistic mix.
    ESTIMATED_SECONDS_PER_STREAM = 10
    # Assume 20% of streams fail and burn the full probe_timeout * (1+retries).
    ESTIMATED_DEAD_RATE = 0.2
    # Per-stream overhead on top of ffprobe analysis (TCP connect, teardown).
    ESTIMATED_PROBE_OVERHEAD_SECONDS = 2

    # --- Version Check ---
    VERSION_CHECK_DURATION = 86400  # Cache version check for 24 hours


class ProgressTracker:
    """Tracks operation progress with periodic WebSocket notifications."""

    def __init__(self, total_items, action_id, logger):
        self.total_items = max(total_items, 1)
        self.action_id = action_id
        self.logger = logger
        self.start_time = time.time()
        self.last_update_time = self.start_time
        # Adaptive interval: shorter for smaller jobs so they still show progress
        self.update_interval = 3 if total_items <= 50 else 5 if total_items <= 200 else 10
        self.processed_items = 0
        logger.info(f"{LOG_PREFIX} [{action_id}] Starting: {total_items} items to process")
        send_websocket_update('updates', 'update', {
            "type": "plugin", "plugin": "IPTV Checker",
            "message": f"🔄 {action_id}: Starting ({total_items} items)"
        })

    def update(self, items_processed=1):
        self.processed_items += items_processed
        now = time.time()
        if now - self.last_update_time >= self.update_interval:
            self.last_update_time = now
            elapsed = now - self.start_time
            pct = (self.processed_items / self.total_items) * 100
            remaining = (elapsed / self.processed_items) * (self.total_items - self.processed_items) if self.processed_items > 0 else 0
            eta_str = ProgressTracker.format_eta(remaining)
            self.logger.info(f"{LOG_PREFIX} [{self.action_id}] {pct:.0f}% ({self.processed_items}/{self.total_items}) - ETA: {eta_str}")
            send_websocket_update('updates', 'update', {
                "type": "plugin", "plugin": "IPTV Checker",
                "message": f"🔄 {self.action_id}: {pct:.0f}% ({self.processed_items}/{self.total_items}) - ⏱️ ETA: {eta_str}"
            })

    def finish(self):
        elapsed = time.time() - self.start_time
        eta_str = ProgressTracker.format_eta(elapsed)
        self.logger.info(f"{LOG_PREFIX} [{self.action_id}] Complete: {self.processed_items}/{self.total_items} in {eta_str}")
        send_websocket_update('updates', 'update', {
            "type": "plugin", "plugin": "IPTV Checker",
            "message": f"✅ {self.action_id}: Complete ({self.processed_items}/{self.total_items}) in {eta_str}"
        })

    @staticmethod
    def format_eta(seconds):
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s"
        else:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            return f"{h}h {m}m"

class Plugin:
    """Dispatcharr IPTV Checker Plugin"""
    
    # Explicitly set the plugin key
    key = "iptv_checker"
    version = "1.26.1161403"

    # Fields and actions are defined in plugin.json (single source of truth)
    def __init__(self):
        self.results_file = PluginConfig.RESULTS_FILE
        self.loaded_channels_file = PluginConfig.LOADED_CHANNELS_FILE
        self.progress_file = PluginConfig.PROGRESS_FILE
        self.check_progress = self._load_progress()
        self.load_progress = {"current": 0, "total": 0, "status": "idle"}  # Track load groups progress
        self._thread = None
        self._thread_lock = threading.Lock()
        self._stop_event = threading.Event()
        self.timeout_retry_queue = []  # Queue for streams that timed out and need retry
        self.version_check_cache = None  # Cached version check result
        self.version_check_time = None  # Time when version was last checked
        LOGGER.info(f"Plugin v{self.version} initialized")

        # Start scheduler on init so it survives container restarts
        self._init_scheduler()

    def _init_scheduler(self):
        """Load saved settings from DB and start the scheduler if configured."""
        try:
            from apps.plugins.models import PluginConfig as DBPluginConfig
            cfg = DBPluginConfig.objects.filter(key=self.key).first()
            if cfg and cfg.settings and cfg.settings.get("scheduled_times", "").strip():
                LOGGER.info("Loading saved settings for scheduler startup")
                self._start_background_scheduler(cfg.settings)
        except Exception as e:
            LOGGER.warning(f"Could not load settings for scheduler on init: {e}")

    def _fresh_settings(self, fallback):
        """Re-read settings from DB so cron uses latest values."""
        try:
            from apps.plugins.models import PluginConfig as DBPluginConfig
            cfg = DBPluginConfig.objects.filter(key=self.key).first()
            if cfg and cfg.settings:
                return cfg.settings
        except Exception as e:
            LOGGER.warning(f"Could not refresh settings from DB; using cached snapshot: {e}")
        return fallback

    def _try_start_thread(self, target, args):
        """Atomically check if a thread is running and start a new one.
        Returns True if started, False if another operation is running."""
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(target=target, args=args, daemon=True)
            self._thread.start()
            return True

    def _load_progress(self):
        """Load check progress from persistent storage"""
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                LOGGER.warning(f"Failed to load progress file: {e}")
        return {"current": 0, "total": 0, "status": "idle", "start_time": None}

    def _save_progress(self):
        """Save check progress to persistent storage"""
        try:
            with open(self.progress_file, 'w') as f:
                json.dump(self.check_progress, f)
        except Exception as e:
            LOGGER.error(f"Failed to save progress file: {e}")

    def _load_json_file(self, filepath):
        """Safely load a JSON file, returning None if corrupted or missing."""
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, ValueError) as e:
            LOGGER.error(f"Corrupted JSON file {filepath}: {e}")
            return None
        except Exception as e:
            LOGGER.error(f"Failed to load JSON file {filepath}: {e}")
            return None

    def _save_json_file(self, filepath, data, indent=None):
        """Atomically save data to a JSON file using temp file + rename."""
        try:
            tmp_path = filepath + '.tmp'
            with open(tmp_path, 'w') as f:
                json.dump(data, f, indent=indent, default=str)
            os.replace(tmp_path, filepath)
        except Exception as e:
            LOGGER.error(f"Failed to save JSON file {filepath}: {e}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def stop(self, context):
        logger = context.get("logger", LOGGER)
        logger.info("Plugin unloading - stopping scheduler and active threads")
        self._stop_background_scheduler()
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
    
    def _parse_scheduled_times(self, scheduled_times_str):
        """
        Parse comma-separated cron expressions.
        Format: 'minute hour day month weekday'
        Example: "0 4 * * *" = daily at 4:00 AM
        Example: "0 3 1 * *" = 1st of month at 3:00 AM
        Returns list of cron expression strings.
        """
        if not scheduled_times_str or not scheduled_times_str.strip():
            return []
        
        cron_expressions = []
        for expr in scheduled_times_str.split(','):
            expr = expr.strip()
            if expr:
                # Validate basic cron format (5 fields)
                parts = expr.split()
                if len(parts) == 5:
                    cron_expressions.append(expr)
                else:
                    LOGGER.warning(f"Invalid cron expression (must have 5 fields): {expr}")
        
        return cron_expressions

    def _cron_matches(self, cron_expr, dt):
        """
        Check if a cron expression matches the given datetime.
        Format: 'minute hour day month weekday'
        Supports: specific values, *, */n (step values), and ranges (not implemented for simplicity)
        """
        try:
            parts = cron_expr.split()
            if len(parts) != 5:
                return False
            
            minute_expr, hour_expr, day_expr, month_expr, weekday_expr = parts
            
            # Check minute (0-59)
            if not self._cron_field_matches(minute_expr, dt.minute, 0, 59):
                return False
            
            # Check hour (0-23)
            if not self._cron_field_matches(hour_expr, dt.hour, 0, 23):
                return False
            
            # Check day of month (1-31)
            if not self._cron_field_matches(day_expr, dt.day, 1, 31):
                return False
            
            # Check month (1-12)
            if not self._cron_field_matches(month_expr, dt.month, 1, 12):
                return False
            
            # Check day of week (0-6, Sunday=0)
            # Python's weekday() returns 0=Monday, so convert: (weekday + 1) % 7
            python_weekday = dt.weekday()
            cron_weekday = (python_weekday + 1) % 7
            if not self._cron_field_matches(weekday_expr, cron_weekday, 0, 6):
                return False
            
            return True
        except Exception as e:
            LOGGER.error(f"Error matching cron expression '{cron_expr}': {e}")
            return False
    
    def _cron_field_matches(self, field_expr, current_value, min_val, max_val):
        """
        Check if a single cron field matches the current value.
        Supports: *, specific number, */n (step), ranges (1-5), lists (1,3,5)
        """
        field_expr = field_expr.strip()
        
        # Wildcard - matches anything
        if field_expr == '*':
            return True
        
        # Step values (e.g., */2 for every 2 units)
        if field_expr.startswith('*/'):
            try:
                step = int(field_expr[2:])
                return current_value % step == 0
            except ValueError:
                return False
        
        # Lists (e.g., 1,3,5)
        if ',' in field_expr:
            try:
                values = [int(v.strip()) for v in field_expr.split(',')]
                return current_value in values
            except ValueError:
                return False
        
        # Ranges (e.g., 1-5)
        if '-' in field_expr:
            try:
                start, end = field_expr.split('-')
                start_val = int(start.strip())
                end_val = int(end.strip())
                return start_val <= current_value <= end_val
            except (ValueError, IndexError):
                return False
        
        # Specific value
        try:
            target_value = int(field_expr)
            return current_value == target_value
        except ValueError:
            return False
    
    def _start_background_scheduler(self, settings):
        """Start the background scheduler thread."""
        global _bg_scheduler_thread, _scheduler_pending_run
        
        # Check if pytz is available
        if not PYTZ_AVAILABLE:
            LOGGER.error("Scheduler requires pytz library but it is not installed")
            return
        
        # Stop any existing scheduler first
        self._stop_background_scheduler()
        
        # Get and validate schedule configuration
        scheduled_times_str = settings.get("scheduled_times", "")
        if not scheduled_times_str:
            LOGGER.warning("Scheduler enabled but no scheduled times configured")
            return
        
        scheduled_times = self._parse_scheduled_times(scheduled_times_str)
        if not scheduled_times:
            LOGGER.error(f"Invalid scheduled times format: {scheduled_times_str}")
            return
        
        # Get timezone
        tz_str = settings.get('scheduler_timezone', PluginConfig.DEFAULT_TIMEZONE)
        try:
            local_tz = pytz.timezone(tz_str)
        except pytz.exceptions.UnknownTimeZoneError:
            LOGGER.error(f"Unknown timezone: {tz_str}, using default: {PluginConfig.DEFAULT_TIMEZONE}")
            tz_str = PluginConfig.DEFAULT_TIMEZONE
            local_tz = pytz.timezone(tz_str)
        
        # Define the scheduler loop
        def scheduler_loop():
            global _scheduler_pending_run
            nonlocal local_tz
            last_run = {}  # Track last run timestamp for each cron expression (to minute precision)
            
            LOGGER.info(f"Scheduler started. Timezone: {tz_str}, Cron expressions: {scheduled_times}")
            
            while not _scheduler_stop_event.is_set():
                try:
                    now = datetime.now(local_tz)
                    # Truncate to minute precision for matching (ignore seconds)
                    current_minute = now.replace(second=0, microsecond=0)
                    
                    for cron_expr in scheduled_times:
                        # Check if this cron expression matches the current time
                        if self._cron_matches(cron_expr, now):
                            # Check if we already ran this minute
                            if last_run.get(cron_expr) == current_minute:
                                continue  # Already ran this minute
                            
                            LOGGER.info(f"⏰ SCHEDULED RUN triggered at {now.strftime('%Y-%m-%d %H:%M:%S')} for cron: {cron_expr}")
                            
                            # Mark as run for this minute immediately to prevent duplicate triggers
                            last_run[cron_expr] = current_minute
                            
                            # Check if a check is already running
                            if self.check_progress.get('status') == 'running':
                                LOGGER.warning("Scheduled run triggered but a check is already running - queuing for later")
                                _scheduler_pending_run = True
                            else:
                                # Execute scheduled task with the latest persisted settings
                                # (not the closure's snapshot — settings may have been edited
                                # since the scheduler started).
                                try:
                                    self._execute_scheduled_check(self._fresh_settings(settings))
                                except Exception as e:
                                    LOGGER.error(f"Scheduled check failed: {e}", exc_info=True)

                            break  # Only trigger one schedule per check cycle

                    # Check if there's a pending run and no check is currently running
                    if _scheduler_pending_run and self.check_progress.get('status') != 'running':
                        LOGGER.info("⏰ Executing queued scheduled run")
                        _scheduler_pending_run = False
                        try:
                            self._execute_scheduled_check(self._fresh_settings(settings))
                        except Exception as e:
                            LOGGER.error(f"Queued scheduled check failed: {e}", exc_info=True)
                    
                    # Sleep efficiently
                    _scheduler_stop_event.wait(PluginConfig.SCHEDULER_CHECK_INTERVAL)
                
                except Exception as e:
                    LOGGER.error(f"Scheduler loop error: {e}", exc_info=True)
                    _scheduler_stop_event.wait(PluginConfig.SCHEDULER_ERROR_WAIT)
            
            LOGGER.info("Scheduler stopped")
        
        # Start the scheduler thread
        _bg_scheduler_thread = threading.Thread(
            target=scheduler_loop,
            name="iptv-checker-scheduler",
            daemon=True
        )
        _bg_scheduler_thread.start()
        LOGGER.info("Background scheduler thread started")
    
    def _stop_background_scheduler(self):
        """Cleanly stop the background scheduler thread."""
        global _bg_scheduler_thread, _scheduler_pending_run
        
        if _bg_scheduler_thread and _bg_scheduler_thread.is_alive():
            LOGGER.info("Stopping scheduler thread...")
            _scheduler_stop_event.set()
            _bg_scheduler_thread.join(timeout=PluginConfig.SCHEDULER_STOP_TIMEOUT)
            _scheduler_stop_event.clear()
            _scheduler_pending_run = False
            _bg_scheduler_thread = None
            LOGGER.info("Scheduler thread stopped")
    
    def _execute_scheduled_check(self, settings):
        """Execute the scheduled stream check (Load Groups + Start Check)."""
        LOGGER.info("⏰ Starting scheduled check sequence")
        
        # Create a logger context for scheduled runs
        scheduled_logger = logging.getLogger("plugins.iptv_checker.scheduled")
        scheduled_logger.setLevel(logging.INFO)
        if not any(isinstance(f, PluginNameFilter) for f in scheduled_logger.filters):
            scheduled_logger.addFilter(PluginNameFilter())
        
        try:
            # Step 1: Load Groups
            LOGGER.info("⏰ SCHEDULED: Loading groups...")
            load_result = self.load_groups_action(settings, scheduled_logger)
            
            if load_result.get('status') != 'ok':
                LOGGER.error(f"⏰ SCHEDULED: Load groups failed: {load_result.get('message')}")
                return
            
            LOGGER.info(f"⏰ SCHEDULED: {load_result.get('message')}")
            
            # Step 2: Start Stream Check
            LOGGER.info("⏰ SCHEDULED: Starting stream check...")
            check_result = self.check_streams_action(settings, scheduled_logger, context={'scheduled': True})
            
            if check_result.get('status') != 'ok':
                LOGGER.error(f"⏰ SCHEDULED: Stream check failed to start: {check_result.get('message')}")
                return
            
            LOGGER.info(f"⏰ SCHEDULED: {check_result.get('message')}")
            
            # Wait for check to complete
            LOGGER.info("⏰ SCHEDULED: Waiting for stream check to complete...")
            while self.check_progress.get('status') == 'running' and not _scheduler_stop_event.is_set():
                time.sleep(5)
            
            LOGGER.info("⏰ SCHEDULED: Stream check completed")
            
            # Step 3: Export CSV if enabled
            if settings.get('scheduler_export_csv', False):
                LOGGER.info("⏰ SCHEDULED: Exporting results to CSV...")
                export_result = self.export_results_action(settings, scheduled_logger)
                LOGGER.info(f"⏰ SCHEDULED: {export_result.get('message')}")
            
            # Step 4: Rename dead channels if enabled
            if settings.get('scheduler_rename_dead_channels', False):
                LOGGER.info("⏰ SCHEDULED: Renaming dead channels...")
                rename_result = self.rename_channels_action(settings, scheduled_logger)
                LOGGER.info(f"⏰ SCHEDULED: {rename_result.get('message')}")
            
            # Step 5: Rename low framerate channels if enabled
            if settings.get('scheduler_rename_low_framerate_channels', False):
                LOGGER.info("⏰ SCHEDULED: Renaming low framerate channels...")
                rename_low_fps_result = self.rename_low_framerate_channels_action(settings, scheduled_logger)
                LOGGER.info(f"⏰ SCHEDULED: {rename_low_fps_result.get('message')}")
            
            # Step 6: Add video format suffix if enabled
            if settings.get('scheduler_add_video_format_suffix', False):
                LOGGER.info("⏰ SCHEDULED: Adding video format suffixes...")
                suffix_result = self.add_video_format_suffix_action(settings, scheduled_logger)
                LOGGER.info(f"⏰ SCHEDULED: {suffix_result.get('message')}")
            
            # Step 7: Move dead channels if enabled
            if settings.get('scheduler_move_dead_channels', False):
                LOGGER.info("⏰ SCHEDULED: Moving dead channels to group...")
                move_dead_result = self.move_dead_channels_action(settings, scheduled_logger)
                LOGGER.info(f"⏰ SCHEDULED: {move_dead_result.get('message')}")
            
            # Step 8: Move low framerate channels if enabled
            if settings.get('scheduler_move_low_framerate_channels', False):
                LOGGER.info("⏰ SCHEDULED: Moving low framerate channels to group...")
                move_low_fps_result = self.move_low_framerate_channels_action(settings, scheduled_logger)
                LOGGER.info(f"⏰ SCHEDULED: {move_low_fps_result.get('message')}")

            # Step 9: Delete dead channels if enabled
            if settings.get('scheduler_delete_dead_channels', False):
                LOGGER.info("⏰ SCHEDULED: Deleting dead channels...")
                delete_result = self.delete_dead_channels_action(settings, scheduled_logger)
                if delete_result.get('status') == 'ok':
                    LOGGER.info(f"⏰ SCHEDULED: {delete_result.get('message')}")
                else:
                    LOGGER.warning(f"⏰ SCHEDULED: {delete_result.get('message')}")

            # Step 10: Fire webhook if enabled
            if settings.get('scheduler_fire_webhook', False):
                LOGGER.info("⏰ SCHEDULED: Firing webhook...")
                webhook_result = self._fire_webhook(settings, scheduled_logger)
                if webhook_result.get('status') == 'ok':
                    LOGGER.info(f"⏰ SCHEDULED: {webhook_result.get('message')}")
                else:
                    LOGGER.warning(f"⏰ SCHEDULED: {webhook_result.get('message')}")

            LOGGER.info("⏰ SCHEDULED: Check sequence completed successfully")
            
        except Exception as e:
            LOGGER.error(f"⏰ SCHEDULED: Error during scheduled check: {e}", exc_info=True)

    def _get_latest_version(self, owner="PiratesIRC", repo="Dispatcharr-IPTV-Checker-Plugin"):
        """
        Fetches the latest release tag from GitHub using only Python's standard library.
        Returns a tuple: (latest_version_tag, status_message)
        Caches the result for 24 hours to avoid excessive API calls.
        """
        # Check if we have a valid cached result
        if self.version_check_cache and self.version_check_time:
            time_elapsed = time.time() - self.version_check_time
            if time_elapsed < PluginConfig.VERSION_CHECK_DURATION:
                LOGGER.debug(f"Using cached version check (age: {time_elapsed:.0f}s)")
                return self.version_check_cache

        # Prepare to fetch latest version from GitHub
        url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
        headers = {'User-Agent': 'Dispatcharr-Plugin-Version-Checker'}

        try:
            # Create request with headers
            req = urllib.request.Request(url, headers=headers)

            # Make the request with a 5-second timeout
            with urllib.request.urlopen(req, timeout=5) as response:
                # Read and decode the response
                data = response.read().decode('utf-8')
                json_data = json.loads(data)

                # Get the tag name (version)
                latest_version = json_data.get("tag_name", "").strip()

                if not latest_version:
                    result = (None, "ℹ️ Version Check: Unable to determine latest version")
                    self.version_check_cache = result
                    self.version_check_time = time.time()
                    return result

                # Remove 'v' prefix if present for comparison
                latest_clean = latest_version.lstrip('v')
                current_clean = self.version.lstrip('v')

                # Compare versions
                if latest_clean == current_clean:
                    message = f"✅ Version Status: You are up to date (v{self.version})"
                else:
                    # Simple version comparison (works for semantic versioning)
                    try:
                        latest_parts = [int(x) for x in latest_clean.split('.')]
                        current_parts = [int(x) for x in current_clean.split('.')]

                        # Pad shorter version with zeros
                        max_len = max(len(latest_parts), len(current_parts))
                        latest_parts += [0] * (max_len - len(latest_parts))
                        current_parts += [0] * (max_len - len(current_parts))

                        if latest_parts > current_parts:
                            message = f"🔔 Update Available: v{latest_version} is available (current: v{self.version})"
                        else:
                            message = f"✅ Version Status: You are up to date (v{self.version})"
                    except (ValueError, AttributeError):
                        # Fallback to string comparison if version parsing fails
                        if latest_version != self.version:
                            message = f"🔔 Update Available: v{latest_version} is available (current: v{self.version})"
                        else:
                            message = f"✅ Version Status: You are up to date (v{self.version})"

                result = (latest_version, message)
                self.version_check_cache = result
                self.version_check_time = time.time()
                LOGGER.info(f"Version check completed: {message}")
                return result

        except urllib.error.HTTPError as http_err:
            if http_err.code == 404:
                error_msg = "ℹ️ Version Check: Repository not found or has no releases"
            else:
                error_msg = f"ℹ️ Version Check: HTTP error {http_err.code}"
            result = (None, error_msg)
            self.version_check_cache = result
            self.version_check_time = time.time()
            LOGGER.warning(f"Version check failed: {error_msg}")
            return result
        except Exception as e:
            # Catch all other errors (timeout, network issues, etc.)
            error_msg = f"ℹ️ Version Check: Unable to check for updates (current: v{self.version})"
            result = (None, error_msg)
            self.version_check_cache = result
            self.version_check_time = time.time()
            LOGGER.debug(f"Version check error: {str(e)}")
            return result

    def run(self, action, params, context):
        """Main plugin entry point"""
        settings = context.get("settings", {})
        logger = context.get("logger", LOGGER)

        try:
            # Restart scheduler if scheduling settings may have changed
            self._start_background_scheduler(settings)

            # Add our filter to context logger to ensure all logs are prefixed
            if logger is not LOGGER and not any(isinstance(f, PluginNameFilter) for f in logger.filters):
                logger.addFilter(PluginNameFilter())

            action_map = {
                "validate_settings": self.validate_settings_action,
                "load_groups": self.load_groups_action,
                "check_streams": self.check_streams_action,
                "view_progress": self.view_progress_action,
                "cancel_check": self.cancel_check_action,
                "view_results": self.view_results_action,
                "rename_channels": self.rename_channels_action,
                "move_dead_channels": self.move_dead_channels_action,
                "rename_low_framerate_channels": self.rename_low_framerate_channels_action,
                "move_low_framerate_channels": self.move_low_framerate_channels_action,
                "add_video_format_suffix": self.add_video_format_suffix_action,
                "view_table": self.view_table_action,
                "export_results": self.export_results_action,
                "clear_csv_exports": self.clear_csv_exports_action,
                "update_schedule": self.update_schedule_action,
                "cleanup_orphaned_tasks": self.cleanup_orphaned_tasks_action,
                "check_scheduler_status": self.check_scheduler_status_action,
                "delete_dead_channels": self.delete_dead_channels_action,
            }

            handler = action_map.get(action)
            if not handler:
                logger.warning(f"{LOG_PREFIX} Unknown action: {action}")
                return {"status": "error", "message": f"Unknown action: {action}"}

            logger.info(f"{LOG_PREFIX} ▶ Action triggered: {action}")

            # Pass context to actions that need it
            if action == "check_streams":
                result = handler(settings, logger, context)
            else:
                result = handler(settings, logger)

            status = result.get("status", "?") if isinstance(result, dict) else "ok"
            msg = result.get("message", "")[:200] if isinstance(result, dict) else ""
            is_bg = result.get("background", False) if isinstance(result, dict) else False
            logger.info(f"{LOG_PREFIX} ◀ Action complete: {action} → {status} | {msg}")

            # Send GUI notification for non-background actions
            if not is_bg:
                emoji = "✅" if status == "ok" else "❌"
                notify_msg = msg.split("\n")[0] if msg else action
                send_websocket_update('updates', 'update', {
                    "type": "plugin", "plugin": "IPTV Checker",
                    "message": f"{emoji} {notify_msg}"
                })

            return result

        except Exception as e:
            self.check_progress['status'] = 'idle'
            self._save_progress()
            LOGGER.error(f"Error in plugin run: {str(e)}")
            send_websocket_update('updates', 'update', {
                "type": "plugin", "plugin": "IPTV Checker",
                "message": f"❌ Error: {str(e)[:100]}"
            })
            return {"status": "error", "message": str(e)}

    def validate_settings_action(self, settings, logger):
        """Validate all plugin settings including database connectivity and groups."""
        validation_results = []
        has_errors = False

        # Test database connectivity directly
        try:
            channel_count = Channel.objects.count()
            group_count = ChannelGroup.objects.count()
            stream_count = Stream.objects.count()
            validation_results.append(
                f"✅ DB OK ({channel_count} channels, {group_count} groups, {stream_count} streams)"
            )

            # Validate groups if specified
            group_names_str = settings.get("group_names", "").strip()
            if group_names_str:
                try:
                    all_groups = self._get_all_groups(logger)
                    all_group_names = {g['name'] for g in all_groups}
                    input_names = [name.strip() for name in group_names_str.split(',') if name.strip()]
                    matched_names = set()
                    unmatched = []

                    for pattern in input_names:
                        if any(c in pattern for c in '*?['):
                            matches = {g for g in all_group_names if fnmatch.fnmatchcase(g, pattern)}
                            if matches:
                                matched_names.update(matches)
                            else:
                                unmatched.append(pattern)
                        elif pattern in all_group_names:
                            matched_names.add(pattern)
                        else:
                            unmatched.append(pattern)

                    if matched_names:
                        validation_results.append(f"✅ Groups ({len(matched_names)}): {', '.join(sorted(matched_names))}")
                    if unmatched:
                        validation_results.append(f"⚠️ No groups matched: {', '.join(unmatched)}")
                        has_errors = True
                except Exception as e:
                    validation_results.append(f"❌ Failed to validate groups: {str(e)}")
                    has_errors = True
            else:
                validation_results.append("ℹ️ No groups specified (will check all)")
        except Exception as e:
            validation_results.append(f"❌ DB error: {str(e)[:100]}")
            has_errors = True

        # Validate other settings - simplified display
        timeout = settings.get("timeout", 10)
        if timeout <= 0:
            validation_results.append(f"⚠️ Timeout must be > 0 (current: {timeout})")
            has_errors = True

        parallel_workers = settings.get("parallel_workers", 2)
        if parallel_workers < 1:
            validation_results.append(f"⚠️ Workers must be >= 1 (current: {parallel_workers})")
            has_errors = True

        analysis_duration = settings.get("ffprobe_analysis_duration", 5)
        if analysis_duration <= 0:
            validation_results.append(f"⚠️ Analysis duration must be > 0 (current: {analysis_duration})")
            has_errors = True

        # Validate scheduler settings if configured
        scheduled_times_str = settings.get("scheduled_times", "").strip()
        if scheduled_times_str:
            scheduled_times = self._parse_scheduled_times(scheduled_times_str)
            if not scheduled_times:
                validation_results.append(f"❌ Invalid cron expression(s): '{scheduled_times_str}'")
                validation_results.append("   Format: 'minute hour day month weekday' (e.g., '0 4 * * *')")
                has_errors = True
            else:
                validation_results.append(f"✅ Cron schedule(s) valid: {', '.join(scheduled_times)}")
                
            # Validate timezone
            scheduler_timezone = settings.get("scheduler_timezone", PluginConfig.DEFAULT_TIMEZONE)
            if PYTZ_AVAILABLE:
                try:
                    pytz.timezone(scheduler_timezone)
                    validation_results.append(f"✅ Timezone valid: {scheduler_timezone}")
                except pytz.exceptions.UnknownTimeZoneError:
                    validation_results.append(f"❌ Unknown timezone: {scheduler_timezone}")
                    has_errors = True
            else:
                validation_results.append("⚠️ pytz not available - scheduler timezone cannot be validated")

        # Validate auto-delete settings
        if settings.get('scheduler_delete_dead_channels', False):
            confirmation = settings.get('auto_delete_confirmation', '').strip()
            if confirmation != 'DELETE':
                validation_results.append("❌ Auto-delete dead channels is enabled but confirmation field does not contain 'DELETE'. Deletion will not run.")
                has_errors = True
            else:
                validation_results.append("⚠️ Auto-delete dead channels is ENABLED. Dead channels will be PERMANENTLY DELETED after scheduled checks.")
            if settings.get('scheduler_rename_dead_channels', False) or settings.get('scheduler_move_dead_channels', False):
                validation_results.append("⚠️ Rename/move dead channels is enabled alongside delete. Rename and move operations are unnecessary if channels will be deleted afterward.")

        # Check for plugin updates
        _, version_message = self._get_latest_version()
        validation_results.append(f"\n{version_message}")

        # Return results
        status = "error" if has_errors else "ok"
        message = "\n".join(validation_results)

        if has_errors:
            message += "\n\n⚠️ Please fix the errors above."
        else:
            message += "\n\n✅ Settings valid. Ready to use!"

        return {"status": status, "message": message}

    def view_progress_action(self, settings, logger):
        """View the current progress of a running operation (load groups or stream check)."""
        # Reload progress from file to get latest state
        self.check_progress = self._load_progress()

        # Check if loading groups is in progress
        if self.load_progress.get('status') == 'loading':
            current, total = self.load_progress['current'], self.load_progress['total']
            percent = (current / total * 100) if total > 0 else 0
            if self.load_progress.get('start_time') and current > 0:
                elapsed = time.time() - self.load_progress['start_time']
                remaining = (elapsed / current) * (total - current)
                eta_str = f"ETA: {ProgressTracker.format_eta(remaining)}"
            else:
                eta_str = "ETA: calculating..."
            return {"status": "ok", "message": f"📥 Loading channels {current}/{total} - {percent:.0f}% complete | {eta_str}"}

        # Check if stream check is in progress
        if self.check_progress['status'] == 'running':
            current, total = self.check_progress['current'], self.check_progress['total']
            percent = (current / total * 100) if total > 0 else 0
            if self.check_progress.get('start_time') and current > 0:
                elapsed = time.time() - self.check_progress['start_time']
                remaining = (elapsed / current) * (total - current)
                eta_str = f"ETA: {ProgressTracker.format_eta(remaining)}"
            else:
                eta_str = "ETA: calculating..."
            return {"status": "ok", "message": f"🔄 Checking streams {current}/{total} - {percent:.0f}% complete | {eta_str}"}

        return {"status": "ok", "message": "No operation is currently running.\n\nUse '📥 Load Group(s)' to load channels or '▶️ Start Stream Check' to begin checking streams."}

    def cancel_check_action(self, settings, logger):
        """Cancel the currently running stream check."""
        # Reload progress from file to get latest state
        self.check_progress = self._load_progress()

        if self.check_progress['status'] != 'running':
            return {"status": "ok", "message": "No stream check is currently running."}

        # Signal the background thread to stop
        self._stop_event.set()

        # Get current progress for the message
        current = self.check_progress['current']
        total = self.check_progress['total']

        # Reset status to idle
        self.check_progress['status'] = 'idle'
        self._save_progress()

        logger.info(f"Stream check cancelled by user. Processed {current}/{total} streams before cancellation.")

        return {"status": "ok", "message": f"✅ Stream check cancelled.\n\nProcessed {current}/{total} streams before cancellation.\n\nPartial results have been saved and can be viewed with '📋 View Last Results'."}

    def view_results_action(self, settings, logger):
        """View summary of the last completed stream check."""
        # Reload progress from file to get latest state
        self.check_progress = self._load_progress()
        
        if self.check_progress['status'] == 'running':
            return {"status": "ok", "message": "A stream check is currently running.\n\nUse '📊 View Check Progress' to see the current status."}

        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "ok", "message": "No results available yet.\n\nUse '▶️ Start Stream Check' to begin checking streams."}

        # Show results summary
        alive = sum(1 for r in results if r.get('status') == 'Alive')
        skipped = sum(1 for r in results if r.get('status') == 'Skipped')
        dead = sum(1 for r in results if r.get('status') == 'Dead')
        formats = {r.get('format', 'Unknown'): 0 for r in results if r.get('status') == 'Alive'}
        for r in results:
            if r.get('status') == 'Alive':
                formats[r.get('format', 'Unknown')] += 1

        summary = [
            f"📊 Last Check Results ({len(results)} streams):",
            f"✅ Alive: {alive}",
            f"❌ Dead: {dead}",
            f"⤼ Skipped: {skipped}\n",
            "📺 Alive Stream Formats:"
        ]
        for fmt, count in sorted(formats.items()):
            if count > 0:
                summary.append(f"  • {fmt}: {count}")

        return {"status": "ok", "message": "\n".join(summary)}

    def _trigger_frontend_refresh(self, settings, logger):
        """Trigger frontend channel list refresh via WebSocket"""
        try:
            send_websocket_update('updates', 'update', {
                "type": "plugin",
                "plugin": self.key,
                "message": "Channels updated"
            })
            logger.info("Frontend refresh triggered via WebSocket")
            return True
        except Exception as e:
            logger.warning(f"Could not trigger frontend refresh: {e}")
        return False

    def _fire_webhook(self, settings, logger):
        """Send check results summary to the configured webhook URL via HTTP POST."""
        webhook_url = settings.get('webhook_url', '').strip()
        if not webhook_url:
            return {"status": "error", "message": "No webhook URL configured. Set the 'Webhook URL' field in plugin settings."}

        if not webhook_url.startswith(('http://', 'https://')):
            return {"status": "error", "message": "Webhook URL must start with http:// or https://"}

        results = self._load_json_file(self.results_file)
        if not results:
            return {"status": "ok", "message": "No results available to send. Run a stream check first."}

        alive = sum(1 for r in results if r.get('status') == 'Alive')
        dead = sum(1 for r in results if r.get('status') == 'Dead')
        skipped = sum(1 for r in results if r.get('status') == 'Skipped')

        payload = json.dumps({
            "plugin": self.key,
            "event": "check_complete",
            "total": len(results),
            "alive": alive,
            "dead": dead,
            "skipped": skipped,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }).encode('utf-8')

        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status_code = resp.status
                logger.info(f"Webhook fired successfully: {webhook_url} (HTTP {status_code})")
                return {"status": "ok", "message": f"Webhook sent to {webhook_url} (HTTP {status_code}). Payload: {alive} alive, {dead} dead, {skipped} skipped out of {len(results)} streams."}
        except urllib.error.HTTPError as e:
            logger.error(f"Webhook HTTP error: {webhook_url} returned HTTP {e.code}")
            return {"status": "error", "message": f"Webhook failed: HTTP {e.code} from {webhook_url}"}
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return {"status": "error", "message": f"Webhook failed: {e}"}

    def _get_all_groups(self, logger):
        """Fetch all channel groups via Django ORM."""
        return list(ChannelGroup.objects.all().values('id', 'name'))

    def _get_all_channels(self, logger, group_ids=None):
        """Fetch channels via Django ORM, optionally filtered by group IDs."""
        qs = Channel.objects.select_related('channel_group').all()
        if group_ids:
            qs = qs.filter(channel_group_id__in=group_ids)
        return list(qs.values('id', 'name', 'channel_number', 'channel_group_id', 'uuid'))

    def _get_channel_streams_bulk(self, channel_ids, logger, check_alternative=True):
        """Fetch streams for multiple channels in a single query.

        Returns dict mapping channel_id -> list of stream dicts.
        """
        qs = ChannelStream.objects.filter(
            channel_id__in=channel_ids
        ).select_related('stream').order_by('channel_id', 'order')

        if not check_alternative:
            qs = qs.filter(order=0)

        streams_by_channel = defaultdict(list)
        for cs in qs:
            streams_by_channel[cs.channel_id].append({
                'id': cs.stream.id,
                'name': cs.stream.name,
                'url': cs.stream.url,
                'channelstream': {'order': cs.order}
            })
        return streams_by_channel

    def _bulk_update_channels(self, updates, fields, logger):
        """Bulk update Channel instances.

        Args:
            updates: list of dicts with 'id' and fields to update
            fields: list of field names to update
        """
        if not updates:
            return 0
        channel_ids = [u['id'] for u in updates]
        channels = {ch.id: ch for ch in Channel.objects.filter(id__in=channel_ids)}
        to_update = []
        for u in updates:
            ch = channels.get(u['id'])
            if ch:
                for field in fields:
                    if field in u:
                        setattr(ch, field, u[field])
                to_update.append(ch)
        if to_update:
            with transaction.atomic():
                Channel.objects.bulk_update(to_update, fields)
            logger.info(f"Bulk updated {len(to_update)} channels (fields: {', '.join(fields)})")
        return len(to_update)

    def _get_or_create_group(self, name, logger):
        """Get or create a channel group by name."""
        group, created = ChannelGroup.objects.get_or_create(name=name)
        if created:
            logger.info(f"Created new group '{name}' (ID: {group.id})")
        return group

    def load_groups_action(self, settings, logger):
        """Load channels and streams from specified Dispatcharr groups."""
        try:
            group_names_str = settings.get("group_names", "").strip()

            # Debug logging for group selection
            logger.info(f"Group Names Setting: '{group_names_str}' (empty={not group_names_str})")

            all_groups = self._get_all_groups(logger)
            group_name_to_id = {g['name']: g['id'] for g in all_groups}

            if not group_names_str:
                # Log warning when loading all groups
                logger.warning("⚠️ No channel groups specified - this will load ALL groups. To filter, specify group names in the 'Channel Groups' field.")
                logger.warning(f"⚠️ Total groups found: {len(group_name_to_id)}")
                logger.warning(f"⚠️ Groups: {', '.join(sorted(group_name_to_id.keys()))}")

                target_group_names, target_group_ids = set(group_name_to_id.keys()), set(group_name_to_id.values())
                if not target_group_ids: return {"status": "error", "message": "No groups found in Dispatcharr."}
            else:
                input_names = [name.strip() for name in group_names_str.split(',') if name.strip()]
                all_group_names = set(group_name_to_id.keys())
                target_group_names = set()
                unmatched_patterns = []

                for pattern in input_names:
                    if any(c in pattern for c in '*?['):
                        # Wildcard pattern — match against all group names
                        matched = {g for g in all_group_names if fnmatch.fnmatchcase(g, pattern)}
                        if matched:
                            logger.info(f"✓ Pattern '{pattern}' matched {len(matched)} group(s): {', '.join(sorted(matched))}")
                            target_group_names.update(matched)
                        else:
                            unmatched_patterns.append(pattern)
                    elif pattern in group_name_to_id:
                        target_group_names.add(pattern)
                    else:
                        unmatched_patterns.append(pattern)

                target_group_ids = {group_name_to_id[name] for name in target_group_names}

                # Log which groups are being loaded
                if target_group_names:
                    logger.info(f"✓ Loading specified groups: {', '.join(sorted(target_group_names))}")
                if unmatched_patterns:
                    logger.warning(f"⚠️ No groups matched: {', '.join(unmatched_patterns)}")

                if not target_group_ids:
                    return {"status": "error", "message": f"No groups matched: {', '.join(unmatched_patterns)}"}

            channels_in_groups = self._get_all_channels(logger, group_ids=target_group_ids)

            # ORM is fast — always load synchronously
            return self._load_groups_sync(channels_in_groups, settings, logger, group_names_str, target_group_names)

        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _load_groups_sync(self, channels_in_groups, settings, logger, group_names_str, target_group_names):
        """Load groups using bulk ORM queries."""
        check_alternative_streams = settings.get("check_alternative_streams", True)

        # Bulk-fetch all streams for all channels in one query
        channel_ids = [ch['id'] for ch in channels_in_groups]
        streams_by_channel = self._get_channel_streams_bulk(channel_ids, logger, check_alternative=check_alternative_streams)

        loaded_channels = []
        for channel in channels_in_groups:
            channel_streams = streams_by_channel.get(channel['id'], [])

            # Log detailed stream information
            if check_alternative_streams and channel_streams:
                logger.info(f"  Channel '{channel.get('name')}' has {len(channel_streams)} stream(s)")
                for stream in channel_streams:
                    order = stream.get('channelstream', {}).get('order', 'unknown')
                    stream_type = "PRIMARY" if order == 0 else f"BACKUP #{order}"
                    logger.info(f"    - {stream_type}: {stream.get('name', 'Unnamed')} (ID: {stream.get('id')})")
            elif channel_streams:
                logger.info(f"  Channel '{channel.get('name')}' has {len(channel_streams)} stream(s) (primary only)")

            loaded_channels.append({**channel, "streams": channel_streams})

        self._save_json_file(self.loaded_channels_file, loaded_channels)

        return self._build_load_success_message(loaded_channels, settings, group_names_str, target_group_names)
    
    def _estimate_check_seconds(self, total_streams, settings):
        """Wall-clock estimate for a full check, including cooldown, retries, and an assumed dead rate."""
        workers = max(1, int(settings.get("parallel_workers", 2) or 1)) if settings.get("enable_parallel_checking", False) else 1
        analysis = float(settings.get("ffprobe_analysis_duration", 5) or 0)
        probe_timeout = float(settings.get("probe_timeout", 20) or 0)
        retries = max(0, int(settings.get("dead_connection_retries", 3) or 0))
        delay = max(0, float(settings.get("stream_check_delay", 2) or 0))
        overhead = PluginConfig.ESTIMATED_PROBE_OVERHEAD_SECONDS
        dead_rate = PluginConfig.ESTIMATED_DEAD_RATE

        per_alive = analysis + overhead
        per_dead = probe_timeout * (1 + retries)
        avg_per_stream = ((1 - dead_rate) * per_alive) + (dead_rate * per_dead) + delay
        return (avg_per_stream * total_streams) / workers

    def _build_load_success_message(self, loaded_channels, settings, group_names_str, target_group_names):
        """Build success message for load groups action"""
        total_streams = sum(len(c.get('streams', [])) for c in loaded_channels)
        group_msg = "all groups" if not group_names_str else f"group(s): {', '.join(target_group_names)}"

        parallel_enabled = settings.get("enable_parallel_checking", False)
        parallel_workers = settings.get("parallel_workers", 2)
        check_alternative_streams = settings.get("check_alternative_streams", True)

        mode_info = f"parallel mode with {parallel_workers} workers" if parallel_enabled else "sequential mode"
        estimated_seconds = self._estimate_check_seconds(total_streams, settings)
        estimated_minutes = max(1, int(estimated_seconds / 60))
        stream_type_msg = "streams (including alternatives)" if check_alternative_streams else "streams (primary only)"
        
        if total_streams > 0:
            message = (
                f"Loaded {len(loaded_channels)} channels / {total_streams} {stream_type_msg} from {group_msg}. "
                f"Estimated check time: ~{estimated_minutes} min ({mode_info}). Next: click Start Stream Check."
            )
        else:
            message = f"Loaded {len(loaded_channels)} channels / 0 streams from {group_msg}."

        return {"status": "ok", "message": message}

    def check_streams_action(self, settings, logger, context=None):
        """Check status and format of all loaded streams with auto status updates."""
        loaded_channels = self._load_json_file(self.loaded_channels_file)
        if loaded_channels is None:
            return {"status": "error", "message": "No channels loaded (or data corrupted). Please run '📥 Load Group(s)' first."}

        all_streams = [
            {"channel_id": ch['id'], "channel_name": ch['name'], "stream_url": s['url'], "stream_id": s['id']}
            for ch in loaded_channels for s in ch.get('streams', []) if s.get('url')
        ]

        if not all_streams:
            return {"status": "error", "message": "The loaded groups contain no streams to check."}

        # Set status to running before starting thread
        self.check_progress = {"current": 0, "total": len(all_streams), "status": "running", "start_time": time.time()}
        self._save_progress()

        # Try to start background thread atomically
        if not self._try_start_thread(self._process_streams_background, (all_streams, settings, logger)):
            return {"status": "ok", "message": "A stream check is already running. Use View Check Progress to monitor."}

        logger.info(f"Starting check for {len(all_streams)} streams...")

        # Calculate estimated time for the response message
        parallel_enabled = settings.get("enable_parallel_checking", False)
        parallel_workers = settings.get("parallel_workers", 2)
        mode_info = f"parallel mode with {parallel_workers} workers" if parallel_enabled else "sequential mode"
        estimated_total_time = max(1, int(self._estimate_check_seconds(len(all_streams), settings) / 60))

        return {"status": "ok", "message": f"Stream check started for {len(all_streams)} streams. Estimated time: ~{estimated_total_time} min ({mode_info}). Use View Check Progress to monitor.", "background": True}

    def _process_streams_background(self, all_streams, settings, logger):
        """Background processing of streams to avoid request timeout"""
        enable_parallel = settings.get("enable_parallel_checking", False)

        if enable_parallel:
            self._process_streams_parallel(all_streams, settings, logger)
        else:
            self._process_streams_sequential(all_streams, settings, logger)

    def _process_streams_sequential(self, all_streams, settings, logger):
        """Sequential stream processing (original implementation)"""
        results = []
        timeout = settings.get("timeout", 10)
        retries = settings.get("dead_connection_retries", 3)
        delay = max(0, float(settings.get("stream_check_delay", 2) or 0))
        self.timeout_retry_queue = []
        streams_processed_since_retry = 0
        tracker = ProgressTracker(len(all_streams), "Stream Check", logger)

        # Load channel data for metadata updates
        channel_map = {}
        loaded_channels = self._load_json_file(self.loaded_channels_file)
        if loaded_channels:
            for channel in loaded_channels:
                channel_map[channel.get('id')] = channel

        try:
            for i, stream_data in enumerate(all_streams):
                if self._stop_event.is_set():  # Allow early termination
                    break

                self.check_progress["current"] = i + 1
                self._save_progress()

                # Check stream - NO immediate retries, we'll handle them in the background queue
                result = self.check_stream(stream_data, timeout, 0, logger, skip_retries=True, settings=settings, retry_attempt=0)

                # Update Dispatcharr metadata if available
                if result.get('dispatcharr_metadata'):
                    channel_data = channel_map.get(stream_data.get('channel_id'))
                    if channel_data:
                        update_success = self._update_dispatcharr_metadata(
                            channel_data,
                            stream_data.get('stream_id'),
                            result['dispatcharr_metadata'],
                            logger
                        )
                        result['metadata_updated'] = update_success
                    else:
                        logger.debug(f"Channel data not found for metadata update: channel_id={stream_data.get('channel_id')}")
                        result['metadata_updated'] = False

                # If stream has a retryable error and retries are enabled, add to retry queue
                retryable_errors = ['Timeout', 'Connection Refused', 'Network Unreachable', 'Stream Unreachable', 'Server Error']
                if result.get('error_type') in retryable_errors and retries > 0:
                    self.timeout_retry_queue.append({**stream_data, "retry_count": 0})
                    logger.info(f"Added '{stream_data.get('channel_name')}' to retry queue due to {result.get('error_type')}")

                results.append({**stream_data, **result})
                streams_processed_since_retry += 1
                tracker.update()

                # Process timeout retry queue every 4 streams
                if streams_processed_since_retry >= 4 and self.timeout_retry_queue:
                    retry_stream = self.timeout_retry_queue.pop(0)
                    retry_stream["retry_count"] += 1

                    if retry_stream["retry_count"] <= retries:
                        logger.info(f"Retrying timeout stream: '{retry_stream.get('channel_name')}' (attempt {retry_stream['retry_count']}/{retries})")
                        retry_result = self.check_stream(retry_stream, timeout, 0, logger, skip_retries=True, settings=settings, retry_attempt=retry_stream["retry_count"])  # No immediate retries

                        # Update Dispatcharr metadata if retry succeeded
                        if retry_result.get('dispatcharr_metadata'):
                            channel_data = channel_map.get(retry_stream.get('channel_id'))
                            if channel_data:
                                update_success = self._update_dispatcharr_metadata(
                                    channel_data,
                                    retry_stream.get('stream_id'),
                                    retry_result['dispatcharr_metadata'],
                                    logger
                                )
                                retry_result['metadata_updated'] = update_success

                        # Update the original result in the results list
                        for j, existing_result in enumerate(results):
                            if (existing_result.get('channel_id') == retry_stream.get('channel_id') and
                                existing_result.get('stream_id') == retry_stream.get('stream_id')):
                                results[j] = {**retry_stream, **retry_result}
                                break

                        # If still has retryable error, add back to queue for another retry
                        if retry_result.get('error_type') in retryable_errors and retry_stream["retry_count"] < retries:
                            self.timeout_retry_queue.append(retry_stream)
                            logger.debug(f"Stream '{retry_stream.get('channel_name')}' still has {retry_result.get('error_type')} error, will retry again")

                    streams_processed_since_retry = 0

                # Cooldown between stream checks (configurable)
                if delay > 0:
                    time.sleep(delay)

            # Process any remaining timeout retries
            while self.timeout_retry_queue:
                retry_stream = self.timeout_retry_queue.pop(0)
                if retry_stream["retry_count"] < retries:
                    retry_stream["retry_count"] += 1
                    logger.info(f"Final retry for timeout stream: '{retry_stream.get('channel_name')}' (attempt {retry_stream['retry_count']}/{retries})")
                    retry_result = self.check_stream(retry_stream, timeout, 0, logger, skip_retries=True, settings=settings, retry_attempt=retry_stream["retry_count"])

                    # Update Dispatcharr metadata if final retry succeeded
                    if retry_result.get('dispatcharr_metadata'):
                        channel_data = channel_map.get(retry_stream.get('channel_id'))
                        if channel_data:
                            update_success = self._update_dispatcharr_metadata(
                                channel_data,
                                retry_stream.get('stream_id'),
                                retry_result['dispatcharr_metadata'],
                                logger
                            )
                            retry_result['metadata_updated'] = update_success

                    # Update the original result in the results list
                    for j, existing_result in enumerate(results):
                        if (existing_result.get('channel_id') == retry_stream.get('channel_id') and
                            existing_result.get('stream_id') == retry_stream.get('stream_id')):
                            results[j] = {**retry_stream, **retry_result}
                            break

            self._save_json_file(self.results_file, results, indent=2)

        except Exception as e:
            logger.error(f"Background stream processing error: {e}")
        finally:
            self.check_progress['status'] = 'idle'
            self.check_progress['end_time'] = time.time()
            self._save_progress()
            tracker.finish()
            self._trigger_frontend_refresh(settings, logger)

    def _process_streams_parallel(self, all_streams, settings, logger):
        """Parallel stream processing using ThreadPoolExecutor"""
        results = []
        timeout = settings.get("timeout", 10)
        retries = settings.get("dead_connection_retries", 3)
        workers = settings.get("parallel_workers", 2)
        delay = max(0, float(settings.get("stream_check_delay", 2) or 0))
        tracker = ProgressTracker(len(all_streams), "Stream Check (Parallel)", logger)

        def check_with_cooldown(stream_data, retry_attempt=0):
            if self._stop_event.is_set():
                return {'status': 'Dead', 'error': 'Cancelled by user', 'error_type': 'Cancelled',
                        'format': 'N/A', 'framerate_num': 0, 'ffprobe_data': {}}
            try:
                return self.check_stream(stream_data, timeout, 0, logger, skip_retries=True, settings=settings, retry_attempt=retry_attempt)
            finally:
                if delay > 0 and not self._stop_event.is_set():
                    time.sleep(delay)

        # Thread-safe data structures
        results_lock = threading.Lock()
        results_dict = {}  # Use dict to track results by stream index

        # Load channel data for metadata updates
        channel_map = {}
        loaded_channels = self._load_json_file(self.loaded_channels_file)
        if loaded_channels:
            for channel in loaded_channels:
                channel_map[channel.get('id')] = channel

        try:
            logger.info(f"Starting parallel stream checking with {workers} workers")

            # First pass: check all streams in parallel
            with ThreadPoolExecutor(max_workers=workers) as executor:
                # Submit all stream checks
                future_to_index = {
                    executor.submit(check_with_cooldown, stream_data, 0): i
                    for i, stream_data in enumerate(all_streams)
                }

                # Process results as they complete
                for future in as_completed(future_to_index):
                    if self._stop_event.is_set():
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

                    index = future_to_index[future]
                    stream_data = all_streams[index]

                    try:
                        result = future.result()

                        # Update Dispatcharr metadata if available
                        if result.get('dispatcharr_metadata'):
                            channel_data = channel_map.get(stream_data.get('channel_id'))
                            if channel_data:
                                update_success = self._update_dispatcharr_metadata(
                                    channel_data,
                                    stream_data.get('stream_id'),
                                    result['dispatcharr_metadata'],
                                    logger
                                )
                                result['metadata_updated'] = update_success
                            else:
                                result['metadata_updated'] = False

                        with results_lock:
                            results_dict[index] = {**stream_data, **result}
                            self.check_progress["current"] = len(results_dict)
                            self._save_progress()
                            tracker.update()

                    except Exception as e:
                        logger.error(f"Error checking stream '{stream_data.get('channel_name')}': {e}")
                        with results_lock:
                            results_dict[index] = {
                                **stream_data,
                                'status': 'Dead',
                                'error': str(e),
                                'error_type': 'Other',
                                'format': 'N/A',
                                'framerate_num': 0,
                                'ffprobe_data': {}
                            }
                            self.check_progress["current"] = len(results_dict)
                            self._save_progress()
                            tracker.update()

            # Rebuild results list in original order
            results = [results_dict[i] for i in range(len(all_streams)) if i in results_dict]

            # Handle retries for streams with retryable errors if enabled
            if retries > 0:
                retryable_errors = ['Timeout', 'Connection Refused', 'Network Unreachable', 'Stream Unreachable', 'Server Error']
                retry_streams = [(i, r) for i, r in enumerate(results) if r.get('error_type') in retryable_errors]

                if retry_streams:
                    logger.info(f"Found {len(retry_streams)} streams with retryable errors, retrying...")

                    # Expose retry work to the ETA: grow total so progress doesn't hit 100% prematurely.
                    with results_lock:
                        self.check_progress["total"] = len(all_streams) + (len(retry_streams) * retries)
                        self._save_progress()

                    for retry_pass in range(retries):
                        if not retry_streams or self._stop_event.is_set():
                            break

                        # Backoff between retry passes so the provider can release slots
                        backoff = delay * 3
                        if backoff > 0:
                            logger.info(f"Waiting {backoff:.1f}s before retry pass to let provider release connection slots")
                            if self._stop_event.wait(backoff):
                                break

                        logger.info(f"Retry attempt {retry_pass + 1}/{retries} for {len(retry_streams)} streams")

                        with ThreadPoolExecutor(max_workers=workers) as executor:
                            future_to_result_index = {
                                executor.submit(
                                    check_with_cooldown,
                                    {k: v for k, v in result.items() if k in ['channel_id', 'channel_name', 'stream_url', 'stream_id']},
                                    retry_pass + 1
                                ): result_index
                                for result_index, result in retry_streams
                            }

                            for future in as_completed(future_to_result_index):
                                if self._stop_event.is_set():
                                    executor.shutdown(wait=False, cancel_futures=True)
                                    break
                                result_index = future_to_result_index[future]
                                try:
                                    retry_result = future.result()
                                    
                                    # Update Dispatcharr metadata if retry succeeded
                                    if retry_result.get('dispatcharr_metadata'):
                                        stream_data = results[result_index]
                                        channel_data = channel_map.get(stream_data.get('channel_id'))
                                        if channel_data:
                                            update_success = self._update_dispatcharr_metadata(
                                                channel_data,
                                                stream_data.get('stream_id'),
                                                retry_result['dispatcharr_metadata'],
                                                logger
                                            )
                                            retry_result['metadata_updated'] = update_success
                                    
                                    # Update the result
                                    results[result_index] = {**results[result_index], **retry_result}
                                except Exception as e:
                                    logger.error(f"Error during retry: {e}")
                                finally:
                                    with results_lock:
                                        self.check_progress["current"] += 1
                                        self._save_progress()

                        # Find remaining streams with retryable errors for next retry
                        retry_streams = [(i, r) for i, r in enumerate(results) if r.get('error_type') in retryable_errors]

                    # If fewer retries ran than budgeted (early success / cancel), snap progress to total.
                    with results_lock:
                        if self.check_progress["current"] < self.check_progress["total"]:
                            self.check_progress["current"] = self.check_progress["total"]
                            self._save_progress()

            self._save_json_file(self.results_file, results, indent=2)

        except Exception as e:
            logger.error(f"Background parallel stream processing error: {e}")
        finally:
            self.check_progress['status'] = 'idle'
            self.check_progress['end_time'] = time.time()
            self._save_progress()
            tracker.finish()
            self._trigger_frontend_refresh(settings, logger)

    def rename_channels_action(self, settings, logger):
        """Rename channels that were marked as dead in the last check."""
        rename_format = settings.get("dead_rename_format", "{name} [DEAD]").strip()
        if not rename_format:
            return {"status": "error", "message": "Please configure a Dead Channel Rename Format before renaming."}

        if "{name}" not in rename_format:
            return {"status": "error", "message": "Dead Channel Rename Format must contain {name} placeholder."}

        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first."}

        dead_channels = {r['channel_id']: r['channel_name'] for r in results if r['status'] == 'Dead'}
        if not dead_channels: return {"status": "ok", "message": "No dead channels found in the last check."}

        payload = []
        for cid, name in dead_channels.items():
            new_name = rename_format.replace('{name}', name)

            if new_name != name:
                payload.append({'id': cid, 'name': new_name})

        if not payload: return {"status": "ok", "message": "No channels needed renaming."}

        try:
            count = self._bulk_update_channels(payload, ['name'], logger)
            self._trigger_frontend_refresh(settings, logger)
            return {"status": "ok", "message": f"Successfully renamed {count} dead channels. GUI refresh triggered."}
        except Exception as e: return {"status": "error", "message": str(e)}

    def move_dead_channels_action(self, settings, logger):
        """Move channels marked as dead to a new group."""
        move_to_group_name = settings.get("move_to_group_name", "Graveyard").strip()
        if not move_to_group_name:
            return {"status": "error", "message": "Please enter a destination group name in the settings."}

        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first."}
        
        dead_channel_ids = {r['channel_id'] for r in results if r['status'] == 'Dead'}
        if not dead_channel_ids: return {"status": "ok", "message": "No dead channels were found in the last check."}
        
        try:
            dest_group = self._get_or_create_group(move_to_group_name, logger)
            new_group_id = dest_group.id

            payload = [{'id': cid, 'channel_group_id': new_group_id} for cid in dead_channel_ids]
            moved_count = self._bulk_update_channels(payload, ['channel_group_id'], logger)
            self._trigger_frontend_refresh(settings, logger)
            return {"status": "ok", "message": f"Successfully moved {moved_count} dead channels to group '{move_to_group_name}'. GUI refresh triggered."}

        except Exception as e: return {"status": "error", "message": str(e)}
        
    def delete_dead_channels_action(self, settings, logger):
        """Permanently delete channels marked as dead from the database."""
        # Safety gate: require confirmation string
        confirmation = settings.get('auto_delete_confirmation', '').strip()
        if confirmation != 'DELETE':
            return {
                "status": "error",
                "message": "Auto-delete safety gate: You must type DELETE (all caps) in the "
                           "'Auto-Delete Confirmation' settings field to enable this feature."
            }

        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first."}

        dead_channel_ids = {r['channel_id'] for r in results if r['status'] == 'Dead'}
        if not dead_channel_ids:
            return {"status": "ok", "message": "No dead channels were found in the last check."}

        # Safety net: only delete channels that are in the currently loaded scope
        # (i.e. matched the user's group filter at load time). Defends against
        # stale results.json or a scheduler running with mismatched settings.
        loaded_channels = self._load_json_file(self.loaded_channels_file)
        if loaded_channels:
            loaded_ids = {ch.get('id') for ch in loaded_channels if ch.get('id') is not None}
            out_of_scope = dead_channel_ids - loaded_ids
            if out_of_scope:
                logger.warning(
                    f"Refusing to delete {len(out_of_scope)} channel(s) that are outside the "
                    f"current load scope: {sorted(out_of_scope)}"
                )
                dead_channel_ids = dead_channel_ids & loaded_ids
            if not dead_channel_ids:
                return {"status": "ok", "message": "No dead channels were found within the loaded scope."}

        logger.warning(f"WARNING: About to PERMANENTLY DELETE {len(dead_channel_ids)} dead channels. This cannot be undone!")
        logger.warning(f"Channel IDs to be deleted: {sorted(dead_channel_ids)}")

        try:
            with transaction.atomic():
                deleted_count, _ = Channel.objects.filter(id__in=dead_channel_ids).delete()

            logger.warning(f"DELETED {deleted_count} dead channels permanently from the database.")
            if deleted_count != len(dead_channel_ids):
                logger.warning(f"Expected to delete {len(dead_channel_ids)} channels but only {deleted_count} were found in the database.")
            self._trigger_frontend_refresh(settings, logger)
            return {
                "status": "ok",
                "message": f"Permanently deleted {deleted_count} dead channels from the database. "
                           f"This action cannot be undone. GUI refresh triggered."
            }
        except Exception as e:
            return {"status": "error", "message": f"Error deleting channels: {str(e)}"}

    def rename_low_framerate_channels_action(self, settings, logger):
        """Rename channels with low framerate streams."""
        rename_format = settings.get("low_framerate_rename_format", "{name} [Slow]").strip()

        if not rename_format:
            return {"status": "error", "message": "Please configure a Low Framerate Rename Format."}

        if "{name}" not in rename_format:
            return {"status": "error", "message": "Low Framerate Rename Format must contain {name} placeholder."}

        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first."}

        low_fps_channels = {r['channel_id']: r['channel_name'] for r in results if 0 < r.get('framerate_num', 0) < 30}
        if not low_fps_channels: return {"status": "ok", "message": "No low framerate channels found."}

        payload = []
        for cid, name in low_fps_channels.items():
            new_name = rename_format.replace('{name}', name)

            if new_name != name:
                payload.append({'id': cid, 'name': new_name})

        if not payload: return {"status": "ok", "message": "No channels needed renaming."}

        try:
            count = self._bulk_update_channels(payload, ['name'], logger)
            self._trigger_frontend_refresh(settings, logger)
            return {"status": "ok", "message": f"Successfully renamed {count} low framerate channels. GUI refresh triggered."}
        except Exception as e: return {"status": "error", "message": str(e)}

    def move_low_framerate_channels_action(self, settings, logger):
        """Move channels with low framerate streams to a new group."""
        group_name = settings.get("move_low_framerate_group", "Slow").strip()
        if not group_name:
            return {"status": "error", "message": "Please enter a destination group name."}

        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first."}
        
        low_fps_channel_ids = {r['channel_id'] for r in results if 0 < r.get('framerate_num', 0) < 30}
        if not low_fps_channel_ids: return {"status": "ok", "message": "No low framerate channels found to move."}
        
        try:
            dest_group = self._get_or_create_group(group_name, logger)
            new_group_id = dest_group.id

            payload = [{'id': cid, 'channel_group_id': new_group_id} for cid in low_fps_channel_ids]
            moved_count = self._bulk_update_channels(payload, ['channel_group_id'], logger)
            self._trigger_frontend_refresh(settings, logger)
            return {"status": "ok", "message": f"Successfully moved {moved_count} low framerate channels to group '{group_name}'. GUI refresh triggered."}
        except Exception as e: return {"status": "error", "message": str(e)}

    def add_video_format_suffix_action(self, settings, logger):
        """Adds a format suffix like [HD] to channel names."""
        suffixes_to_add_str = settings.get("video_format_suffixes", "UHD, FHD, HD, SD, Unknown").strip().lower()
        if not suffixes_to_add_str:
            return {"status": "error", "message": "Please specify which video formats should have a suffix added."}

        suffixes_to_add = {s.strip() for s in suffixes_to_add_str.split(',')}
        logger.info(f"DEBUG: Configured suffixes to add: {suffixes_to_add}")

        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first."}
        logger.info(f"DEBUG: Loaded {len(results)} results from last check")

        channel_formats = {}
        for r in results:
            if r['status'] == 'Alive':
                channel_formats[r['channel_id']] = r.get('format', 'Unknown')

        logger.info(f"DEBUG: Found {len(channel_formats)} alive channels in results")
        if channel_formats:
            # Log format distribution
            format_counts = {}
            for fmt in channel_formats.values():
                format_counts[fmt] = format_counts.get(fmt, 0) + 1
            logger.info(f"DEBUG: Format distribution: {format_counts}")

        if not channel_formats: return {"status": "ok", "message": "No alive channels found to update."}

        try:
            all_channels = self._get_all_channels(logger)
            channel_id_to_name = {c['id']: c['name'] for c in all_channels}
            logger.info(f"DEBUG: Retrieved {len(all_channels)} channels from DB")

            payload = []
            skipped_not_in_suffixes = 0
            skipped_already_has_suffix = 0
            skipped_channel_not_found = 0

            for cid, fmt in channel_formats.items():
                logger.debug(f"DEBUG: Processing channel_id={cid}, format='{fmt}'")

                # Check if format is in the list of formats to add suffixes for
                if fmt.lower() not in suffixes_to_add:
                    logger.debug(f"DEBUG:   - Skipped: format '{fmt}' not in configured suffixes")
                    skipped_not_in_suffixes += 1
                    continue

                current_name = channel_id_to_name.get(cid)
                if not current_name:
                    logger.debug(f"DEBUG:   - Skipped: channel_id {cid} not found in DB channels")
                    skipped_channel_not_found += 1
                    continue

                suffix = f" [{fmt.upper()}]"
                logger.debug(f"DEBUG:   - Current name: '{current_name}'")
                logger.debug(f"DEBUG:   - Will add suffix: '{suffix}'")
                logger.debug(f"DEBUG:   - Already ends with suffix? {current_name.endswith(suffix)}")

                if current_name.endswith(suffix):
                    logger.debug(f"DEBUG:   - Skipped: already has suffix '{suffix}'")
                    skipped_already_has_suffix += 1
                else:
                    new_name = current_name + suffix
                    logger.info(f"DEBUG:   ✓ Adding to payload: '{current_name}' -> '{new_name}'")
                    payload.append({'id': cid, 'name': new_name})

            logger.info(f"DEBUG: Payload summary:")
            logger.info(f"DEBUG:   - Channels to update: {len(payload)}")
            logger.info(f"DEBUG:   - Skipped (format not in configured list): {skipped_not_in_suffixes}")
            logger.info(f"DEBUG:   - Skipped (already has suffix): {skipped_already_has_suffix}")
            logger.info(f"DEBUG:   - Skipped (channel not found in DB): {skipped_channel_not_found}")

            if not payload:
                reason_parts = []
                if skipped_already_has_suffix > 0:
                    reason_parts.append(f"{skipped_already_has_suffix} already have suffix")
                if skipped_not_in_suffixes > 0:
                    reason_parts.append(f"{skipped_not_in_suffixes} format not in configured list")
                if skipped_channel_not_found > 0:
                    reason_parts.append(f"{skipped_channel_not_found} not found in DB")

                reason = " • ".join(reason_parts) if reason_parts else "All channels already up to date"
                return {"status": "ok", "message": f"No channels needed a format suffix added.\n\nReason: {reason}"}

            updated_count = self._bulk_update_channels(payload, ['name'], logger)
            self._trigger_frontend_refresh(settings, logger)
            return {"status": "ok", "message": f"Successfully added format suffixes to {updated_count} channels. GUI refresh triggered."}

        except Exception as e: return {"status": "error", "message": str(e)}

    def view_table_action(self, settings, logger):
        """Display results in table format"""
        results = self._load_json_file(self.results_file)
        if results is None: return {"status": "error", "message": "No results available."}
        lines = ["="*120, f"{'Channel Name':<35} {'Status':<8} {'Format':<8} {'FPS':<8} {'Error Type':<20} {'Error Details':<35}", "="*120]
        for r in results:
            fps = r.get('framerate_num', 0)
            fps_str = f"{fps:.1f}" if fps > 0 else "N/A"
            error_type = r.get('error_type', 'N/A')
            error_details = r.get('error', '')[:34] if r.get('error') else ''
            lines.append(f"{r.get('channel_name', 'N/A')[:34]:<35} {r.get('status', 'N/A'):<8} {r.get('format', 'N/A'):<8} {fps_str:<8} {error_type:<20} {error_details:<35}")
        lines.append("="*120)
        return {"status": "ok", "message": "\n".join(lines)}

    def _generate_csv_header_comments(self, settings, results):
        """Generate CSV header comments with settings and statistics"""
        lines = []
        lines.append("# IPTV Checker Plugin - Export Results")
        lines.append(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"# Plugin Version: {self.version}")
        lines.append("#")

        # Add timing information
        if self.check_progress.get('start_time') and self.check_progress.get('end_time'):
            start_time = self.check_progress['start_time']
            end_time = self.check_progress['end_time']
            start_str = datetime.fromtimestamp(start_time).strftime('%Y-%m-%d %H:%M:%S')
            end_str = datetime.fromtimestamp(end_time).strftime('%Y-%m-%d %H:%M:%S')
            duration_seconds = end_time - start_time
            hours = int(duration_seconds // 3600)
            minutes = int((duration_seconds % 3600) // 60)
            seconds = int(duration_seconds % 60)

            lines.append("# Check Timing:")
            lines.append(f"#   Start Time: {start_str}")
            lines.append(f"#   End Time: {end_str}")
            if hours > 0:
                lines.append(f"#   Duration: {hours}h {minutes}m {seconds}s")
            elif minutes > 0:
                lines.append(f"#   Duration: {minutes}m {seconds}s")
            else:
                lines.append(f"#   Duration: {seconds}s")
            lines.append("#")

        # Add plugin settings (excluding sensitive information)
        lines.append("# Plugin Settings:")
        lines.append(f"#   Group(s) Checked: {settings.get('group_names', 'All groups')}")
        lines.append(f"#   Connection Timeout: {settings.get('timeout', 10)} seconds")
        lines.append(f"#   Probe Timeout: {settings.get('probe_timeout', 20)} seconds")
        lines.append(f"#   Dead Connection Retries: {settings.get('dead_connection_retries', 3)}")
        lines.append(f"#   Dead Rename Format: {settings.get('dead_rename_format', '{name} [DEAD]')}")
        lines.append(f"#   Move Dead to Group: {settings.get('move_to_group_name', 'Graveyard')}")
        lines.append(f"#   Low Framerate Rename Format: {settings.get('low_framerate_rename_format', '{name} [Slow]')}")
        lines.append(f"#   Move Low Framerate to Group: {settings.get('move_low_framerate_group', 'Slow')}")
        lines.append(f"#   Video Format Suffixes: {settings.get('video_format_suffixes', 'UHD, FHD, HD, SD, Unknown')}")
        lines.append(f"#   Parallel Checking Enabled: {settings.get('enable_parallel_checking', False)}")
        lines.append(f"#   Parallel Workers: {settings.get('parallel_workers', 2)}")
        lines.append(f"#   FFprobe Flags: {settings.get('ffprobe_flags', '-show_streams')}")
        lines.append(f"#   FFprobe Analysis Duration: {settings.get('ffprobe_analysis_duration', 5)} seconds")
        lines.append("#")

        # Calculate cumulative statistics
        total_streams = len(results)
        alive_streams = sum(1 for r in results if r.get('status') == 'Alive')
        skipped_streams = sum(1 for r in results if r.get('status') == 'Skipped')
        dead_streams = sum(1 for r in results if r.get('status') == 'Dead')

        # Format distribution
        format_counts = {}
        for r in results:
            if r.get('status') == 'Alive':
                fmt = r.get('format', 'Unknown')
                format_counts[fmt] = format_counts.get(fmt, 0) + 1

        # Average framerate for alive streams
        alive_framerates = [r.get('framerate_num', 0) for r in results if r.get('status') == 'Alive' and r.get('framerate_num', 0) > 0]
        avg_framerate = sum(alive_framerates) / len(alive_framerates) if alive_framerates else 0

        # Error type distribution
        error_counts = {}
        for r in results:
            if r.get('status') == 'Dead':
                error_type = r.get('error_type', 'Other')
                error_counts[error_type] = error_counts.get(error_type, 0) + 1

        # Add cumulative statistics
        lines.append("# Cumulative Statistics:")
        lines.append(f"#   Total Streams: {total_streams}")
        lines.append(f"#   Alive Streams: {alive_streams} ({(alive_streams/total_streams*100):.1f}%)")
        lines.append(f"#   Dead Streams: {dead_streams} ({(dead_streams/total_streams*100):.1f}%)")
        if skipped_streams:
            lines.append(f"#   Skipped Streams: {skipped_streams} ({(skipped_streams/total_streams*100):.1f}%)")

        if format_counts:
            lines.append("#")
            lines.append("#   Alive Stream Formats:")
            for fmt in sorted(format_counts.keys()):
                count = format_counts[fmt]
                lines.append(f"#     {fmt}: {count} ({(count/alive_streams*100):.1f}%)")

        if avg_framerate > 0:
            lines.append("#")
            lines.append(f"#   Average Framerate (Alive): {avg_framerate:.1f} fps")

        # Low framerate streams
        low_fps_count = sum(1 for r in results if r.get('status') == 'Alive' and 0 < r.get('framerate_num', 0) < 30)
        if low_fps_count > 0:
            lines.append(f"#   Low Framerate Streams (<30fps): {low_fps_count}")

        if error_counts:
            lines.append("#")
            lines.append("#   Error Type Distribution:")
            for error_type in sorted(error_counts.keys()):
                count = error_counts[error_type]
                lines.append(f"#     {error_type}: {count} ({(count/dead_streams*100):.1f}%)")

        lines.append("#")
        lines.append("# " + "="*80)
        lines.append("#")

        return lines

    def export_results_action(self, settings, logger):
        """Export results to CSV"""
        results = self._load_json_file(self.results_file)
        if results is None: return {"status": "error", "message": "No results to export."}

        # Flatten ffprobe_data and round framerate for cleaner CSV
        for result in results:
            if 'framerate_num' in result and result['framerate_num'] > 0:
                result['framerate_num'] = round(result['framerate_num'])

            # Flatten ffprobe_data into top-level fields
            if 'ffprobe_data' in result and isinstance(result['ffprobe_data'], dict):
                ffprobe_data = result.pop('ffprobe_data')
                for key, value in ffprobe_data.items():
                    result[f'ffprobe_{key}'] = value

        # Determine all possible fieldnames including dynamic ffprobe fields
        base_fieldnames = ['channel_id', 'channel_name', 'stream_id', 'status', 'format', 'framerate_num', 'error_type', 'error', 'retry_count', 'connection_timeout_seconds', 'probe_timeout_seconds', 'ffprobe_monitoring_seconds']
        ffprobe_fieldnames = set()
        for result in results:
            for key in result.keys():
                if key.startswith('ffprobe_'):
                    ffprobe_fieldnames.add(key)

        # Create complete fieldnames list
        fieldnames = base_fieldnames + sorted(list(ffprobe_fieldnames))

        filepath = f"/data/exports/iptv_checker_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        os.makedirs(PluginConfig.EXPORTS_DIR, exist_ok=True)
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            # Write header comments
            header_comments = self._generate_csv_header_comments(settings, results)
            for comment_line in header_comments:
                f.write(comment_line + '\n')

            # Write CSV data
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(results)
        return {"status": "ok", "message": f"Results exported to {filepath}"}

    def clear_csv_exports_action(self, settings, logger):
        """Delete all CSV export files created by this plugin."""
        exports_dir = PluginConfig.EXPORTS_DIR

        if not os.path.exists(exports_dir):
            return {"status": "ok", "message": "No exports directory found. No CSV files to delete."}

        # Find all CSV files that match our naming pattern
        csv_files = [f for f in os.listdir(exports_dir) if f.startswith('iptv_checker_results_') and f.endswith('.csv')]

        if not csv_files:
            return {"status": "ok", "message": "No CSV export files found in /data/exports/."}

        # Delete all matching CSV files
        deleted_count = 0
        for csv_file in csv_files:
            try:
                filepath = os.path.join(exports_dir, csv_file)
                os.remove(filepath)
                deleted_count += 1
                logger.info(f"Deleted CSV export: {csv_file}")
            except Exception as e:
                logger.error(f"Failed to delete {csv_file}: {e}")

        if deleted_count == 0:
            return {"status": "error", "message": "Failed to delete any CSV files."}
        elif deleted_count < len(csv_files):
            return {"status": "ok", "message": f"⚠️ Partially cleared: Deleted {deleted_count} of {len(csv_files)} CSV files.\n\nSome files could not be deleted. Check logs for details."}
        else:
            return {"status": "ok", "message": f"✅ Successfully deleted {deleted_count} CSV export file(s) from /data/exports/."}

    def update_schedule_action(self, settings, logger):
        """Update the scheduler configuration and restart the scheduler."""
        try:
            scheduled_times_str = settings.get("scheduled_times", "").strip()
            scheduler_timezone = settings.get("scheduler_timezone", PluginConfig.DEFAULT_TIMEZONE)
            
            # If scheduled times are empty, stop the scheduler
            if not scheduled_times_str:
                logger.info("Scheduled times empty - stopping scheduler")
                self._stop_background_scheduler()
                return {
                    "status": "ok",
                    "message": "✅ Schedule cleared. Scheduler has been stopped.\n\nTo enable scheduling, configure scheduled times in cron format."
                }
            
            # Validate scheduled times format (cron expressions)
            scheduled_times = self._parse_scheduled_times(scheduled_times_str)
            if not scheduled_times:
                return {
                    "status": "error",
                    "message": f"❌ Invalid cron expression format: '{scheduled_times_str}'\n\nPlease use cron format (e.g., '0 4 * * *' for daily at 4 AM).\nFormat: minute hour day month weekday"
                }
            
            # Validate timezone
            if PYTZ_AVAILABLE:
                try:
                    pytz.timezone(scheduler_timezone)
                except pytz.exceptions.UnknownTimeZoneError:
                    return {
                        "status": "error",
                        "message": f"❌ Unknown timezone: {scheduler_timezone}\n\nPlease select a valid timezone from the dropdown."
                    }
            else:
                return {
                    "status": "error",
                    "message": "❌ Scheduler requires pytz library but it is not installed.\n\nPlease install pytz to use scheduling features."
                }
            
            # Restart scheduler with new settings
            logger.info(f"Updating schedule: Times={scheduled_times_str}, Timezone={scheduler_timezone}")
            self._start_background_scheduler(settings)
            
            # Build status message
            times_display = ', '.join(scheduled_times)  # Already strings (cron expressions)
            
            message = f"✅ Schedule updated successfully!\n\n"
            message += f"Cron Schedules: {times_display}\n"
            message += f"Timezone: {scheduler_timezone}\n"
            message += f"Status: Enabled ✓\n\n"
            message += f"The scheduler will run checks at the configured times."
            
            return {"status": "ok", "message": message}
            
        except Exception as e:
            logger.error(f"Error updating schedule: {e}", exc_info=True)
            return {"status": "error", "message": f"Failed to update schedule: {str(e)}"}

    def cleanup_orphaned_tasks_action(self, settings, logger):
        """Remove any orphaned Celery periodic tasks from old plugin versions."""
        try:
            # Try to import Celery's PeriodicTask model
            try:
                from django_celery_beat.models import PeriodicTask
                from django.db.models import Q
            except ImportError:
                return {
                    "status": "error",
                    "message": "❌ Celery Beat is not available.\n\nThis feature requires django-celery-beat to be installed in Dispatcharr."
                }
            
            # Find tasks related to this plugin
            task_patterns = [
                'iptv_checker',
                'IPTV Checker',
            ]
            
            # Build query to find related tasks
            query = Q()
            for pattern in task_patterns:
                query |= Q(name__icontains=pattern) | Q(task__icontains=pattern)
            
            # Find all matching tasks
            orphaned_tasks = PeriodicTask.objects.filter(query)
            task_count = orphaned_tasks.count()
            
            if task_count == 0:
                return {
                    "status": "ok",
                    "message": "✅ No orphaned tasks found.\n\nThe database is clean."
                }
            
            # Get task names for reporting
            task_names = list(orphaned_tasks.values_list('name', flat=True))
            
            # Delete the tasks
            deleted_count, _ = orphaned_tasks.delete()
            
            logger.info(f"Cleaned up {deleted_count} orphaned periodic tasks: {task_names}")
            
            return {
                "status": "ok",
                "message": f"✅ Cleaned up {deleted_count} orphaned task(s):\n\n" + "\n".join(f"  • {name}" for name in task_names)
            }
            
        except Exception as e:
            logger.error(f"Error cleaning up orphaned tasks: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"❌ Failed to cleanup orphaned tasks: {str(e)}"
            }
    
    def check_scheduler_status_action(self, settings, logger):
        """Display scheduler thread status and diagnostic information."""
        global _bg_scheduler_thread
        
        try:
            status_lines = []
            status_lines.append("🔍 Scheduler Status Report")
            status_lines.append("=" * 60)
            status_lines.append("")
            
            # Check scheduler thread status
            status_lines.append("📊 Thread Status:")
            if _bg_scheduler_thread is None:
                status_lines.append("  • Thread: Not created")
                thread_status = "❌ Not Running"
            elif _bg_scheduler_thread.is_alive():
                status_lines.append(f"  • Thread: Alive (ID: {_bg_scheduler_thread.ident})")
                status_lines.append(f"  • Thread Name: {_bg_scheduler_thread.name}")
                status_lines.append(f"  • Daemon: {_bg_scheduler_thread.daemon}")
                thread_status = "✅ Running"
            else:
                status_lines.append("  • Thread: Created but not alive")
                thread_status = "⚠️ Stopped"
            
            status_lines.append(f"  • Status: {thread_status}")
            status_lines.append("")
            
            # Check configuration
            status_lines.append("⚙️ Configuration:")
            scheduled_times_str = settings.get("scheduled_times", "").strip()
            if scheduled_times_str:
                scheduled_times = self._parse_scheduled_times(scheduled_times_str)
                status_lines.append(f"  • Cron Expressions: {', '.join(scheduled_times)}")
                status_lines.append(f"  • Valid: {'Yes ✓' if scheduled_times else 'No ✗'}")
            else:
                status_lines.append("  • Cron Expressions: Not configured")
            
            scheduler_timezone = settings.get("scheduler_timezone", PluginConfig.DEFAULT_TIMEZONE)
            status_lines.append(f"  • Timezone: {scheduler_timezone}")
            
            if PYTZ_AVAILABLE:
                try:
                    tz = pytz.timezone(scheduler_timezone)
                    now = datetime.now(tz)
                    status_lines.append(f"  • Current Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                except:
                    status_lines.append(f"  • Current Time: Unable to determine (invalid timezone)")
            else:
                status_lines.append(f"  • Current Time: Unable to determine (pytz not available)")
            
            export_csv = settings.get("scheduler_export_csv", False)
            status_lines.append(f"  • Auto-export CSV: {'Enabled ✓' if export_csv else 'Disabled'}")
            status_lines.append("")
            
            # Check dependencies
            status_lines.append("📦 Dependencies:")
            status_lines.append(f"  • pytz: {'Available ✓' if PYTZ_AVAILABLE else 'Not Available ✗'}")
            status_lines.append("")
            
            # Check if there's a pending run
            global _scheduler_pending_run
            status_lines.append("⏳ Pending Operations:")
            status_lines.append(f"  • Queued Run: {'Yes' if _scheduler_pending_run else 'No'}")
            status_lines.append("")
            
            # Current check status
            status_lines.append("🔄 Current Check Status:")
            check_status = self.check_progress.get('status', 'idle')
            status_lines.append(f"  • Status: {check_status.title()}")
            if check_status == 'running':
                current = self.check_progress.get('current', 0)
                total = self.check_progress.get('total', 0)
                percent = (current / total * 100) if total > 0 else 0
                status_lines.append(f"  • Progress: {current}/{total} ({percent:.1f}%)")
            status_lines.append("")
            
            # Recommendations
            status_lines.append("💡 Recommendations:")
            if not scheduled_times_str:
                status_lines.append("  ⚠️ Configure cron expressions to enable scheduling")
            elif not PYTZ_AVAILABLE:
                status_lines.append("  ⚠️ Install pytz for timezone support")
            elif not _bg_scheduler_thread or not _bg_scheduler_thread.is_alive():
                status_lines.append("  ⚠️ Scheduler thread is not running - try clicking '📅 Update Schedule'")
            else:
                status_lines.append("  ✅ Scheduler is configured and running properly")
            
            return {
                "status": "ok",
                "message": "\n".join(status_lines)
            }
            
        except Exception as e:
            logger.error(f"Error checking scheduler status: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"❌ Failed to check scheduler status: {str(e)}"
            }

    def _get_stream_format(self, resolution_str):
        """Determine video format from a resolution string."""
        if 'x' not in resolution_str: return "Unknown"
        try:
            width = int(resolution_str.split('x')[0])
            if width >= 3800: return "UHD"
            if width >= 1900: return "FHD"
            if width >= 1200: return "HD"
            if width > 0: return "SD"
            return "Unknown"
        except: return "Unknown"
        
    def parse_framerate(self, framerate_str):
        """Parse framerate string like '30000/1001' to a float."""
        try:
            if '/' in framerate_str:
                num, den = map(float, framerate_str.split('/'))
                return num / den if den != 0 else 0
            return float(framerate_str)
        except (ValueError, ZeroDivisionError): return 0

    def _mask_url_in_error(self, error_message, stream_url, stream_id):
        """Mask URLs in error messages to avoid exposing sensitive stream URLs."""
        if not error_message or not stream_url:
            return error_message

        # Replace full URL with stream ID reference
        masked_error = error_message.replace(stream_url, f"[Stream ID: {stream_id}]")

        # Also try to mask URL-encoded version
        try:
            import urllib.parse
            encoded_url = urllib.parse.quote(stream_url, safe='')
            if encoded_url in masked_error:
                masked_error = masked_error.replace(encoded_url, f"[Stream ID: {stream_id}]")
        except:
            pass

        return masked_error

    # Default host suffixes that ffprobe cannot validate (served via Streamlink).
    # Overridable via the 'streamlink_hosts' plugin setting.
    DEFAULT_STREAMLINK_HOSTS = "youtube.com, youtu.be, twitch.tv, kick.com"

    def _streamlink_host_suffixes(self, settings):
        raw = (settings or {}).get('streamlink_hosts')
        if not raw or not raw.strip():
            raw = self.DEFAULT_STREAMLINK_HOSTS
        return [h.strip().lower().lstrip('.') for h in raw.split(',') if h.strip()]

    def _is_streamlink_only_url(self, url, settings=None):
        if not url:
            return False
        try:
            host = urllib.parse.urlparse(url).hostname or ''
        except Exception:
            return False
        host = host.lower()
        suffixes = self._streamlink_host_suffixes(settings)
        return any(host == s or host.endswith('.' + s) for s in suffixes)

    def check_stream(self, stream_data, timeout, retries, logger, skip_retries=False, settings=None, retry_attempt=0):
        """Check individual stream status with optional retries."""
        url, channel_name = stream_data.get('stream_url'), stream_data.get('channel_name')
        stream_id = stream_data.get('stream_id', 'unknown')
        last_error = "Unknown error"
        last_error_type = "Other"

        # Get probe timeout early for use in default return
        probe_timeout = settings.get('probe_timeout', 20) if settings else 20

        # Streamlink-only URLs (YouTube, Twitch, etc.) cannot be validated by
        # ffprobe. Mark them Skipped so dead-channel rename/move/delete actions
        # do not touch them.
        if self._is_streamlink_only_url(url, settings):
            logger.info(f"⤼ '{channel_name}' SKIPPED - Streamlink-only host ({url})")
            return {
                'status': 'Skipped',
                'error': 'Streamlink-only host (ffprobe cannot validate)',
                'error_type': 'Skipped',
                'format': 'N/A',
                'framerate_num': 0,
                'ffprobe_data': {},
                'dispatcharr_metadata': {
                    'video_codec': None,
                    'resolution': '0x0',
                    'width': 0,
                    'height': 0,
                    'source_fps': None,
                    'pixel_format': None,
                    'video_bitrate': None,
                    'audio_codec': None,
                    'sample_rate': None,
                    'audio_channels': None,
                    'audio_bitrate': None,
                    'stream_type': None
                },
                'retry_count': retry_attempt,
                'connection_timeout_seconds': timeout,
                'probe_timeout_seconds': probe_timeout,
                'ffprobe_monitoring_seconds': 0,
            }
        
        # Default return for dead streams with null metadata
        default_return = {
            'status': 'Dead',
            'error': '',
            'error_type': 'Other',
            'format': 'N/A',
            'framerate_num': 0,
            'ffprobe_data': {},
            'dispatcharr_metadata': {
                'video_codec': None,
                'resolution': '0x0',
                'width': 0,
                'height': 0,
                'source_fps': None,
                'pixel_format': None,
                'video_bitrate': None,
                'audio_codec': None,
                'sample_rate': None,
                'audio_channels': None,
                'audio_bitrate': None,
                'stream_type': None
            },
            'retry_count': retry_attempt,
            'connection_timeout_seconds': timeout,
            'probe_timeout_seconds': probe_timeout,
            'ffprobe_monitoring_seconds': 0
        }

        # Log stream check start at DEBUG level (reduced verbosity)
        retry_info = f" (retry {retry_attempt})" if retry_attempt > 0 else ""
        logger.debug(f"Checking stream{retry_info}: '{channel_name}' - URL: {url}")

        # Determine how many attempts to make
        max_attempts = 1 if skip_retries else (retries + 1)

        # Parse ffprobe flags from settings
        ffprobe_flags_str = settings.get('ffprobe_flags', '-show_streams,-show_frames,-show_packets,-loglevel error') if settings else '-show_streams,-show_frames,-show_packets,-loglevel error'
        ffprobe_flags = [flag.strip() for flag in ffprobe_flags_str.split(',') if flag.strip()]

        # Get ffprobe path from settings
        ffprobe_path = settings.get('ffprobe_path', '/usr/local/bin/ffprobe') if settings else '/usr/local/bin/ffprobe'

        # Build base command with both network timeout and probe duration
        # -timeout: network I/O timeout (for dead streams)
        # -analyzeduration: how long to wait for stream data (for slow-starting streams)
        # -probesize: buffer size for stream analysis
        cmd = [
            ffprobe_path,
            '-print_format', 'json',
            '-user_agent', 'VLC/3.0.21 LibVLC/3.0.21',
            '-timeout', str(timeout * 1000000),  # Network I/O timeout in microseconds
            '-analyzeduration', str(probe_timeout * 1000000),  # Stream probe timeout in microseconds
            '-probesize', '10000000'  # 10MB probe buffer for slow streams
        ]

        # Add loglevel flag if specified, otherwise use default quiet mode
        has_loglevel = any('loglevel' in flag for flag in ffprobe_flags)
        if has_loglevel:
            # Add loglevel flags from user config
            for flag in ffprobe_flags:
                if 'loglevel' in flag:
                    cmd.extend(flag.split())
        else:
            cmd.extend(['-v', 'quiet'])

        # Add show flags (streams, frames, packets)
        for flag in ffprobe_flags:
            if flag.startswith('-show_'):
                cmd.append(flag)

        # Ensure -show_streams is always included for basic validation
        if '-show_streams' not in cmd:
            cmd.append('-show_streams')

        # Ensure -show_format is always included so we can read the container-level
        # bit_rate (the standard "bandwidth" metric). Live MPEG-TS / HLS streams
        # almost never expose bit_rate at the per-stream level.
        if '-show_format' not in cmd:
            cmd.append('-show_format')

        # If using frame or packet analysis, add duration limit using read_intervals
        analysis_duration = 0
        if any(flag in cmd for flag in ['-show_frames', '-show_packets']):
            analysis_duration = settings.get('ffprobe_analysis_duration', 5) if settings else 5
            # Use -read_intervals which is the correct ffprobe option (not -t which is for ffmpeg)
            # Format: %+<duration> reads <duration> seconds from the start
            cmd.extend(['-read_intervals', f'%+{analysis_duration}'])
            logger.debug(f"Added analysis duration: {analysis_duration} seconds for frame/packet analysis")

        # Add URL at the end
        cmd.append(url)

        # Calculate total timeout: probe timeout + analysis duration + 5 second buffer
        # Use probe_timeout (not connection timeout) as the main timeout since that's what
        # determines how long ffprobe will wait for stream data
        total_timeout = probe_timeout + analysis_duration + 5

        # Log the ffprobe command being executed at DEBUG level (reduced verbosity)
        logger.debug(f"Executing ffprobe command for '{channel_name}': {' '.join(cmd)}")

        for attempt in range(max_attempts):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=total_timeout)

                if result.returncode == 0:
                    probe_data = json.loads(result.stdout)
                    video_stream = next((s for s in probe_data.get('streams', []) if s['codec_type'] == 'video'), None)
                    audio_stream = next((s for s in probe_data.get('streams', []) if s['codec_type'] == 'audio'), None)
                    
                    if video_stream:
                        # Extract video metadata
                        width = video_stream.get('width', 0)
                        height = video_stream.get('height', 0)
                        resolution = f"{width}x{height}"
                        framerate_num = round(self.parse_framerate(video_stream.get('r_frame_rate', '0/1')), 1)  # Round to 1 decimal place
                        video_codec = video_stream.get('codec_name', 'unknown')
                        pixel_format = video_stream.get('pix_fmt', 'unknown')
                        
                        # Extract video bitrate. Sources, in order of reliability for live streams:
                        # 1. video_stream.bit_rate (rare on live MPEG-TS / HLS)
                        # 2. format.bit_rate (container-level "bandwidth" — usually present)
                        # 3. packet-based fallback below
                        video_bitrate = None
                        if video_stream.get('bit_rate'):
                            try:
                                video_bitrate = float(video_stream['bit_rate']) / 1000.0
                            except (ValueError, TypeError):
                                pass
                        if video_bitrate is None and probe_data.get('format', {}).get('bit_rate'):
                            try:
                                video_bitrate = float(probe_data['format']['bit_rate']) / 1000.0
                            except (ValueError, TypeError):
                                pass

                        # Extract audio metadata
                        audio_codec = None
                        sample_rate = None
                        audio_channels = None
                        audio_bitrate = None
                        
                        if audio_stream:
                            audio_codec = audio_stream.get('codec_name', 'unknown')
                            sample_rate = audio_stream.get('sample_rate')
                            if sample_rate:
                                try:
                                    sample_rate = int(sample_rate)
                                except (ValueError, TypeError):
                                    sample_rate = None
                            
                            # Get channel layout
                            audio_channels = audio_stream.get('channel_layout') or audio_stream.get('channels')
                            if isinstance(audio_channels, int):
                                # Convert channel count to layout name
                                channel_map = {1: 'mono', 2: 'stereo', 6: '5.1', 8: '7.1'}
                                audio_channels = channel_map.get(audio_channels, f'{audio_channels}ch')
                            
                            # Extract audio bitrate
                            if audio_stream.get('bit_rate'):
                                try:
                                    audio_bitrate = float(audio_stream['bit_rate']) / 1000.0  # Convert to kbps as float
                                except (ValueError, TypeError):
                                    pass

                        # Determine stream type from format
                        stream_type = None
                        if probe_data.get('format'):
                            format_name = probe_data['format'].get('format_name', '')
                            if 'mpegts' in format_name:
                                stream_type = 'mpegts'
                            elif 'hls' in format_name or 'm3u8' in format_name:
                                stream_type = 'hls'
                            elif 'flv' in format_name:
                                stream_type = 'flv'
                            else:
                                stream_type = format_name.split(',')[0] if format_name else 'unknown'

                        # Collect additional ffprobe data for export
                        ffprobe_extra_data = {}

                        # Add frame data if available
                        if probe_data.get('frames'):
                            frames = probe_data['frames']
                            ffprobe_extra_data['frame_count'] = len(frames)
                            ffprobe_extra_data['first_frame_pts'] = frames[0].get('pts', 'N/A') if frames else 'N/A'

                        # Add packet data and calculate bitrate if available
                        if probe_data.get('packets'):
                            packets = probe_data['packets']
                            ffprobe_extra_data['packet_count'] = len(packets)
                            # Calculate average bitrate from packets if not already available.
                            # Restrict to the video stream so audio packets don't dilute the result.
                            if not video_bitrate:
                                video_idx = video_stream.get('index')
                                video_packets = [p for p in packets if p.get('stream_index') == video_idx] or packets
                                total_size = sum(int(p.get('size', 0)) for p in video_packets)
                                total_duration = sum(float(p.get('duration_time') or 0) for p in video_packets)
                                if total_duration > 0:
                                    video_bitrate = (total_size * 8) / (total_duration * 1000)
                                    ffprobe_extra_data['calculated_bitrate_kbps'] = video_bitrate

                        stream_format = self._get_stream_format(resolution)
                        logger.info(f"✓ '{channel_name}' ALIVE - {stream_format} {resolution} {framerate_num:.1f}fps")

                        # Build complete metadata for Dispatcharr integration
                        dispatcharr_metadata = {
                            'video_codec': video_codec,
                            'resolution': resolution,
                            'width': width,
                            'height': height,
                            'source_fps': framerate_num,
                            'pixel_format': pixel_format,
                            'video_bitrate': video_bitrate,
                            'audio_codec': audio_codec,
                            'sample_rate': sample_rate,
                            'audio_channels': audio_channels,
                            'audio_bitrate': audio_bitrate,
                            'stream_type': stream_type
                        }

                        return {
                            'status': 'Alive',
                            'error': '',
                            'error_type': 'N/A',
                            'format': stream_format,
                            'framerate_num': framerate_num,
                            'ffprobe_data': ffprobe_extra_data,
                            'dispatcharr_metadata': dispatcharr_metadata,
                            'retry_count': retry_attempt,
                            'connection_timeout_seconds': timeout,
                            'probe_timeout_seconds': probe_timeout,
                            'ffprobe_monitoring_seconds': analysis_duration
                        }
                    else:
                        last_error = 'No video stream found'
                        last_error_type = 'No Video Stream'
                else:
                    error_output = result.stderr.strip() or 'Stream not accessible'
                    last_error = error_output

                    # Categorize the error type based on common ffprobe error patterns
                    error_lower = error_output.lower()
                    if 'timed out' in error_lower or 'timeout' in error_lower or 'connection timeout' in error_lower:
                        last_error_type = 'Timeout'
                    elif 'option not found' in error_lower or 'unrecognized option' in error_lower:
                        last_error_type = 'FFprobe Option Error'
                    elif '404' in error_output or ('not found' in error_lower and 'http' in error_lower):
                        last_error_type = '404 Not Found'
                    elif '403' in error_output or 'forbidden' in error_lower:
                        last_error_type = '403 Forbidden'
                    elif '500' in error_output or 'internal server error' in error_lower:
                        last_error_type = 'Server Error'
                    elif 'connection refused' in error_lower:
                        last_error_type = 'Connection Refused'
                    elif 'network unreachable' in error_lower or 'no route to host' in error_lower:
                        last_error_type = 'Network Unreachable'
                    elif 'invalid data found' in error_lower or 'invalid argument' in error_lower:
                        last_error_type = 'Invalid Stream'
                    elif 'protocol not supported' in error_lower:
                        last_error_type = 'Unsupported Protocol'
                    elif result.returncode == 1:
                        # Common ffprobe return code for unreachable streams
                        last_error_type = 'Stream Unreachable'
                    else:
                        last_error_type = 'Other'

            except subprocess.TimeoutExpired:
                last_error = f'Connection timeout after {total_timeout} seconds'
                last_error_type = 'Timeout'
            except Exception as e:
                last_error = str(e)
                last_error_type = 'Other'

            # Only do immediate retries if not skipping them and not the last attempt
            if not skip_retries and attempt < max_attempts - 1:
                logger.debug(f"Channel '{channel_name}' stream check failed. Retrying ({attempt+1}/{retries})...")
                time.sleep(1)

        # Log final result once if stream is dead after all attempts
        logger.info(f"✗ '{channel_name}' DEAD - {last_error_type}")

        # Mask URL in error message before returning
        masked_error = self._mask_url_in_error(last_error, url, stream_id)

        default_return['error'] = masked_error
        default_return['error_type'] = last_error_type
        return default_return

    def _update_dispatcharr_metadata(self, channel_data, stream_id, metadata, logger):
        """Update stream metadata in Dispatcharr (PostgreSQL only to avoid orphaned Redis keys)"""
        if not DISPATCHARR_INTEGRATION_AVAILABLE:
            logger.debug("Dispatcharr integration not available - skipping metadata update")
            return False
        
        if not metadata:
            logger.debug(f"No metadata to update for stream {stream_id}")
            return False
        
        try:
            channel_uuid = channel_data.get('uuid')
            if not channel_uuid:
                logger.warning(f"Channel UUID not found for stream {stream_id} - skipping metadata update")
                return False
            
            # Check if this is null metadata (all values are None) - indicates a dead stream
            all_none = all(v is None for v in metadata.values())
            
            if all_none:
                # Dead stream - completely clear stream_stats by setting to empty dict
                logger.debug(f"Clearing metadata for dead stream {stream_id}")
                try:
                    from apps.proxy.ts_proxy.models import Stream as ProxyStream
                    stream = ProxyStream.objects.filter(id=stream_id).first()
                    if stream:
                        stream.stream_stats = {}  # Clear all stats
                        stream.save(update_fields=['stream_stats'])
                        logger.debug(f"Cleared all stream_stats for dead stream {stream_id}")
                        return True
                    else:
                        logger.warning(f"Stream {stream_id} not found in database")
                        return False
                except Exception as e:
                    logger.error(f"Failed to clear stream_stats for stream {stream_id}: {e}")
                    return False
            
            # Filter out None values for cleaner storage (alive streams)
            clean_metadata = {k: v for k, v in metadata.items() if v is not None}
            
            if not clean_metadata:
                logger.debug(f"No valid metadata to update for stream {stream_id}")
                return False
            
            # Skip Redis updates to avoid "orphaned metadata" warnings from Dispatcharr's cleanup process
            # Redis metadata is only meaningful for actively streaming channels
            # PostgreSQL provides persistent storage which is sufficient for this plugin's purpose
            
            # Update PostgreSQL for persistent storage
            try:
                success = ChannelService._update_stream_stats_in_db(
                    stream_id=stream_id,
                    **clean_metadata
                )
                if success:
                    logger.debug(f"Updated database metadata for stream {stream_id}")
                else:
                    logger.warning(f"Database metadata update returned False for stream {stream_id}")
                return success
            except Exception as e:
                logger.error(f"Failed to update database metadata for stream {stream_id}: {e}")
                return False
                
        except Exception as e:
            logger.error(f"Unexpected error updating Dispatcharr metadata for stream {stream_id}: {e}")
            return False
