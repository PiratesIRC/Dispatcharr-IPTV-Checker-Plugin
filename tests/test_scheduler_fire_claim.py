"""Cross-process, cross-import claim on a cron minute (`_claim_scheduler_fire`).

Regression for the duplicate-fire incident on 2026-08-07: the cron expression
'0 23 * * *' fired twice, 1.34 seconds apart, and two complete five-hour scans
ran concurrently and emailed two reports.

Root cause. A plugin discovery pass on 2026-08-06 at 18:05 re-imported the
plugin module inside worker 251, which was already the elected scheduler owner.
That left two scheduler loops alive in one process, holding two SEPARATE module
objects, so all three existing guards passed:

  - the election lock names PID 251 and both loops run in PID 251, so
    _scheduler_lock_taken_over sees no different PID;
  - _scheduler_last_fired is a module-level dict, so each module object has its
    own copy and neither sees the other's claim;
  - _bg_scheduler_thread is module-level too, so the older loop still sees
    itself and never self-evicts.

The claim is therefore recorded on disk with os.open(O_CREAT|O_EXCL), the same
mechanism the election lock uses, which no amount of re-importing can defeat.
Clearing _scheduler_last_fired between two claims is exactly what a re-import
looks like from the claim's point of view, so that is how these tests simulate
it.
"""
import os
import stat

import pytest


@pytest.fixture
def claims(plugin, pmod, monkeypatch, tmp_path):
    """Point the claim directory at a temp dir and start from a clean slate."""
    d = str(tmp_path / "fire_claims")
    monkeypatch.setattr(pmod.PluginConfig, "SCHEDULER_FIRE_CLAIM_DIR", d)
    pmod._scheduler_last_fired.clear()
    return d


def _minute(pmod, hour=23, minute=0, day=7):
    return pmod.datetime(2026, 8, day, hour, minute, 0)


def test_first_caller_claims_the_minute(plugin, pmod, claims):
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True


def test_second_caller_in_the_same_process_is_refused(plugin, pmod, claims):
    m = _minute(pmod)
    assert plugin._claim_scheduler_fire("0 23 * * *", m) is True
    assert plugin._claim_scheduler_fire("0 23 * * *", m) is False


def test_a_re_imported_module_cannot_claim_the_same_minute(plugin, pmod, claims):
    """The 2026-08-07 incident. A second scheduler loop holding a fresh module
    object has an empty _scheduler_last_fired, so the in-process guard cannot
    see the first loop's claim. The on-disk claim must still refuse it."""
    m = _minute(pmod)
    assert plugin._claim_scheduler_fire("0 23 * * *", m) is True

    pmod._scheduler_last_fired.clear()  # what a module re-import looks like

    assert plugin._claim_scheduler_fire("0 23 * * *", m) is False


def test_a_later_minute_can_still_fire(plugin, pmod, claims):
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True
    pmod._scheduler_last_fired.clear()
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod, minute=1)) is True


def test_a_different_cron_expression_in_the_same_minute_can_fire(plugin, pmod, claims):
    """Two schedules legitimately due in the same minute must both run."""
    m = _minute(pmod)
    assert plugin._claim_scheduler_fire("0 23 * * *", m) is True
    pmod._scheduler_last_fired.clear()
    assert plugin._claim_scheduler_fire("0 23 * * 1", m) is True


def test_the_claim_file_is_not_world_readable(plugin, pmod, claims, monkeypatch):
    """Same reasoning as the election lock: it records a PID and nothing outside
    the owning user needs to read it. Asserting on the mode ARGUMENT rather than
    on stat() keeps this meaningful on Windows, which has no POSIX mode bits."""
    seen = []
    real_open = pmod.os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        seen.append((str(path), mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(pmod.os, "open", spy)
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True

    created = [(p, m) for p, m in seen if claims in p]
    assert created, "no claim file was created, so this test proves nothing"
    for path, mode in created:
        assert mode & 0o077 == 0, f"{path} created with group/other access: {oct(mode)}"


def test_an_unwritable_claim_directory_fails_OPEN(plugin, pmod, claims, monkeypatch):
    """Never fail closed on a backstop. If the claim cannot be recorded, run the
    schedule anyway and complain: a possible duplicate run beats a scheduler that
    silently stops firing forever."""
    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(pmod.os, "open", boom)
    warnings = []
    monkeypatch.setattr(pmod.LOGGER, "warning", lambda msg, *a, **k: warnings.append(str(msg)))

    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True
    assert warnings, "failing open must be logged, or it is a silent degradation"


def test_stale_claim_files_are_pruned(plugin, pmod, claims):
    """The directory must not grow without bound. A claim from a previous day is
    dead: its minute can never come round again."""
    old = _minute(pmod, day=1)
    assert plugin._claim_scheduler_fire("0 23 * * *", old) is True
    assert len(os.listdir(claims)) == 1

    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod, day=7)) is True

    names = os.listdir(claims)
    assert len(names) == 1, f"the stale claim was not pruned: {names}"
    assert "20260807" in names[0]


