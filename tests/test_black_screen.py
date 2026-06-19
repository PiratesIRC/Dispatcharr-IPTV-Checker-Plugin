"""Black-screen detection: blackdetect parsing, ffmpeg wrapper, and the
check_stream integration that reclassifies a black Alive stream as Dead.

Feature spec: docs/superpowers/specs/2026-06-19-black-screen-detection-design.md
"""
import json  # noqa: F401 — used by Task 4's _load_fields() helper


# ---- Task 1: _parse_blackdetect_output ----------------------------------

# A realistic ffmpeg -loglevel info stderr fragment with one black segment.
_ONE_SEGMENT = (
    "Input #0, mpegts, from 'http://x/1.ts':\n"
    "  Duration: N/A, start: 1.400000, bitrate: N/A\n"
    "[blackdetect @ 0x55e0] black_start:0 black_end:6.0 black_duration:6\n"
)

_TWO_SEGMENTS = (
    "[blackdetect @ 0x1] black_start:0 black_end:2.5 black_duration:2.5\n"
    "[blackdetect @ 0x1] black_start:3.0 black_end:6.0 black_duration:3\n"
)

_NO_SEGMENT = (
    "Input #0, mpegts, from 'http://x/1.ts':\n"
    "  Stream #0:0: Video: h264, yuv420p, 1920x1080, 30 fps\n"
    "frame=  180 fps=0.0 q=-0.0 Lsize=N/A time=00:00:06.00 bitrate=N/A\n"
)


def test_parse_single_black_segment(pmod):
    segs = pmod.Plugin._parse_blackdetect_output(_ONE_SEGMENT)
    assert segs == [(0.0, 6.0, 6.0)]


def test_parse_multiple_black_segments_in_order(pmod):
    segs = pmod.Plugin._parse_blackdetect_output(_TWO_SEGMENTS)
    assert segs == [(0.0, 2.5, 2.5), (3.0, 6.0, 3.0)]


def test_parse_no_black_segment(pmod):
    assert pmod.Plugin._parse_blackdetect_output(_NO_SEGMENT) == []


def test_parse_empty_or_garbage(pmod):
    assert pmod.Plugin._parse_blackdetect_output("") == []
    assert pmod.Plugin._parse_blackdetect_output("totally unrelated text") == []
    assert pmod.Plugin._parse_blackdetect_output(None) == []


# ---- Task 2: _check_black_screen ----------------------------------------

import subprocess  # noqa: E402


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _ffmpeg_run(stderr="", returncode=0, capture=None):
    """Return a fake subprocess.run that records the ffmpeg command."""
    def _run(cmd, *a, **k):
        if capture is not None:
            capture.append(cmd)
        return _FakeCompleted(stderr=stderr, returncode=returncode)
    return _run


_BS_SETTINGS = {
    "ffmpeg_path": "/usr/local/bin/ffmpeg",
    "black_screen_sample_seconds": 6,
    "black_screen_min_black_seconds": 3,
    "black_screen_ffmpeg_timeout": 20,
}


def test_check_black_true_on_segment(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_ONE_SEGMENT, returncode=0))
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is True


def test_check_black_true_even_on_nonzero_exit(plugin, pmod, monkeypatch, quiet_logger):
    # blackdetect often prints a segment then ffmpeg exits non-zero (stream ends).
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_ONE_SEGMENT, returncode=1))
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is True


def test_check_black_false_no_segment_clean_exit(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_NO_SEGMENT, returncode=0))
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is False


def test_check_black_none_on_nonzero_without_segment(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr="Server returned 500", returncode=1))
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is None


def test_check_black_none_when_ffmpeg_missing(plugin, pmod, monkeypatch, quiet_logger):
    def _boom(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(pmod.subprocess, "run", _boom)
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is None


def test_check_black_none_on_timeout(plugin, pmod, monkeypatch, quiet_logger):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=20)
    monkeypatch.setattr(pmod.subprocess, "run", _boom)
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is None


