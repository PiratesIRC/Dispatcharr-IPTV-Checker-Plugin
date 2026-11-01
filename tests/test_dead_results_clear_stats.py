"""A Dead check_stream result clears the stream's stored stats.

A connection failure (timeout, 403 Forbidden, refused) used to return
resolution 0x0 with width 0 and height 0. Those values are not None, so
_update_dispatcharr_metadata skipped its all_none clearing branch and MERGED
them into the stored stats, leaving the codec, frame rate and bitrate from an
earlier healthy check on a dead stream (measured on the 2026-10-30 run: 1302
Dead streams held 0x0 plus a stale codec or bitrate). Every Dead result must
carry all-None metadata so the stored stats are replaced by {}, which
Stream-Mapparr's _stream_stats_says_dead reads as dead.
"""
import subprocess
import sys

import pytest


_SETTINGS = {"probe_timeout": 1, "ffprobe_analysis_duration": 1}

_STALE_STATS = {
    "resolution": "1920x1080", "width": 1920, "height": 1080,
    "video_codec": "h264", "source_fps": 30.0, "video_bitrate": 2656,
    "audio_codec": "aac", "sample_rate": 48000, "audio_channels": "stereo",
}


class _FakeCompleted:
    def __init__(self, returncode=1, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stream():
    return {"stream_url": "http://example.com/live/1.ts", "channel_name": "Test", "stream_id": 1}


def _timeout(*a, **k):
    raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)


def _stderr(text):
    return lambda *a, **k: _FakeCompleted(stderr=text)


_DEAD_CAUSES = {
    "timeout": _timeout,
    "403": _stderr("Server returned 403 Forbidden (access denied)"),
    "refused": _stderr("Connection refused"),
}


def _dead_result(plugin, pmod, monkeypatch, quiet_logger, cause):
    monkeypatch.setattr(pmod.subprocess, "run", _DEAD_CAUSES[cause])
    result = plugin.check_stream(_stream(), 1, 0, quiet_logger, skip_retries=True, settings=_SETTINGS)
    assert result["status"] == "Dead"
    return result


@pytest.mark.parametrize("cause", sorted(_DEAD_CAUSES))
def test_dead_result_nulls_every_metadata_value(plugin, pmod, monkeypatch, quiet_logger, cause):
    result = _dead_result(plugin, pmod, monkeypatch, quiet_logger, cause)
    metadata = result["dispatcharr_metadata"]
    assert metadata, "a Dead result must still reach the writer (truthy dict)"
    assert set(metadata.values()) == {None}


class _FakeStreamRow:
    def __init__(self, stats):
        self.stream_stats = dict(stats)
        self.saved_fields = None

    def save(self, update_fields=None):
        self.saved_fields = update_fields


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeObjects:
    def __init__(self, row):
        self._row = row

    def filter(self, **kwargs):
        return _FakeQuery(self._row)


class _NoMergeChannelService:
    calls = []

    @classmethod
    def _update_stream_stats_in_db(cls, **kwargs):
        cls.calls.append(kwargs)
        return True


@pytest.mark.parametrize("cause", sorted(_DEAD_CAUSES))
def test_dead_result_replaces_stale_stats_with_empty(plugin, pmod, monkeypatch, quiet_logger, cause):
    """End to end through the writer: the old healthy stats are gone, not merged."""
    result = _dead_result(plugin, pmod, monkeypatch, quiet_logger, cause)

    row = _FakeStreamRow(_STALE_STATS)
    stream_model = sys.modules["apps.channels.models"].Stream
    monkeypatch.setattr(stream_model, "objects", _FakeObjects(row))
    monkeypatch.setattr(pmod, "DISPATCHARR_INTEGRATION_AVAILABLE", True)
    _NoMergeChannelService.calls = []
    monkeypatch.setattr(pmod, "ChannelService", _NoMergeChannelService, raising=False)

    ok = plugin._update_dispatcharr_metadata(
        {"uuid": "u-1"}, 1, result["dispatcharr_metadata"], quiet_logger
    )

    assert ok is True
    assert row.stream_stats == {}
    assert row.saved_fields == ["stream_stats"]
    assert _NoMergeChannelService.calls == []
