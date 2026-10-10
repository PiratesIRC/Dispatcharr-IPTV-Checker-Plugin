"""A Skipped check_stream result writes no stream stats (issue #30).

A Skipped stream was not judged, so its result must carry an EMPTY
dispatcharr_metadata. Callers write metadata only when that dict is truthy,
so an empty dict leaves the stream's stored stats alone. A 0x0 resolution
would read as dead to other plugins (Stream-Mapparr's _stream_stats_says_dead).
"""
import subprocess


class _FakeCompleted:
    def __init__(self, returncode=1, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_SETTINGS = {"probe_timeout": 1, "ffprobe_analysis_duration": 1}


def _stream(url="http://example.com/live/1.ts"):
    return {"stream_url": url, "channel_name": "Test", "stream_id": 1}


def _probe_stderr(plugin, pmod, monkeypatch, quiet_logger, stderr):
    """check_stream against an ffprobe that fails with `stderr`."""
    monkeypatch.setattr(
        pmod.subprocess, "run", lambda *a, **k: _FakeCompleted(stderr=stderr)
    )
    return plugin.check_stream(_stream(), 1, 0, quiet_logger, skip_retries=True, settings=_SETTINGS)


def _probe_audio_only(plugin, pmod, monkeypatch, quiet_logger):
    """check_stream against an ffprobe that reports an audio-only stream."""
    import json

    probe = {
        "streams": [{"codec_type": "audio", "index": 0, "codec_name": "aac"}],
        "format": {"format_name": "mpegts"},
    }
    monkeypatch.setattr(
        pmod.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout=json.dumps(probe)),
    )
    return plugin.check_stream(_stream(), 1, 0, quiet_logger, skip_retries=True, settings=_SETTINGS)


def test_streamlink_only_skipped_writes_no_stats(plugin, pmod, monkeypatch, quiet_logger):
    result = plugin.check_stream(
        _stream("https://twitch.tv/somechannel"), 1, 0, quiet_logger,
        skip_retries=True, settings=_SETTINGS,
    )
    assert result["status"] == "Skipped"
    assert result["dispatcharr_metadata"] == {}


def test_rate_limited_skipped_writes_no_stats(plugin, pmod, monkeypatch, quiet_logger):
    result = _probe_stderr(plugin, pmod, monkeypatch, quiet_logger,
                           "Server returned HTTP error 429 Too Many Requests")
    assert result["status"] == "Skipped"
    assert result["error_type"] == "Rate Limited"
    assert result["dispatcharr_metadata"] == {}


def test_no_video_stream_skipped_writes_no_stats(plugin, pmod, monkeypatch, quiet_logger):
    result = _probe_audio_only(plugin, pmod, monkeypatch, quiet_logger)
    assert result["status"] == "Skipped"
    assert result["error_type"] == "No Video Stream"
    assert result["dispatcharr_metadata"] == {}


def test_a_dead_result_still_writes_zero_dimensions(plugin, pmod, monkeypatch, quiet_logger):
    """Dead is unchanged: a timed-out probe keeps the 0x0 metadata."""
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)

    monkeypatch.setattr(pmod.subprocess, "run", _timeout)
    result = plugin.check_stream(_stream(), 1, 0, quiet_logger, skip_retries=True, settings=_SETTINGS)
    assert result["status"] == "Dead"
    assert result["dispatcharr_metadata"]["resolution"] == "0x0"
    assert result["dispatcharr_metadata"]["width"] == 0
    assert result["dispatcharr_metadata"]["height"] == 0
