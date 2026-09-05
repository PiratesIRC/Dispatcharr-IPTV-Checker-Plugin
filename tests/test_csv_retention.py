"""Deleting old CSV exports by age.

The export directory is SHARED with other plugins. At the time this was written
it held only Stream-Mapparr files. So the selection is scoped to this plugin's
own filename prefix, and a mutation that widens it to every CSV is caught by a
test, because the alternative is deleting another project's data.

Three further rules exist because this deletes files on other people's
installations:

  - it is off unless a number of days is set, so nobody loses files on upgrade;
  - the file just written is never deleted, whatever the arithmetic says;
  - at least one file always survives, so a small number cannot empty the
    directory.
"""

import pytest


DAY = 86400.0
NOW = 1_800_000_000.0
MINE = "iptv_checker_results_"


def _entry(name, days_old):
    return (name, NOW - days_old * DAY)


def _plan(plugin, entries, days=5, now=NOW, protect=None):
    return plugin._csv_exports_to_delete(entries, days, now, protect)


# --- off by default ---------------------------------------------------------

@pytest.mark.parametrize("days", [0, None, "", -1, "abc"])
def test_no_retention_configured_deletes_nothing(plugin, days):
    # Several old files, not one. With a single file the survivor rule keeps it
    # anyway, so the test would pass even with the off-by-default guard removed.
    entries = [_entry(MINE + "a.csv", 400), _entry(MINE + "b.csv", 300),
               _entry(MINE + "c.csv", 500)]
    assert _plan(plugin, entries, days=days) == []


# --- the age rule -----------------------------------------------------------

def test_a_file_older_than_the_limit_is_deleted(plugin):
    entries = [_entry(MINE + "old.csv", 9), _entry(MINE + "new.csv", 1)]
    assert _plan(plugin, entries, days=5) == [MINE + "old.csv"]


def test_a_file_younger_than_the_limit_is_kept(plugin):
    entries = [_entry(MINE + "a.csv", 1), _entry(MINE + "b.csv", 4)]
    assert _plan(plugin, entries, days=5) == []


def test_the_boundary_is_not_deleted(plugin):
    """Exactly five days old is not OLDER than five days."""
    entries = [_entry(MINE + "edge.csv", 5), _entry(MINE + "keep.csv", 0)]
    assert _plan(plugin, entries, days=5) == []


def test_just_past_the_boundary_is_deleted(plugin):
    entries = [_entry(MINE + "edge.csv", 5.001), _entry(MINE + "keep.csv", 0)]
    assert _plan(plugin, entries, days=5) == [MINE + "edge.csv"]


# --- never another plugin's files -------------------------------------------

def test_another_plugins_csv_is_never_deleted(plugin):
    """The export directory is shared. This is the most important test here."""
    entries = [
        _entry("stream_mapparr_sorted_20260101_000000.csv", 400),
        _entry("dustarr_usage_20260101.csv", 400),
        _entry(MINE + "mine.csv", 400),
        _entry(MINE + "recent.csv", 0),
    ]
    assert _plan(plugin, entries, days=5) == [MINE + "mine.csv"]


def test_a_file_that_is_not_a_csv_is_never_deleted(plugin):
    entries = [
        _entry(MINE + "notes.txt", 400),
        _entry(MINE + "real.csv", 400),
        _entry(MINE + "recent.csv", 0),
    ]
    assert _plan(plugin, entries, days=5) == [MINE + "real.csv"]


# --- protections ------------------------------------------------------------

def test_the_file_just_written_is_never_deleted(plugin):
    entries = [_entry(MINE + "just_written.csv", 400), _entry(MINE + "other.csv", 400)]
    plan = _plan(plugin, entries, days=5, protect=MINE + "just_written.csv")
    assert MINE + "just_written.csv" not in plan
    assert plan == [MINE + "other.csv"]


def test_at_least_one_file_always_survives(plugin):
    """Every file is old. Keeping the newest stops a small number emptying it."""
    entries = [_entry(MINE + "a.csv", 400), _entry(MINE + "b.csv", 300),
               _entry(MINE + "c.csv", 500)]
    plan = _plan(plugin, entries, days=5)
    assert MINE + "b.csv" not in plan, "the newest of this plugin's files must survive"
    assert sorted(plan) == [MINE + "a.csv", MINE + "c.csv"]


def test_a_single_old_file_is_kept(plugin):
    assert _plan(plugin, [_entry(MINE + "only.csv", 400)], days=5) == []


def test_the_survivor_is_not_counted_from_another_plugins_files(plugin):
    """A newer foreign file must not license deleting all of ours."""
    entries = [_entry("stream_mapparr_sorted_x.csv", 0), _entry(MINE + "mine.csv", 400)]
    assert _plan(plugin, entries, days=5) == []


# --- total over its input ---------------------------------------------------

def test_an_empty_directory_is_fine(plugin):
    assert _plan(plugin, [], days=5) == []


@pytest.mark.parametrize("mtime", [None, "not a number"])
def test_an_unreadable_timestamp_is_left_alone(plugin, mtime):
    entries = [(MINE + "odd.csv", mtime), _entry(MINE + "recent.csv", 0)]
    assert _plan(plugin, entries, days=5) == []


