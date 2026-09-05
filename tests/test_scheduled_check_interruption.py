"""A scheduler stop request must not make a running scan look finished.

The scan runs in its own background thread. The scheduled check waits for it on
a condition that is true while the check status is running AND the module level
scheduler stop event is clear. Stopping or restarting the scheduler sets that
same event, so the wait ends while the scan thread is still running, and
execution used to continue into the CSV export, the emailed report and the
rename, move, restore and delete actions.

Those phases do not act on a partial results file. The results file is written
exactly once, at the end of each scan, so while a scan is in flight it still
holds the PREVIOUS run's complete results. The fault is therefore that the
post-scan phases re-apply the previous run's verdicts at an arbitrary later
time, which can delete a channel that has since recovered.

The discriminator is whether this session wrote new results, measured by the
results file modification time before the scan against the same value after the
wait. A status value cannot be used: the in-memory copy is process local, and
the on-disk copy is written a moment after the status flips, so a scan that
genuinely finished can briefly read as still running and would lose its report.
"""

import pytest


def test_new_results_written_during_the_session_runs_the_phases(plugin):
    assert plugin._post_scan_phases_should_run(100.0, 200.0) is True


def test_unchanged_results_file_does_not_run_the_phases(plugin):
    """The scan never wrote results, so the file still holds the previous run."""
    assert plugin._post_scan_phases_should_run(100.0, 100.0) is False


def test_results_file_older_afterwards_does_not_run_the_phases(plugin):
    assert plugin._post_scan_phases_should_run(200.0, 100.0) is False


def test_results_file_created_during_the_session_runs_the_phases(plugin):
    """No results file existed before, and the scan wrote one."""
    assert plugin._post_scan_phases_should_run(None, 200.0) is True


def test_results_file_absent_afterwards_does_not_run_the_phases(plugin):
    assert plugin._post_scan_phases_should_run(100.0, None) is False


def test_results_file_absent_throughout_does_not_run_the_phases(plugin):
    """No evidence that anything was probed is not evidence that it was."""
    assert plugin._post_scan_phases_should_run(None, None) is False


def test_results_mtime_returns_none_when_the_file_is_missing(plugin):
    assert plugin._results_file_mtime() is None


def test_results_mtime_returns_the_modification_time_when_present(plugin, tmp_path):
    import os

    with open(plugin.results_file, "w") as handle:
        handle.write("[]")
    assert plugin._results_file_mtime() == pytest.approx(
        os.path.getmtime(plugin.results_file)
    )


# --- the predicate must actually gate the post-scan phases -------------------

_PHASE_METHODS = (
    "export_results_action",
    "restore_channels_action",
    "rename_channels_action",
    "rename_low_framerate_channels_action",
    "rename_black_screen_channels_action",
    "add_video_format_suffix_action",
    "move_dead_channels_action",
    "move_low_framerate_channels_action",
    "move_black_screen_channels_action",
    "delete_dead_channels_action",
    "_build_and_deliver_report",
)

_ALL_PHASE_GATES = {
    "scheduler_email_report": True,
    "scheduler_restore_channels": True,
    "scheduler_rename_dead_channels": True,
    "scheduler_rename_low_framerate_channels": True,
    "scheduler_rename_black_screen_channels": True,
    "scheduler_add_video_format_suffix": True,
    "scheduler_move_dead_channels": True,
    "scheduler_move_low_framerate_channels": True,
    "scheduler_move_black_screen_channels": True,
    "scheduler_delete_dead_channels": True,
}


def _arrange(plugin, monkeypatch, scan_writes_results):
    """Drive _execute_scheduled_check with every phase replaced by a recorder.

    scan_writes_results mimics a scan that reached the end and saved results.
    When it is False the results file keeps the modification time it had before
    the scan, which is what an interrupted scan leaves behind.
    """
    import os

    calls = []
    with open(plugin.results_file, "w") as handle:
        handle.write("[]")
    os.utime(plugin.results_file, (1000.0, 1000.0))

    def make_recorder(name):
        def recorder(*args, **kwargs):
            calls.append(name)
            # The report caller unpacks two values; every other caller reads a dict.
            if name == "_build_and_deliver_report":
                return None, []
            return {"status": "ok", "message": "", "restored": 0}
        return recorder

    for name in _PHASE_METHODS:
        monkeypatch.setattr(plugin, name, make_recorder(name))

    monkeypatch.setattr(
        plugin, "load_groups_action",
        lambda *a, **k: {"status": "ok", "message": "loaded"},
    )

    def fake_check(*args, **kwargs):
        if scan_writes_results:
            os.utime(plugin.results_file, (2000.0, 2000.0))
        return {"status": "ok", "message": "started"}

    monkeypatch.setattr(plugin, "check_streams_action", fake_check)
    plugin.check_progress = {"status": "idle"}
    return calls


def test_interrupted_scan_skips_every_post_scan_phase(plugin, monkeypatch):
    """The results file still holds the previous run, so nothing may act on it."""
    calls = _arrange(plugin, monkeypatch, scan_writes_results=False)

    plugin._execute_scheduled_check(dict(_ALL_PHASE_GATES))

    assert calls == []


