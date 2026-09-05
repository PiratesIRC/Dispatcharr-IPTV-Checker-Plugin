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
import hashlib
import time
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
import collections
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Django ORM imports (plugins run inside the Django backend process)
from apps.channels.models import Channel, ChannelGroup, Stream, ChannelStream, ChannelProfileMembership
from django.db import transaction
from core.utils import send_websocket_update

# Scheduler imports
try:
    import pytz
    PYTZ_AVAILABLE = True
except ImportError:
    PYTZ_AVAILABLE = False
    # Will log warning later when scheduler is attempted to be used

# Django/Dispatcharr imports for metadata updates.
# Dispatcharr's dev branch renamed apps.proxy.ts_proxy -> apps.proxy.live_proxy.
# Prefer the new path, fall back to the legacy one so the plugin works on both
# current-release and next-release Dispatcharr. The ChannelService API
# (_update_stream_stats_in_db(stream_id, **stats)) is unchanged across the rename.
try:
    from apps.proxy.live_proxy.services.channel_service import ChannelService
    DISPATCHARR_INTEGRATION_AVAILABLE = True
except ImportError:
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
_scheduler_init_lock = threading.Lock()  # Serialize concurrent _init_scheduler calls
_scheduler_initialized = False  # Set True after the first Plugin instance bootstraps the scheduler
# Re-entrant: makes the stop->create->assign-global sequence in
# _start_background_scheduler atomic against a concurrent start from another
# thread (e.g. _init_scheduler racing the run()/update-schedule path). RLock so
# _start_background_scheduler can call _stop_background_scheduler while holding
# it. Distinct from _scheduler_init_lock to avoid a deadlock with _init_scheduler.
_scheduler_lifecycle_lock = threading.RLock()
# Process-shared "already fired this minute" guard. The fire-claim in
# scheduler_loop reads-and-sets this atomically under _scheduler_fire_lock, so
# if a lifecycle race ever leaves two scheduler_loop threads alive in one
# process, only ONE fires a given (cron_expr, minute). Replaces the old
# loop-local `last_run` dict, which gave each duplicate loop its own guard and
# let '0 23 * * *' fire twice 4s apart on 2026-07-03 (buglog
# bug-sched-double-fire-dup-loop-thread). Cross-process is separately prevented
# by the single-owner O_EXCL election lock.
_scheduler_fire_lock = threading.Lock()
_scheduler_last_fired = {}  # cron_expr -> current_minute datetime of last claim
# _RATE_LIMIT_GUARD is initialized eagerly below the RateLimitGuard class
# definition so all Plugin instances share one guard counter.

LOG_PREFIX = "[IPTV Checker]"


class PluginConfig:
    # --- File Paths ---
    DATA_DIR = "/data"
    EXPORTS_DIR = "/data/exports"

    # --- Report output ---
    # /config/<plugin>/ sits under Dispatcharr's existing bind mount, so it is a
    # real folder on the host that the operator can open by double-clicking.
    #
    # DELIBERATELY NOT /data/logos/: Dispatcharr's nginx serves that path to the
    # whole LAN with NO AUTHENTICATION, which would publish an unauthenticated
    # page listing every channel in this install. There is also no report URL
    # setting, because there is nothing to point one at and a setting is an
    # invitation to serve the directory again.
    #
    # DELIBERATELY NOT /data/<plugin>/: that is a named volume with no host
    # path, so nothing the operator must read may live there.
    REPORT_DIR = "/config/iptv_checker"
    RESULTS_FILE = "/data/iptv_checker_results.json"
    LOADED_CHANNELS_FILE = "/data/iptv_checker_loaded_channels.json"
    PROGRESS_FILE = "/data/iptv_checker_progress.json"
    PENDING_RESUME_FILE = "/data/iptv_checker_pending_resume.json"
    # Append-only tally of how many streams each completed pass probed, one JSON
    # object per line. It exists because RESULTS_FILE is OVERWRITTEN by every run
    # and the dated CSVs are pruned to the last few, so nothing else on disk can
    # be added up into a lifetime total. Deliberately carries integers and a mode
    # word only: no channel name, no group, no URL. A public README badge is
    # built from it, so anything identifying that landed here would be published.
    STREAM_COUNT_LEDGER_FILE = "/data/iptv_checker_stream_counts.jsonl"
    CHANNEL_STATE_FILE = "/data/iptv_checker_channel_state.json"
    SCHEDULER_LOCK_FILE = "/data/iptv_checker_scheduler.pid"
    SCHEDULER_RELOAD_FLAG = "/data/iptv_checker_scheduler_reload.flag"
    # One file per (cron expression, minute) actually fired. Survives a module
    # re-import, which is what defeated every in-memory guard on 2026-08-07.
    SCHEDULER_FIRE_CLAIM_DIR = "/data/iptv_checker_fire_claims"
    # Process types that must NOT host the scheduler (GitHub #25). The daphne
    # ASGI server can win the election but never brings a live scheduler loop up,
    # wedging every uwsgi worker into deferring to it. Matched (case-insensitive)
    # against /proc/self/cmdline; a process we can't fingerprint stays eligible.
    SCHEDULER_INELIGIBLE_HOST_MARKERS = ("daphne", "dispatcharr.asgi")

    # --- Scheduler ---
    DEFAULT_TIMEZONE = "UTC"
    # How long progress.json may go unwritten, inside ONE container life, before
    # it is treated as debris rather than as a running check. ProgressTracker
    # writes at worst every 10 seconds, and a slow probe widens the real gap, so
    # this is deliberately far above the cadence: clearing a live run is worse
    # than leaving a dead file for another few minutes.
    PROGRESS_STALE_AFTER_SECONDS = 900
    SCHEDULER_CHECK_INTERVAL = 30  # Check every 30 seconds
    SCHEDULER_TIME_WINDOW = 30  # ±30 second window to trigger
    SCHEDULER_ERROR_WAIT = 60  # Wait 60s if error occurs
    SCHEDULER_STOP_TIMEOUT = 5  # Max wait for thread to stop

    # --- Bitrate calc ---
    # Packet-based video_bitrate is the average over the sampled packets.
    # With very few packets (e.g. probe captured 2), the estimate is dominated
    # by per-packet noise and can spike to wildly inflated values (observed:
    # 22924 kbps from 2 packets). Below this threshold we leave video_bitrate
    # unset rather than persist a misleading number — the next probe will get
    # a fresh shot. 30 ≈ 1s of 30fps video; healthy probes return 200-400.
    MIN_PACKETS_FOR_BITRATE_CALC = 30

    # --- Low framerate detection ---
    # A stream is flagged "low framerate" (eligible for [Slow]/Slow group) when
    # 0 < fps < this threshold. Kept below 25 so PAL/European broadcasts (25fps)
    # and film-rate feeds (24fps; NTSC 23.976 rounds to 24.0) are NOT flagged —
    # only genuinely choppy streams are. See _is_low_framerate.
    LOW_FRAMERATE_THRESHOLD = 24

    # --- Action result sizing ---
    # Dispatcharr renders a transient toast of roughly 280 characters, clipped
    # from the MIDDLE with no ellipsis, and newlines collapse into one
    # paragraph. Anything longer loses its middle silently, which reads as a
    # complete sentence with the finding removed. Sized just under that.
    TOAST_BUDGET = 270

    # --- Channel group selection ---
    # One list plus a mode, replacing the old separate include and exclude
    # boxes. Include is the default because it is the pre-existing meaning of a
    # group list: a stored mode this build does not understand must keep a list
    # of WANTED groups meaning wanted, rather than inverting it into a list of
    # skipped ones.
    CHANNEL_GROUPS_MODES = ('include', 'exclude')
    DEFAULT_CHANNEL_GROUPS_MODE = 'include'
    # The setting ids the mode pair replaced. Dispatcharr never prunes a stored
    # setting when its field is removed, so these survive in the database and
    # are read as a migration fallback.
    LEGACY_GROUP_INCLUDE_KEY = 'group_names'
    LEGACY_GROUP_EXCLUDE_KEY = 'group_names_exclude'

    # --- Content-analysis defaults (one shared ffmpeg decode pass) ---
    # How many seconds of video to decode. Also bounds how long a freeze can
    # be observed, so the freeze threshold is clamped against it.
    DEFAULT_BLACK_SAMPLE_SECONDS = 6
    # Minimum continuous still-picture run, in seconds, before a stream counts
    # as frozen. freezedetect only reports once this has elapsed.
    DEFAULT_FREEZE_MIN_SECONDS = 4
    # Mean volume at or below this (dBFS) counts as silence. Measured basis:
    # digital silence encodes as -91.0 dB (confirmed here on ffmpeg 8.1.2 with
    # anullsrc, and in the field by Sentinelarr), while the quietest real
    # channel measured was -44.4 dB (a film). -70 sits between the two with
    # about 25 dB of margin on each side.
    DEFAULT_SILENT_AUDIO_MAX_DB = -70.0

    # --- FFprobe defaults ---
    # Single source of truth for the default ffprobe flags. -show_packets is
    # required for the packet-based video_bitrate fallback; -loglevel error keeps
    # stderr usable for error classification. Both check_stream and the CSV audit
    # preamble fall back to this so the recorded flags match the flags actually
    # used (the preamble previously fell back to just '-show_streams').
    DEFAULT_FFPROBE_FLAGS = '-show_streams,-show_packets,-loglevel error'

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


class RateLimitGuard:
    """Adaptive backoff for upstream HTTP 429 (rate limit) responses.

    Tracks 429 hits in a sliding window; trips a cooldown that doubles each
    re-trip and decays to baseline after a clean stretch. Used by both
    sequential and parallel check loops via wait_if_throttled() before each
    ffprobe and record_hit() when a 429 classification is produced.
    """
    WINDOW_SECONDS = 60          # sliding window for hit counting
    TRIP_THRESHOLD = 5           # hits within WINDOW_SECONDS to trip
    BASE_COOLDOWN_SECONDS = 60   # first cooldown duration
    MAX_COOLDOWN_SECONDS = 600   # cap doubled cooldowns at 10 min
    DECAY_AFTER_SECONDS = 300    # clean window before resetting cooldown growth

    def __init__(self):
        self._lock = threading.Lock()
        self._hit_times = collections.deque()
        self._cooldown_until = 0.0
        self._next_cooldown = self.BASE_COOLDOWN_SECONDS
        self._last_hit_time = 0.0

    def record_hit(self, logger=None):
        now = time.time()
        with self._lock:
            self._hit_times.append(now)
            self._last_hit_time = now
            cutoff = now - self.WINDOW_SECONDS
            while self._hit_times and self._hit_times[0] < cutoff:
                self._hit_times.popleft()
            if len(self._hit_times) >= self.TRIP_THRESHOLD and now >= self._cooldown_until:
                cooldown = self._next_cooldown
                self._cooldown_until = now + cooldown
                self._next_cooldown = min(self._next_cooldown * 2, self.MAX_COOLDOWN_SECONDS)
                # Reset window so we only re-trip on a fresh burst after cooldown;
                # in-cooldown hits still get appended below on subsequent calls.
                self._hit_times.clear()
                if logger:
                    logger.warning(f"⚠️ Rate-limit guard tripped: pausing checks for {int(cooldown)}s after {self.TRIP_THRESHOLD}+ HTTP 429s in {self.WINDOW_SECONDS}s")

    def wait_if_throttled(self, logger=None, stop_event=None):
        with self._lock:
            now = time.time()
            if self._last_hit_time and (now - self._last_hit_time) > self.DECAY_AFTER_SECONDS:
                self._next_cooldown = self.BASE_COOLDOWN_SECONDS
            initial_wait = self._cooldown_until - now
        if initial_wait <= 0:
            return
        if logger:
            logger.info(f"⚠️ Rate-limit cooldown active — sleeping {int(initial_wait)}s before next check")
        # Re-read _cooldown_until each iteration so a fresh trip that EXTENDS
        # the cooldown during this sleep is honored (avoids TOCTOU where N
        # parallel workers all wake on the original deadline).
        while True:
            with self._lock:
                remaining = self._cooldown_until - time.time()
            if remaining <= 0:
                return
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(min(remaining, 1.0))


# Eager module-level singleton — runs once under the import lock, so all
# Plugin instances created during Django plugin reloads share one counter.
_RATE_LIMIT_GUARD = RateLimitGuard()


_CONTAINER_BOOT_TOKEN = None


def _container_boot_token():
    """A token that is stable within one container lifetime but changes on every
    container (re)start. Used to detect a scheduler lock file left behind on the
    persistent /data volume by a previous container — after a restart the OS
    recycles low PID numbers, so the old holder PID frequently collides with a
    live, unrelated process and os.kill(pid, 0) succeeds, wedging the election.

    Composed of the host kernel boot_id (changes on host reboot) and PID 1's
    starttime in clock ticks (changes on every container start, since PID 1 is
    the container entrypoint). Either component alone is insufficient: boot_id
    is unchanged by a container restart, and starttime can recur across a host
    reboot. Cached after first computation. Returns "" if nothing readable, in
    which case callers fall back to the legacy dead-PID check only.
    """
    global _CONTAINER_BOOT_TOKEN
    if _CONTAINER_BOOT_TOKEN is not None:
        return _CONTAINER_BOOT_TOKEN
    boot_id = ""
    try:
        with open('/proc/sys/kernel/random/boot_id', 'r') as f:
            boot_id = f.read().strip()
    except OSError:
        pass
    pid1_start = ""
    try:
        with open('/proc/1/stat', 'r') as f:
            # Field 22 (1-indexed) is starttime. The comm field (2) may contain
            # spaces/parens, so split on the final ')' first.
            stat = f.read()
            after = stat[stat.rfind(')') + 1:].split()
            pid1_start = after[19]  # 22nd field overall -> index 19 after comm
    except (OSError, IndexError):
        pass
    _CONTAINER_BOOT_TOKEN = f"{boot_id}:{pid1_start}" if (boot_id or pid1_start) else ""
    return _CONTAINER_BOOT_TOKEN


