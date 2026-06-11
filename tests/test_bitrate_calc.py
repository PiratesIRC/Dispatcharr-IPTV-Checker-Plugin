"""Packet-based video bitrate calculation in check_stream.

Covers the v1.26.1221035 incident: 22,924 kbps reported from a 2-packet
sample. Live MPEG-TS/HLS rarely exposes bit_rate, so the packet fallback is
the only reliable source — but only with a minimum sample size.
"""
import json


class _FakeCompleted:
    def __init__(self, stdout):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def _alive_probe(plugin, pmod, monkeypatch, quiet_logger, probe_data):
    monkeypatch.setattr(
        pmod.subprocess, "run", lambda *a, **k: _FakeCompleted(json.dumps(probe_data))
    )
    stream = {"stream_url": "http://example.com/live/1.ts", "channel_name": "Test", "stream_id": 1}
    settings = {"probe_timeout": 1, "ffprobe_analysis_duration": 1}
    return plugin.check_stream(stream, 1, 0, quiet_logger, skip_retries=True, settings=settings)


def _video_stream(index=0, **extra):
    base = {
        "codec_type": "video",
        "index": index,
        "width": 1920,
        "height": 1080,
        "r_frame_rate": "30/1",
        "codec_name": "h264",
        "pix_fmt": "yuv420p",
    }
    base.update(extra)
    return base


def _packets(count, size=1250, duration_time=0.01, stream_index=0):
    return [
        {"stream_index": stream_index, "size": str(size), "duration_time": str(duration_time)}
        for _ in range(count)
    ]


def test_bitrate_from_sufficient_packet_sample(plugin, pmod, monkeypatch, quiet_logger):
    """30 packets x 1250 B / 0.01 s each => exactly 1000 kbps."""
    result = _alive_probe(plugin, pmod, monkeypatch, quiet_logger, {
        "streams": [_video_stream()],
        "packets": _packets(30),
        "format": {"format_name": "mpegts"},
    })
    assert result["status"] == "Alive"
    assert result["dispatcharr_metadata"]["video_bitrate"] == 1000


def test_bitrate_suppressed_below_min_packet_sample(plugin, pmod, monkeypatch, quiet_logger):
    """A 2-packet sample produced 22924 kbps in production. Must stay None."""
    result = _alive_probe(plugin, pmod, monkeypatch, quiet_logger, {
        "streams": [_video_stream()],
        "packets": _packets(2, size=28655, duration_time=0.01),
        "format": {"format_name": "mpegts"},
    })
    assert result["status"] == "Alive"
    assert result["dispatcharr_metadata"]["video_bitrate"] is None


def test_min_packet_threshold_matches_config(pmod):
    assert pmod.PluginConfig.MIN_PACKETS_FOR_BITRATE_CALC == 30


def test_format_level_bitrate_preferred_over_packet_calc(plugin, pmod, monkeypatch, quiet_logger):
    result = _alive_probe(plugin, pmod, monkeypatch, quiet_logger, {
        "streams": [_video_stream()],
        "packets": _packets(100),
        "format": {"format_name": "mpegts", "bit_rate": "2500000"},
    })
    assert result["dispatcharr_metadata"]["video_bitrate"] == 2500


def test_bitrate_is_integer_kbps(plugin, pmod, monkeypatch, quiet_logger):
    """v1.26.1220052: rounded to whole kbps before storing in stream_stats."""
    result = _alive_probe(plugin, pmod, monkeypatch, quiet_logger, {
        "streams": [_video_stream()],
        "packets": _packets(31),  # 31 x 1250 B / 0.31 s => 1000.0 kbps exactly
        "format": {"format_name": "mpegts"},
    })
    assert isinstance(result["dispatcharr_metadata"]["video_bitrate"], int)


def test_combined_packets_and_frames_key_yields_no_bitrate(plugin, pmod, monkeypatch, quiet_logger):
    """When -show_frames AND -show_packets are passed, ffprobe emits a single
    'packets_and_frames' array. The parser only reads 'packets', so the
    fallback must not fire — this is why -show_frames stays out of the
    default ffprobe_flags."""
    result = _alive_probe(plugin, pmod, monkeypatch, quiet_logger, {
        "streams": [_video_stream()],
        "packets_and_frames": _packets(100),
        "format": {"format_name": "mpegts"},
    })
    assert result["status"] == "Alive"
    assert result["dispatcharr_metadata"]["video_bitrate"] is None


def test_audio_packets_excluded_from_video_bitrate(plugin, pmod, monkeypatch, quiet_logger):
    """Packets are filtered to the video stream index so audio doesn't dilute."""
    video_packets = _packets(30, size=1250, duration_time=0.01, stream_index=0)
    audio_packets = _packets(30, size=125, duration_time=0.01, stream_index=1)
    result = _alive_probe(plugin, pmod, monkeypatch, quiet_logger, {
        "streams": [
            _video_stream(index=0),
            {"codec_type": "audio", "index": 1, "codec_name": "aac"},
        ],
        "packets": video_packets + audio_packets,
        "format": {"format_name": "mpegts"},
    })
    assert result["dispatcharr_metadata"]["video_bitrate"] == 1000


def test_no_video_stream_is_dead(plugin, pmod, monkeypatch, quiet_logger):
    result = _alive_probe(plugin, pmod, monkeypatch, quiet_logger, {
        "streams": [{"codec_type": "audio", "index": 0, "codec_name": "aac"}],
        "format": {"format_name": "mpegts"},
    })
    assert result["status"] == "Dead"
    assert result["error_type"] == "No Video Stream"
