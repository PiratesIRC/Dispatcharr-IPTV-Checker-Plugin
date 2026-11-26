"""The progress file, the reload flag and every JSON state file are created owner-only (0o600).

The mode is asserted on the os.open argument, not on stat(): Windows has no POSIX permission
bits, and the Hub's CodeQL rule judges the mode written in the source.
"""
import json
import stat
import sys
import os

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


def test_save_json_file_creates_its_temp_file_owner_only(plugin, tmp_path, monkeypatch):
    seen = _spy_on_os_open(monkeypatch)
    target = tmp_path / "state.json"

    plugin._save_json_file(str(target), {"a": 1})

    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    temp_modes = [mode for path, mode in seen if path.endswith(".tmp")]
    assert temp_modes == [0o600]


def test_the_reload_flag_is_created_owner_only(plugin, tmp_path, monkeypatch):
    flag = tmp_path / "reload.flag"
    monkeypatch.setattr(pm.PluginConfig, "STORAGE_LAYOUT", None)
    monkeypatch.setattr(pm.PluginConfig, "SCHEDULER_RELOAD_FLAG", str(flag))
    seen = _spy_on_os_open(monkeypatch)

    plugin._request_scheduler_reload()

    assert flag.exists()
    assert [mode for path, mode in seen if path == str(flag)] == [0o600]


def test_a_stale_temp_file_is_removed_before_the_owner_only_create(plugin, tmp_path, monkeypatch):
    """A crashed earlier write can leave <file>.tmp at 0644. O_TRUNC would keep that mode and
    os.replace would carry it onto the state file, so the leftover is removed first."""
    events = []
    real_remove = os.remove
    real_open = os.open
    stale = str(tmp_path / "state.json.tmp")

    def spy_remove(path, *args, **kwargs):
        events.append(("remove", os.fspath(path)))
        return real_remove(path, *args, **kwargs)

    def spy_open(path, flags, mode=0o777, *args, **kwargs):
        events.append(("open", os.fspath(path), mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(pm.os, "remove", spy_remove)
    monkeypatch.setattr(pm.os, "open", spy_open)
    with open(stale, "w", encoding="utf-8") as fh:
        fh.write("old crashed write")

    plugin._save_json_file(str(tmp_path / "state.json"), {"a": 1})

    assert events[0] == ("remove", stale)
    assert events[1] == ("open", stale, 0o600)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_stale_0644_temp_file_does_not_leave_the_state_file_world_readable(plugin, tmp_path):
    target = tmp_path / "state.json"
    stale = str(target) + ".tmp"
    with open(stale, "w", encoding="utf-8") as fh:
        fh.write("old crashed write")
    os.chmod(stale, 0o644)

    plugin._save_json_file(str(target), {"a": 1})

    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert stat.S_IMODE(os.stat(target).st_mode) & 0o077 == 0
