"""The progress file, the reload flag and every JSON state file are created owner-only (0o600).

The mode is asserted on the os.open argument, not on stat(): Windows has no POSIX permission
bits, and the Hub's CodeQL rule judges the mode written in the source.
"""
import json
import os

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