class Plugin:
    """Dispatcharr IPTV Checker Plugin"""
    
    # Explicitly set the plugin key
    key = "iptv_checker"
    version = "1.26.2481321"

    # Fields and actions are defined in plugin.json (single source of truth)
    def __init__(self):
        self.results_file = PluginConfig.RESULTS_FILE
        self.loaded_channels_file = PluginConfig.LOADED_CHANNELS_FILE
        self.progress_file = PluginConfig.PROGRESS_FILE
        self.pending_resume_file = PluginConfig.PENDING_RESUME_FILE
        self.channel_state_file = PluginConfig.CHANNEL_STATE_FILE
        self.check_progress = self._load_progress()
        self.load_progress = {"current": 0, "total": 0, "status": "idle"}  # Track load groups progress
        self._thread = None
        self._thread_lock = threading.Lock()
        self._stop_event = threading.Event()
        self.timeout_retry_queue = []  # Queue for streams that timed out and need retry
        # Module-level singleton so multiple Plugin instances created during
        # Django plugin reload share one guard counter (see _RATE_LIMIT_GUARD).
        self._rate_limit_guard = _RATE_LIMIT_GUARD
        # Active windowed-schedule state (None when not in a window run)
        self._active_window_end = None
        self._active_window_tz = None
        self._restart_resume_active = False
        self.version_check_cache = None  # Cached version check result
        self.version_check_time = None  # Time when version was last checked
        LOGGER.info(f"Plugin v{self.version} initialized")

        # Start scheduler on init so it survives container restarts
        self._init_scheduler()

    def _init_scheduler(self):
        """Load saved settings from DB and start the scheduler if configured.

        Dispatcharr runs ~9 separate Python processes (4 uwsgi workers, celery
        worker/beat, daphne ASGI, supervisors) — each imports this module and
        constructs Plugin instances independently. Module-level locks/flags do
        not cross process boundaries, so a per-process file lock at
        SCHEDULER_LOCK_FILE elects exactly one process to host the scheduler;
        every other process no-ops. Within the elected process, the module-
        level lock+flag still de-dupe Django's per-process plugin reloads.
        """
        global _scheduler_initialized
        with _scheduler_init_lock:
            if _scheduler_initialized:
                return
            try:
                self._normalize_stale_progress()
                if not self._acquire_scheduler_lock():
                    _scheduler_initialized = True  # mark so this process stops trying
                    return
                from apps.plugins.models import PluginConfig as DBPluginConfig
                cfg = DBPluginConfig.objects.filter(key=self.key).first()
                if cfg and cfg.settings:
                    if cfg.settings.get("scheduled_times", "").strip():
                        LOGGER.info("Loading saved settings for scheduler startup")
                        self._start_background_scheduler(cfg.settings)
                    # If a window was open when the container went down, resume it now
                    self._maybe_resume_after_restart(cfg.settings)
                _scheduler_initialized = True
            except Exception as e:
                LOGGER.warning(f"Could not load settings for scheduler on init: {e}")

    @staticmethod
    def _read_scheduler_lock(lock_path):
        """Return (holder_pid, holder_token, readable) for the lock file.

        holder_pid is 0 and readable is False when the file is missing, blank,
        or corrupt — callers treat an unreadable existing lock as reclaimable.
        """
        try:
            with open(lock_path, 'r') as f:
                raw = f.read().splitlines()
        except OSError:
            return 0, '', False
        try:
            holder_pid = int((raw[0].strip() if raw else '') or '0')
        except ValueError:
            return 0, '', False
        holder_token = raw[1].strip() if len(raw) > 1 else ''
        return holder_pid, holder_token, True

    @staticmethod
    def _write_lock_fd(fd, my_pid, my_token):
        """Write `pid\n{token}` through a buffered stream, then close fd.

        os.fdopen's write loops until every byte is flushed, avoiding the
        short-write hazard of a bare os.write — a truncated body would drop the
        token line and silently degrade the lock to legacy single-line form.
        """
        with os.fdopen(fd, 'w') as f:
            f.write(f"{my_pid}\n{my_token}")
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _scheduler_holder_alive(holder_pid, holder_token, my_token):
        """True iff the lock holder is a live process we must respect.

        A boot-token mismatch means the lock belongs to a previous container
        (after a restart the recycled PID often collides with a live unrelated
        process, so os.kill() alone can't be trusted) — treat as not alive.
        """
        if my_token and holder_token and holder_token != my_token:
            return False
        try:
            os.kill(holder_pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True   # alive, owned by another uid
        except OSError:
            return True   # unknown — don't steal a possibly-live lock

    def _reclaim_scheduler_lock(self, lock_path, my_pid, my_token):
        """Reclaim a stale lock under an exclusive guard file so concurrent
        reclaimers can't both install themselves. Returns (outcome, owner_pid)
        where outcome is 'won', 'skip', or 'retry'.

        os.open(O_CREAT|O_EXCL) on the guard elects a single reclaimer; only it
        clears the stale lock and installs a fresh one, so the read->remove->
        create sequence is atomic with respect to other reclaimers. Losers get
        'retry' and re-race the top-level create, where they find the fresh
        (current-token) lock and skip.
        """
        guard = f"{lock_path}.reclaim"
        try:
            gfd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Another reclaimer holds the guard. If it was left by a previous
            # container, clear it; otherwise back off and let it finish.
            _gp, g_token, g_read = self._read_scheduler_lock(guard)
            if g_read and my_token and g_token and g_token != my_token:
                try:
                    os.unlink(guard)
                except OSError:
                    pass
            return ('retry', 0)
        except OSError:
            return ('retry', 0)
        try:
            self._write_lock_fd(gfd, my_pid, my_token)
            # Exclusive section: no other reclaimer is here, and a still-present
            # stale lock blocks any top-level creator, so the lock can't change
            # under us until we replace it.
            holder_pid, holder_token, readable = self._read_scheduler_lock(lock_path)
            if readable and holder_pid and holder_pid != my_pid and \
                    self._scheduler_holder_alive(holder_pid, holder_token, my_token):
                return ('skip', holder_pid)
            # Atomically swap our lock in over the stale one (os.replace leaves no
            # empty-slot gap a top-level creator could win, on POSIX or Windows).
            new_path = f"{lock_path}.new.{my_pid}"
            try:
                # Same 0o600 as the two os.open sites above: the builtin open()
                # would take the process umask instead, so the reclaim path would
                # install a world-readable lock over the restricted one it replaced.
                nfd = os.open(new_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                with os.fdopen(nfd, 'w') as f:
                    f.write(f"{my_pid}\n{my_token}")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(new_path, lock_path)
            except OSError:
                try:
                    os.unlink(new_path)
                except OSError:
                    pass
                return ('retry', 0)
            return ('won', my_pid)
        finally:
            # Always release the guard, even if writing it raised.
            try:
                os.unlink(guard)
            except OSError:
                pass

    @staticmethod
    def _cmdline_is_ineligible_scheduler_host(cmdline):
        """Pure classifier: True if this process command line names a process
        type that must not host the scheduler (daphne/ASGI).

        Each marker is matched against the BASENAME of a command-line token, not
        as a raw substring of the whole string, so an install PATH or username
        containing 'daphne' (e.g. /home/daphne/... or a uwsgi ini under it) can't
        accidentally exclude a uwsgi/celery worker and leave the deployment with
        no scheduler host — the very silent failure #25 fixes. A marker matches a
        token when the token's basename equals it (executable, e.g.
        '/venv/bin/daphne') or begins with 'marker:' (module target, e.g.
        'dispatcharr.asgi:app'). Fail-open: a blank command line is eligible."""
        if not cmdline:
            return False
        markers = PluginConfig.SCHEDULER_INELIGIBLE_HOST_MARKERS
        for token in cmdline.lower().split():
            base = token.rsplit("/", 1)[-1]
            for marker in markers:
                if base == marker or base.startswith(marker + ":"):
                    return True
        return False

    def _read_own_cmdline(self):
        """This process's command line as a space-joined string, or '' if it
        can't be read (non-Linux / no /proc) so the caller fails open."""
        try:
            with open("/proc/self/cmdline", "rb") as f:
                return f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def _is_ineligible_scheduler_host(self):
        """True iff this process must be excluded from scheduler election (#25)."""
        return self._cmdline_is_ineligible_scheduler_host(self._read_own_cmdline())

    def _acquire_scheduler_lock(self):
        """Cross-process lock — return True iff this PID should host the scheduler.

        Mutual exclusion comes from os.open(O_CREAT|O_EXCL): the kernel
        guarantees exactly one of any number of racing processes creates the lock
        file, so exactly one wins. (The previous write-tmp -> rename ->
        read-back-and-confirm protocol was a TOCTOU, not mutual exclusion: a
        contender could read back its own PID in the gap before another
        contender's rename overwrote it, so several processes each confirmed they
        had won and the cron fired once per duplicate owner — the 2026-06-24
        double-fire, where PIDs 235 and 246 both logged "Scheduler lock acquired"
        3ms apart.)

        A lock left behind by a previous container (boot-token mismatch) or held
        by a dead PID is reclaimed by _reclaim_scheduler_lock, which serializes
        reclaimers under an exclusive guard so they can't both install
        themselves. The file is still 2 lines — `pid\n{boot_token}` — so
        _owns_scheduler_lock and the triage tooling are unchanged. The lock is
        "released" only by the next startup reclaiming it; Dispatcharr's
        processes are long-lived so there's no mid-lifetime failover. Requires
        /data to be a local volume — NFS weakens O_EXCL atomicity.
        """
        # GitHub #25: the daphne/ASGI process must never win the election. It can
        # acquire the lock during a boot import (before the DB-backed schedule is
        # readable) and then hold it without hosting a live loop, wedging every
        # uwsgi worker into deferring to it so the cron never fires. Exclude it as
        # a candidate so a uwsgi worker reliably hosts the scheduler.
        if self._is_ineligible_scheduler_host():
            LOGGER.info(f"Process {os.getpid()} is the daphne/ASGI server (not a uwsgi "
                        "worker); not eligible to host the scheduler.")
            return False

        lock_path = PluginConfig.SCHEDULER_LOCK_FILE
        my_pid = os.getpid()
        my_token = _container_boot_token()

        # Bounded: each iteration either returns or clears one stale obstacle.
        for _attempt in range(8):
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                holder_pid, holder_token, readable = self._read_scheduler_lock(lock_path)
                if holder_pid == my_pid and not (my_token and holder_token and holder_token != my_token):
                    # We already hold it THIS container life — idempotent re-entry.
                    # But only when the token matches: after a restart the OS
                    # recycles low PIDs, so a previous-container stale lock can name
                    # our new PID (recycled PID 243 colliding with the prior holder
                    # 243 on 2026-07-02). Honoring the bare PID match there let the
                    # recycled PID silently "already own" the stale lock while
                    # another process reclaimed it — two winners, two scheduler
                    # loops, the 2026-07-03 cron double-fire. On a token mismatch we
                    # fall through to the reclaim path so exactly one process wins.
                    return True
                if readable and holder_pid:
                    if my_token and holder_token and holder_token != my_token:
                        LOGGER.info(f"Stale scheduler lock from a previous container (PID {holder_pid}, token mismatch); reclaiming as PID {my_pid}.")
                    else:
                        try:
                            os.kill(holder_pid, 0)
                            LOGGER.info(f"Scheduler already owned by PID {holder_pid}; this process ({my_pid}) will skip scheduler bootstrap.")
                            return False
                        except ProcessLookupError:
                            LOGGER.info(f"Stale scheduler lock for dead PID {holder_pid}; reclaiming as PID {my_pid}.")
                        except PermissionError:
                            LOGGER.info(f"Scheduler lock held by PID {holder_pid} (different uid); skipping.")
                            return False
                        except OSError:
                            # Transient error probing the holder — don't steal.
                            return False
                outcome, owner_pid = self._reclaim_scheduler_lock(lock_path, my_pid, my_token)
                if outcome == 'won':
                    LOGGER.info(f"Scheduler lock acquired by PID {my_pid}.")
                    return True
                if outcome == 'skip':
                    LOGGER.info(f"Scheduler already owned by PID {owner_pid or holder_pid}; this process ({my_pid}) will skip scheduler bootstrap.")
                    return False
                continue  # 'retry' — guard contended; re-race the create
            else:
                self._write_lock_fd(fd, my_pid, my_token)
                LOGGER.info(f"Scheduler lock acquired by PID {my_pid}.")
                return True

        return False

    def _owns_scheduler_lock(self):
        """True iff this process is the elected scheduler holder.

        Read-only check used by UI-triggered code paths (run(), update_schedule_action)
        so non-elected uwsgi workers don't spawn rogue scheduler threads. A non-owner
        that needs to reconfigure the scheduler should write SCHEDULER_RELOAD_FLAG
        instead, which the owner's scheduler_loop polls each iteration.
        """
        try:
            with open(PluginConfig.SCHEDULER_LOCK_FILE, 'r') as f:
                return int((f.readline().strip() or '0')) == os.getpid()
        except (OSError, ValueError):
            return False

    def _scheduler_lock_taken_over(self):
        """True iff the election lock now names a DIFFERENT readable PID than this
        process — i.e. this scheduler_loop has been superseded and must stop firing.

        Defense-in-depth against a duplicate cross-process election: if two
        processes ever both bootstrap a scheduler (e.g. the recycled-PID double-win
        this guards against at the source), the one whose PID is NOT in the lock
        file yields instead of zombie-firing cron. An unreadable/missing lock
        returns False (don't kill the real owner on a transient read glitch).
        """
        holder_pid, _token, readable = self._read_scheduler_lock(PluginConfig.SCHEDULER_LOCK_FILE)
        return bool(readable and holder_pid and holder_pid != os.getpid())

    def _request_scheduler_reload(self):
        """Signal the elected scheduler process to re-read settings from DB."""
        try:
            with open(PluginConfig.SCHEDULER_RELOAD_FLAG, 'w') as f:
                f.write(str(time.time()))
        except OSError as e:
            LOGGER.warning(f"Could not write scheduler reload flag: {e}")

    def _normalize_stale_progress(self):
        """Clear progress.json when it claims a check is running and no check is.

        WHY IT EXISTS. A container kill bypasses the `finally` block that flips
        status to 'idle', so the file is left saying 'running' forever and every
        later cron fire self-queues believing a check is in flight. The schedule
        then never runs again.

        WHY IT MUST NOT FIRE BLINDLY. This runs from __init__, and the old
        version assumed "at __init__ time no thread can possibly be running".
        That is true within one process and FALSE across processes: Dispatcharr
        runs about nine of them, and a plugin discovery pass constructs a Plugin
        instance in whichever worker serves it. Measured on 2026-08-08, two
        discovery passes during a live check clamped it to idle at 418/2691 and
        488/2691 streams. The check carried on, but while the file says idle a
        cron fire will not defer to the run that is actually in progress.

        HOW THE TWO ARE TOLD APART. The container boot token, the same signal
        the scheduler election lock uses: a file stamped by a PREVIOUS container
        cannot belong to a running check, so it is debris whatever it says.
        Within one container life the mtime is used instead, because a live
        check rewrites this file continuously; silence longer than
        PROGRESS_STALE_AFTER_SECONDS means no live writer. A file with NO token
        was written before this release and is treated as debris, which restores
        the old behaviour for it. That is the safe default here: the alternative
        leaves a genuinely stuck file stuck forever.
        """
        try:
            if not os.path.exists(self.progress_file):
                return
            with open(self.progress_file, 'r') as f:
                data = json.load(f) or {}
            if data.get('status') != 'running':
                return

            token = data.get('boot_token')
            same_container = bool(token) and token == _container_boot_token()
            if same_container:
                try:
                    quiet_for = time.time() - os.path.getmtime(self.progress_file)
                except OSError:
                    quiet_for = 0
                if quiet_for < PluginConfig.PROGRESS_STALE_AFTER_SECONDS:
                    LOGGER.debug(
                        f"{LOG_PREFIX} progress.json says running "
                        f"({data.get('current', 0)}/{data.get('total', 0)}) and was written "
                        f"{int(quiet_for)}s ago by this container; leaving a live check alone."
                    )
                    return
                reason = (f"nothing has written it for {int(quiet_for)}s, so its worker is gone")
            else:
                reason = "it was written by a previous container"

            LOGGER.warning(
                f"Found stale progress.json with status=running "
                f"({data.get('current', 0)}/{data.get('total', 0)}); {reason}. "
                f"Normalizing to idle."
            )
            data['status'] = 'idle'
            data['end_time'] = time.time()
            self._save_json_file(self.progress_file, data, indent=2)
            self.check_progress = data
        except Exception as e:
            LOGGER.warning(f"Could not normalize progress.json on startup: {e}")

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

    # ---------------- Windowed schedule helpers ----------------

    def _compute_window_end(self, now_local, settings, tz):
        """Compute the absolute window-end datetime in tz given window start = now_local.

        Returns None if config is invalid. `time` mode wraps past midnight.
        """
        mode = (settings.get("schedule_end_mode", "duration") or "duration").lower()
        if mode == "duration":
            try:
                hours = float(settings.get("schedule_duration_hours", 4) or 4)
            except (ValueError, TypeError):
                return None
            if hours <= 0:
                return None
            return now_local + timedelta(hours=hours)
        if mode == "time":
            end_str = (settings.get("schedule_end_time", "04:00") or "04:00").strip()
            try:
                hh_str, mm_str = end_str.split(":")
                hh, mm = int(hh_str), int(mm_str)
            except (ValueError, AttributeError):
                return None
            if not (0 <= hh < 24 and 0 <= mm < 60):
                return None
            end = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if end <= now_local:
                end = end + timedelta(days=1)
            return end
        return None

    def _past_window_end(self):
        if self._active_window_end is None or self._active_window_tz is None:
            return False
        return datetime.now(self._active_window_tz) >= self._active_window_end

    @staticmethod
    def _coerce_timezone(value):
        """Return a valid IANA timezone name, or PluginConfig.DEFAULT_TIMEZONE as a
        safe fallback. Accepts None / blank / non-string / invalid -> default."""
        if not isinstance(value, str) or not value.strip():
            return PluginConfig.DEFAULT_TIMEZONE
        candidate = value.strip()
        try:
            import pytz
            pytz.timezone(candidate)
        except Exception:
            return PluginConfig.DEFAULT_TIMEZONE
        return candidate

    def _dispatcharr_timezone(self):
        """Resolve the effective timezone from Dispatcharr's global setting
        (General Settings -> Time Zone, core.models.CoreSettings). Falls back to
        PluginConfig.DEFAULT_TIMEZONE ('UTC') when unreadable/invalid or when
        running outside Dispatcharr. Lazy import so the module loads in tests."""
        try:
            from core.models import CoreSettings
            return self._coerce_timezone(CoreSettings.get_system_time_zone())
        except Exception as e:
            LOGGER.debug(f"{LOG_PREFIX} Could not read Dispatcharr timezone, using {PluginConfig.DEFAULT_TIMEZONE}: {e}")
            return PluginConfig.DEFAULT_TIMEZONE

    def _setup_window_state(self, settings):
        """Resolve TZ and compute end-of-window. Stores state on self. Returns False on bad config."""
        if not PYTZ_AVAILABLE:
            LOGGER.error("Windowed schedule requires pytz")
            return False
        tz_str = self._dispatcharr_timezone()
        try:
            tz = pytz.timezone(tz_str)
        except Exception:
            tz = pytz.timezone(PluginConfig.DEFAULT_TIMEZONE)
        now = datetime.now(tz)
        end = self._compute_window_end(now, settings, tz)
        if end is None:
            LOGGER.error("⏰ WINDOW: invalid schedule_end_mode/end_time/duration; aborting run")
            return False
        self._active_window_end = end
        self._active_window_tz = tz
        LOGGER.info(f"⏰ WINDOW: starts {now.isoformat()} → ends {end.isoformat()}")
        return True

    def _clear_window_state(self):
        self._active_window_end = None
        self._active_window_tz = None

    def _settings_fingerprint(self, settings):
        # Drives windowed-resume drift detection: a pending resume whose
        # fingerprint no longer matches is DISCARDED rather than resumed against
        # a scope it was not built for. The group list and its MODE must both be
        # in here -- flipping include to exclude turns the scope into its exact
        # complement while every other value stays identical.
        patterns, mode, legacy_exclude = self._resolve_channel_groups(settings)
        return {
            'channel_groups': patterns,
            'channel_groups_mode': mode,
            'channel_groups_legacy_exclude': legacy_exclude,
            'check_alternative_streams': bool(settings.get('check_alternative_streams', True)),
            'only_visible_channels': bool(settings.get('only_visible_channels', False)),
        }

    def _seed_pending_from_loaded_channels(self, settings):
        """Write pending_resume.json from the current loaded_channels.json."""
        loaded = self._load_json_file(self.loaded_channels_file) or []
        stream_ids = [s['id'] for ch in loaded for s in ch.get('streams', []) if 'id' in s]
        payload = {
            'started_at': datetime.utcnow().isoformat() + 'Z',
            'window_end_iso': self._active_window_end.isoformat() if self._active_window_end else None,
            'tz': str(self._active_window_tz) if self._active_window_tz else None,
            'settings_fingerprint': self._settings_fingerprint(settings),
            'remaining_stream_ids': stream_ids,
        }
        self._save_json_file(self.pending_resume_file, payload)

    def _apply_pending_resume_to_loaded_channels(self, settings, logger):
        """Filter loaded_channels.json down to streams still in pending_resume.json.

        Returns True if a usable resume state was applied; False if caller should
        fall back to a fresh load.
        """
        pending = self._load_json_file(self.pending_resume_file)
        if not pending or not pending.get('remaining_stream_ids'):
            return False

        saved_fp = pending.get('settings_fingerprint') or {}
        if saved_fp != self._settings_fingerprint(settings):
            logger.warning(
                f"⏰ WINDOW RESUME: settings changed since last window — discarding stale "
                f"pending state and starting a fresh load with current settings. "
                f"saved={saved_fp} current={self._settings_fingerprint(settings)}"
            )
            self._clear_pending_resume()
            return False

        # A saved window end in the PAST is the normal cross-window case, not
        # dead state: last night's window closed at 04:00 and tonight's opens at
        # 23:00, so the saved end is always behind. Discarding it here is what
        # stopped multi-window resume ever working, and a full pass on a large
        # lineup needs more than one night.
        #
        # It is only dead when there is no ACTIVE window to resume into. When
        # one is open, fall through: the re-anchor below moves the pending file
        # onto the active window.
        saved_end_iso = pending.get('window_end_iso')
        if saved_end_iso and self._active_window_end is None:
            try:
                saved_end = datetime.fromisoformat(saved_end_iso)
                saved_tz = pytz.timezone(pending.get('tz') or self._dispatcharr_timezone())
                if saved_end.tzinfo is None:
                    saved_end = saved_tz.localize(saved_end)
                if datetime.now(saved_tz) >= saved_end:
                    logger.warning(
                        "⏰ WINDOW RESUME: saved window already elapsed — discarding "
                        "stale pending state and starting a fresh load"
                    )
                    self._clear_pending_resume()
                    return False
            except Exception as e:
                logger.warning(f"⏰ WINDOW RESUME: could not parse saved window end ({e}); discarding stale pending state")
                self._clear_pending_resume()
                return False

        loaded = self._load_json_file(self.loaded_channels_file) or []
        if not loaded:
            logger.warning("⏰ WINDOW RESUME: pending state present but loaded_channels.json missing — falling back to fresh load")
            self._clear_pending_resume()
            return False

        remaining = set(pending['remaining_stream_ids'])
        channel_ids = [ch['id'] for ch in loaded]
        live_ids = set(Channel.objects.filter(id__in=channel_ids).values_list('id', flat=True))

        filtered = []
        for ch in loaded:
            if ch['id'] not in live_ids:
                continue
            kept = [s for s in ch.get('streams', []) if s.get('id') in remaining]
            if kept:
                filtered.append({**ch, 'streams': kept})

        if not filtered:
            logger.warning("⏰ WINDOW RESUME: no remaining streams match live channels — clearing pending state, falling back to fresh load")
            self._clear_pending_resume()
            return False

        self._save_json_file(self.loaded_channels_file, filtered)
        total_streams = sum(len(ch.get('streams', [])) for ch in filtered)
        logger.info(f"⏰ WINDOW RESUME: continuing with {len(filtered)} channels / {total_streams} streams")

        # Re-anchor the pending file's window metadata to the active window.
        # Without this, a stale window_end from a prior cron-fire is preserved
        # by _mark_stream_done, and _maybe_resume_after_restart would refuse
        # to resume after a container restart inside the new window. Leave
        # settings_fingerprint untouched so subsequent windows can still
        # detect drift relative to the original run.
        if self._active_window_end is not None:
            pending['window_end_iso'] = self._active_window_end.isoformat()
            pending['tz'] = str(self._active_window_tz) if self._active_window_tz else pending.get('tz')
            self._save_json_file(self.pending_resume_file, pending)
        return True

    def _mark_stream_done(self, stream_id):
        """Remove a stream id from pending_resume.json. Deletes the file when empty.

        Safe no-op when not in a windowed run.
        """
        if self._active_window_end is None or stream_id is None:
            return
        pending = self._load_json_file(self.pending_resume_file)
        if not pending or 'remaining_stream_ids' not in pending:
            return
        try:
            pending['remaining_stream_ids'].remove(stream_id)
        except ValueError:
            return
        if not pending['remaining_stream_ids']:
            self._clear_pending_resume()
        else:
            self._save_json_file(self.pending_resume_file, pending)

    def _clear_pending_resume(self):
        try:
            os.remove(self.pending_resume_file)
        except FileNotFoundError:
            pass
        except Exception as e:
            LOGGER.warning(f"Could not remove pending_resume.json: {e}")

    def _has_pending_resume(self):
        pending = self._load_json_file(self.pending_resume_file)
        return bool(pending and pending.get('remaining_stream_ids'))

    def _maybe_resume_after_restart(self, settings):
        """If a window was open when the container died, kick off the check immediately."""
        if not settings.get("schedule_window_enabled", False):
            return
        pending = self._load_json_file(self.pending_resume_file)
        if not pending or not pending.get("remaining_stream_ids"):
            return
        end_iso = pending.get("window_end_iso")
        tz_str = pending.get("tz") or self._dispatcharr_timezone()
        try:
            tz = pytz.timezone(tz_str)
            end = datetime.fromisoformat(end_iso) if end_iso else None
            if end is not None and end.tzinfo is None:
                end = tz.localize(end)
        except Exception as e:
            LOGGER.warning(f"Could not parse pending window state on restart: {e}")
            return
        if end is None:
            return
        now = datetime.now(tz)
        if now >= end:
            # The window this state belongs to has closed, so do NOT resume
            # right now. But KEEP the file: the next window needs it to
            # continue mid-list. Deleting it here meant any reload or restart
            # between windows silently threw the progress away, and opening the
            # Dispatcharr plugins page is enough to trigger it.
            LOGGER.info(
                "⏰ WINDOW: pending state is from a closed window; keeping it "
                "for the next window rather than resuming now"
            )
            return
        LOGGER.info(f"⏰ WINDOW: pending state detected (ends {end.isoformat()}); resuming check after restart")
        # Set the guard BEFORE spawning so the scheduler_loop's first tick
        # doesn't queue a duplicate cron-fire while the resume is starting up.
        self._restart_resume_active = True

        def _do_resume():
            try:
                self._execute_scheduled_check(
                    self._fresh_settings(settings),
                    preserved_window_end=end,
                    preserved_window_tz=tz,
                )
            finally:
                self._restart_resume_active = False

        threading.Thread(
            target=_do_resume,
            daemon=True,
            name="iptv-checker-restart-resume"
        ).start()

    # ---------------- /Windowed schedule helpers ----------------

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
        """Save check progress to persistent storage.

        Uses the atomic tmp-file + os.replace helper rather than a plain
        open(path, 'w'). A direct write fails with EACCES when an existing
        progress file is owned by root and not group-writable (e.g. TrueNAS
        SCALE, where the app runs as uid 568 — see issue #21). The atomic
        path writes a fresh temp file owned by the current user and renames
        it over the target, which only requires write permission on the
        parent directory, so it succeeds regardless of the old file's owner.

        The container boot token is stamped on every write. Without it
        _normalize_stale_progress cannot tell a live check from a file left
        behind by a killed container, and it clears live checks.
        """
        try:
            self.check_progress['boot_token'] = _container_boot_token()
        except Exception:
            pass
        self._save_json_file(self.progress_file, self.check_progress)

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

    def _record_streams_checked(self, count, mode, logger=None):
        """Append one line recording how many streams a finished pass probed.

        APPEND, NOT READ-MODIFY-WRITE. Several Dispatcharr processes hold this
        module, and a read-then-write total would lose an increment whenever two
        of them raced. An O_APPEND write of a single short line does not.

        IT NEVER RAISES. This is called from the `finally` of the two stream
        processing paths, so an exception here would replace whatever the run was
        already reporting. A tally that cannot be written is worth strictly less
        than the check that produced it.

        A count of zero is still written: a pass that probed nothing is a fact
        about the run, and dropping it would make the ledger silently sparse.
        """
        log = logger or LOGGER
        try:
            count = int(count)
        except (TypeError, ValueError):
            log.warning("Stream tally skipped: count was not a number")
            return False
        if count < 0:
            log.warning("Stream tally skipped: count was negative")
            return False
        line = json.dumps({"ts": int(time.time()), "streams": count, "mode": mode})
        try:
            with open(PluginConfig.STREAM_COUNT_LEDGER_FILE, 'a') as f:
                f.write(line + "\n")
            return True
        except Exception as e:
            log.warning(f"Could not record the stream tally: {e}")
            return False

    def stop(self, context):
        logger = context.get("logger", LOGGER)
        logger.info("Plugin unloading - stopping scheduler and active threads")
        self._stop_background_scheduler()
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
    
    def _parse_scheduled_times(self, scheduled_times_str):
        """
        Parse one or more cron expressions separated by a semicolon, a newline
        or a comma.
        Format: 'minute hour day month weekday'
        Example: "0 4 * * *" = daily at 4:00 AM
        Example: "0 3 1 * *" = 1st of month at 3:00 AM
        Example: "0 0,8,16 * * *" = daily at midnight, 8 AM and 4 PM
        Returns list of cron expression strings.
        """
        if not scheduled_times_str or not scheduled_times_str.strip():
            return []
        
        cron_expressions = []
        for chunk in re.split(r'[;\r\n]', scheduled_times_str):
            chunk = chunk.strip()
            if not chunk:
                continue

            # A comma is legal INSIDE a cron field, where it lists values, so a
            # chunk that already reads as one complete 5-field expression is
            # taken whole. Splitting on commas first turned the single valid
            # expression "0 0,8,16 * * *" into three fragments and rejected the
            # whole schedule (GitHub issue 27).
            if len(chunk.split()) == 5:
                cron_expressions.append(chunk)
                continue

            # Otherwise fall back to the comma as a separator, which is what
            # the setting has always documented and what saved schedules use.
            for expr in chunk.split(','):
                expr = expr.strip()
                if not expr:
                    continue
                if len(expr.split()) == 5:
                    cron_expressions.append(expr)
                else:
                    LOGGER.warning(
                        "Invalid cron expression (must have 5 fields): %s. If this is part of a "
                        "list of values inside one expression, separate your expressions with a "
                        "semicolon instead of a comma." % expr
                    )
        
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
    
    @staticmethod
    def _fire_claim_path(cron_expr, current_minute):
        """Path of the on-disk claim for one (cron expression, minute).

        The cron expression is hashed rather than written into the name because
        it contains spaces, asterisks and slashes. Eight hex characters are
        plenty: the only thing that must not collide is two DIFFERENT schedules
        due in the SAME minute, of which an install has a handful at most.
        """
        stamp = current_minute.strftime('%Y%m%dT%H%M')
        digest = hashlib.sha256(cron_expr.encode('utf-8')).hexdigest()[:8]
        return os.path.join(PluginConfig.SCHEDULER_FIRE_CLAIM_DIR, f"fire-{stamp}-{digest}")

    @staticmethod
    def _prune_fire_claims(current_minute):
        """Drop claim files from an earlier DAY. Their minute cannot come round
        again, and the directory would otherwise grow without bound. Claims from
        earlier today are kept: an earlier minute today is exactly what stops a
        duplicate loop re-firing it."""
        today = current_minute.strftime('%Y%m%d')
        try:
            names = os.listdir(PluginConfig.SCHEDULER_FIRE_CLAIM_DIR)
        except OSError:
            return
        for name in names:
            parts = name.split('-')
            if len(parts) != 3 or parts[0] != 'fire':
                continue
            day = parts[1].split('T')[0]
            if len(day) == 8 and day.isdigit() and day < today:
                try:
                    os.unlink(os.path.join(PluginConfig.SCHEDULER_FIRE_CLAIM_DIR, name))
                except OSError:
                    pass

    def _claim_scheduler_fire(self, cron_expr, current_minute):
        """Atomically claim (cron_expr, current_minute) for firing.

        Returns True for the first caller to claim this minute anywhere on the
        box and False for every caller after.

        TWO LAYERS, and the second one is the load-bearing one.

        In memory, _scheduler_last_fired stops two scheduler_loop threads that
        share a module object (the 2026-07-03 double-fire, where a lifecycle
        race left two loops alive each with its own last_run).

        On disk, os.open(O_CREAT|O_EXCL) stops everything else. A module
        RE-IMPORT gives the new scheduler loop a fresh _scheduler_last_fired,
        so the in-memory layer cannot see the older loop's claim at all. That
        is what happened on 2026-08-07: a plugin discovery pass re-imported the
        module inside the process that already owned the election lock, leaving
        two loops in one PID holding two module objects. The election lock saw
        one PID and passed, the in-memory claim saw two dicts and passed, and
        '0 23 * * *' fired twice 1.34 seconds apart, running two concurrent
        five-hour scans that emailed two reports.

        FAILS OPEN. If the claim cannot be written the schedule still runs and
        the failure is logged. A duplicate run is recoverable; a scheduler that
        silently stops firing is not.
        """
        with _scheduler_fire_lock:
            if _scheduler_last_fired.get(cron_expr) == current_minute:
                return False
            _scheduler_last_fired[cron_expr] = current_minute

        try:
            os.makedirs(PluginConfig.SCHEDULER_FIRE_CLAIM_DIR, exist_ok=True)
            fd = os.open(self._fire_claim_path(cron_expr, current_minute),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            LOGGER.warning(f"Cron '{cron_expr}' was already claimed for this minute by another "
                           "scheduler loop; not firing again. A duplicate loop is running, which "
                           "a container restart clears.")
            return False
        except OSError as e:
            LOGGER.warning(f"Could not record the scheduler fire claim ({type(e).__name__}); "
                           "running the schedule anyway, but a duplicate run is now possible.")
            return True

        try:
            os.write(fd, f"{os.getpid()}\n".encode('utf-8'))
        except OSError:
            pass
        finally:
            os.close(fd)

        self._prune_fire_claims(current_minute)
        return True

    def _start_background_scheduler(self, settings):
        """Start the background scheduler thread."""
        global _bg_scheduler_thread, _scheduler_pending_run
        
        # Check if pytz is available
        if not PYTZ_AVAILABLE:
            LOGGER.error("Scheduler requires pytz library but it is not installed")
            return
        
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
        tz_str = self._dispatcharr_timezone()
        try:
            local_tz = pytz.timezone(tz_str)
        except pytz.exceptions.UnknownTimeZoneError:
            LOGGER.error(f"Unknown timezone: {tz_str}, using default: {PluginConfig.DEFAULT_TIMEZONE}")
            tz_str = PluginConfig.DEFAULT_TIMEZONE
            local_tz = pytz.timezone(tz_str)
        
        # Define the scheduler loop
        def scheduler_loop():
            global _scheduler_pending_run
            nonlocal local_tz, tz_str, scheduled_times

            LOGGER.info(f"Scheduler started. Timezone: {tz_str}, Cron expressions: {scheduled_times}")

            while not _scheduler_stop_event.is_set():
                try:
                    # Self-evict if superseded: a newer _start_background_scheduler
                    # may have replaced the global without _stop_background_scheduler
                    # having joined this thread. Belt-and-suspenders alongside the
                    # lifecycle lock so an orphaned loop can never double-fire.
                    if _bg_scheduler_thread is not threading.current_thread():
                        LOGGER.info("Scheduler loop superseded by a newer thread — exiting orphaned loop")
                        return

                    # Cross-process guard: if another process now holds the election
                    # lock, this loop belongs to a de-elected process (a duplicate
                    # election could have started it) — yield so cron fires once.
                    if self._scheduler_lock_taken_over():
                        LOGGER.warning("Scheduler loop no longer owns the election lock; "
                                       "exiting to avoid duplicate cron fire")
                        return

                    # Reload schedule from DB if a non-owner worker requested it
                    # (UI "Update Schedule" handled in a different uwsgi process).
                    if os.path.exists(PluginConfig.SCHEDULER_RELOAD_FLAG):
                        try:
                            os.remove(PluginConfig.SCHEDULER_RELOAD_FLAG)
                        except OSError:
                            pass
                        fresh = self._fresh_settings(settings)
                        new_times_str = (fresh.get("scheduled_times") or "").strip()
                        new_times = self._parse_scheduled_times(new_times_str) if new_times_str else []
                        new_tz_str = self._dispatcharr_timezone()
                        try:
                            new_tz = pytz.timezone(new_tz_str)
                        except pytz.exceptions.UnknownTimeZoneError:
                            new_tz_str = PluginConfig.DEFAULT_TIMEZONE
                            new_tz = pytz.timezone(new_tz_str)
                        if new_times != scheduled_times or new_tz_str != tz_str:
                            LOGGER.info(
                                f"Scheduler reloaded: tz={new_tz_str}, "
                                f"cron={new_times if new_times else '(empty — idle)'}"
                            )
                            scheduled_times = new_times
                            tz_str = new_tz_str
                            local_tz = new_tz

                    now = datetime.now(local_tz)
                    # Truncate to minute precision for matching (ignore seconds)
                    current_minute = now.replace(second=0, microsecond=0)

                    for cron_expr in scheduled_times:
                        # Check if this cron expression matches the current time
                        if self._cron_matches(cron_expr, now):
                            # Claim this minute atomically across every scheduler_loop
                            # thread in this process. If a lifecycle race leaves two
                            # loops alive (each formerly with its own last_run), only
                            # the first claimant fires — the guard against the
                            # 2026-07-03 double-fire where '0 23 * * *' fired twice
                            # 4s apart from the same owner PID.
                            if not self._claim_scheduler_fire(cron_expr, current_minute):
                                continue  # Already fired this minute (this or a sibling loop)

                            LOGGER.info(f"⏰ SCHEDULED RUN triggered at {now.strftime('%Y-%m-%d %H:%M:%S')} for cron: {cron_expr}")

                            # Check if a check is already running
                            if self.check_progress.get('status') == 'running':
                                if getattr(self, '_restart_resume_active', False):
                                    LOGGER.info("Cron fire ignored: restart-resume is in progress for this window")
                                else:
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
        
        # Atomically stop any existing scheduler and install this one. Holding
        # _scheduler_lifecycle_lock across stop+create+assign prevents a
        # concurrent start (e.g. _init_scheduler racing the run()/update-schedule
        # path) from leaving two live scheduler_loop threads — each with its own
        # last_run, which caused duplicate cron fires.
        with _scheduler_lifecycle_lock:
            self._stop_background_scheduler()
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

        # Re-entrant: _start_background_scheduler calls this while already
        # holding _scheduler_lifecycle_lock; an external stop serializes here.
        with _scheduler_lifecycle_lock:
            if _bg_scheduler_thread and _bg_scheduler_thread.is_alive():
                LOGGER.info("Stopping scheduler thread...")
                prev_thread = _bg_scheduler_thread
                _scheduler_stop_event.set()
                prev_thread.join(timeout=PluginConfig.SCHEDULER_STOP_TIMEOUT)
                _scheduler_stop_event.clear()
                _scheduler_pending_run = False
                _bg_scheduler_thread = None
                if prev_thread.is_alive():
                    # The old loop didn't exit within the join timeout (the shared
                    # stop_event was cleared for the incoming thread). It self-evicts
                    # at its next iteration via the _bg_scheduler_thread identity
                    # check; until then the shared fire-claim guard prevents it from
                    # double-firing. Surfaced so a recurring leak is visible.
                    LOGGER.warning("Previous scheduler thread still alive after stop timeout; "
                                   "it will self-evict on its next iteration")
                LOGGER.info("Scheduler thread stopped")
    
    @staticmethod
    def _post_scan_phases_should_run(results_mtime_before, results_mtime_after):
        """True only when this session's scan wrote a new results file.

        The results file is written exactly once, at the end of a scan, so a
        scan that was interrupted leaves the PREVIOUS run's complete results in
        place. Running the CSV export, the emailed report and the rename, move
        and delete actions against that file re-applies verdicts from an earlier
        run at an arbitrary later time, which can delete a channel that has
        recovered since.

        A status value cannot answer this. The in-memory copy is process local,
        and the on-disk copy is written a moment after the status flips, so a
        scan that genuinely finished can briefly read as still running and would
        lose its report. Comparing the file's own modification time across the
        scan is a direct test and needs no clock.
        """
        if results_mtime_after is None:
            return False
        if results_mtime_before is None:
            return True
        return results_mtime_after > results_mtime_before

    def _results_file_mtime(self):
        """Modification time of the results file, or None when unreadable."""
        try:
            return os.path.getmtime(self.results_file)
        except OSError:
            return None

    def _execute_scheduled_check(self, settings, preserved_window_end=None, preserved_window_tz=None):
        """Execute the scheduled stream check (Load Groups + Start Check).

        Honors `schedule_window_enabled`. Per-stream progress is persisted to
        pending_resume.json so the next window resumes where the last left off.
        Post-check actions (rename/move/delete) only run on the window
        that finishes the list.

        preserved_window_end / preserved_window_tz come from
        `_maybe_resume_after_restart` so the original window end is honored
        instead of being re-anchored to "now + duration" after a container restart.
        """
        LOGGER.info("⏰ Starting scheduled check sequence")

        # Create a logger context for scheduled runs
        scheduled_logger = logging.getLogger("plugins.iptv_checker.scheduled")
        scheduled_logger.setLevel(logging.INFO)
        if not any(isinstance(f, PluginNameFilter) for f in scheduled_logger.filters):
            scheduled_logger.addFilter(PluginNameFilter())

        is_window = bool(settings.get("schedule_window_enabled", False))
        if is_window:
            if preserved_window_end is not None and preserved_window_tz is not None:
                self._active_window_end = preserved_window_end
                self._active_window_tz = preserved_window_tz
                LOGGER.info(f"⏰ WINDOW: resuming preserved window → ends {preserved_window_end.isoformat()}")
            elif not self._setup_window_state(settings):
                return

        # Set before the try so the finally can tell an interrupted session from a
        # finished one without having to re-read anything.
        scan_unfinished = False

        try:
            # Step 1: Load Groups (or apply pending resume in window mode)
            resumed = False
            if is_window and self._apply_pending_resume_to_loaded_channels(settings, scheduled_logger):
                resumed = True
                LOGGER.info("⏰ SCHEDULED: Resuming from prior window")
            else:
                LOGGER.info("⏰ SCHEDULED: Loading groups...")
                load_result = self.load_groups_action(settings, scheduled_logger)

                if load_result.get('status') != 'ok':
                    LOGGER.error(f"⏰ SCHEDULED: Load groups failed: {load_result.get('message')}")
                    return

                LOGGER.info(f"⏰ SCHEDULED: {load_result.get('message')}")
                if is_window:
                    self._seed_pending_from_loaded_channels(settings)

            # Step 2: Start Stream Check
            # Recorded before the scan so the post-scan phases can tell whether THIS
            # session produced results, rather than trusting a status value.
            results_mtime_before = self._results_file_mtime()
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
            
            # The wait above also ends when the scheduler stop event is set, which
            # _stop_background_scheduler does in order to end the scheduler loop. That
            # leaves the scan thread still running, so reaching this point is not proof
            # that anything was probed. Acting anyway would run the CSV export, the
            # emailed report and every rename, move and delete against a results file
            # that still holds the PREVIOUS run, re-applying that run's verdicts at an
            # arbitrary later time and potentially deleting a channel that has since
            # recovered.
            if not self._post_scan_phases_should_run(
                    results_mtime_before, self._results_file_mtime()):
                scan_unfinished = True
                LOGGER.warning(
                    "⏰ SCHEDULED: this session wrote no new results, so the scan did "
                    "not finish. Skipping the CSV export, the emailed report and every "
                    "rename, move and delete, because the results file still holds the "
                    "previous run. A scan that is still running continues unaffected."
                )
                return

            LOGGER.info("⏰ SCHEDULED: Stream check completed")

            # CSV export runs on every scheduled session, BEFORE the mid-list gate.
            # The CSV is the authoritative audit record of what was probed in this
            # window — it must be written even when the window closes mid-list,
            # otherwise partial-window runs leave no on-disk trace. Wrapped in
            # try/except so a CSV failure does not abort post-actions on full runs.
            LOGGER.info("⏰ SCHEDULED: Exporting results to CSV...")
            try:
                export_result = self.export_results_action(settings, scheduled_logger)
                LOGGER.info(f"⏰ SCHEDULED: {export_result.get('message')}")
            except Exception as e:
                LOGGER.error(f"⏰ SCHEDULED: CSV export failed: {e}", exc_info=True)

            # Emailed report, beside the CSV export and on the same terms: it
            # runs on every scheduled session, BEFORE the mid-list gate, so a
            # window that closes part-way still leaves a report. Wrapped in
            # try/except so a delivery failure never aborts the post-actions.
            if settings.get('scheduler_email_report', False):
                LOGGER.info("⏰ SCHEDULED: Building and emailing the report...")
                try:
                    written, problems = self._build_and_deliver_report(
                        settings, scheduled_logger, email=True)
                    path = (written or {}).get('html_path')
                    if path:
                        LOGGER.info(f"⏰ SCHEDULED: Report written to {path}")
                    if problems:
                        # Loud, because a routing mistake delivers the report
                        # somewhere else and every other signal reads healthy.
                        LOGGER.warning("⏰ SCHEDULED: Report email problem: %s"
                                       % "; ".join(problems))
                    elif path:
                        LOGGER.info("⏰ SCHEDULED: Report queued for delivery")
                except Exception as e:
                    LOGGER.error(f"⏰ SCHEDULED: Report step failed: {e}", exc_info=True)

            # In window mode, only run post-actions when the channel list completed
            # (pending_resume.json deleted = nothing left). Otherwise defer until the
            # next window finishes the remaining streams.
            if is_window and self._has_pending_resume():
                LOGGER.info("⏰ WINDOW: closed mid-list — post-actions deferred to next window")
                return
            
            # Step 3b: Restore recovered channels FIRST (heal before re-marking)
            restored_count = 0
            if settings.get('scheduler_restore_channels', False):
                LOGGER.info("⏰ SCHEDULED: Restoring recovered channels...")
                restore_result = self.restore_channels_action(settings, scheduled_logger)
                restored_count = restore_result.get('restored', 0)
                LOGGER.info(f"⏰ SCHEDULED: {restore_result.get('message')}")

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

            # Step 5b: Rename black-screen channels if enabled
            if settings.get('scheduler_rename_black_screen_channels', False):
                LOGGER.info("⏰ SCHEDULED: Renaming black-screen channels...")
                rename_black_result = self.rename_black_screen_channels_action(settings, scheduled_logger)
                LOGGER.info(f"⏰ SCHEDULED: {rename_black_result.get('message')}")

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

            # Step 8b: Move black-screen channels if enabled
            if settings.get('scheduler_move_black_screen_channels', False):
                LOGGER.info("⏰ SCHEDULED: Moving black-screen channels to group...")
                move_black_result = self.move_black_screen_channels_action(settings, scheduled_logger)
                LOGGER.info(f"⏰ SCHEDULED: {move_black_result.get('message')}")

            # Step 9: Delete dead channels if enabled
            if settings.get('scheduler_delete_dead_channels', False):
                LOGGER.info("⏰ SCHEDULED: Deleting dead channels...")
                delete_result = self.delete_dead_channels_action(settings, scheduled_logger)
                if delete_result.get('status') == 'ok':
                    LOGGER.info(f"⏰ SCHEDULED: {delete_result.get('message')}")
                else:
                    LOGGER.warning(f"⏰ SCHEDULED: {delete_result.get('message')}")

            LOGGER.info("⏰ SCHEDULED: Check sequence completed successfully")

        except Exception as e:
            LOGGER.error(f"⏰ SCHEDULED: Error during scheduled check: {e}", exc_info=True)
        finally:
            # Clearing the window state removes the boundary the running scan checks
            # between streams, so an interrupted session must leave it alone or that
            # scan runs on to the end of the list instead of stopping at its window end.
            if is_window and not scan_unfinished:
                self._clear_window_state()

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
                            message = f"🔔 Update Available: v{latest_clean} is available (current: v{current_clean})"
                        else:
                            message = f"✅ Version Status: You are up to date (v{self.version})"
                    except (ValueError, AttributeError):
                        # Fallback to string comparison if version parsing fails
                        if latest_clean != current_clean:
                            message = f"🔔 Update Available: v{latest_clean} is available (current: v{current_clean})"
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
            # Scheduler lifecycle is owned by the elected process and managed via
            # _init_scheduler + the SCHEDULER_RELOAD_FLAG. Non-owner workers must
            # NOT spawn a thread here — that's how duplicate cron fires happen.

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
                "email_report": self.email_report_action,
                "clear_csv_exports": self.clear_csv_exports_action,
                "update_schedule": self.update_schedule_action,
                "reset_progress": self.reset_progress_action,
                "cleanup_orphaned_tasks": self.cleanup_orphaned_tasks_action,
                "check_scheduler_status": self.check_scheduler_status_action,
                "delete_dead_channels": self.delete_dead_channels_action,
                "rename_black_screen_channels": self.rename_black_screen_channels_action,
                "move_black_screen_channels": self.move_black_screen_channels_action,
                "restore_channels": self.restore_channels_action,
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
        """Validate the settings and return a result that FITS THE TOAST.

        Dispatcharr renders a transient toast of roughly 280 characters,
        clipped from the MIDDLE with no ellipsis, and newlines collapse into
        one paragraph. A longer result does not scroll: its middle is silently
        removed, so the operator reads the start and the end of a sentence with
        the finding cut out from between them.

        So this reports COUNTS, never group names. Measured 2026-08-05 on a
        952-group install, enumerating the skipped groups produced a single
        4,690 character line, 16.8 times the whole budget, while the counts
        that carry the same meaning cost about 50.

        Problems always survive; the short confirmations are dropped first when
        space runs out, because a confirmation the operator cannot read costs
        nothing and a problem they cannot read costs everything.
        """
        problems = []   # must be fixed; never dropped
        facts = []      # confirmations; dropped first when space is tight

        try:
            channel_count = Channel.objects.count()
            group_count = ChannelGroup.objects.count()
            stream_count = Stream.objects.count()
            facts.append(f"DB {channel_count}ch/{group_count}grp/{stream_count}str")

            group_names_str, groups_mode, legacy_exclude = self._resolve_channel_groups(settings)
            if group_names_str:
                try:
                    all_group_names = {g['name'] for g in self._get_all_groups(logger)}
                    unmatched = [
                        p for p in (n.strip() for n in group_names_str.split(','))
                        if p and not self._match_group_names(p, all_group_names)
                    ]
                    selected = self._select_groups(group_names_str, groups_mode, all_group_names)

                    # The count the operator is actually validating: how many
                    # groups this run will touch.
                    if groups_mode == 'exclude':
                        skipped = len(all_group_names) - len(selected)
                        facts.insert(0, f"checking {len(selected)} groups, skipping {skipped}")
                    else:
                        facts.insert(0, f"checking {len(selected)} of {len(all_group_names)} groups")

                    # A pattern matching nothing is an error in BOTH modes. In
                    # exclude mode a typo leaves the group being checked and
                    # acted on, which is the outcome the operator was avoiding.
                    if unmatched:
                        problems.append(f"no groups matched: {self._fit(', '.join(unmatched), 90)}")
                    if not selected:
                        problems.append("nothing left to check with this list and mode")
                except Exception as e:
                    problems.append(f"group validation failed: {str(e)[:60]}")
            else:
                facts.insert(0, f"checking all {group_count} groups")

            if legacy_exclude:
                problems.append(
                    f"old settings in use, also excluding '{self._fit(legacy_exclude, 30)}'; "
                    "save Channel Groups to migrate"
                )
        except Exception as e:
            problems.append(f"DB error: {str(e)[:60]}")

        if settings.get("timeout", 10) <= 0:
            problems.append("timeout must be > 0")
        if settings.get("parallel_workers", 2) < 1:
            problems.append("workers must be >= 1")
        if settings.get("ffprobe_analysis_duration", 5) <= 0:
            problems.append("analysis duration must be > 0")

        scheduled_times_str = (settings.get("scheduled_times", "") or "").strip()
        if scheduled_times_str:
            scheduled_times = self._parse_scheduled_times(scheduled_times_str)
            if not scheduled_times:
                problems.append(f"invalid cron '{self._fit(scheduled_times_str, 30)}'")
            elif not PYTZ_AVAILABLE:
                problems.append("pytz missing, scheduler cannot run")
            else:
                facts.append(f"cron {', '.join(scheduled_times)} {self._dispatcharr_timezone()}")

        if settings.get('scheduler_delete_dead_channels', False):
            if (settings.get('auto_delete_confirmation', '') or '').strip() != 'DELETE':
                problems.append("auto-delete on but confirmation is not DELETE, so it will not run")
            else:
                # Not an error: the operator asked for it. It is stated anyway,
                # because it is the one setting that destroys data.
                facts.insert(0, "AUTO-DELETE ON, dead channels will be removed")

        # Only mention the version when there is something to DO about it.
        try:
            _, version_message = self._get_latest_version()
            if version_message and 'up to date' not in version_message.lower():
                facts.append(self._fit(version_message, 60))
        except Exception:
            pass

        if problems:
            body = f"{len(problems)} problem(s): " + "; ".join(problems)
            return {"status": "error", "error": self._fit(body, PluginConfig.TOAST_BUDGET)}
        body = "Settings valid. " + " | ".join(facts)
        return {"status": "ok", "message": self._fit(body, PluginConfig.TOAST_BUDGET)}

    @staticmethod
    def _fit(text, budget):
        """Trim to `budget` characters, cutting from the END.

        Dispatcharr clips an over-long toast from the MIDDLE with no ellipsis,
        which removes the finding and leaves a sentence that still reads as
        complete. Trimming from the end with a visible marker keeps the loss
        obvious and keeps the front of the message, which is where the counts
        and the first problem are.
        """
        text = ' '.join((text or '').split())
        if len(text) <= budget:
            return text
        return text[:max(0, budget - 3)].rstrip() + '...'

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

    def _results_timestamp_str(self):
        """Best-effort 'when were these results produced' string, or None.

        Prefers the check's recorded end_time; falls back to the results file mtime
        (e.g. results restored from disk after a restart, where progress was reset).
        """
        ts = None
        progress = self.check_progress if isinstance(self.check_progress, dict) else {}
        if progress.get('end_time'):
            ts = progress['end_time']
        else:
            try:
                ts = os.path.getmtime(self.results_file)
            except OSError:
                ts = None
        if not ts:
            return None
        try:
            return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
        except (OverflowError, OSError, ValueError):
            return None

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

        black = sum(1 for r in results if self._is_black_screen(r))
        checked_at = self._results_timestamp_str()
        summary = [
            f"📊 Last Check Results ({len(results)} streams):",
        ]
        if checked_at:
            summary.append(f"🕐 Checked: {checked_at}")
        summary += [
            f"✅ Alive: {alive}",
            f"❌ Dead: {dead}" + (f"  (⬛ {black} black/blank)" if black else ""),
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

    def _get_all_groups(self, logger):
        """Fetch all channel groups via Django ORM."""
        return list(ChannelGroup.objects.all().values('id', 'name'))

    def _get_all_channels(self, logger, group_ids=None):
        """Fetch channels via Django ORM, optionally filtered by group IDs."""
        qs = Channel.objects.select_related('channel_group').all()
        if group_ids:
            qs = qs.filter(channel_group_id__in=group_ids)
        return list(qs.values('id', 'name', 'channel_number', 'channel_group_id', 'uuid'))

    def _get_visible_channel_ids(self, logger):
        """Return set of channel IDs that are enabled in at least one ChannelProfile.

        A channel is "visible" if any ChannelProfileMembership row for it has enabled=True.
        Channels with no membership rows at all, or whose every membership is disabled,
        are excluded.
        """
        return set(
            ChannelProfileMembership.objects.filter(enabled=True)
            .values_list('channel_id', flat=True)
            .distinct()
        )

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

    @staticmethod
    def _match_group_names(patterns_str, all_group_names):
        """Return the set of group names matching any comma-separated pattern.
        Wildcards (containing * ? [) use fnmatch.fnmatchcase (case-sensitive);
        literals use case-sensitive exact membership — symmetric with the include path."""
        matched = set()
        for pattern in (p.strip() for p in (patterns_str or '').split(',')):
            if not pattern:
                continue
            if any(c in pattern for c in '*?['):
                matched |= {g for g in all_group_names if fnmatch.fnmatchcase(g, pattern)}
            elif pattern in all_group_names:
                matched.add(pattern)
        return matched

    @staticmethod
    def _resolve_channel_groups(settings):
        """Resolve the group selection to (patterns, mode, legacy_exclude).

        `patterns` is the comma-separated list the operator typed, `mode` is
        'include' or 'exclude' and never anything else, and `legacy_exclude` is
        normally empty.

        Resolve from the LIVE settings dict passed to run(), never from
        self.saved_settings and never from cached instance state: that is the
        bug-139 shape, where a value primed on one entry path is read back on
        another and silently defaults.

        MIGRATION. This pair replaced two separate boxes, `group_names` (check
        these) and `group_names_exclude` (skip these). Dispatcharr never prunes
        a stored setting when its field is removed, so those values survive in
        the database. They are used when the new box is EMPTY, so an upgrade
        does not silently widen the scope of a destructive action.

        The old pair could apply an include list AND an exclude list at once,
        which one list plus a mode cannot express. Rather than pick one and
        change behaviour, that case returns the include list as `patterns` and
        the exclude list as `legacy_exclude`, and the caller applies both. A
        NEW configuration can only be one or the other.

        Anything typed into the new box stops the fallback, so `*` in include
        mode is the escape hatch for an operator who wants every group while a
        stale legacy value is still stored.
        """
        s = settings if isinstance(settings, dict) else {}

        raw_mode = s.get('channel_groups_mode', PluginConfig.DEFAULT_CHANNEL_GROUPS_MODE)
        if isinstance(raw_mode, str):
            mode = raw_mode.strip().lower()
            if mode not in PluginConfig.CHANNEL_GROUPS_MODES:
                mode = PluginConfig.DEFAULT_CHANNEL_GROUPS_MODE
        else:
            mode = PluginConfig.DEFAULT_CHANNEL_GROUPS_MODE

        patterns = s.get('channel_groups', '')
        patterns = patterns.strip() if isinstance(patterns, str) else ''
        if patterns:
            return patterns, mode, ''

        legacy_include = s.get(PluginConfig.LEGACY_GROUP_INCLUDE_KEY, '')
        legacy_include = legacy_include.strip() if isinstance(legacy_include, str) else ''
        legacy_exclude = s.get(PluginConfig.LEGACY_GROUP_EXCLUDE_KEY, '')
        legacy_exclude = legacy_exclude.strip() if isinstance(legacy_exclude, str) else ''

        if legacy_include and legacy_exclude:
            return legacy_include, 'include', legacy_exclude
        if legacy_include:
            return legacy_include, 'include', ''
        if legacy_exclude:
            return legacy_exclude, 'exclude', ''
        return '', mode, ''

    @staticmethod
    def _select_groups(patterns_str, mode, all_group_names):
        """Apply a group list and its mode to every known group name.

        An EMPTY list means every group, in both modes: there is nothing to
        include down to and nothing to skip. That is distinct from a non-empty
        list that matches nothing, which returns the empty set so the caller can
        report a typo rather than silently checking everything.
        """
        names = set(all_group_names or ())
        patterns_str = (patterns_str or '').strip()
        if not patterns_str:
            return names
        matched = Plugin._match_group_names(patterns_str, names)
        if mode == 'exclude':
            return names - matched
        return matched

    def load_groups_action(self, settings, logger):
        """Load channels and streams from specified Dispatcharr groups."""
        try:
            group_names_str, mode, legacy_exclude = self._resolve_channel_groups(settings)

            all_groups = self._get_all_groups(logger)
            group_name_to_id = {g['name']: g['id'] for g in all_groups}
            all_group_names = set(group_name_to_id.keys())

            logger.info(f"Channel Groups: '{group_names_str}' mode={mode} (empty={not group_names_str})")
            if legacy_exclude:
                logger.warning(
                    "⚠️ Using the OLD group settings: this install had both a check list and an "
                    f"exclude list, which one list plus a mode cannot express. Excluding "
                    f"'{legacy_exclude}' on top of the list above. Save the Channel Groups setting "
                    "to complete the migration."
                )

            if not group_names_str:
                logger.warning("⚠️ No channel groups specified - this will load ALL groups. To filter, specify group names in the 'Channel Groups' field.")
                logger.warning(f"⚠️ Total groups found: {len(all_group_names)}")
                logger.warning(f"⚠️ Groups: {', '.join(sorted(all_group_names))}")

                target_group_names = set(all_group_names)
                if not target_group_names:
                    return {"status": "error", "message": "No groups found in Dispatcharr."}
            else:
                # Report per-pattern so a typo is visible, then apply the mode.
                # A pattern matching nothing is an error in BOTH modes: in
                # exclude mode a typo would leave the group being checked and
                # acted on, which is the outcome the operator was avoiding.
                unmatched_patterns = [
                    p for p in (n.strip() for n in group_names_str.split(','))
                    if p and not self._match_group_names(p, all_group_names)
                ]
                if unmatched_patterns:
                    logger.warning(f"⚠️ No groups matched: {', '.join(unmatched_patterns)}")

                target_group_names = self._select_groups(group_names_str, mode, all_group_names)

                if mode == 'exclude':
                    skipped = all_group_names - target_group_names
                    if skipped:
                        logger.info(f"🚫 Excluding {len(skipped)} group(s) from check: {', '.join(sorted(skipped))}")
                    logger.info(f"✓ Loading {len(target_group_names)} remaining group(s)")
                elif target_group_names:
                    logger.info(f"✓ Loading specified groups: {', '.join(sorted(target_group_names))}")

                if not target_group_names:
                    if mode == 'exclude':
                        return {"status": "error", "message":
                                "Every group was excluded by the Channel Groups filter. Nothing to check."}
                    return {"status": "error", "message":
                            f"No groups matched: {', '.join(unmatched_patterns) or group_names_str}"}

            # Migration only: an install that had BOTH old settings keeps its
            # exclude list applied on top, so an upgrade cannot widen the scope.
            if legacy_exclude:
                excluded = self._match_group_names(legacy_exclude, all_group_names)
                removed = target_group_names & excluded
                if removed:
                    logger.info(f"🚫 Excluding {len(removed)} group(s) via the old exclude setting: {', '.join(sorted(removed))}")
                    target_group_names = target_group_names - excluded
                if not target_group_names:
                    return {"status": "error", "message":
                            "All target groups were removed by the old exclude setting. Nothing to check."}

            target_group_ids = {group_name_to_id[name] for name in target_group_names}
            channels_in_groups = self._get_all_channels(logger, group_ids=target_group_ids)

            only_visible = bool(settings.get("only_visible_channels", False))
            if only_visible:
                visible_ids = self._get_visible_channel_ids(logger)
                before = len(channels_in_groups)
                channels_in_groups = [ch for ch in channels_in_groups if ch['id'] in visible_ids]
                hidden = before - len(channels_in_groups)
                logger.info(f"👁️ Only Visible Channels: kept {len(channels_in_groups)}/{before} (skipped {hidden} hidden)")

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
        patterns, mode, legacy_exclude = self._resolve_channel_groups(settings)
        if not group_names_str:
            group_msg = "all groups"
        elif mode == 'exclude':
            group_msg = f"all groups except '{patterns}' ({len(target_group_names)} remaining)"
        else:
            group_msg = f"group(s): {', '.join(sorted(target_group_names))}"
        if legacy_exclude:
            group_msg += f" (also excluding '{legacy_exclude}' from the old setting)"
        if settings.get("only_visible_channels", False):
            group_msg += " (visible channels only)"

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
        enable_parallel = settings.get("enable_parallel_checking", True)

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
                if self._past_window_end():
                    logger.info("⏰ WINDOW: end-of-window reached — halting stream check")
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
                self._mark_stream_done(stream_data.get('stream_id'))
                streams_processed_since_retry += 1
                tracker.update()

                # Process timeout retry queue every 4 streams
                if streams_processed_since_retry >= 4 and self.timeout_retry_queue:
                    retry_stream = self.timeout_retry_queue.pop(0)
                    retry_stream["retry_count"] += 1

                    if retry_stream["retry_count"] <= retries:
                        logger.info(f"Retrying timeout stream: '{retry_stream.get('channel_name')}' (attempt {retry_stream['retry_count']}/{retries})")
                        if delay > 0:
                            time.sleep(delay * 3)
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
            retry_backoff = delay * 3
            while self.timeout_retry_queue:
                if self._stop_event.is_set() or self._past_window_end():
                    logger.info("⏰ WINDOW: end-of-window reached — abandoning final-flush retries")
                    break
                retry_stream = self.timeout_retry_queue.pop(0)
                if retry_stream["retry_count"] < retries:
                    retry_stream["retry_count"] += 1
                    logger.info(f"Final retry for timeout stream: '{retry_stream.get('channel_name')}' (attempt {retry_stream['retry_count']}/{retries})")
                    if retry_backoff > 0:
                        time.sleep(retry_backoff)
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
            # In the `finally` on purpose: a pass that was cancelled or that
            # raised part way still probed the streams it got to, and a windowed
            # run that stops at its boundary is the NORMAL case here rather than
            # a failure. Counting only complete passes would undercount by
            # roughly the share of nights the window closes first.
            self._record_streams_checked(len(results), 'sequential', logger)
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
                    if self._past_window_end():
                        logger.info("⏰ WINDOW: end-of-window reached — cancelling remaining stream checks")
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
                        self._mark_stream_done(stream_data.get('stream_id'))

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
                        self._mark_stream_done(stream_data.get('stream_id'))

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
                        # Honor the schedule window: once the window has closed,
                        # do not start another retry pass — destructive actions
                        # downstream are gated on window completion, not retry
                        # exhaustion, and a 9s+probe loop here can run minutes
                        # past window-end (observed 14m overrun on May 1).
                        if self._past_window_end():
                            logger.info("⏰ WINDOW: end-of-window reached — skipping remaining retry passes")
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
                                if self._past_window_end():
                                    logger.info("⏰ WINDOW: end-of-window reached — cancelling in-flight retry probes")
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
            # See the sequential path: recorded in `finally` so a cancelled or
            # window-truncated pass still contributes the streams it probed.
            # Counted from results_dict, NOT from `results`: `results` is built
            # from it near the end of the try block, so an exception before that
            # line would report zero for a pass that probed thousands. Rows the
            # cancel path fabricated without probing anything are excluded.
            self._record_streams_checked(
                sum(1 for row in results_dict.values()
                    if (row or {}).get('error_type') != 'Cancelled'),
                'parallel', logger)
            self.check_progress['status'] = 'idle'
            self.check_progress['end_time'] = time.time()
            self._save_progress()
            tracker.finish()
            self._trigger_frontend_refresh(settings, logger)

    # --- Tag taxonomy (shared by black-flag handling and restore) -----------
    # Standard labels this plugin can append to a channel name. Used as a
    # defensive floor so a previously-applied standard tag is always strippable
    # even after the user edits their rename formats (same approach as the
    # issue-#18 suffix stripper).
    STANDARD_STATUS_TAGS = ('DEAD', 'Slow', 'Blank')
    STANDARD_QUALITY_TAGS = ('UHD', 'FHD', 'HD', 'SD', 'Unknown')

    @staticmethod
    def _is_low_framerate_row(result):
        """Row-level form of the low-framerate test, for use as a predicate."""
        try:
            return Plugin._is_low_framerate(float((result or {}).get('framerate_num', 0) or 0))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _channels_where(results, predicate, among=None):
        """channel_id -> channel_name for channels whose EVERY considered stream
        matches `predicate`, and which have at least one considered stream.

        WHY EVERY, NOT ANY. Check results are recorded per STREAM, while every
        rename, move and delete acts per CHANNEL. Selecting on any single row
        marked a channel dead because ONE of its streams failed, even when a
        backup stream was Alive. Dispatcharr fails over to that backup, so the
        channel plays perfectly, and it was eligible for rename, move and
        permanent deletion. Measured 2026-08-05.

        `Skipped` is not evidence in either direction and so fails the
        predicate, which excludes the channel. A channel with one Dead and one
        rate-limited stream has NOT been shown to be dead: the rate-limited
        stream may work fine. A not-proven-bad verdict never authorises an
        action.

        `among` optionally restricts which rows are considered, for judgements
        that only make sense over streams that actually play: a framerate is
        read from the Alive streams, because a dead stream cannot be played and
        says nothing about how the channel looks. A channel with NO considered
        rows is EXCLUDED rather than vacuously matching, which is what `all()`
        over an empty sequence would otherwise do.
        """
        considered = {}
        for row in (results or []):
            if not isinstance(row, dict):
                continue
            cid = row.get('channel_id')
            if cid is None:
                continue
            if among is not None and not among(row):
                continue
            considered.setdefault(cid, []).append(row)

        # No emptiness guard here on purpose: setdefault(...).append(row)
        # above means every key holds at least one row, so `rows` cannot be
        # empty. A channel whose rows were ALL filtered out by `among` never
        # becomes a key at all, which is what excludes it -- rather than
        # reaching an all() over an empty sequence, which returns True.
        out = {}
        for cid, rows in considered.items():
            if all(predicate(r) for r in rows):
                out[cid] = rows[0].get('channel_name')
        return out

    @staticmethod
    def _is_dead_nonblack(result):
        """Dead due to a probing failure, NOT a black/blank screen."""
        return result.get('status') == 'Dead' and result.get('error_type') != 'Black Screen'

    @staticmethod
    def _is_black_screen(result):
        """Marked Dead specifically because the stream is a black/blank screen."""
        return result.get('status') == 'Dead' and result.get('error_type') == 'Black Screen'

    @staticmethod
    def _extract_format_tags(fmt):
        """Pull bracketed labels out of a rename format (e.g. 'DEAD' from '{name} [DEAD]')."""
        if not fmt:
            return []
        return re.findall(r'\[([^\[\]]+)\]', fmt)

    @staticmethod
    def _compile_trailing_tag_re(tags):
        """Compile a case-insensitive regex matching one-or-more trailing ' [TAG]' groups."""
        labels = sorted({t.strip() for t in tags if t and t.strip()}, key=len, reverse=True)
        if not labels:
            return None
        pattern = r'(?:\s*\[(?:' + '|'.join(re.escape(t) for t in labels) + r')\])+\s*$'
        return re.compile(pattern, re.IGNORECASE)

    @classmethod
    def _derive_status_tags(cls, settings):
        """Compiled regex of PROBLEM tags only (DEAD/Slow/Blank + custom) — used for eligibility."""
        tags = list(cls.STANDARD_STATUS_TAGS)
        for key in ('dead_rename_format', 'low_framerate_rename_format', 'black_screen_rename_format'):
            tags.extend(cls._extract_format_tags(settings.get(key, '')))
        return cls._compile_trailing_tag_re(tags)

    @classmethod
    def _derive_strippable_tags(cls, settings):
        """Compiled regex of ALL tags this plugin can append (status + quality)."""
        tags = list(cls.STANDARD_STATUS_TAGS) + list(cls.STANDARD_QUALITY_TAGS)
        for key in ('dead_rename_format', 'low_framerate_rename_format', 'black_screen_rename_format'):
            tags.extend(cls._extract_format_tags(settings.get(key, '')))
        tags.extend(s.strip() for s in (settings.get('video_format_suffixes', '') or '').split(','))
        return cls._compile_trailing_tag_re(tags)

    @staticmethod
    def _compute_restore_plan(alive_names_by_id, state, strip_re, status_re, existing_group_ids):
        """Pure planner for the restore action.

        A channel is eligible iff it has stored original-group state OR its current
        name carries a trailing status tag ([DEAD]/[Slow]/[Blank] or custom). Quality
        tags alone never make a healthy channel eligible. Eligible channels have ALL
        plugin tags stripped from the name and are moved back to their original group
        when it still exists.
        """
        name_updates = []
        group_updates = []
        entries_to_clear = set()
        missing_group_ids = {}

        for cid, name in alive_names_by_id.items():
            entry = state.get(str(cid))
            has_state = entry is not None
            has_status_tag = bool(status_re and name and status_re.search(name))
            if not has_state and not has_status_tag:
                continue

            if strip_re and name:
                base = strip_re.sub('', name).rstrip()
                if base and base != name:
                    name_updates.append({'id': cid, 'name': base})

            if has_state:
                orig = entry.get('original_group_id')
                if orig is not None and orig in existing_group_ids:
                    group_updates.append({'id': cid, 'channel_group_id': orig})
                else:
                    missing_group_ids[str(cid)] = orig
                entries_to_clear.add(str(cid))

        return {
            'name_updates': name_updates,
            'group_updates': group_updates,
            'entries_to_clear': entries_to_clear,
            'missing_group_ids': missing_group_ids,
        }

    @staticmethod
    def _compute_capture_state(channel_ids, current_group_by_id, group_name_by_id,
                               managed_group_names, existing_state, now_iso):
        """Pure planner for original-state capture. Returns ONLY the new entries to add.

        Skips channels already tracked, channels with no current group (nothing to
        restore to), and channels currently sitting in a managed destination group (so
        a second move never records a dead/slow/black group as the 'original').
        """
        managed = {n.strip().lower() for n in managed_group_names if n and n.strip()}
        new_entries = {}
        for cid in channel_ids:
            key = str(cid)
            if key in existing_state:
                continue
            gid = current_group_by_id.get(cid)
            if gid is None:
                continue  # no current group -> nothing to restore TO; name-strip eligibility still covers it
            gname = group_name_by_id.get(gid, '')
            if gname and gname.strip().lower() in managed:
                continue
            new_entries[key] = {
                'original_group_id': gid,
                'original_group_name': gname,
                'moved_at': now_iso,
            }
        return new_entries

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

        dead_channels = self._channels_where(results, self._is_dead_nonblack)
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
        
        dead_channel_ids = set(self._channels_where(results, self._is_dead_nonblack))
        if not dead_channel_ids: return {"status": "ok", "message": "No dead channels were found in the last check."}

        try:
            self._capture_original_state(dead_channel_ids, settings, logger)
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

        dead_channel_ids = set(self._channels_where(
            results, lambda r: r.get('status') == 'Dead'))
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

            # Hygiene: drop original-state entries for channels we just deleted.
            try:
                state = self._load_json_file(self.channel_state_file) or {}
                if state:
                    for cid in dead_channel_ids:
                        state.pop(str(cid), None)
                    self._save_json_file(self.channel_state_file, state, indent=2)
            except Exception as e:
                logger.warning(f"Could not prune restore-state after delete: {e}")

            self._trigger_frontend_refresh(settings, logger)
            return {
                "status": "ok",
                "message": f"Permanently deleted {deleted_count} dead channels from the database. "
                           f"This action cannot be undone. GUI refresh triggered."
            }
        except Exception as e:
            return {"status": "error", "message": f"Error deleting channels: {str(e)}"}

    @staticmethod
    def _is_low_framerate(fps):
        """True when fps is a known, sub-threshold framerate (PAL 25 / film 24 are safe)."""
        try:
            fps = float(fps)
        except (TypeError, ValueError):
            return False
        return 0 < fps < PluginConfig.LOW_FRAMERATE_THRESHOLD

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

        low_fps_channels = self._channels_where(
            results, self._is_low_framerate_row,
            among=lambda r: r.get('status') == 'Alive')
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
        
        low_fps_channel_ids = set(self._channels_where(
            results, self._is_low_framerate_row,
            among=lambda r: r.get('status') == 'Alive'))
        if not low_fps_channel_ids: return {"status": "ok", "message": "No low framerate channels found to move."}

        try:
            self._capture_original_state(low_fps_channel_ids, settings, logger)
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

        # Recognized format tags that may already be appended to a channel name.
        # Always includes the standard set so a previously-applied tag is stripped
        # even if the user later narrows video_format_suffixes (issue #18).
        known_format_tags = {s for s in suffixes_to_add if s} | {'uhd', 'fhd', 'hd', 'sd', 'unknown'}
        # Matches one or more trailing " [TAG]" groups (case-insensitive).
        trailing_tag_re = re.compile(
            r'(?:\s*\[(?:' + '|'.join(re.escape(t) for t in known_format_tags) + r')\])+\s*$',
            re.IGNORECASE,
        )

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
                # Strip any previously-applied format tag(s) before re-appending so
                # quality changes replace the suffix instead of stacking (issue #18).
                base_name = trailing_tag_re.sub('', current_name).rstrip()
                new_name = base_name + suffix
                logger.debug(f"DEBUG:   - Current name: '{current_name}'")
                logger.debug(f"DEBUG:   - Base name (tags stripped): '{base_name}'")
                logger.debug(f"DEBUG:   - Will set suffix: '{suffix}'")

                if new_name == current_name:
                    logger.debug(f"DEBUG:   - Skipped: already has correct suffix '{suffix}'")
                    skipped_already_has_suffix += 1
                else:
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

    def _capture_original_state(self, channel_ids, settings, logger):
        """Persist each channel's current group as its 'original' before a move so
        restore can return it later. Never overwrites an existing entry and never
        records a managed destination group. Best-effort: never aborts the move.

        The state file is read-modify-written atomically (_save_json_file = tmp +
        os.replace), but two processes mutating it concurrently are last-writer-wins.
        That is acceptable: a lost capture only forfeits the exact-group restore for
        that channel (its name is still restored). Same accepted RMW model as
        pending_resume.json; moves are not actions users typically fire in parallel."""
        try:
            channel_ids = list(channel_ids)
            if not channel_ids:
                return
            current_group_by_id = {c['id']: c.get('channel_group_id') for c in self._get_all_channels(logger)}
            group_name_by_id = {g['id']: g['name'] for g in self._get_all_groups(logger)}
            managed_group_names = [
                settings.get('move_to_group_name', ''),
                settings.get('move_low_framerate_group', ''),
                settings.get('move_black_screen_group', ''),
            ]
            state = self._load_json_file(self.channel_state_file) or {}
            now_iso = datetime.utcnow().isoformat() + 'Z'
            new_entries = self._compute_capture_state(
                channel_ids, current_group_by_id, group_name_by_id,
                managed_group_names, state, now_iso,
            )
            if new_entries:
                state.update(new_entries)
                self._save_json_file(self.channel_state_file, state, indent=2)
                logger.info(f"Captured original group for {len(new_entries)} channel(s) before move.")
        except Exception as e:
            logger.warning(f"Could not capture original channel state (continuing): {e}")

    def rename_black_screen_channels_action(self, settings, logger):
        """Rename channels marked Dead specifically because they are a black/blank screen."""
        rename_format = settings.get("black_screen_rename_format", "{name} [Blank]").strip()
        if not rename_format:
            return {"status": "error", "message": "Please configure a Black-Screen Channel Rename Format."}
        if "{name}" not in rename_format:
            return {"status": "error", "message": "Black-Screen Channel Rename Format must contain {name} placeholder."}

        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first."}

        black_channels = self._channels_where(results, self._is_black_screen)
        if not black_channels:
            return {"status": "ok", "message": "No black-screen channels found in the last check."}

        payload = []
        for cid, name in black_channels.items():
            new_name = rename_format.replace('{name}', name)
            if new_name != name:
                payload.append({'id': cid, 'name': new_name})

        if not payload:
            return {"status": "ok", "message": "No channels needed renaming."}
        try:
            count = self._bulk_update_channels(payload, ['name'], logger)
            self._trigger_frontend_refresh(settings, logger)
            return {"status": "ok", "message": f"Successfully renamed {count} black-screen channels. GUI refresh triggered."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def move_black_screen_channels_action(self, settings, logger):
        """Move black/blank-screen channels to a dedicated group (captures original group first)."""
        group_name = settings.get("move_black_screen_group", "Black Screens").strip()
        if not group_name:
            return {"status": "error", "message": "Please enter a destination group name for black-screen channels."}

        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first."}

        black_channel_ids = set(self._channels_where(results, self._is_black_screen))
        if not black_channel_ids:
            return {"status": "ok", "message": "No black-screen channels found to move."}
        try:
            self._capture_original_state(black_channel_ids, settings, logger)
            dest_group = self._get_or_create_group(group_name, logger)
            payload = [{'id': cid, 'channel_group_id': dest_group.id} for cid in black_channel_ids]
            moved_count = self._bulk_update_channels(payload, ['channel_group_id'], logger)
            self._trigger_frontend_refresh(settings, logger)
            return {"status": "ok", "message": f"Successfully moved {moved_count} black-screen channels to group '{group_name}'. GUI refresh triggered."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def restore_channels_action(self, settings, logger):
        """Restore recovered channels: strip plugin name tags and move back to the
        original group for channels that are Alive again but were previously marked."""
        results = self._load_json_file(self.results_file)
        if results is None:
            return {"status": "error", "message": "No check results found (or data corrupted). Please run 'Check Streams' first.", "restored": 0}

        alive_ids = {r['channel_id'] for r in results if r.get('status') == 'Alive'}
        if not alive_ids:
            return {"status": "ok", "message": "No alive channels in the last check to restore.", "restored": 0}

        state = self._load_json_file(self.channel_state_file) or {}
        strip_re = self._derive_strippable_tags(settings)
        status_re = self._derive_status_tags(settings)

        alive_names_by_id = {c['id']: c['name'] for c in self._get_all_channels(logger) if c['id'] in alive_ids}
        existing_group_ids = {g['id'] for g in self._get_all_groups(logger)}

        plan = self._compute_restore_plan(alive_names_by_id, state, strip_re, status_re, existing_group_ids)

        affected = {u['id'] for u in plan['name_updates']} | {u['id'] for u in plan['group_updates']}
        if not affected and not plan['entries_to_clear']:
            return {"status": "ok", "message": "No recovered channels needed restoring.", "restored": 0}

        try:
            renamed = self._bulk_update_channels(plan['name_updates'], ['name'], logger)
            moved = self._bulk_update_channels(plan['group_updates'], ['channel_group_id'], logger)

            if plan['missing_group_ids']:
                logger.warning(
                    f"Restore: original group no longer exists for {len(plan['missing_group_ids'])} channel(s); "
                    f"name restored but left in current group: {sorted(plan['missing_group_ids'])}"
                )

            for key in plan['entries_to_clear']:
                state.pop(key, None)
            self._save_json_file(self.channel_state_file, state, indent=2)

            self._trigger_frontend_refresh(settings, logger)
            return {"status": "ok", "restored": len(affected),
                    "message": f"Restored {len(affected)} recovered channel(s): {renamed} renamed, {moved} moved back to original group. GUI refresh triggered."}
        except Exception as e:
            return {"status": "error", "message": str(e), "restored": 0}

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
        # The CSV is the authoritative audit record when destructive actions
        # follow, so it must record the MODE as well as the list: the same list
        # means opposite things in the two modes.
        csv_groups, csv_mode, csv_legacy_exclude = self._resolve_channel_groups(settings)
        lines.append(f"#   Channel Groups: {csv_groups or '(all groups)'}")
        lines.append(f"#   Channel Groups Mode: {csv_mode}")
        if csv_legacy_exclude:
            lines.append(f"#   Also excluded (old setting): {csv_legacy_exclude}")
        lines.append(f"#   Only Visible Channels: {settings.get('only_visible_channels', False)}")
        if settings.get('schedule_window_enabled', False):
            end_mode = settings.get('schedule_end_mode', 'duration')
            if end_mode == 'duration':
                lines.append(f"#   Schedule Mode: window (duration {settings.get('schedule_duration_hours', 4)}h, tz {self._dispatcharr_timezone()})")
            else:
                lines.append(f"#   Schedule Mode: window (until {settings.get('schedule_end_time', '04:00')}, tz {self._dispatcharr_timezone()})")
        lines.append(f"#   Connection Timeout: {settings.get('timeout', 10)} seconds")
        lines.append(f"#   Probe Timeout: {settings.get('probe_timeout', 20)} seconds")
        lines.append(f"#   Dead Connection Retries: {settings.get('dead_connection_retries', 3)}")
        lines.append(f"#   Dead Rename Format: {settings.get('dead_rename_format', '{name} [DEAD]')}")
        lines.append(f"#   Move Dead to Group: {settings.get('move_to_group_name', 'Graveyard')}")
        lines.append(f"#   Black-Screen Rename Format: {settings.get('black_screen_rename_format', '{name} [Blank]')}")
        lines.append(f"#   Move Black-Screen to Group: {settings.get('move_black_screen_group', 'Black Screens')}")
        lines.append(f"#   Low Framerate Rename Format: {settings.get('low_framerate_rename_format', '{name} [Slow]')}")
        lines.append(f"#   Move Low Framerate to Group: {settings.get('move_low_framerate_group', 'Slow')}")
        lines.append(f"#   Video Format Suffixes: {settings.get('video_format_suffixes', 'UHD, FHD, HD, SD, Unknown')}")
        lines.append(f"#   Parallel Checking Enabled: {settings.get('enable_parallel_checking', False)}")
        lines.append(f"#   Parallel Workers: {settings.get('parallel_workers', 2)}")
        lines.append(f"#   FFprobe Flags: {settings.get('ffprobe_flags', PluginConfig.DEFAULT_FFPROBE_FLAGS)}")
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
        low_fps_count = sum(1 for r in results if r.get('status') == 'Alive' and self._is_low_framerate(r.get('framerate_num', 0)))
        if low_fps_count > 0:
            lines.append(f"#   Low Framerate Streams (<{PluginConfig.LOW_FRAMERATE_THRESHOLD}fps): {low_fps_count}")

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

    @staticmethod
    def _compute_csv_fieldnames(results):
        """Ordered CSV columns: fixed base columns + any dynamic ffprobe_* keys.

        ffprobe_monitoring_seconds lives in base_fieldnames AND is emitted on each
        result under its ffprobe_-prefixed key, so it must be excluded from the
        auto-collected set or it lands in the header twice (bug-csv-dup-monitoring-col).
        """
        base_fieldnames = ['channel_id', 'channel_name', 'stream_id', 'status', 'format', 'framerate_num', 'error_type', 'error', 'retry_count', 'connection_timeout_seconds', 'probe_timeout_seconds', 'ffprobe_monitoring_seconds']
        ffprobe_fieldnames = set()
        for result in results:
            for key in result.keys():
                if key.startswith('ffprobe_') and key not in base_fieldnames:
                    ffprobe_fieldnames.add(key)
        return base_fieldnames + sorted(ffprobe_fieldnames)

    def _newsflasharr_settings(self, logger):
        """Newsflasharr's stored settings, or None when they cannot be read.

        The Django import is FUNCTION-LOCAL: a module-scope one breaks the
        plugin loader, which imports this file outside Dispatcharr in tests.

        None is distinct from an empty dict. It means the question could not be
        asked, so a caller must not conclude that the routing is fine.
        """
        try:
            from apps.plugins.models import PluginConfig as _PluginConfigModel
            row = _PluginConfigModel.objects.filter(key="newsflasharr").first()
        except Exception as e:
            logger.warning(f"Could not read Newsflasharr settings: {type(e).__name__}")
            return None
        if row is None:
            return None
        return row.settings or {}

    def _build_and_deliver_report(self, settings, logger, email):
        """Build the HTML report, then optionally queue it for delivery.

        Returns (written, problems) where `written` is write_report's dict and
        `problems` is a list of plain sentences. An empty list means nothing
        went wrong.

        THE REPORT IS BUILT FIRST, ALWAYS, and its path is returned even when
        the delivery half fails, so a routing mistake never costs the operator
        the report as well.

        A GREEN RESULT MUST NOT MEAN "wrote nothing". The counts come from the
        model, which exists before anything is written, so callers gate on a
        truthy html_path rather than on the counts.
        """
        from . import notify_report, reports

        results = self._load_json_file(self.results_file)
        if not results:
            return None, ["No check results found. Run Check Streams first."]

        now = time.time()
        model = reports.build_model(results, settings, now=now,
                                    version=getattr(self, "version", ""))
        model["plugin_dir"] = os.path.dirname(os.path.abspath(__file__))

        written = reports.write_report(model, PluginConfig.REPORT_DIR,
                                       PluginConfig.REPORT_DIR, now)
        if not written.get("html_path"):
            return written, ["The report could not be written. %s"
                             % (written.get("error") or "")]

        written["model"] = model
        if not email:
            return written, []

        nf_settings = self._newsflasharr_settings(logger)
        if nf_settings is None:
            return written, ["Newsflasharr settings could not be read, so where the "
                             "email would go is unknown. Is that plugin installed?"]

        # Refuse LOUDLY. Without a matching routing rule the event still spools
        # SUCCESSFULLY and is delivered somewhere else, which is
        # indistinguishable from working.
        ok, problems = notify_report.preflight(nf_settings)
        if not ok:
            return written, problems

        from .notify_client import notify
        queued, reason = notify_report.emit_report(
            notify, model, attachment_path=written["html_path"])
        if not queued:
            return written, ["The email was not queued. %s" % (reason or "")]
        return written, []

    def email_report_action(self, settings, logger):
        """Build the report and queue it for delivery by email.

        ONE BUTTON, NOT TWO. A build-only button beside an email-only button
        would run the same job and differ solely in whether the mail step runs.
        Pressing a button labelled Email Report means the operator wants it
        emailed, so this always tries.
        """
        written, problems = self._build_and_deliver_report(settings, logger, email=True)

        path = (written or {}).get("html_path")
        if not path:
            return {"status": "error",
                    "error": self._fit(" ".join(problems) or "The report could not be built.",
                                       PluginConfig.TOAST_BUDGET)}

        totals = (written.get("model") or {}).get("totals", {})
        headline = ("%s channels checked, %s playing, %s listed."
                    % (totals.get("channels", 0), totals.get("channels_working", 0),
                       totals.get("channels_listed", 0)))

        if problems:
            # The report exists; say so beside the path, and make the delivery
            # failure the RED persistent half so it is not missed.
            logger.warning("Report email problem: %s" % "; ".join(problems))
            return {"status": "error", "file": path,
                    "error": self._fit("Report written, but it will NOT arrive by email. "
                                       + " ".join(problems), PluginConfig.TOAST_BUDGET)}

        # "Queued for delivery", never "sent": notify() returning True means
        # durably SPOOLED, and Newsflasharr delivers later on its own retries.
        return {"status": "ok", "file": path,
                "message": self._fit("Report written and queued for delivery. " + headline,
                                     PluginConfig.TOAST_BUDGET)}

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
        fieldnames = self._compute_csv_fieldnames(results)

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

    def reset_progress_action(self, settings, logger):
        """Clear pending windowed-resume state so the next window starts fresh."""
        try:
            if os.path.exists(self.pending_resume_file):
                os.remove(self.pending_resume_file)
                logger.info("Pending resume state cleared")
                return {"status": "ok", "message": "✅ Resume progress reset. Next scheduled window will start fresh."}
            return {"status": "ok", "message": "No pending resume state to clear."}
        except Exception as e:
            return {"status": "error", "message": f"Failed to reset progress: {str(e)}"}

    def update_schedule_action(self, settings, logger):
        """Update the scheduler configuration and restart the scheduler."""
        try:
            scheduled_times_str = settings.get("scheduled_times", "").strip()
            scheduler_timezone = self._dispatcharr_timezone()
            
            # If scheduled times are empty, signal the elected scheduler to go idle
            # rather than tearing the loop down. Killing the thread leaves no
            # consumer for a future reload flag, so a later re-add via the UI
            # would be silently dropped until the next process restart.
            if not scheduled_times_str:
                logger.info("Scheduled times empty - signaling scheduler to idle")
                self._request_scheduler_reload()
                return {
                    "status": "ok",
                    "message": "✅ Schedule cleared. Scheduler has been stopped.\n\nTo enable scheduling, configure scheduled times in cron format."
                }
            
            # Validate scheduled times format (cron expressions)
            scheduled_times = self._parse_scheduled_times(scheduled_times_str)
            if not scheduled_times:
                return {
                    "status": "error",
                    "message": f"❌ Invalid cron expression format: '{scheduled_times_str}'\n\nPlease use cron format (e.g., '0 4 * * *' for daily at 4 AM).\nFormat: minute hour day month weekday\nSeparate several expressions with a semicolon."
                }
            
            # Timezone comes from Dispatcharr's global setting; only pytz is required.
            if not PYTZ_AVAILABLE:
                return {
                    "status": "error",
                    "message": "❌ Scheduler requires pytz library but it is not installed.\n\nPlease install pytz to use scheduling features."
                }
            
            # Restart scheduler with new settings. Only the elected scheduler-owner
            # process may touch the thread directly; non-owner workers signal via
            # the reload flag so we don't accumulate one rogue thread per uwsgi
            # worker that handles the UI request.
            logger.info(f"Updating schedule: Times={scheduled_times_str}, Timezone={scheduler_timezone}")
            if self._owns_scheduler_lock():
                self._start_background_scheduler(settings)
            else:
                self._request_scheduler_reload()
            
            # Build status message
            times_display = ', '.join(scheduled_times)  # Already strings (cron expressions)
            
            message = f"✅ Schedule updated successfully!\n\n"
            message += f"Cron Schedules: {times_display}\n"
            message += f"Timezone (from Dispatcharr): {scheduler_timezone}\n"
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
    
    def _humanize_cron(self, expr):
        """Convert a 5-field cron expression into a human-readable phrase."""
        parts = expr.strip().split()
        if len(parts) != 5:
            return expr
        minute, hour, dom, month, dow = parts

        day_names = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
                     "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun"}
        month_names = {"1": "Jan", "2": "Feb", "3": "Mar", "4": "Apr",
                       "5": "May", "6": "Jun", "7": "Jul", "8": "Aug",
                       "9": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

        def fmt_time(h, m):
            try:
                if any(c in h for c in "*/,-") or any(c in m for c in "*/,-"):
                    return None
                hi, mi = int(h), int(m)
                if not (0 <= hi < 24 and 0 <= mi < 60):
                    return None
                suffix = "AM" if hi < 12 else "PM"
                disp = hi % 12 or 12
                return f"{disp}:{mi:02d} {suffix}"
            except ValueError:
                return None

        def fmt_step(minute_field, hour_field):
            if hour_field == "*" and minute_field.startswith("*/"):
                step = minute_field[2:]
                if step.isdigit():
                    return f"every {step} minute{'s' if step != '1' else ''}"
            if minute_field == "0" and hour_field.startswith("*/"):
                step = hour_field[2:]
                if step.isdigit():
                    return f"every {step} hour{'s' if step != '1' else ''}"
            return None

        def fmt_dow(d):
            if d == "*":
                return "every day"
            if "-" in d and "/" not in d:
                a, b = d.split("-", 1)
                return f"{day_names.get(a, a)}–{day_names.get(b, b)}"
            if "," in d:
                return ", ".join(day_names.get(x, x) for x in d.split(","))
            return day_names.get(d, d)

        step_str = fmt_step(minute, hour)
        time_str = fmt_time(hour, minute)
        when = []
        if step_str:
            when.append(step_str)
        elif time_str:
            when.append(f"at {time_str}")
        else:
            return expr

        if dow != "*":
            when.append(f"on {fmt_dow(dow)}")
        elif dom != "*":
            if "/" in dom:
                _, step = dom.split("/", 1)
                when.append(f"every {step} days")
            else:
                when.append(f"on day {dom} of the month")
        else:
            when.append("daily")

        if month != "*":
            if "," in month:
                when.append("in " + ", ".join(month_names.get(x, x) for x in month.split(",")))
            else:
                when.append(f"in {month_names.get(month, month)}")

        return " ".join(when)

    def check_scheduler_status_action(self, settings, logger):
        """Compact scheduler status — fits in a single toast notification."""
        global _bg_scheduler_thread, _scheduler_pending_run

        try:
            # The scheduler runs in exactly ONE elected process; a UI status click
            # lands in whatever worker the load balancer picks — almost never the
            # owner (often daphne). Checking only the process-local
            # _bg_scheduler_thread reported "Not running" in every other worker even
            # when the scheduler was healthy (GitHub #25). Consult the cross-process
            # election lock file: a live holder PID means it's running somewhere.
            if _bg_scheduler_thread is not None and _bg_scheduler_thread.is_alive():
                thread_status = "✅ Running"
                scheduler_running = True
            else:
                holder_pid, holder_token, readable = self._read_scheduler_lock(
                    PluginConfig.SCHEDULER_LOCK_FILE)
                other_owner_alive = bool(
                    readable and holder_pid and holder_pid != os.getpid()
                    and self._scheduler_holder_alive(holder_pid, holder_token, _container_boot_token())
                )
                if other_owner_alive:
                    thread_status = f"✅ Running (owner PID {holder_pid})"
                    scheduler_running = True
                elif _bg_scheduler_thread is not None:
                    thread_status = "⚠️ Stopped"
                    scheduler_running = False
                else:
                    thread_status = "❌ Not running"
                    scheduler_running = False

            scheduled_times_str = settings.get("scheduled_times", "").strip()
            cron_lines = []
            if scheduled_times_str:
                for expr in self._parse_scheduled_times(scheduled_times_str):
                    cron_lines.append(f"  • {expr}  →  {self._humanize_cron(expr)}")
            else:
                cron_lines.append("  • (none configured)")

            tz_name = self._dispatcharr_timezone()
            now_str = "?"
            if PYTZ_AVAILABLE:
                try:
                    now_str = datetime.now(pytz.timezone(tz_name)).strftime('%Y-%m-%d %H:%M %Z')
                except Exception:
                    now_str = "(invalid tz)"

            check_status = self.check_progress.get('status', 'idle')
            check_line = check_status.title()
            if check_status == 'running':
                cur = self.check_progress.get('current', 0)
                tot = self.check_progress.get('total', 0)
                pct = (cur / tot * 100) if tot > 0 else 0
                check_line = f"Running ({cur}/{tot}, {pct:.1f}%)"

            if not scheduled_times_str:
                hint = "⚠️ Set cron expressions and click 💾 Save Schedule"
            elif not PYTZ_AVAILABLE:
                hint = "⚠️ pytz not available"
            elif not scheduler_running:
                hint = "⚠️ Click 📅 Save Schedule to start the scheduler"
            else:
                hint = "✅ Scheduler healthy"

            lines = [
                f"Scheduler: {thread_status}  |  Now: {now_str}",
                "Schedule:",
                *cron_lines,
                f"Queued run: {'yes' if _scheduler_pending_run else 'no'}  |  Check: {check_line}",
                hint,
            ]
            return {"status": "ok", "message": "\n".join(lines)}
            
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

    @staticmethod
    def _parse_blackdetect_output(stderr):
        # Parse ffmpeg blackdetect stderr into a list of (start, end, duration)
        # float tuples. Returns [] when no black segments are present.
        segments = []
        if not stderr:
            return segments
        pattern = re.compile(
            r'black_start:(?P<start>[\d.]+)\s+'
            r'black_end:(?P<end>[\d.]+)\s+'
            r'black_duration:(?P<dur>[\d.]+)'
        )
        for m in pattern.finditer(stderr):
            try:
                segments.append((
                    float(m.group('start')),
                    float(m.group('end')),
                    float(m.group('dur')),
                ))
            except (ValueError, TypeError):
                continue
        return segments

    @staticmethod
    def _parse_container_duration(probe_data):
        # Container duration in seconds, or None when the stream does not
        # report one. A continuous live mpegts has no duration at all; a
        # finite value means ffprobe is reading a FILE, not a broadcast.
        try:
            raw = (probe_data or {}).get('format') or {}
            raw = raw.get('duration')
        except AttributeError:
            return None
        if raw in (None, '', 'N/A'):
            return None
        try:
            seconds = float(raw)
        except (ValueError, TypeError):
            return None
        if seconds != seconds or seconds <= 0:   # NaN or non-positive
            return None
        return seconds

    @staticmethod
    def _parse_container_bitrate_kbps(probe_data):
        # Container-level bit_rate in whole kbps, or None. Recorded as
        # evidence beside the duration: the provider placeholder measured on
        # 2026-07-11 reported 192953 bps on every one of 18 dead channels.
        try:
            raw = (probe_data or {}).get('format') or {}
            raw = raw.get('bit_rate')
        except AttributeError:
            return None
        if raw in (None, '', 'N/A'):
            return None
        try:
            return int(round(float(raw) / 1000.0))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _is_placeholder_file(probe_data):
        # True when a stream reports a fixed container duration. Measured on a
        # full ffprobe sweep of 156 provider channels (2026-07-11): all 138
        # healthy channels reported no duration, while 19 reported one -- 18 of
        # them the same 10-minute black placeholder file reused under many
        # content ids, the 19th a 22.6 second file on a 24/7 channel.
        #
        # Deliberately NOT gated on resolution or on the exact bitrate. All 18
        # dead channels were 1920x1080, but so were ten HEALTHY ones, so frame
        # size discriminates nothing; and requiring the exact 192953 bps would
        # miss the short file, which reported no bitrate at all.
        return Plugin._parse_container_duration(probe_data) is not None

    @staticmethod
    def _parse_freezedetect_output(stderr):
        # Parse ffmpeg freezedetect stderr into a list of freeze start times.
        # Returns [] when the picture was moving.
        #
        # Real captured output (ffmpeg 8.1.2, lavfi color=red still source):
        #   [Parsed_freezedetect_1 @ 0x70f0a8003240] lavfi.freezedetect.freeze_start: 0
        # A freeze running to end-of-capture prints freeze_start with NO
        # freeze_end, so keying on freeze_end would miss the common case.
        starts = []
        if not stderr:
            return starts
        pattern = re.compile(r'lavfi\.freezedetect\.freeze_start:\s*(?P<start>[\d.]+)')
        for m in pattern.finditer(stderr):
            try:
                starts.append(float(m.group('start')))
            except (ValueError, TypeError):
                continue
        return starts

    @staticmethod
    def _parse_mean_volume_db(stderr):
        # Parse volumedetect's mean_volume in dBFS, or None when absent.
        #
        # `-?(?:inf|[\d.]+)` is deliberate: ffmpeg documents `mean_volume:
        # -inf dB` for digital silence, and a regex that cannot read it
        # returns None, which is byte-identical to "no audio measured at all"
        # and would fail OPEN on the strongest possible evidence of silence.
        # Measured on ffmpeg 8.1.2, both anullsrc and aevalsrc=0 printed
        # -91.0 rather than -inf, matching Sentinelarr's field measurement.
        if not stderr:
            return None
        m = re.search(r'mean_volume:\s*(-?(?:inf|[\d.]+))\s*dB', stderr)
        if not m:
            return None
        try:
            return float(m.group(1))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _is_silent_audio(mean_db, threshold_db):
        # True only on POSITIVE evidence of silence. An unmeasured level
        # (None) is NOT silence: no measurement never authorizes an action.
        if mean_db is None:
            return False
        try:
            return float(mean_db) <= float(threshold_db)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _effective_freeze_seconds(min_seconds, sample_seconds):
        # freezedetect only reports once a freeze has lasted d seconds, so a
        # d greater than or equal to the sample length can NEVER mature: the
        # setting would look active while being structurally unable to fire.
        # Clamp it to fit inside the sample instead of obeying it literally.
        try:
            wanted = int(float(min_seconds))
        except (TypeError, ValueError):
            wanted = PluginConfig.DEFAULT_FREEZE_MIN_SECONDS
        try:
            sample = int(float(sample_seconds))
        except (TypeError, ValueError):
            sample = PluginConfig.DEFAULT_BLACK_SAMPLE_SECONDS
        return max(1, min(wanted, sample - 1))

    def _analyze_stream_content(self, url, timeout, settings, logger,
                                want_freeze=False, want_audio=False):
        # ONE ffmpeg decode pass answering up to three questions: is the
        # picture black, is it frozen, and how loud is the audio. Running them
        # together is what keeps the cost at a single provider connection when
        # several detectors are enabled; verified against ffmpeg 8.1.2 that
        # blackdetect, freezedetect and volumedetect all report from one pass.
        #
        # Returns {'black': ..., 'frozen': ..., 'audio_db': ...}. Each verdict
        # is True/False, or None meaning NOT MEASURED or NOT DECIDABLE -- and
        # None never authorizes an action, so the caller leaves the stream
        # Alive. Never raises.
        unknown = {'black': None, 'frozen': None, 'audio_db': None}
        s = settings or {}
        ffmpeg_path = s.get('ffmpeg_path', '/usr/local/bin/ffmpeg')
        sample_seconds = s.get('black_screen_sample_seconds', PluginConfig.DEFAULT_BLACK_SAMPLE_SECONDS)
        min_black = s.get('black_screen_min_black_seconds', 3)
        ffmpeg_timeout = s.get('black_screen_ffmpeg_timeout', 20)

        # blackdetect stays FIRST in the chain so its verdict is always
        # available for precedence: a black screen is also a still picture, so
        # freezedetect fires on it too and the two must not be confused.
        video_filters = [f'blackdetect=d={min_black}:pic_th=0.98']
        if want_freeze:
            freeze_seconds = self._effective_freeze_seconds(
                s.get('frozen_video_min_seconds', PluginConfig.DEFAULT_FREEZE_MIN_SECONDS),
                sample_seconds,
            )
            video_filters.append(f'freezedetect=n=-60dB:d={freeze_seconds}')

        # Input options (-user_agent, -rw_timeout) MUST precede -i or ffmpeg
        # silently ignores them. -loglevel info is required: blackdetect,
        # freezedetect and volumedetect ALL log at info level, so
        # -loglevel error would suppress every one of them.
        cmd = [
            ffmpeg_path,
            '-hide_banner', '-nostats', '-loglevel', 'info',
            '-user_agent', 'VLC/3.0.21 LibVLC/3.0.21',
            '-rw_timeout', str(int(timeout) * 1000000),
            '-i', url,
            '-t', str(sample_seconds),
        ]
        if want_audio:
            cmd.extend(['-af', 'volumedetect'])
        else:
            cmd.append('-an')
        cmd.extend(['-vf', ','.join(video_filters)])
        cmd.extend(['-f', 'null', '-'])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=ffmpeg_timeout
            )
        except FileNotFoundError:
            logger.warning(f"[Content Check] ffmpeg not found at {ffmpeg_path}; leaving stream Alive")
            return dict(unknown)
        except subprocess.TimeoutExpired:
            logger.warning(f"[Content Check] ffmpeg timed out after {ffmpeg_timeout}s; leaving stream Alive")
            return dict(unknown)
        except Exception as e:
            logger.warning(f"[Content Check] ffmpeg error ({e}); leaving stream Alive")
            return dict(unknown)

        stderr = result.stderr or ''
        clean_exit = result.returncode == 0

        verdicts = dict(unknown)

        if self._parse_blackdetect_output(stderr):
            verdicts['black'] = True
        elif clean_exit:
            verdicts['black'] = False

        if want_freeze:
            if self._parse_freezedetect_output(stderr):
                verdicts['frozen'] = True
            elif clean_exit:
                verdicts['frozen'] = False

        if want_audio:
            verdicts['audio_db'] = self._parse_mean_volume_db(stderr)

        return verdicts

    def check_stream(self, stream_data, timeout, retries, logger, skip_retries=False, settings=None, retry_attempt=0):
        """Check individual stream status with optional retries."""
        url, channel_name = stream_data.get('stream_url'), stream_data.get('channel_name')
        stream_id = stream_data.get('stream_id', 'unknown')
        last_error = "Unknown error"
        last_error_type = "Other"

        # Honor rate-limit cooldown for every probe (covers sequential, parallel,
        # and retry call sites without each having to remember to call it).
        self._rate_limit_guard.wait_if_throttled(logger, self._stop_event)

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
        ffprobe_flags_str = settings.get('ffprobe_flags', PluginConfig.DEFAULT_FFPROBE_FLAGS) if settings else PluginConfig.DEFAULT_FFPROBE_FLAGS
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
                                if len(video_packets) >= PluginConfig.MIN_PACKETS_FOR_BITRATE_CALC:
                                    total_size = sum(int(p.get('size', 0)) for p in video_packets)
                                    total_duration = sum(float(p.get('duration_time') or 0) for p in video_packets)
                                    if total_duration > 0:
                                        video_bitrate = (total_size * 8) / (total_duration * 1000)
                                        ffprobe_extra_data['calculated_bitrate_kbps'] = video_bitrate

                        # Round video_bitrate to nearest whole kbps before handing
                        # to Dispatcharr — the channel-menu UI displays it as an
                        # integer, so storing fractions just adds noise to the
                        # stream_stats jsonb and to CSV exports.
                        if video_bitrate is not None:
                            video_bitrate = int(round(video_bitrate))

                        stream_format = self._get_stream_format(resolution)

                        # Placeholder-file evidence. Recorded for EVERY stream
                        # that reports a container duration, whether or not the
                        # detection setting is on, so the operator can see the
                        # fingerprint in the CSV before deciding to act on it.
                        # Costs nothing: -show_format is already requested and
                        # the format block is already parsed above.
                        container_duration = self._parse_container_duration(probe_data)
                        if container_duration is not None:
                            ffprobe_extra_data['container_duration_seconds'] = container_duration
                            container_bitrate = self._parse_container_bitrate_kbps(probe_data)
                            if container_bitrate is not None:
                                ffprobe_extra_data['container_bitrate_kbps'] = container_bitrate

                        # Optional placeholder-file classification. A live
                        # stream reporting a fixed duration is serving a finite
                        # file, which on this provider is a black slate reused
                        # across many dead channels. Null metadata mirrors
                        # every other Dead stream so the DB stats get cleared
                        # (see _update_dispatcharr_metadata all_none).
                        if (settings and settings.get('placeholder_file_detection')
                                and self._is_placeholder_file(probe_data)):
                            logger.info(
                                f"✗ '{channel_name}' DEAD - Placeholder File "
                                f"({container_duration:.1f}s fixed duration)"
                            )
                            placeholder_return = dict(default_return)
                            placeholder_return['dispatcharr_metadata'] = {k: None for k in default_return['dispatcharr_metadata']}
                            placeholder_return['error'] = (
                                f'Stream reports a fixed {container_duration:.1f}s duration; '
                                'a continuous live stream reports none'
                            )
                            placeholder_return['error_type'] = 'Placeholder File'
                            placeholder_return['ffprobe_data'] = ffprobe_extra_data
                            return placeholder_return

                        # Optional content verification. An Alive-by-ffprobe
                        # stream can still decode to a black picture, a frozen
                        # picture, or silence. All three verdicts come from ONE
                        # ffmpeg decode pass, so enabling several costs a single
                        # provider connection rather than one each.
                        #
                        # Fail-open throughout: any ffmpeg problem yields None,
                        # and None never marks a stream Dead. Every Dead return
                        # nulls its metadata so the DB stats get cleared (see
                        # _update_dispatcharr_metadata all_none).
                        s = settings or {}
                        want_black = bool(s.get('black_screen_detection'))
                        want_freeze = bool(s.get('frozen_video_detection'))
                        # A stream with no audio track cannot be silent; that is
                        # a different fault and this detector must not claim it.
                        want_audio = bool(s.get('silent_audio_detection')) and audio_stream is not None

                        if (want_black or want_freeze or want_audio) and not self._stop_event.is_set():
                            verdicts = self._analyze_stream_content(
                                url, timeout, settings, logger,
                                want_freeze=want_freeze, want_audio=want_audio,
                            )

                            def _dead(error_type, message):
                                dead_return = dict(default_return)
                                dead_return['dispatcharr_metadata'] = {k: None for k in default_return['dispatcharr_metadata']}
                                dead_return['error'] = message
                                dead_return['error_type'] = error_type
                                dead_return['ffprobe_data'] = ffprobe_extra_data
                                return dead_return

                            # Precedence matters. A black screen is ALSO a still
                            # picture, so freezedetect fires on it too; blank
                            # screen must win or enabling freeze detection would
                            # silently relabel every blank screen and break the
                            # blank-screen rename/move actions, which match on
                            # error_type 'Black Screen'.
                            if want_black and verdicts['black'] is True:
                                logger.info(f"✗ '{channel_name}' DEAD - Black Screen ({resolution})")
                                return _dead('Black Screen', 'Stream decodes to a black screen')

                            if want_freeze and verdicts['frozen'] is True:
                                logger.info(f"✗ '{channel_name}' DEAD - Frozen Video ({resolution})")
                                return _dead(
                                    'Frozen Video',
                                    'Stream decodes to a frozen (unchanging) picture',
                                )

                            if want_audio:
                                threshold_db = s.get('silent_audio_max_db', PluginConfig.DEFAULT_SILENT_AUDIO_MAX_DB)
                                mean_db = verdicts['audio_db']
                                if mean_db is not None:
                                    ffprobe_extra_data['mean_volume_db'] = mean_db
                                if self._is_silent_audio(mean_db, threshold_db):
                                    logger.info(f"✗ '{channel_name}' DEAD - Silent Audio ({mean_db} dB)")
                                    return _dead(
                                        'Silent Audio',
                                        f'Audio track is silent (mean volume {mean_db} dB, '
                                        f'threshold {threshold_db} dB)',
                                    )

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
                    elif ('too many requests' in error_lower
                          or 'rate limit' in error_lower
                          or re.search(r'\b429\b', error_output)):
                        last_error_type = 'Rate Limited'
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

        # Mask URL in error message before returning
        masked_error = self._mask_url_in_error(last_error, url, stream_id)

        # Rate-limited responses are not real failures — classify as Skipped so
        # destructive actions (rename/move/delete) leave the stream alone, and
        # notify the rate-limit guard to back off subsequent checks.
        if last_error_type == 'Rate Limited':
            self._rate_limit_guard.record_hit(logger)
            logger.info(f"⤼ '{channel_name}' SKIPPED - Rate Limited (HTTP 429)")
            default_return['status'] = 'Skipped'
            default_return['error'] = masked_error
            default_return['error_type'] = 'Rate Limited'
            return default_return

        # Audio-only streams (e.g. radio stations) return "No video stream found"
        # with a clean ffprobe exit — they are working streams that simply carry no
        # video track, not dead channels. Classify as Skipped so destructive actions
        # (rename/move/delete) leave them alone.
        if last_error_type == 'No Video Stream':
            logger.info(f"⤼ '{channel_name}' SKIPPED - No Video Stream (audio-only)")
            default_return['status'] = 'Skipped'
            default_return['error'] = masked_error
            default_return['error_type'] = 'No Video Stream'
            return default_return

        # Log final result once if stream is dead after all attempts
        logger.info(f"✗ '{channel_name}' DEAD - {last_error_type}")

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
                    # The Stream model carrying stream_stats lives in
                    # apps.channels.models (it never lived under apps.proxy.*);
                    # the old apps.proxy.ts_proxy.models path always ImportError'd
                    # and silently disabled this dead-stream cleanup path.
                    from apps.channels.models import Stream as ProxyStream
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
