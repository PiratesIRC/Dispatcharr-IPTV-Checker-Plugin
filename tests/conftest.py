"""Shared fixtures for the IPTV Checker plugin test suite.

The plugin imports Dispatcharr/Django modules at module level. Those modules
only exist inside the Dispatcharr container, so we inject lightweight stubs
into sys.modules BEFORE importing the plugin. This mirrors how the plugin
already degrades gracefully for the optional ChannelService import.
"""
import sys
import threading
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeQuerySet(list):
    """Just enough of a Django queryset for the code paths under test."""

    def values_list(self, field, flat=False):
        return list(self)

    def filter(self, **kwargs):
        return self


class FakeManager:
    """Configurable stand-in for Model.objects. Set .live_ids per test."""

    def __init__(self):
        self.live_ids = []

    def filter(self, **kwargs):
        return FakeQuerySet(self.live_ids)

    def all(self):
        return FakeQuerySet(self.live_ids)


def _make_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def _install_dispatcharr_stubs():
    if "apps.channels.models" in sys.modules:
        return

    class Channel:
        objects = FakeManager()

    class ChannelGroup:
        objects = FakeManager()

    class Stream:
        objects = FakeManager()

    class ChannelStream:
        objects = FakeManager()

    class ChannelProfileMembership:
        objects = FakeManager()

    _make_module("apps")
    _make_module("apps.channels")
    _make_module(
        "apps.channels.models",
        Channel=Channel,
        ChannelGroup=ChannelGroup,
        Stream=Stream,
        ChannelStream=ChannelStream,
        ChannelProfileMembership=ChannelProfileMembership,
    )

    class _Transaction:
        @staticmethod
        def atomic(*args, **kwargs):
            class _Ctx:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return _Ctx()

    _make_module("django")
    _make_module("django.db", transaction=_Transaction())
    _make_module("core")
    _make_module("core.utils", send_websocket_update=lambda *a, **k: None)


_install_dispatcharr_stubs()
sys.path.insert(0, str(PROJECT_ROOT))

import iptv_checker.plugin as plugin_mod  # noqa: E402


@pytest.fixture(scope="session")
def pmod():
    """The imported plugin module (with Dispatcharr stubs in place)."""
    return plugin_mod


@pytest.fixture
def plugin(tmp_path):
    """A Plugin instance built without running __init__ (no scheduler threads,
    no /data file I/O). Only the attributes the tested methods touch are set.
    """
    inst = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    inst.key = "iptv_checker"
    inst.version = "0.0.0-test"
    inst._rate_limit_guard = plugin_mod.RateLimitGuard()
    inst._stop_event = threading.Event()
    inst._active_window_end = None
    inst._active_window_tz = None
    inst.results_file = str(tmp_path / "results.json")
    inst.loaded_channels_file = str(tmp_path / "loaded_channels.json")
    inst.pending_resume_file = str(tmp_path / "pending_resume.json")
    inst.progress_file = str(tmp_path / "progress.json")
    return inst


@pytest.fixture
def quiet_logger():
    import logging

    logger = logging.getLogger("iptv_checker.tests")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


class FakeClock:
    """Deterministic replacement for time.time / time.sleep."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch, pmod):
    clock = FakeClock()
    monkeypatch.setattr(pmod.time, "time", clock.time)
    monkeypatch.setattr(pmod.time, "sleep", clock.sleep)
    return clock