def test_completed_scan_runs_every_post_scan_phase(plugin, monkeypatch):
    """Control: with new results on disk every phase must still run."""
    calls = _arrange(plugin, monkeypatch, scan_writes_results=True)

    plugin._execute_scheduled_check(dict(_ALL_PHASE_GATES))

    for name in _PHASE_METHODS:
        assert name in calls, f"{name} did not run for a completed scan"


def test_window_state_is_not_cleared_while_the_scan_is_still_running(plugin, monkeypatch):
    """Clearing it strips the running scan's boundary so it runs to the end."""
    calls = _arrange(plugin, monkeypatch, scan_writes_results=False)
    monkeypatch.setattr(plugin, "_setup_window_state", lambda *a, **k: True)
    monkeypatch.setattr(
        plugin, "_apply_pending_resume_to_loaded_channels", lambda *a, **k: False
    )
    cleared = []
    monkeypatch.setattr(plugin, "_clear_window_state", lambda: cleared.append(True))

    settings = dict(_ALL_PHASE_GATES)
    settings["schedule_window_enabled"] = True
    plugin._execute_scheduled_check(settings)

    assert cleared == []
    assert calls == []


def test_window_state_is_cleared_when_the_scan_completed(plugin, monkeypatch):
    """Control for the test above."""
    _arrange(plugin, monkeypatch, scan_writes_results=True)
    monkeypatch.setattr(plugin, "_setup_window_state", lambda *a, **k: True)
    monkeypatch.setattr(
        plugin, "_apply_pending_resume_to_loaded_channels", lambda *a, **k: False
    )
    monkeypatch.setattr(plugin, "_has_pending_resume", lambda: False)
    cleared = []
    monkeypatch.setattr(plugin, "_clear_window_state", lambda: cleared.append(True))

    settings = dict(_ALL_PHASE_GATES)
    settings["schedule_window_enabled"] = True
    plugin._execute_scheduled_check(settings)

    assert cleared == [True]


# --- a cancelled session must not report or act ------------------------------

def test_a_cancelled_session_still_exports_the_csv(plugin, monkeypatch):
    """The CSV is the audit record of what was probed, so it is unconditional."""
    calls = _arrange(plugin, monkeypatch, scan_writes_results=True)
    plugin._stop_event.set()

    plugin._execute_scheduled_check(dict(_ALL_PHASE_GATES))

    assert "export_results_action" in calls


def test_a_cancelled_session_does_not_email_a_report(plugin, monkeypatch):
    """Loading the Dispatcharr Plugins page ends the session and used to email.

    Measured on 2026-09-05: one window produced two reports, because a plugin
    reload ended the first session part way and the end-of-session step treated
    it like a finished one. The second covered nine minutes and 125 streams.
    """
    calls = _arrange(plugin, monkeypatch, scan_writes_results=True)
    plugin._stop_event.set()

    plugin._execute_scheduled_check(dict(_ALL_PHASE_GATES))

    assert "_build_and_deliver_report" not in calls


def test_a_cancelled_session_does_not_rename_move_or_delete(plugin, monkeypatch):
    """Those verdicts would come from a list that was only partly probed."""
    calls = _arrange(plugin, monkeypatch, scan_writes_results=True)
    plugin._stop_event.set()

    plugin._execute_scheduled_check(dict(_ALL_PHASE_GATES))

    for name in ("restore_channels_action", "rename_channels_action",
                 "move_dead_channels_action", "delete_dead_channels_action"):
        assert name not in calls, f"{name} ran after a cancelled session"


def test_a_session_that_was_not_cancelled_reports_and_acts(plugin, monkeypatch):
    """Control, so the three tests above are not passing for another reason."""
    calls = _arrange(plugin, monkeypatch, scan_writes_results=True)
    assert not plugin._stop_event.is_set()

    plugin._execute_scheduled_check(dict(_ALL_PHASE_GATES))

    for name in _PHASE_METHODS:
        assert name in calls, f"{name} did not run for a session that ended normally"


def test_a_scheduled_delete_stored_as_the_string_false_does_not_run(plugin, monkeypatch):
    """End to end, because the unit test alone cannot show the wiring.

    Every boolean read used plain truthiness until 2026-09-05, so a value stored
    as the string "false" was read as on. Nine of those reads gate a scheduled
    rename, move or delete.
    """
    calls = _arrange(plugin, monkeypatch, scan_writes_results=True)
    settings = dict(_ALL_PHASE_GATES)
    settings["scheduler_delete_dead_channels"] = "false"
    settings["scheduler_move_dead_channels"] = "false"

    plugin._execute_scheduled_check(settings)

    assert "delete_dead_channels_action" not in calls, "a switched-off delete ran"
    assert "move_dead_channels_action" not in calls, "a switched-off move ran"
    assert "rename_channels_action" in calls, (
        "the settings left on must still run, or this proves nothing")
