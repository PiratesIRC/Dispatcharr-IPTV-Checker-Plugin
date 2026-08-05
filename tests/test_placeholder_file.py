"""Placeholder-file detection: a live stream that reports a fixed container
duration is serving a finite file, not a continuous broadcast.

Ground truth is a full ffprobe sweep of 156 provider channels (2026-07-11,
Sentinelarr docs/2026-07-11-24-7-black-slate-sweep.jsonl): all 138 healthy
channels reported format.duration null AND format.bit_rate null, while 19
reported a duration. Eighteen of those 19 were the SAME 10-minute black
placeholder file (duration 600.046444, bit_rate 192953) reused under many
content ids; the 19th was a 22.6 second file on a 24/7 channel.

Representative rows from that sweep are embedded below rather than read from
the Sentinelarr repository. A test that reads a file outside its own repo
fails whenever another session edits that file, which is a known problem with
Sentinelarr's own tests/test_client_parity.py.
"""
import json


# ---- _parse_container_duration ------------------------------------------

def test_duration_parsed_from_format_block(pmod):
    probe = {"format": {"duration": "600.046444"}}
    assert pmod.Plugin._parse_container_duration(probe) == 600.046444


def test_duration_absent_returns_none(pmod):
    # A continuous mpegts reports no duration at all. This is the healthy case.
    assert pmod.Plugin._parse_container_duration({"format": {"format_name": "mpegts"}}) is None


def test_duration_none_or_na_returns_none(pmod):
    assert pmod.Plugin._parse_container_duration({"format": {"duration": None}}) is None
    assert pmod.Plugin._parse_container_duration({"format": {"duration": "N/A"}}) is None
    assert pmod.Plugin._parse_container_duration({"format": {"duration": ""}}) is None


def test_duration_non_positive_returns_none(pmod):
    # A zero or negative duration is a probe artifact, not a finite file.
    assert pmod.Plugin._parse_container_duration({"format": {"duration": "0"}}) is None
    assert pmod.Plugin._parse_container_duration({"format": {"duration": "-1.5"}}) is None


def test_duration_missing_format_block_returns_none(pmod):
    assert pmod.Plugin._parse_container_duration({}) is None
    assert pmod.Plugin._parse_container_duration({"format": None}) is None
    assert pmod.Plugin._parse_container_duration(None) is None


# ---- _parse_container_bitrate_kbps --------------------------------------

def test_container_bitrate_converted_to_kbps(pmod):
    # 192953 bps -> 193 kbps, matching the int(round(...)) convention the
    # plugin already uses for video_bitrate.
    probe = {"format": {"bit_rate": "192953"}}
    assert pmod.Plugin._parse_container_bitrate_kbps(probe) == 193


def test_container_bitrate_absent_returns_none(pmod):
    assert pmod.Plugin._parse_container_bitrate_kbps({"format": {}}) is None
    assert pmod.Plugin._parse_container_bitrate_kbps({"format": {"bit_rate": "N/A"}}) is None
    assert pmod.Plugin._parse_container_bitrate_kbps(None) is None


# ---- ground-truth regression --------------------------------------------

# The 10-minute black placeholder, byte-identical across all 18 dead channels.
_SLATE = {"duration": "600.046444", "bit_rate": "192953"}
# The 19th duration-bearing row: a 22.6 second file, no bit_rate reported.
_SHORT_FILE = {"duration": "22.646444"}
# Healthy live channels: both fields absent. Three real resolutions from the
# sweep, including 1920x1080 -- ten HEALTHY channels share the dead set's
# resolution, so frame size discriminates nothing and must not be gated on.
_LIVE = {"format_name": "mpegts"}


def test_slate_fingerprint_is_flagged(pmod):
    assert pmod.Plugin._is_placeholder_file({"format": _SLATE}) is True


def test_short_finite_file_is_flagged(pmod):
    assert pmod.Plugin._is_placeholder_file({"format": _SHORT_FILE}) is True


def test_healthy_live_stream_is_not_flagged(pmod):
    assert pmod.Plugin._is_placeholder_file({"format": _LIVE}) is False