def test_a_claim_from_a_previous_run_of_the_same_day_is_kept(plugin, pmod, claims):
    """Pruning must not delete a claim that is still doing its job. An earlier
    minute today is exactly what stops a duplicate loop re-firing it."""
    assert plugin._claim_scheduler_fire("0 4 * * *", _minute(pmod, hour=4)) is True
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod, hour=23)) is True
    assert len(os.listdir(claims)) == 2


class _StatWithOwner:
    """An os.stat result whose owner is replaced; every other field is the real one."""

    def __init__(self, real, uid, gid, mode=None):
        self._real = real
        self.st_uid = uid
        self.st_gid = gid
        self._mode = mode

    @property
    def st_mode(self):
        return self._mode if self._mode is not None else self._real.st_mode

    def __getattr__(self, name):
        return getattr(self._real, name)


def _claim_folder_owned_by(pmod, monkeypatch, claims, uid, gid, mode=None):
    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if str(path) == claims:
            return _StatWithOwner(st, uid, gid, mode)
        return st

    monkeypatch.setattr(pmod.os, "lstat", fake_lstat)


def _process_is_root(pmod, monkeypatch, euid):
    monkeypatch.setattr(pmod.os, "geteuid", lambda: euid, raising=False)


def _plugin_folder_owner(pmod, monkeypatch, uid, gid):
    monkeypatch.setattr(pmod.Plugin, "_plugin_owner_ids", staticmethod(lambda: (uid, gid)))


def _record_chown(pmod, monkeypatch, raises=None):
    calls = []

    def chown(path, uid, gid, **kwargs):
        calls.append((str(path), uid, gid))
        if raises is not None:
            raise raises

    monkeypatch.setattr(pmod.os, "chown", chown, raising=False)
    return calls


def test_a_root_process_hands_the_claim_folder_to_the_plugin_owner(plugin, pmod, claims, monkeypatch):
    """Root can create claims anywhere, but the folder it makes must belong to the
    user that owns this plugin, or the Dispatcharr workers cannot write claims."""
    _process_is_root(pmod, monkeypatch, 0)
    _plugin_folder_owner(pmod, monkeypatch, 1000, 1000)
    _claim_folder_owned_by(pmod, monkeypatch, claims, 0, 0)
    calls = _record_chown(pmod, monkeypatch)
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True
    assert calls == [(claims, 1000, 1000)]


def test_a_non_root_process_never_changes_the_owner(plugin, pmod, claims, monkeypatch):
    _process_is_root(pmod, monkeypatch, 1000)
    _plugin_folder_owner(pmod, monkeypatch, 1000, 1000)
    _claim_folder_owned_by(pmod, monkeypatch, claims, 0, 0)
    calls = _record_chown(pmod, monkeypatch)
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True
    assert calls == []
    assert len(os.listdir(claims)) == 1


def test_a_folder_already_owned_by_the_plugin_owner_is_left_alone(plugin, pmod, claims, monkeypatch):
    _process_is_root(pmod, monkeypatch, 0)
    _plugin_folder_owner(pmod, monkeypatch, 1000, 1000)
    _claim_folder_owned_by(pmod, monkeypatch, claims, 1000, 1000)
    calls = _record_chown(pmod, monkeypatch)
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True
    assert calls == []


