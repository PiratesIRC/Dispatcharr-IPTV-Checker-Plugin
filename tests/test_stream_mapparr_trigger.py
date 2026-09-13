"""The post-scan hand-off to Stream-Mapparr.

Dispatcharr's connect event bus refuses event names outside its fixed list, so
a scheduled scan that finished the whole list calls the Stream-Mapparr plugin
in process through PluginManager.run_action, the same call Dispatcharr makes
for its own m3u_refresh event. The wiring into _execute_scheduled_check is
covered by tests/test_scheduled_check_interruption.py, which lists the method
among the post-scan phases; this file pins the method itself.
"""
import io
import json
import logging
import os
import sys
import types

import pytest

log = logging.getLogger("t")

_MANIFEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "iptv_checker", "plugin.json")


@pytest.fixture(scope="module")
def plugin_mod_manifest():
    with io.open(_MANIFEST, encoding="utf-8") as handle:
        return json.load(handle)


class _FakeManager:
    def __init__(self, registered=True, raises=None):
        self.registered = registered
        self.raises = raises
        self.calls = []

    def get_plugin(self, key):
        return object() if self.registered else None

    def run_action(self, key, action_id, params=None):
        self.calls.append((key, action_id, params))
        if self.raises is not None:
            raise self.raises
        return {"status": "success"}


@pytest.fixture
def loader(monkeypatch):
    """Install a fake apps.plugins.loader so the lazy import inside the method resolves."""
    manager = _FakeManager()
    module = types.ModuleType("apps.plugins.loader")
    module.PluginManager = types.SimpleNamespace(get=lambda: manager)
    monkeypatch.setitem(sys.modules, "apps.plugins.loader", module)
    return manager


def test_the_setting_sits_inside_the_auto_run_section(plugin_mod_manifest):
    ids = [f["id"] for f in plugin_mod_manifest["fields"]]
    start = ids.index("_section_auto_run")
    end = ids.index("_section_auto_rename_move")
    assert "scheduler_trigger_stream_mapparr" in ids[start:end]


def test_the_setting_defaults_off(plugin_mod_manifest):
    field = next(f for f in plugin_mod_manifest["fields"]
                 if f["id"] == "scheduler_trigger_stream_mapparr")
    assert field["type"] == "boolean"
    assert field["default"] is False


def test_it_calls_the_stream_mapparr_handler_with_a_small_payload(plugin, loader):
    plugin.check_progress = {"current": 7, "total": 7, "status": "idle"}
    assert plugin._trigger_stream_mapparr({}, log) is True
    assert len(loader.calls) == 1
    key, action_id, params = loader.calls[0]
    assert key == "stream-mapparr"
    assert action_id == "on_iptv_checker_scan"
    assert params["event"] == "iptv_checker_scan_complete"
    payload = params["payload"]
    assert payload["source"] == "iptv_checker"
    assert payload["streams_checked"] == 7
    assert payload["version"] == plugin.version
    assert "finished_at" in payload
    # Nothing that names a provider or a stream may travel in the payload.
    assert set(payload) == {"source", "version", "finished_at", "streams_checked"}


def test_it_skips_quietly_when_stream_mapparr_is_not_installed(plugin, loader):
    loader.registered = False
    assert plugin._trigger_stream_mapparr({}, log) is False
    assert loader.calls == []


def test_a_disabled_stream_mapparr_is_reported_not_raised(plugin, loader):
    loader.raises = PermissionError("Plugin 'stream-mapparr' is disabled")
    assert plugin._trigger_stream_mapparr({}, log) is False


def test_any_other_failure_is_logged_and_swallowed(plugin, loader):
    loader.raises = RuntimeError("boom")
    assert plugin._trigger_stream_mapparr({}, log) is False


def test_a_missing_plugin_manager_module_is_logged_and_swallowed(plugin, monkeypatch):
    monkeypatch.delitem(sys.modules, "apps.plugins.loader", raising=False)
    monkeypatch.delitem(sys.modules, "apps.plugins", raising=False)
    assert plugin._trigger_stream_mapparr({}, log) is False


def test_the_streams_checked_count_survives_a_missing_progress_dict(plugin, loader):
    if hasattr(plugin, "check_progress"):
        del plugin.check_progress
    assert plugin._trigger_stream_mapparr({}, log) is True
    assert loader.calls[0][2]["payload"]["streams_checked"] is None
