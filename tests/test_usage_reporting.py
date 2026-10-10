"""IPTV Checker reports its Streams Checked total through the vendored usage client.

Three call sites: the end of run() for actions that finish in the request (live settings),
the end of every thread started by _try_start_thread, and _record_streams_checked after a
successful ledger write (forced only when streams were checked). The checkbox is a static
field in plugin.json whose help text must equal what with_usage_field generates.
"""
import json
import pathlib

import pytest

import threading

from iptv_checker import plugin as plugin_module
from iptv_checker import usage_client
from iptv_checker.plugin import PluginConfig

ROOT = pathlib.Path(__file__).resolve().parent.parent


class FakeUsage:
    def __init__(self):
        self.calls = []

    def report(self, settings=None, logger=None, force=False):
        self.calls.append((settings, force))


@pytest.fixture
def usage(monkeypatch):
    fake = FakeUsage()
    monkeypatch.setattr(plugin_module, "USAGE", fake)
    return fake


def test_the_reporter_is_configured_for_iptv_checker():
    u = plugin_module.USAGE
    assert (u.plugin, u.counter, u.label) == ("iptv_checker", "streams_checked", "Streams Checked")
    assert u.settings_fn is not None


def test_the_total_sums_the_stream_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "counts.jsonl"
    ledger.write_text(json.dumps({"streams": 5}) + "\n" + json.dumps({"streams": 7}) + "\n", encoding="utf-8")
    monkeypatch.setattr(PluginConfig, "STREAM_COUNT_LEDGER_FILE", str(ledger))
    assert plugin_module.USAGE.total_fn() == 12


def test_the_manifest_checkbox_matches_the_client_text():
    fields = json.loads((ROOT / "iptv_checker" / "plugin.json").read_text(encoding="utf-8"))["fields"]
    expected = usage_client.with_usage_field([], plugin_module.USAGE)[0]
    assert fields[-1] == expected
    assert sum(1 for f in fields if f.get("id") == "share_usage_counts") == 1


def test_a_failed_ledger_write_does_not_report(plugin, usage, tmp_path, monkeypatch):
    monkeypatch.setattr(PluginConfig, "STREAM_COUNT_LEDGER_FILE", str(tmp_path / "no-dir" / "c.jsonl"))
    assert plugin._record_streams_checked(4, "sequential", plugin_module.LOGGER) is False
    assert usage.calls == []


def test_a_recorded_pass_reports_and_forces_when_streams_were_checked(plugin, usage, tmp_path, monkeypatch):
    monkeypatch.setattr(PluginConfig, "STREAM_COUNT_LEDGER_FILE", str(tmp_path / "c.jsonl"))
    assert plugin._record_streams_checked(4, "sequential", plugin_module.LOGGER) is True
    assert usage.calls == [(None, True)]


def test_a_zero_count_pass_reports_without_forcing(plugin, usage, tmp_path, monkeypatch):
    monkeypatch.setattr(PluginConfig, "STREAM_COUNT_LEDGER_FILE", str(tmp_path / "c.jsonl"))
    assert plugin._record_streams_checked(0, "parallel", plugin_module.LOGGER) is True
    assert usage.calls == [(None, False)]


def test_a_background_thread_reports_when_it_finishes(plugin, usage):
    plugin._thread = None
    plugin._thread_lock = threading.Lock()
    ran = []
    assert plugin._try_start_thread(lambda a, b: ran.append((a, b)), ("s", "l")) is True
    plugin._thread.join(5)
    assert ran == [("s", "l")]
    assert usage.calls == [(None, False)]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_background_thread_that_raises_still_reports(plugin, usage):
    plugin._thread = None
    plugin._thread_lock = threading.Lock()

    def boom(a, b):
        raise RuntimeError("background failed")

    assert plugin._try_start_thread(boom, ("s", "l")) is True
    plugin._thread.join(5)
    assert usage.calls == [(None, False)]


def test_a_reporter_that_raises_never_breaks_the_ledger_write(plugin, monkeypatch, tmp_path):
    class Exploding:
        def report(self, settings=None, logger=None, force=False):
            raise RuntimeError("boom")

    monkeypatch.setattr(plugin_module, "USAGE", Exploding())
    ledger = tmp_path / "c.jsonl"
    monkeypatch.setattr(PluginConfig, "STREAM_COUNT_LEDGER_FILE", str(ledger))
    assert plugin._record_streams_checked(2, "sequential", plugin_module.LOGGER) is True
    assert ledger.read_text(encoding="utf-8").count("\n") == 1


def test_a_request_action_reports_with_the_live_settings(plugin, usage, monkeypatch):
    settings = {"share_usage_counts": True}
    monkeypatch.setattr(plugin, "validate_settings_action",
                        lambda s, l: {"status": "ok", "message": "fine"})
    result = plugin.run("validate_settings", {}, {"settings": settings, "logger": plugin_module.LOGGER})
    assert result == {"status": "ok", "message": "fine"}
    assert usage.calls == [(settings, False)]


def test_a_background_action_does_not_report_from_run(plugin, usage, monkeypatch):
    monkeypatch.setattr(plugin, "check_streams_action",
                        lambda s, l, context=None: {"status": "ok", "message": "started", "background": True})
    result = plugin.run("check_streams", {}, {"settings": {}, "logger": plugin_module.LOGGER})
    assert result["background"] is True
    assert usage.calls == []


def test_a_failing_action_does_not_report(plugin, usage, monkeypatch):
    plugin.check_progress = {"status": "running"}

    def boom(s, l):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin, "validate_settings_action", boom)
    result = plugin.run("validate_settings", {}, {"settings": {}, "logger": plugin_module.LOGGER})
    assert result == {"status": "error", "message": "boom"}
    assert usage.calls == []
