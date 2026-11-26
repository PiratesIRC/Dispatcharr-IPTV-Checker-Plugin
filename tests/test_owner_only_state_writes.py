"""Every state file is written through a unique temp file that is owner-only (0o600 on POSIX).

Two saves of the same file can overlap across Dispatcharr workers, so each save uses its own
temp file. A fixed temp name would let one save remove or replace the other's, and that save
would be dropped with an error.
"""
import glob
import json
import logging
import os
import stat
import sys
import time

import pytest

import iptv_checker.plugin as pm


def _spy_on_os_open(monkeypatch):
    seen = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        seen.append((os.fspath(path), mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(pm.os, "open", spy)
    return seen


def test_the_reload_flag_is_created_owner_only(plugin, tmp_path, monkeypatch):
    flag = tmp_path / "reload.flag"
    monkeypatch.setattr(pm.PluginConfig, "STORAGE_LAYOUT", None)
    monkeypatch.setattr(pm.PluginConfig, "SCHEDULER_RELOAD_FLAG", str(flag))
    seen = _spy_on_os_open(monkeypatch)

    plugin._request_scheduler_reload()

    assert flag.exists()
    assert [mode for path, mode in seen if path == str(flag)] == [0o600]


def test_the_temp_file_sits_beside_the_target_with_a_unique_name(plugin, tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    sources = []
    real_replace = os.replace

    def spy_replace(src, dst, *args, **kwargs):
        sources.append(os.fspath(src))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(pm.os, "replace", spy_replace)

    plugin._save_json_file(str(target), {"a": 1})

    assert len(sources) == 1
    temp = sources[0]
    assert os.path.dirname(temp) == str(tmp_path)
    assert os.path.basename(temp).startswith(".state.json.")
    assert temp.endswith(".tmp")
    assert temp != str(target) + ".tmp"


def test_two_interleaved_saves_of_one_file_both_complete(plugin, tmp_path, monkeypatch, caplog):
    """A second save starts after the first has written its temp file and before the first
    replaces the target. With one fixed temp name the second save removed the first's temp,
    and the first save's replace failed."""
    target = tmp_path / "state.json"
    real_replace = os.replace
    state = {"inside": False}

    def replace_with_a_second_save_first(src, dst, *args, **kwargs):
        if not state["inside"] and os.fspath(dst) == str(target):
            state["inside"] = True
            try:
                plugin._save_json_file(str(target), {"who": "second"})
            finally:
                state["inside"] = False
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(pm.os, "replace", replace_with_a_second_save_first)

    with caplog.at_level(logging.ERROR, logger="plugins.iptv_checker"):
        plugin._save_json_file(str(target), {"who": "first"})

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], "a save was dropped"
    assert json.loads(target.read_text(encoding="utf-8")) == {"who": "first"}
    assert glob.glob(str(tmp_path / ".state.json.*")) == []


def test_a_failed_save_removes_its_temp_file(plugin, tmp_path, monkeypatch, caplog):
    target = tmp_path / "state.json"

    def failing_replace(src, dst, *args, **kwargs):
        raise OSError("replace refused")

    monkeypatch.setattr(pm.os, "replace", failing_replace)

    with caplog.at_level(logging.ERROR, logger="plugins.iptv_checker"):
        plugin._save_json_file(str(target), {"a": 1})

    assert not target.exists()
    assert glob.glob(str(tmp_path / ".state.json.*")) == [], "temp file left behind"
    assert any("replace refused" in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_state_file_is_owner_only_on_posix(plugin, tmp_path):
    target = tmp_path / "state.json"

    plugin._save_json_file(str(target), {"a": 1})

    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert stat.S_IMODE(os.stat(target).st_mode) & 0o077 == 0


def test_a_failed_fdopen_closes_the_descriptor_and_the_temp_file(plugin, tmp_path, monkeypatch, caplog):
    """If os.fdopen raises after mkstemp, the descriptor is not yet owned by a file object, so
    the save must close it itself and remove the temp file."""
    import tempfile

    target = tmp_path / "state.json"
    made = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        made.append(fd)
        return fd, path

    closed = []
    real_close = os.close

    def recording_close(fd):
        closed.append(fd)
        return real_close(fd)

    def failing_fdopen(fd, *args, **kwargs):
        raise OSError("fdopen refused")

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(pm.os, "close", recording_close)
    monkeypatch.setattr(pm.os, "fdopen", failing_fdopen)

    with caplog.at_level(logging.ERROR, logger="plugins.iptv_checker"):
        plugin._save_json_file(str(target), {"a": 1})

    assert made and made[0] in closed, "the descriptor from mkstemp was never closed"
    assert not target.exists()
    assert glob.glob(str(tmp_path / ".state.json.*")) == [], "temp file left behind"
    assert any("fdopen refused" in r.getMessage() for r in caplog.records)


def _age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_an_old_leftover_temp_file_of_this_plugin_is_removed(plugin, tmp_path):
    leftover = tmp_path / ".results.json.a1b2c3d4.tmp"
    leftover.write_text("crashed write", encoding="utf-8")
    _age(leftover, 3600)

    plugin._remove_old_temp_files()

    assert not leftover.exists()


def test_a_fresh_temp_file_is_kept_because_a_save_may_be_writing_it(plugin, tmp_path):
    fresh = tmp_path / ".results.json.e5f6g7h8.tmp"
    fresh.write_text("writing now", encoding="utf-8")

    plugin._remove_old_temp_files()

    assert fresh.exists()


def test_unrelated_files_are_kept_even_when_old(plugin, tmp_path):
    keep = [
        tmp_path / "notes.txt",
        tmp_path / ".results.json.bak",
        tmp_path / ".other.json.a1b2c3d4.tmp",
        tmp_path / ".results.json.short.tmp",
        tmp_path / "results.json.a1b2c3d4.tmp",
    ]
    for path in keep:
        path.write_text("keep", encoding="utf-8")
        _age(path, 3600)

    plugin._remove_old_temp_files()

    assert all(path.exists() for path in keep)


def test_the_startup_normalise_also_removes_old_leftovers(plugin, tmp_path):
    leftover = tmp_path / ".progress.json.a1b2c3d4.tmp"
    leftover.write_text("crashed", encoding="utf-8")
    _age(leftover, 3600)

    plugin._normalize_stale_progress()

    assert not leftover.exists()
