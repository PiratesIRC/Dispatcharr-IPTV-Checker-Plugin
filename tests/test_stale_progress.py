"""Clearing a stuck progress file must never clear a LIVE run.

Two situations look identical in `progress.json`, and telling them apart is the
whole job:

  DEBRIS. A container kill bypasses the `finally` block that flips status to
  idle, so the file is left saying 'running' forever. Every later cron fire then
  self-queues believing a check is in flight, and the schedule never runs again.
  This is why the routine exists.

  A LIVE RUN. Another Dispatcharr worker constructs a Plugin instance while the
  elected worker is mid-check. Until 2026-08-07 the routine assumed this could
  not happen -- its comment read "At __init__ time no thread can possibly be
  running, so it's safe to normalize" -- which is true within one process and
  false across processes.

Measured on the live install on 2026-08-08: two plugin discovery passes at
04:24:58 and 04:29:31 each re-imported the module in another worker and clamped
a running check to idle, at 418/2691 and 488/2691 streams. The check itself
carried on and finished, so the damage was limited, but while the file says idle
a cron fire will not defer to the run that is actually in progress.

The signal is the container boot token already used by the scheduler election
lock: a progress file stamped by a PREVIOUS container cannot belong to a running
check. Within one container life, an mtime older than
PluginConfig.PROGRESS_STALE_AFTER_SECONDS is used instead, which covers a worker
killed without taking the container down.
"""
import json
import os
import time

import pytest


@pytest.fixture
def progress(plugin, pmod, monkeypatch):
    """Write a progress file and pin the current container boot token."""
    monkeypatch.setattr(pmod, "_container_boot_token", lambda: "thisboot:100")

    def write(status="running", token="thisboot:100", age_seconds=0, current=418, total=2691):
        data = {"current": current, "total": total, "status": status,
                "start_time": time.time() - 600}
        if token is not None:
            data["boot_token"] = token
        with open(plugin.progress_file, "w") as fh:
            json.dump(data, fh)
        if age_seconds:
            old = time.time() - age_seconds
            os.utime(plugin.progress_file, (old, old))
        return data

    return write


def _status_on_disk(plugin):
    with open(plugin.progress_file) as fh:
        return json.load(fh)["status"]


def test_a_running_check_from_this_container_is_left_alone(plugin, progress):
    """The 2026-08-08 incident. Another worker constructs a Plugin instance
    while the elected worker is mid-check; the file is fresh and stamped by this
    container, so it belongs to a run that is genuinely in progress."""
    progress(status="running", token="thisboot:100", age_seconds=0)

    plugin._normalize_stale_progress()

    assert _status_on_disk(plugin) == "running"


def test_a_running_file_from_a_previous_container_is_cleared(plugin, progress):
    """The original defect this routine exists for: a container kill leaves the
    file saying running forever, and every later cron fire self-queues."""
    progress(status="running", token="oldboot:99", age_seconds=0)

    plugin._normalize_stale_progress()

    assert _status_on_disk(plugin) == "idle"


def test_a_running_file_with_no_token_is_cleared(plugin, progress):
    """Files written before this release carry no token. Treating them as
    debris restores the previous behaviour for them, which is the safe default:
    the alternative leaves a genuinely stuck file stuck forever."""
    progress(status="running", token=None, age_seconds=0)

    plugin._normalize_stale_progress()

    assert _status_on_disk(plugin) == "idle"


def test_a_stalled_run_in_this_container_is_cleared_once_it_goes_quiet(plugin, pmod, progress):
    """A worker killed without taking the container down leaves a file with a
    matching token that nothing is updating any more. The check writes progress
    continuously, so silence for longer than the threshold means no live run."""
    progress(status="running", token="thisboot:100",
             age_seconds=pmod.PluginConfig.PROGRESS_STALE_AFTER_SECONDS + 60)

    plugin._normalize_stale_progress()

    assert _status_on_disk(plugin) == "idle"


def test_a_recent_write_below_the_threshold_is_left_alone(plugin, pmod, progress):
    """Just under the threshold is still a live run. The threshold has to clear
    the gap between two progress writes, which widens with slow probes."""
    progress(status="running", token="thisboot:100",
             age_seconds=max(pmod.PluginConfig.PROGRESS_STALE_AFTER_SECONDS - 60, 1))

    plugin._normalize_stale_progress()

    assert _status_on_disk(plugin) == "running"


def test_the_threshold_is_wider_than_the_slowest_progress_cadence(pmod):
    """ProgressTracker writes every 10 seconds on the largest jobs, and a slow
    probe widens the real gap well beyond that. A threshold near the cadence
    would clear live runs."""
    assert pmod.PluginConfig.PROGRESS_STALE_AFTER_SECONDS >= 300


def test_an_idle_file_is_never_rewritten(plugin, progress):
    """Nothing to normalize means nothing to write. Rewriting would move the
    mtime and make the next caller think a run had just reported in."""
    progress(status="idle", token="oldboot:99", age_seconds=0)
    before = os.path.getmtime(plugin.progress_file)

    plugin._normalize_stale_progress()

    assert os.path.getmtime(plugin.progress_file) == before


def test_a_missing_file_is_not_an_error(plugin):
    if os.path.exists(plugin.progress_file):
        os.unlink(plugin.progress_file)
    plugin._normalize_stale_progress()  # must not raise


def test_a_corrupt_file_is_not_an_error(plugin):
    with open(plugin.progress_file, "w") as fh:
        fh.write("{not json")
    plugin._normalize_stale_progress()  # must not raise


def test_saved_progress_carries_the_current_boot_token(plugin, pmod, monkeypatch):
    """The token has to be written for any of the above to work. Without it
    every file looks like debris and the incident repeats."""
    monkeypatch.setattr(pmod, "_container_boot_token", lambda: "thisboot:100")
    plugin.check_progress = {"current": 5, "total": 10, "status": "running"}

    plugin._save_progress()

    with open(plugin.progress_file) as fh:
        assert json.load(fh)["boot_token"] == "thisboot:100"
