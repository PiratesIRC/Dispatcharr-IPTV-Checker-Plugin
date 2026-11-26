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
