"""A channel is judged by ALL of its streams, never by one of them.

Check results are recorded PER STREAM. `check_alternative_streams` defaults to
on, so a channel with a primary and a backup produces two rows. Every action
that renames, moves or deletes acts PER CHANNEL, and it used to select with a
comprehension that matched if ANY single row matched:

    dead_channels = {r['channel_id']: r['channel_name']
                     for r in results if self._is_dead_nonblack(r)}

Measured 2026-08-05: a channel whose primary stream timed out and whose backup
stream was Alive was selected as dead, and so was eligible for rename to
[DEAD], move to the graveyard group, and PERMANENT DELETION. Dispatcharr fails
over to the backup, so that channel plays perfectly.

THE RULE THESE TESTS BIND: a destructive action may act on a channel only when
EVERY stream considered says so. One working stream means the channel works.

`Skipped` is not evidence either way, and never authorises an action. A channel
with one Dead stream and one Skipped stream has not been shown to be dead: the
Skipped stream might be a rate-limited stream that works fine.
"""


def _row(cid, status, name="Ch", stream_id=None, error_type="N/A", fps=0):
    return {
        "channel_id": cid,
        "channel_name": name,
        "stream_id": stream_id if stream_id is not None else cid * 100,
        "status": status,
        "error_type": error_type,
        "framerate_num": fps,
    }


# ---- the general aggregator ---------------------------------------------

def test_all_streams_dead_selects_the_channel(pmod):
    P = pmod.Plugin
    results = [_row(1, "Dead", stream_id=10, error_type="Timeout"),
               _row(1, "Dead", stream_id=11, error_type="Timeout")]
    assert P._channels_where(results, P._is_dead_nonblack) == {1: "Ch"}


def test_one_alive_stream_spares_the_channel(pmod):
    """The regression. A working backup means the channel works."""
    P = pmod.Plugin
    results = [_row(1, "Dead", stream_id=10, error_type="Timeout"),
               _row(1, "Alive", stream_id=11)]
    assert P._channels_where(results, P._is_dead_nonblack) == {}


def test_one_skipped_stream_spares_the_channel(pmod):
    """Skipped means NOT JUDGED. A rate-limited stream may work perfectly, so a
    channel with one dead and one skipped stream has not been shown dead."""
    P = pmod.Plugin
    results = [_row(1, "Dead", stream_id=10, error_type="Timeout"),
               _row(1, "Skipped", stream_id=11, error_type="Rate Limited")]
    assert P._channels_where(results, P._is_dead_nonblack) == {}


def test_single_stream_channel_still_works(pmod):
    P = pmod.Plugin
    assert P._channels_where([_row(1, "Dead", error_type="Timeout")],
                             P._is_dead_nonblack) == {1: "Ch"}


def test_channels_are_judged_independently(pmod):
    P = pmod.Plugin
    results = [
        _row(1, "Dead", name="AllDead", stream_id=10, error_type="Timeout"),
        _row(1, "Dead", name="AllDead", stream_id=11, error_type="Timeout"),
        _row(2, "Dead", name="HasBackup", stream_id=20, error_type="Timeout"),
        _row(2, "Alive", name="HasBackup", stream_id=21),
        _row(3, "Alive", name="Healthy", stream_id=30),
    ]
    assert P._channels_where(results, P._is_dead_nonblack) == {1: "AllDead"}


def test_empty_and_malformed_input_is_safe(pmod):
    P = pmod.Plugin
    assert P._channels_where([], P._is_dead_nonblack) == {}
    assert P._channels_where(None, P._is_dead_nonblack) == {}
    # A row with no channel_id cannot be acted on and must not crash the pass.
    assert P._channels_where([{"status": "Dead"}], P._is_dead_nonblack) == {}


# ---- black screen --------------------------------------------------------