def test_a_timestamp_that_is_not_a_number_cannot_become_the_survivor(plugin):
    """Keeping it would let it stand in as the file that survives.

    Comparisons against a not-a-number value are all false, so it would win the
    "newest" test and every real file would be deleted instead of one surviving.
    """
    entries = [(MINE + "odd.csv", float("nan")),
               _entry(MINE + "old_a.csv", 400),
               _entry(MINE + "old_b.csv", 300)]

    plan = _plan(plugin, entries, days=5)

    assert plan == [MINE + "old_a.csv"]
    assert MINE + "old_b.csv" not in plan, "the newest real file must survive"
    assert MINE + "odd.csv" not in plan


# --- the part that touches the filesystem -----------------------------------

def _seed(directory, name, days_old, now):
    import os
    path = directory / name
    path.write_text("x", encoding="utf-8")
    stamp = now - days_old * DAY
    os.utime(path, (stamp, stamp))
    return path


def test_pruning_removes_only_the_old_files_of_this_plugin(
        plugin, pmod, monkeypatch, tmp_path):
    import time

    now = time.time()
    monkeypatch.setattr(pmod.PluginConfig, "EXPORTS_DIR", str(tmp_path))
    old = _seed(tmp_path, MINE + "old.csv", 30, now)
    recent = _seed(tmp_path, MINE + "recent.csv", 1, now)
    foreign = _seed(tmp_path, "stream_mapparr_sorted_x.csv", 30, now)

    removed = plugin._prune_csv_exports(5)

    assert removed == 1
    assert not old.exists()
    assert recent.exists()
    assert foreign.exists(), "another plugin's file was deleted"


def test_pruning_protects_the_file_just_written(
        plugin, pmod, monkeypatch, tmp_path):
    import time

    now = time.time()
    monkeypatch.setattr(pmod.PluginConfig, "EXPORTS_DIR", str(tmp_path))
    just_written = _seed(tmp_path, MINE + "just.csv", 30, now)
    other = _seed(tmp_path, MINE + "other.csv", 30, now)

    plugin._prune_csv_exports(5, protect=MINE + "just.csv")

    assert just_written.exists()
    assert not other.exists()


def test_pruning_a_missing_directory_does_not_raise(plugin, pmod, monkeypatch, tmp_path):
    monkeypatch.setattr(pmod.PluginConfig, "EXPORTS_DIR", str(tmp_path / "gone"))
    assert plugin._prune_csv_exports(5) == 0


def test_pruning_survives_a_file_it_cannot_delete(
        plugin, pmod, monkeypatch, tmp_path):
    """It runs after a successful export and must never turn one into a failure."""
    import time

    now = time.time()
    monkeypatch.setattr(pmod.PluginConfig, "EXPORTS_DIR", str(tmp_path))
    _seed(tmp_path, MINE + "a.csv", 30, now)
    _seed(tmp_path, MINE + "b.csv", 30, now)
    _seed(tmp_path, MINE + "keep.csv", 0, now)

    def boom(path):
        raise OSError("permission denied")

    monkeypatch.setattr(pmod.os, "remove", boom)

    assert plugin._prune_csv_exports(5) == 0


# --- wired into the export ---------------------------------------------------

def _export(plugin, pmod, monkeypatch, tmp_path, settings):
    monkeypatch.setattr(pmod.PluginConfig, "EXPORTS_DIR", str(tmp_path))
    import json
    with open(plugin.results_file, "w", encoding="utf-8") as handle:
        json.dump([{"channel_name": "Sample", "status": "Alive"}], handle)
    monkeypatch.setattr(plugin, "_generate_csv_header_comments", lambda *a, **k: [])
    return plugin.export_results_action(settings, __import__("logging").getLogger("x"))


def test_exporting_prunes_old_files(plugin, pmod, monkeypatch, tmp_path):
    import time

    old = _seed(tmp_path, MINE + "old.csv", 30, time.time())
    _seed(tmp_path, MINE + "recent.csv", 1, time.time())

    result = _export(plugin, pmod, monkeypatch, tmp_path, {"csv_retention_days": 5})

    assert result["status"] == "ok"
    assert not old.exists()


def test_exporting_without_the_setting_prunes_nothing(plugin, pmod, monkeypatch, tmp_path):
    """Control: nobody loses files just by upgrading."""
    import time

    old = _seed(tmp_path, MINE + "old.csv", 900, time.time())

    result = _export(plugin, pmod, monkeypatch, tmp_path, {})

    assert result["status"] == "ok"
    assert old.exists()


def test_exporting_never_deletes_the_file_it_just_wrote(
        plugin, pmod, monkeypatch, tmp_path):
    import time

    _seed(tmp_path, MINE + "old.csv", 900, time.time())

    _export(plugin, pmod, monkeypatch, tmp_path, {"csv_retention_days": 1})

    written = sorted(p.name for p in tmp_path.glob(MINE + "*.csv"))
    assert written, "the export wrote nothing, so this test proves nothing"
