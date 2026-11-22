"""One View Status button replaces View Check Progress and View Last Results.

- view_status shows the live check while one runs, else the last results.
- A check whose progress file has gone quiet shows its progress line AND the
  last results, because it may have ended without writing a final state.
- The two old action ids stay routed so an old page or caller still gets an answer.
"""
import io
import json
import os
import time
from pathlib import Path

import pytest

from iptv_checker.plugin import PluginConfig


PLUGIN_JSON = Path(__file__).resolve().parent.parent / "iptv_checker" / "plugin.json"
PLUGIN_PY = Path(__file__).resolve().parent.parent / "iptv_checker" / "plugin.py"
_DATA = json.load(io.open(PLUGIN_JSON, encoding="utf-8"))
_IDS = {a["id"] for a in _DATA["actions"]}


@pytest.fixture(autouse=True)
def _no_load_running(plugin):
    """The conftest fixture skips __init__, which is where load_progress is set."""
    plugin.load_progress = {"current": 0, "total": 0, "status": "idle"}


def _write_results(plugin, rows):
    with open(plugin.results_file, "w") as f:
        json.dump(rows, f)


def _write_progress(plugin, data, age_seconds=0.0):
    with open(plugin.progress_file, "w") as f:
        json.dump(data, f)
    when = time.time() - age_seconds
    os.utime(plugin.progress_file, (when, when))


# --- declaration and routing ---------------------------------------------------

def test_view_status_is_the_declared_button():
    assert "view_status" in _IDS


def test_old_view_buttons_are_no_longer_declared():
    assert "view_progress" not in _IDS
    assert "view_results" not in _IDS


def test_old_action_ids_stay_routed():
    source = io.open(str(PLUGIN_PY), encoding="utf-8").read()
    start = source.index("action_map = {")
    end = source.index("}", start)
    block = source[start:end]
    for action_id in ("view_status", "view_progress", "view_results"):
        assert '"%s":' % action_id in block, action_id


# --- view_status behaviour ------------------------------------------------------

def test_idle_with_results_shows_last_results(plugin, quiet_logger):
    _write_results(plugin, [{"status": "Alive", "format": "HD", "framerate_num": 30}])
    out = plugin.view_status_action({}, quiet_logger)
    assert out["status"] == "ok"
    assert "Last Check Results" in out["message"]


def test_idle_without_results_says_so(plugin, quiet_logger):
    out = plugin.view_status_action({}, quiet_logger)
    assert "No results available yet" in out["message"]


def test_running_fresh_shows_progress_only(plugin, quiet_logger):
    _write_results(plugin, [{"status": "Alive", "format": "HD", "framerate_num": 30}])
    _write_progress(plugin, {"status": "running", "current": 5, "total": 10,
                             "start_time": time.time()})
    out = plugin.view_status_action({}, quiet_logger)
    assert "Checking streams 5/10" in out["message"]
    assert "Last Check Results" not in out["message"]


def test_running_stale_also_shows_last_results(plugin, quiet_logger):
    _write_results(plugin, [{"status": "Alive", "format": "HD", "framerate_num": 30}])
    age = PluginConfig.PROGRESS_STALE_AFTER_SECONDS + 60
    _write_progress(plugin, {"status": "running", "current": 5, "total": 10,
                             "start_time": time.time() - age}, age_seconds=age)
    out = plugin.view_status_action({}, quiet_logger)
    assert "Checking streams 5/10" in out["message"]
    assert "No progress update" in out["message"]
    assert "Last Check Results" in out["message"]


def test_stale_check_is_judged_by_the_progress_file_not_the_status(plugin, quiet_logger):
    _write_progress(plugin, {"status": "running", "current": 1, "total": 2,
                             "start_time": time.time()})
    assert plugin._progress_file_is_stale() is False
    old = time.time() - (PluginConfig.PROGRESS_STALE_AFTER_SECONDS + 1)
    os.utime(plugin.progress_file, (old, old))
    assert plugin._progress_file_is_stale() is True


def test_missing_progress_file_counts_as_stale(plugin):
    assert not os.path.exists(plugin.progress_file)
    assert plugin._progress_file_is_stale() is True


# --- old ids still answer -------------------------------------------------------

def test_view_progress_still_answers(plugin, quiet_logger):
    out = plugin.view_progress_action({}, quiet_logger)
    assert out["status"] == "ok" and out["message"]


def test_view_results_still_answers(plugin, quiet_logger):
    _write_results(plugin, [{"status": "Dead", "format": "SD"}])
    out = plugin.view_results_action({}, quiet_logger)
    assert "Last Check Results" in out["message"]


def test_check_start_message_names_view_status(plugin, quiet_logger):
    src = io.open(str(PLUGIN_PY), encoding="utf-8").read()
    assert "View Check Progress" not in src
    assert "View Last Results" not in src
    assert "Use View Status to monitor." in src