def test_all_streams_black_selects_the_channel(pmod):
    P = pmod.Plugin
    results = [_row(1, "Dead", stream_id=10, error_type="Black Screen"),
               _row(1, "Dead", stream_id=11, error_type="Black Screen")]
    assert P._channels_where(results, P._is_black_screen) == {1: "Ch"}


def test_black_plus_alive_spares_the_channel(pmod):
    P = pmod.Plugin
    results = [_row(1, "Dead", stream_id=10, error_type="Black Screen"),
               _row(1, "Alive", stream_id=11)]
    assert P._channels_where(results, P._is_black_screen) == {}


def test_mixed_black_and_timeout_is_dead_but_not_black(pmod):
    """Every stream failed, so the channel IS dead and the dead actions should
    take it. It is not uniformly a blank screen, so the blank-screen actions,
    which park channels in their own group, must not claim it."""
    P = pmod.Plugin
    results = [_row(1, "Dead", stream_id=10, error_type="Black Screen"),
               _row(1, "Dead", stream_id=11, error_type="Timeout")]
    assert P._channels_where(results, P._is_black_screen) == {}
    assert P._channels_where(results, P._is_dead_nonblack) == {}
    # It is still fully dead, which the delete predicate sees.
    assert P._channels_where(results, lambda r: r.get("status") == "Dead") == {1: "Ch"}


# ---- low framerate: judged over the streams that actually play ----------

def test_low_framerate_needs_every_alive_stream_to_be_slow(pmod):
    P = pmod.Plugin
    results = [_row(1, "Alive", stream_id=10, fps=15.0),
               _row(1, "Alive", stream_id=11, fps=15.0)]
    assert P._channels_where(results, P._is_low_framerate_row,
                             among=lambda r: r.get("status") == "Alive") == {1: "Ch"}


def test_one_full_framerate_stream_spares_the_channel(pmod):
    """Dispatcharr can play the 60fps stream, so the channel is not slow."""
    P = pmod.Plugin
    results = [_row(1, "Alive", stream_id=10, fps=15.0),
               _row(1, "Alive", stream_id=11, fps=60.0)]
    assert P._channels_where(results, P._is_low_framerate_row,
                             among=lambda r: r.get("status") == "Alive") == {}


def test_dead_streams_do_not_veto_a_low_framerate_verdict(pmod):
    """A dead stream cannot be played, so it says nothing about how the channel
    looks. Only the streams that work are considered."""
    P = pmod.Plugin
    results = [_row(1, "Alive", stream_id=10, fps=15.0),
               _row(1, "Dead", stream_id=11, error_type="Timeout", fps=0)]
    assert P._channels_where(results, P._is_low_framerate_row,
                             among=lambda r: r.get("status") == "Alive") == {1: "Ch"}


def test_channel_with_no_considered_streams_is_excluded(pmod):
    """All streams dead means there is no framerate to judge. Excluded rather
    than counted as vacuously matching, which is what `all()` over an empty
    sequence would otherwise do."""
    P = pmod.Plugin
    results = [_row(1, "Dead", stream_id=10, error_type="Timeout", fps=0)]
    assert P._channels_where(results, P._is_low_framerate_row,
                             among=lambda r: r.get("status") == "Alive") == {}


def test_low_framerate_row_predicate(pmod):
    P = pmod.Plugin
    assert P._is_low_framerate_row({"framerate_num": 15.0}) is True
    assert P._is_low_framerate_row({"framerate_num": 25.0}) is False
    assert P._is_low_framerate_row({"framerate_num": 0}) is False
    assert P._is_low_framerate_row({}) is False


# ---- the actions themselves use it --------------------------------------