def test_check_black_command_shape(plugin, pmod, monkeypatch, quiet_logger):
    capture = []
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_NO_SEGMENT, capture=capture))
    plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger)
    cmd = capture[0]
    # input options precede -i; uses -rw_timeout (not -timeout); info loglevel.
    assert cmd[0] == "/usr/local/bin/ffmpeg"
    assert "-rw_timeout" in cmd and "-timeout" not in cmd
    i_idx = cmd.index("-i")
    assert cmd.index("-rw_timeout") < i_idx
    assert cmd.index("-user_agent") < i_idx
    assert "-loglevel" in cmd and cmd[cmd.index("-loglevel") + 1] == "info"
    assert any(p.startswith("blackdetect=d=3:pic_th=0.98") for p in cmd)
    assert cmd[-3:] == ["-f", "null", "-"]
    assert cmd[i_idx + 1] == "http://x/1.ts"


# ---- Task 3: check_stream integration -----------------------------------

def _video_stream():
    return {
        "codec_type": "video", "index": 0,
        "width": 1920, "height": 1080, "r_frame_rate": "30/1",
        "codec_name": "h264", "pix_fmt": "yuv420p",
    }


def _dual_run(probe_data, ffmpeg_stderr="", ffmpeg_rc=0, capture=None):
    """Fake subprocess.run that answers ffprobe with JSON and ffmpeg
    (any command containing a blackdetect filter) with stderr."""
    def _run(cmd, *a, **k):
        if capture is not None:
            capture.append(cmd)
        is_ffmpeg = any("blackdetect" in str(p) for p in cmd)
        if is_ffmpeg:
            return _FakeCompleted(stderr=ffmpeg_stderr, returncode=ffmpeg_rc)
        return _FakeCompleted(stdout=json.dumps(probe_data), returncode=0)
    return _run


def _probe():
    return {"streams": [_video_stream()], "format": {"format_name": "mpegts"}}


def _run_check(plugin, settings, quiet_logger):
    stream = {"stream_url": "http://x/1.ts", "channel_name": "T", "stream_id": 1}
    base = {"probe_timeout": 1, "ffprobe_analysis_duration": 1}
    base.update(settings)
    return plugin.check_stream(stream, 1, 0, quiet_logger, skip_retries=True, settings=base)


def test_black_stream_becomes_dead(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run",
                        _dual_run(_probe(), ffmpeg_stderr=_ONE_SEGMENT, ffmpeg_rc=0))
    result = _run_check(plugin, {"black_screen_detection": True}, quiet_logger)
    assert result["status"] == "Dead"
    assert result["error_type"] == "Black Screen"
    # Metadata must be all-null so _update_dispatcharr_metadata clears stats.
    assert all(v is None for v in result["dispatcharr_metadata"].values())


def test_alive_stream_stays_alive_when_not_black(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run",
                        _dual_run(_probe(), ffmpeg_stderr=_NO_SEGMENT, ffmpeg_rc=0))
    result = _run_check(plugin, {"black_screen_detection": True}, quiet_logger)
    assert result["status"] == "Alive"


def test_toggle_off_never_invokes_ffmpeg(plugin, pmod, monkeypatch, quiet_logger):
    capture = []
    monkeypatch.setattr(pmod.subprocess, "run",
                        _dual_run(_probe(), ffmpeg_stderr=_ONE_SEGMENT, capture=capture))
    result = _run_check(plugin, {"black_screen_detection": False}, quiet_logger)
    assert result["status"] == "Alive"
    assert not any(any("blackdetect" in str(p) for p in c) for c in capture)


def test_ffmpeg_error_fails_open_to_alive(plugin, pmod, monkeypatch, quiet_logger):
    # ffmpeg returns None verdict (non-zero, no segment) -> stays Alive.
    monkeypatch.setattr(pmod.subprocess, "run",
                        _dual_run(_probe(), ffmpeg_stderr="boom", ffmpeg_rc=1))
    result = _run_check(plugin, {"black_screen_detection": True}, quiet_logger)
    assert result["status"] == "Alive"


def test_stop_event_skips_black_check(plugin, pmod, monkeypatch, quiet_logger):
    capture = []
    monkeypatch.setattr(pmod.subprocess, "run",
                        _dual_run(_probe(), ffmpeg_stderr=_ONE_SEGMENT, capture=capture))
    plugin._stop_event.set()
    result = _run_check(plugin, {"black_screen_detection": True}, quiet_logger)
    assert result["status"] == "Alive"
    assert not any(any("blackdetect" in str(p) for p in c) for c in capture)
    plugin._stop_event.clear()
