"""Windowed-schedule math and pending-resume scope guards.

Covers the v1.26.1362003 incident class: a settings drift resumed 2,280 stale
streams, and an elapsed saved window was resumed against the wrong boundary.
"""
import json
from datetime import datetime, timedelta

import pytz

TZ = pytz.timezone("America/Chicago")


def _now():
    return datetime.now(TZ)


# --- _compute_window_end ------------------------------------------------------

def test_duration_mode_adds_hours(plugin):
    now = _now()
    end = plugin._compute_window_end(now, {"schedule_end_mode": "duration", "schedule_duration_hours": 4}, TZ)
    assert end == now + timedelta(hours=4)


def test_duration_mode_defaults_to_4_hours(plugin):
    now = _now()
    end = plugin._compute_window_end(now, {}, TZ)
    assert end == now + timedelta(hours=4)


def test_duration_mode_zero_falls_back_to_default(plugin):
    """0 is falsy, so `settings.get(...) or 4` maps it to the 4h default
    (it is NOT rejected). Only negative values reject."""
    now = _now()
    end = plugin._compute_window_end(now, {"schedule_end_mode": "duration", "schedule_duration_hours": 0}, TZ)
    assert end == now + timedelta(hours=4)


def test_duration_mode_rejects_negative(plugin):
    now = _now()
    assert plugin._compute_window_end(now, {"schedule_end_mode": "duration", "schedule_duration_hours": -2}, TZ) is None


def test_duration_mode_rejects_garbage(plugin):
    now = _now()
    assert plugin._compute_window_end(now, {"schedule_end_mode": "duration", "schedule_duration_hours": "soon"}, TZ) is None


def test_time_mode_end_later_today(plugin):
    now = TZ.localize(datetime(2026, 6, 10, 1, 0, 0))
    end = plugin._compute_window_end(now, {"schedule_end_mode": "time", "schedule_end_time": "04:00"}, TZ)
    assert end.hour == 4 and end.minute == 0
    assert end.date() == now.date()


def test_time_mode_wraps_past_midnight(plugin):
    now = TZ.localize(datetime(2026, 6, 10, 23, 50, 0))
    end = plugin._compute_window_end(now, {"schedule_end_mode": "time", "schedule_end_time": "04:00"}, TZ)
    assert end.date() == (now + timedelta(days=1)).date()


def test_time_mode_rejects_invalid_clock_values(plugin):
    now = _now()
    assert plugin._compute_window_end(now, {"schedule_end_mode": "time", "schedule_end_time": "25:00"}, TZ) is None
    assert plugin._compute_window_end(now, {"schedule_end_mode": "time", "schedule_end_time": "04:75"}, TZ) is None
    assert plugin._compute_window_end(now, {"schedule_end_mode": "time", "schedule_end_time": "garbage"}, TZ) is None


def test_unknown_mode_returns_none(plugin):
    assert plugin._compute_window_end(_now(), {"schedule_end_mode": "lunar"}, TZ) is None


# --- _past_window_end ----------------------------------------------------------

def test_past_window_end_false_when_no_window(plugin):
    assert plugin._past_window_end() is False


def test_past_window_end_false_inside_window(plugin):
    plugin._active_window_tz = TZ
    plugin._active_window_end = _now() + timedelta(hours=1)
    assert plugin._past_window_end() is False


def test_past_window_end_true_after_window(plugin):
    plugin._active_window_tz = TZ
    plugin._active_window_end = _now() - timedelta(seconds=1)
    assert plugin._past_window_end() is True


# --- settings fingerprint -------------------------------------------------------

def test_fingerprint_tracks_scope_settings(plugin):
    fp = plugin._settings_fingerprint({
        "channel_groups": "STL", "channel_groups_mode": "include",
        "check_alternative_streams": False, "only_visible_channels": True,
    })
    assert fp == {
        "channel_groups": "STL",
        "channel_groups_mode": "include",
        "channel_groups_legacy_exclude": "",
        "check_alternative_streams": False,
        "only_visible_channels": True,
    }


def test_fingerprint_differs_on_group_change(plugin):
    a = plugin._settings_fingerprint({"channel_groups": "STL"})
    b = plugin._settings_fingerprint({"channel_groups": "KC"})
    assert a != b


def test_fingerprint_differs_on_mode_change(plugin):
    """Flipping the mode turns the scope into its exact complement while every
    other value stays identical. A fingerprint that missed it would resume a
    windowed run against the opposite set of groups."""
    a = plugin._settings_fingerprint({"channel_groups": "STL", "channel_groups_mode": "include"})
    b = plugin._settings_fingerprint({"channel_groups": "STL", "channel_groups_mode": "exclude"})
    assert a != b


def test_fingerprint_differs_on_legacy_exclude_change(plugin):
    """The migration case: an install carrying both old settings. The old
    exclude list is still applied, so it still changes the scope."""
    a = plugin._settings_fingerprint({"group_names": "STL"})
    b = plugin._settings_fingerprint({"group_names": "STL", "group_names_exclude": "US-PPV-*"})
    assert a != b