_MIXED = [
    {"channel_id": 1, "channel_name": "AllDead", "stream_id": 10,
     "status": "Dead", "error_type": "Timeout", "framerate_num": 0},
    {"channel_id": 1, "channel_name": "AllDead", "stream_id": 11,
     "status": "Dead", "error_type": "Timeout", "framerate_num": 0},
    {"channel_id": 2, "channel_name": "HasWorkingBackup", "stream_id": 20,
     "status": "Dead", "error_type": "Timeout", "framerate_num": 0},
    {"channel_id": 2, "channel_name": "HasWorkingBackup", "stream_id": 21,
     "status": "Alive", "error_type": "N/A", "framerate_num": 50.0},
]


def test_rename_dead_spares_the_channel_with_a_working_backup(plugin, monkeypatch, quiet_logger):
    sent = {}
    monkeypatch.setattr(plugin, "_load_json_file", lambda path: _MIXED)
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: sent.update(payload=payload) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: None)
    plugin.rename_channels_action({"dead_rename_format": "{name} [DEAD]"}, quiet_logger)
    ids = {p["id"] for p in sent.get("payload", [])}
    assert 2 not in ids, "the channel with a working backup must not be renamed"
    assert ids == {1}


def test_delete_dead_spares_the_channel_with_a_working_backup(plugin, pmod, monkeypatch, quiet_logger):
    """The most consequential path: deletion cannot be undone."""
    loaded = [{"id": 1}, {"id": 2}]

    def fake_load(path):
        return _MIXED if "result" in str(path) else loaded

    monkeypatch.setattr(plugin, "_load_json_file", fake_load)

    deleted = {}

    class _QS:
        def delete(self):
            return (len(deleted.get("ids", ())), {})

    class _Mgr:
        def filter(self, id__in=None, **kw):
            deleted["ids"] = set(id__in or ())
            return _QS()

    monkeypatch.setattr(pmod.Channel, "objects", _Mgr(), raising=False)

    class _Atomic:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pmod, "transaction", type("T", (), {"atomic": staticmethod(lambda: _Atomic())}))
    plugin.delete_dead_channels_action({"auto_delete_confirmation": "DELETE"}, quiet_logger)
    assert 2 not in deleted.get("ids", set()),         "a channel with a working backup must never be permanently deleted"
    assert deleted.get("ids") == {1}


# ---- low-framerate actions, not just the helper -------------------------
#
# A mutation that removed the `among` filter from the low-framerate actions
# SURVIVED the first version of this file: the helper was tested directly, but
# neither action was. Without that filter a channel with one slow Alive stream
# and one Dead stream is no longer flagged, because the Dead row reports a
# framerate of 0 and fails the predicate.

_SLOW_WITH_DEAD_BACKUP = [
    {"channel_id": 1, "channel_name": "SlowChannel", "stream_id": 10,
     "status": "Alive", "error_type": "N/A", "framerate_num": 15.0},
    {"channel_id": 1, "channel_name": "SlowChannel", "stream_id": 11,
     "status": "Dead", "error_type": "Timeout", "framerate_num": 0},
    {"channel_id": 2, "channel_name": "FastAndSlow", "stream_id": 20,
     "status": "Alive", "error_type": "N/A", "framerate_num": 15.0},
    {"channel_id": 2, "channel_name": "FastAndSlow", "stream_id": 21,
     "status": "Alive", "error_type": "N/A", "framerate_num": 60.0},
]


def test_rename_low_framerate_judges_only_the_playable_streams(plugin, monkeypatch, quiet_logger):
    sent = {}
    monkeypatch.setattr(plugin, "_load_json_file", lambda path: _SLOW_WITH_DEAD_BACKUP)
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: sent.update(payload=payload) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: None)
    plugin.rename_low_framerate_channels_action(
        {"low_framerate_rename_format": "{name} [Slow]"}, quiet_logger)
    ids = {p["id"] for p in sent.get("payload", [])}
    # Channel 1: its only playable stream is slow, so it IS slow.
    assert 1 in ids, "a dead backup must not stop a genuinely slow channel being flagged"
    # Channel 2: one playable stream runs at full rate, so the channel is fine.
    assert 2 not in ids, "a channel with a full-framerate stream must not be flagged slow"