def test_ground_truth_counts(pmod):
    """18 slates + 1 short file flagged; 138 healthy channels untouched."""
    dead = [{"format": dict(_SLATE)} for _ in range(18)] + [{"format": dict(_SHORT_FILE)}]
    live = [{"format": dict(_LIVE)} for _ in range(138)]
    assert sum(1 for p in dead if pmod.Plugin._is_placeholder_file(p)) == 19
    assert sum(1 for p in live if pmod.Plugin._is_placeholder_file(p)) == 0


# ---- check_stream integration -------------------------------------------

class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _video_stream():
    return {
        "codec_type": "video", "index": 0,
        "width": 1920, "height": 1080, "r_frame_rate": "30/1",
        "codec_name": "h264", "pix_fmt": "yuv420p",
    }


def _probe_run(probe_data):
    def _run(cmd, *a, **k):
        return _FakeCompleted(stdout=json.dumps(probe_data), returncode=0)
    return _run


def _slate_probe():
    fmt = {"format_name": "mpegts"}
    fmt.update(_SLATE)
    return {"streams": [_video_stream()], "format": fmt}


def _live_probe():
    return {"streams": [_video_stream()], "format": dict(_LIVE)}


def _run_check(plugin, settings, quiet_logger):
    stream = {"stream_url": "http://x/1.ts", "channel_name": "T", "stream_id": 1}
    base = {"probe_timeout": 1, "ffprobe_analysis_duration": 1}
    base.update(settings)
    return plugin.check_stream(stream, 1, 0, quiet_logger, skip_retries=True, settings=base)


def test_placeholder_becomes_dead_when_enabled(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _probe_run(_slate_probe()))
    result = _run_check(plugin, {"placeholder_file_detection": True}, quiet_logger)
    assert result["status"] == "Dead"
    assert result["error_type"] == "Placeholder File"


def test_placeholder_dead_result_nulls_metadata(plugin, pmod, monkeypatch, quiet_logger):
    """Every metadata value must be None so _update_dispatcharr_metadata's
    all_none branch CLEARS the stored stats. A Dead result carrying real
    metadata would instead write stats onto a dead channel."""
    monkeypatch.setattr(pmod.subprocess, "run", _probe_run(_slate_probe()))
    result = _run_check(plugin, {"placeholder_file_detection": True}, quiet_logger)
    assert set(result["dispatcharr_metadata"].values()) == {None}


def test_placeholder_stays_alive_when_disabled(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _probe_run(_slate_probe()))
    result = _run_check(plugin, {"placeholder_file_detection": False}, quiet_logger)
    assert result["status"] == "Alive"


def test_evidence_recorded_even_when_detection_disabled(plugin, pmod, monkeypatch, quiet_logger):
    """The duration and container bitrate are recorded for every stream
    regardless of the setting, so the operator can see the fingerprint in the
    CSV before deciding to act on it."""
    monkeypatch.setattr(pmod.subprocess, "run", _probe_run(_slate_probe()))
    result = _run_check(plugin, {"placeholder_file_detection": False}, quiet_logger)
    assert result["ffprobe_data"]["container_duration_seconds"] == 600.046444
    assert result["ffprobe_data"]["container_bitrate_kbps"] == 193


def test_no_evidence_keys_on_healthy_live_stream(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _probe_run(_live_probe()))
    result = _run_check(plugin, {"placeholder_file_detection": True}, quiet_logger)
    assert result["status"] == "Alive"
    assert "container_duration_seconds" not in result["ffprobe_data"]


def test_healthy_stream_unaffected_when_enabled(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _probe_run(_live_probe()))
    assert _run_check(plugin, {"placeholder_file_detection": True}, quiet_logger)["status"] == "Alive"


# ---- routing into the existing dead-channel actions ---------------------

def test_placeholder_counts_as_dead_nonblack(pmod):
    """No new rename/move actions are added. A placeholder is routed through
    the existing dead-channel rename/move/delete, which match _is_dead_nonblack."""
    result = {"status": "Dead", "error_type": "Placeholder File"}
    assert pmod.Plugin._is_dead_nonblack(result) is True
    assert pmod.Plugin._is_black_screen(result) is False