def test_a_failed_chown_still_records_the_claim(plugin, pmod, claims, monkeypatch):
    """A root process that cannot change the owner still runs the schedule and
    still records the claim; the only cost is a possible duplicate run."""
    _process_is_root(pmod, monkeypatch, 0)
    _plugin_folder_owner(pmod, monkeypatch, 1000, 1000)
    _claim_folder_owned_by(pmod, monkeypatch, claims, 0, 0)
    _record_chown(pmod, monkeypatch, raises=PermissionError(1, "Operation not permitted"))
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True
    assert len(os.listdir(claims)) == 1


def test_a_symlinked_claim_folder_is_never_chowned(plugin, pmod, claims, monkeypatch):
    """A symlink at the claim path could point the chown at any file the root
    process can reach, so a symlink is left exactly as it is."""
    _process_is_root(pmod, monkeypatch, 0)
    _plugin_folder_owner(pmod, monkeypatch, 1000, 1000)
    _claim_folder_owned_by(pmod, monkeypatch, claims, 0, 0, mode=stat.S_IFLNK | 0o777)
    calls = _record_chown(pmod, monkeypatch)
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True
    assert calls == []


def test_a_root_owned_plugin_folder_names_no_ids(plugin, pmod, claims, monkeypatch, caplog):
    """A root-owned plugin folder is not a worker owner. The owner is unknown, so
    no chown runs and the ERROR names the generic user instead of 0:0."""
    plugin_dir = os.path.dirname(os.path.abspath(pmod.__file__))
    real_stat = os.stat

    def root_owned_plugin_dir(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if os.path.abspath(str(path)) == plugin_dir:
            return _StatWithOwner(st, 0, 0)
        return st

    monkeypatch.setattr(pmod.os, "stat", root_owned_plugin_dir)
    _process_is_root(pmod, monkeypatch, 0)
    assert plugin._plugin_owner_ids() is None
    calls = _record_chown(pmod, monkeypatch)

    real_open = pmod.os.open

    def denied_for_claims(path, flags, mode=0o777, *args, **kwargs):
        if claims in str(path):
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(pmod.os, "open", denied_for_claims)
    with caplog.at_level("ERROR", logger="plugins.iptv_checker"):
        assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True

    assert calls == []
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("the user that runs Dispatcharr" in m for m in errors), errors
    assert not any("0:0" in m for m in errors), errors


def test_no_world_writable_chmod_remains():
    """The Plugin Hub runs CodeQL, which refuses a listing whose chmod sets a
    world-writable mode (rule py/overly-permissive-file). This keeps that call out."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "iptv_checker", "plugin.py")
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    for needle in ("0o1777", "0o777", "os.chmod("):
        assert needle not in source, f"{needle} found in plugin.py"


def test_no_permission_to_write_a_claim_runs_anyway_and_names_the_repair(
        plugin, pmod, claims, monkeypatch, caplog):
    """The root-owned directory case from issue #29. The schedule still runs
    (fail open), and the ERROR names the directory and the one-line repair."""
    real_open = pmod.os.open

    def denied_for_claims(path, flags, mode=0o777, *args, **kwargs):
        if claims in str(path):
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(pmod.os, "open", denied_for_claims)
    with caplog.at_level("ERROR", logger="plugins.iptv_checker"):
        assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "a claim that cannot be written must be logged at ERROR"
    assert any("as root: chown" in m and claims in m for m in errors), errors
    assert not any("chmod" in m for m in errors), errors


def test_a_directory_it_cannot_create_names_mkdir(plugin, pmod, claims, monkeypatch, caplog):
    """A parent folder that is not writable: the directory never exists, so
    chmod cannot help and the ERROR must name the mkdir repair instead."""
    assert not os.path.exists(claims)

    def denied_makedirs(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(pmod.os, "makedirs", denied_makedirs)
    with caplog.at_level("ERROR", logger="plugins.iptv_checker"):
        assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("mkdir " in m and "chown" in m and claims in m for m in errors), errors
    assert not any("chmod" in m for m in errors), errors


def test_other_os_errors_still_run_anyway_with_a_warning(
        plugin, pmod, claims, monkeypatch, caplog):
    """Errors other than permission keep the original WARNING branch."""
    def no_space(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pmod.os, "open", no_space)
    with caplog.at_level("WARNING", logger="plugins.iptv_checker"):
        assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("Could not record the scheduler fire claim (OSError)" in m for m in warnings), warnings