def test_move_low_framerate_judges_only_the_playable_streams(plugin, monkeypatch, quiet_logger):
    seen = {}
    monkeypatch.setattr(plugin, "_load_json_file", lambda path: _SLOW_WITH_DEAD_BACKUP)
    monkeypatch.setattr(plugin, "_get_or_create_group",
                        lambda name, logger: type("G", (), {"id": 99, "name": name})())
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: seen.update(payload=payload) or len(payload))
    monkeypatch.setattr(plugin, "_capture_original_state", lambda *a, **k: None)
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: None)
    plugin.move_low_framerate_channels_action(
        {"move_low_framerate_group": "Slow"}, quiet_logger)
    ids = {p["id"] for p in seen.get("payload", [])}
    assert 1 in ids
    assert 2 not in ids


# ---- the remaining channel-mutating actions -----------------------------
#
# Mutations reverting move-dead and both blank-screen actions to the old
# any-row selection SURVIVED an earlier version of this file. Only the
# rename-dead and delete actions were covered at action level; testing the
# shared helper does not prove each caller uses it.

_BLACK_WITH_ALIVE_BACKUP = [
    {"channel_id": 1, "channel_name": "AllBlack", "stream_id": 10,
     "status": "Dead", "error_type": "Black Screen", "framerate_num": 0},
    {"channel_id": 1, "channel_name": "AllBlack", "stream_id": 11,
     "status": "Dead", "error_type": "Black Screen", "framerate_num": 0},
    {"channel_id": 2, "channel_name": "BlackButHasBackup", "stream_id": 20,
     "status": "Dead", "error_type": "Black Screen", "framerate_num": 0},
    {"channel_id": 2, "channel_name": "BlackButHasBackup", "stream_id": 21,
     "status": "Alive", "error_type": "N/A", "framerate_num": 50.0},
]


def _wire_move(plugin, monkeypatch, results):
    seen = {}
    monkeypatch.setattr(plugin, "_load_json_file", lambda path: results)
    monkeypatch.setattr(plugin, "_get_or_create_group",
                        lambda name, logger: type("G", (), {"id": 99, "name": name})())
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: seen.update(payload=payload) or len(payload))
    monkeypatch.setattr(plugin, "_capture_original_state", lambda *a, **k: None)
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: None)
    return seen


def test_move_dead_spares_the_channel_with_a_working_backup(plugin, monkeypatch, quiet_logger):
    seen = _wire_move(plugin, monkeypatch, _MIXED)
    plugin.move_dead_channels_action({"move_to_group_name": "Graveyard"}, quiet_logger)
    ids = {p["id"] for p in seen.get("payload", [])}
    assert 2 not in ids, "a channel with a working backup must not be moved to the graveyard"
    assert ids == {1}


def test_rename_blank_screen_spares_the_channel_with_a_working_backup(plugin, monkeypatch, quiet_logger):
    sent = {}
    monkeypatch.setattr(plugin, "_load_json_file", lambda path: _BLACK_WITH_ALIVE_BACKUP)
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: sent.update(payload=payload) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: None)
    plugin.rename_black_screen_channels_action(
        {"black_screen_rename_format": "{name} [Blank]"}, quiet_logger)
    ids = {p["id"] for p in sent.get("payload", [])}
    assert 2 not in ids, "a channel with a working backup must not be renamed blank"
    assert ids == {1}


def test_move_blank_screen_spares_the_channel_with_a_working_backup(plugin, monkeypatch, quiet_logger):
    seen = _wire_move(plugin, monkeypatch, _BLACK_WITH_ALIVE_BACKUP)
    plugin.move_black_screen_channels_action(
        {"move_black_screen_group": "Black Screens"}, quiet_logger)
    ids = {p["id"] for p in seen.get("payload", [])}
    assert 2 not in ids, "a channel with a working backup must not be parked as blank"
    assert ids == {1}