def test_fingerprint_is_stable_across_the_migration(plugin):
    """An install with only the old include list produces the same fingerprint
    as the equivalent new configuration, so upgrading does NOT discard a pending
    windowed resume that is still valid."""
    legacy = plugin._settings_fingerprint({"group_names": "STL"})
    modern = plugin._settings_fingerprint({"channel_groups": "STL", "channel_groups_mode": "include"})
    assert legacy == modern


# --- pending-resume scope guards -------------------------------------------------

def _write_pending(plugin, fingerprint, window_end=None, stream_ids=(1, 2, 3)):
    payload = {
        "started_at": "2026-06-10T00:00:00Z",
        "window_end_iso": window_end,
        "tz": "America/Chicago",
        "settings_fingerprint": fingerprint,
        "remaining_stream_ids": list(stream_ids),
    }
    with open(plugin.pending_resume_file, "w") as f:
        json.dump(payload, f)


def test_resume_discarded_on_fingerprint_drift(plugin, quiet_logger):
    import os

    settings = {"group_names": "KC", "check_alternative_streams": True, "only_visible_channels": False}
    _write_pending(plugin, {"group_names": "STL", "check_alternative_streams": True, "only_visible_channels": False})
    assert plugin._apply_pending_resume_to_loaded_channels(settings, quiet_logger) is False
    assert not os.path.exists(plugin.pending_resume_file), "stale pending state must be cleared"


def test_resume_discarded_when_saved_window_elapsed(plugin, quiet_logger):
    import os

    settings = {"group_names": "STL", "check_alternative_streams": True, "only_visible_channels": False}
    elapsed_end = (_now() - timedelta(hours=2)).isoformat()
    _write_pending(plugin, plugin._settings_fingerprint(settings), window_end=elapsed_end)
    assert plugin._apply_pending_resume_to_loaded_channels(settings, quiet_logger) is False
    assert not os.path.exists(plugin.pending_resume_file)


def test_resume_applies_and_reanchors_window(plugin, pmod, quiet_logger):
    """Happy path: live channels intersected, window_end re-anchored to the
    ACTIVE window (the v1.26.1181126 re-anchor fix)."""
    settings = {"group_names": "STL", "check_alternative_streams": True, "only_visible_channels": False}
    future_end = (_now() + timedelta(hours=2)).isoformat()
    _write_pending(plugin, plugin._settings_fingerprint(settings), window_end=future_end, stream_ids=(11, 22))

    loaded = [
        {"id": 1, "name": "Ch1", "streams": [{"id": 11}, {"id": 99}]},
        {"id": 2, "name": "Ch2", "streams": [{"id": 22}]},
        {"id": 3, "name": "GoneCh", "streams": [{"id": 33}]},
    ]
    with open(plugin.loaded_channels_file, "w") as f:
        json.dump(loaded, f)

    # Channels 1 and 2 still exist in the DB; channel 3 was deleted.
    pmod.Channel.objects.live_ids = [1, 2]

    active_end = _now() + timedelta(hours=4)
    plugin._active_window_end = active_end
    plugin._active_window_tz = TZ

    assert plugin._apply_pending_resume_to_loaded_channels(settings, quiet_logger) is True

    with open(plugin.loaded_channels_file) as f:
        filtered = json.load(f)
    kept_ids = {s["id"] for ch in filtered for s in ch["streams"]}
    assert kept_ids == {11, 22}
    assert {ch["id"] for ch in filtered} == {1, 2}

    with open(plugin.pending_resume_file) as f:
        pending = json.load(f)
    assert pending["window_end_iso"] == active_end.isoformat(), "window must re-anchor to active window"


def test_resume_falls_back_when_no_pending_file(plugin, quiet_logger):
    settings = {"group_names": "STL"}
    assert plugin._apply_pending_resume_to_loaded_channels(settings, quiet_logger) is False


# ---- multi-window resume across nights ----------------------------------
#
# THE FEATURE THIS PROTECTS: a windowed run that does not finish the channel
# list in one night must continue from the same place when the window next
# opens. Measured 2026-08-06 on a real install: a 5-hour window covered 4,889
# of 7,637 streams, so a full pass needs roughly two nights.
#
# It never worked. Two separate places discarded the pending state whenever the
# SAVED window end was in the past, which is true by definition once that
# window has closed:
#
#   _apply_pending_resume_to_loaded_channels  discarded on the next cron fire
#   _maybe_resume_after_restart               discarded on any reload or restart
#
# The saved end being in the past is the NORMAL cross-window case, not evidence
# of dead state. It is only dead when there is no active window to resume into.

def test_resume_across_nights_when_a_new_window_is_active(plugin, pmod, quiet_logger):
    """The case that never worked: last night's window closed at 04:00, tonight's
    opens at 23:00. The saved end is in the past, and that is expected."""
    settings = {"channel_groups": "STL", "check_alternative_streams": True,
                "only_visible_channels": False}
    last_night_end = (_now() - timedelta(hours=19)).isoformat()
    _write_pending(plugin, plugin._settings_fingerprint(settings),
                   window_end=last_night_end, stream_ids=(11, 22))

    with open(plugin.loaded_channels_file, "w") as f:
        json.dump([{"id": 1, "name": "Ch1", "streams": [{"id": 11}, {"id": 99}]},
                   {"id": 2, "name": "Ch2", "streams": [{"id": 22}]}], f)
    pmod.Channel.objects.live_ids = [1, 2]

    tonight_end = _now() + timedelta(hours=5)
    plugin._active_window_end = tonight_end
    plugin._active_window_tz = TZ

    assert plugin._apply_pending_resume_to_loaded_channels(settings, quiet_logger) is True, \
        "a closed prior window must resume into tonight's window, not be discarded"

    with open(plugin.pending_resume_file) as f:
        pending = json.load(f)
    assert pending["window_end_iso"] == tonight_end.isoformat(), \
        "the pending state must re-anchor to the ACTIVE window"

    with open(plugin.loaded_channels_file) as f:
        kept = {s["id"] for ch in json.load(f) for s in ch["streams"]}
    assert kept == {11, 22}, "only the unchecked streams should remain"


def test_elapsed_pending_is_still_discarded_with_no_active_window(plugin, pmod, quiet_logger):
    """The original intent, preserved: with nothing to resume INTO, an elapsed
    pending file is dead state.

    A VALID loaded_channels.json is written on purpose. Without one, the
    function discards via the missing-file branch instead, and this test passed
    even with the elapsed check deleted entirely. Caught by mutation.
    """
    import os
    settings = {"channel_groups": "STL", "check_alternative_streams": True,
                "only_visible_channels": False}
    _write_pending(plugin, plugin._settings_fingerprint(settings),
                   window_end=(_now() - timedelta(hours=2)).isoformat(),
                   stream_ids=(11,))
    with open(plugin.loaded_channels_file, "w") as f:
        json.dump([{"id": 1, "name": "Ch1", "streams": [{"id": 11}]}], f)
    pmod.Channel.objects.live_ids = [1]
    plugin._active_window_end = None
    plugin._active_window_tz = None
    assert plugin._apply_pending_resume_to_loaded_channels(settings, quiet_logger) is False,         "with no active window, an elapsed pending file is dead state"
    assert not os.path.exists(plugin.pending_resume_file)


def test_restart_after_the_window_closed_keeps_the_pending_state(plugin, quiet_logger):
    """A reload or restart between windows must NOT destroy the progress.

    Measured 2026-08-06: opening the Dispatcharr plugins page mid-morning
    deleted a pending file holding 2,748 unchecked streams, so that night's
    window restarted the whole list.
    """
    import os
    settings = {"schedule_window_enabled": True, "channel_groups": "STL"}
    _write_pending(plugin, plugin._settings_fingerprint(settings),
                   window_end=(_now() - timedelta(hours=7)).isoformat())
    plugin._maybe_resume_after_restart(settings)
    assert os.path.exists(plugin.pending_resume_file), \
        "a closed window is not a reason to delete progress the next window needs"
    assert getattr(plugin, "_restart_resume_active", False) is not True, \
        "it must still decline to resume immediately outside a window"


def test_restart_inside_an_open_window_still_resumes(plugin, pmod, quiet_logger, monkeypatch):
    """Unchanged behaviour: a container that died mid-window picks straight up.

    The resume runs on a REAL thread whose finally clause resets
    _restart_resume_active, so asserting that flag after letting the thread run
    is a race. A first version of this test did exactly that and passed or
    failed depending on timing. Capture the thread instead of starting it,
    which makes the assertion deterministic.
    """
    settings = {"schedule_window_enabled": True, "channel_groups": "STL"}
    _write_pending(plugin, plugin._settings_fingerprint(settings),
                   window_end=(_now() + timedelta(hours=2)).isoformat())

    captured = {}

    class _CapturedThread:
        def __init__(self, target=None, daemon=None, name=None, **kw):
            captured["target"] = target
            captured["name"] = name

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(pmod.threading, "Thread", _CapturedThread)
    plugin._maybe_resume_after_restart(settings)

    assert captured.get("started") is True, \
        "a window that is still open must resume immediately after a restart"
    assert plugin._restart_resume_active is True, \
        "the guard must be set BEFORE spawning, so the scheduler loop cannot double-fire"


def test_restart_with_no_pending_file_is_a_noop(plugin):
    settings = {"schedule_window_enabled": True}
    plugin._maybe_resume_after_restart(settings)
    assert getattr(plugin, "_restart_resume_active", False) is not True
