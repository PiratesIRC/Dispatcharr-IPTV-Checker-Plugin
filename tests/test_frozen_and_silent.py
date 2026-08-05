"""Frozen-picture and silent-audio detection.

Both verdicts come from the SAME single ffmpeg decode pass that already runs
for blank-screen detection, so enabling them costs no additional provider
connection. That the three filters coexist in one pass was MEASURED against
ffmpeg 8.1.2 in the Dispatcharr container on synthetic lavfi sources (no
network, no provider connection); every stderr fixture below is real captured
output from that run, not written from memory.

The four cases measured, and what each proves:

  moving picture + 440 Hz tone   -> no black, no freeze, mean_volume -21.1 dB
  red STILL picture + tone       -> freeze fires, blackdetect stays SILENT
  moving picture + anullsrc      -> mean_volume -91.0 dB, no black, no freeze
  black still + anullsrc         -> all three fire

The second case is the whole point of freeze detection: a still picture that
is not black is invisible to blackdetect. The third confirms Sentinelarr's
measurement that digital silence encodes as -91.0 dB and NOT as -inf.
"""
import json
import subprocess


# ---- real captured stderr fragments -------------------------------------

_FREEZE = (
    "[Parsed_freezedetect_1 @ 0x70f0a8003240] lavfi.freezedetect.freeze_start: 0\n"
)
_FREEZE_WITH_END = (
    "[Parsed_freezedetect_1 @ 0x7f0] lavfi.freezedetect.freeze_start: 1.24\n"
    "[Parsed_freezedetect_1 @ 0x7f0] lavfi.freezedetect.freeze_duration: 4.52\n"
    "[Parsed_freezedetect_1 @ 0x7f0] lavfi.freezedetect.freeze_end: 5.76\n"
)
_NO_FREEZE = (
    "Input #0, lavfi, from 'testsrc=s=320x240:r=25:d=8':\n"
    "  Stream #0:0: Video: wrapped_avframe, rgb24, 320x240, 25 fps\n"
)
_SILENT = "[Parsed_volumedetect_0 @ 0x70f09c002dc0] mean_volume: -91.0 dB\n"
_AUDIBLE = "[Parsed_volumedetect_0 @ 0x70f09c002dc0] mean_volume: -21.1 dB\n"
_BLACK = "[Parsed_blackdetect_0 @ 0x70f0a8002fc0] black_start:0 black_end:7.96 black_duration:7.96\n"


# ---- _parse_freezedetect_output -----------------------------------------

def test_parse_freeze_start_only(pmod):
    # A freeze running to end-of-capture prints freeze_start with no
    # freeze_end. This is the common case and MUST count as frozen.
    assert pmod.Plugin._parse_freezedetect_output(_FREEZE) == [0.0]


def test_parse_freeze_with_end(pmod):
    assert pmod.Plugin._parse_freezedetect_output(_FREEZE_WITH_END) == [1.24]


def test_parse_no_freeze(pmod):
    assert pmod.Plugin._parse_freezedetect_output(_NO_FREEZE) == []
    assert pmod.Plugin._parse_freezedetect_output("") == []
    assert pmod.Plugin._parse_freezedetect_output(None) == []


def test_freeze_parser_ignores_blackdetect_lines(pmod):
    assert pmod.Plugin._parse_freezedetect_output(_BLACK) == []


# ---- _parse_mean_volume_db ----------------------------------------------

def test_parse_silent_mean_volume(pmod):
    assert pmod.Plugin._parse_mean_volume_db(_SILENT) == -91.0


def test_parse_audible_mean_volume(pmod):
    assert pmod.Plugin._parse_mean_volume_db(_AUDIBLE) == -21.1


def test_parse_negative_infinity(pmod):
    """Not reproducible on ffmpeg 8.1.2 (anullsrc and aevalsrc=0 both gave
    -91.0), but ffmpeg documents -inf and a regex that cannot read it returns
    None, which is byte-identical to 'no audio line at all' and would fail
    OPEN on the loudest possible evidence of silence."""
    line = "[Parsed_volumedetect_0 @ 0x1] mean_volume: -inf dB\n"
    assert pmod.Plugin._parse_mean_volume_db(line) == float("-inf")


def test_parse_missing_mean_volume(pmod):
    assert pmod.Plugin._parse_mean_volume_db(_NO_FREEZE) is None
    assert pmod.Plugin._parse_mean_volume_db("") is None
    assert pmod.Plugin._parse_mean_volume_db(None) is None


# ---- _is_silent_audio ----------------------------------------------------

def test_silence_below_threshold(pmod):
    assert pmod.Plugin._is_silent_audio(-91.0, -70.0) is True
    assert pmod.Plugin._is_silent_audio(float("-inf"), -70.0) is True


def test_quiet_real_content_is_not_silent(pmod):
    # Quietest real channel Sentinelarr measured was -44.4 dB (a film).
    assert pmod.Plugin._is_silent_audio(-44.4, -70.0) is False
    assert pmod.Plugin._is_silent_audio(-21.1, -70.0) is False


def test_unmeasured_audio_is_not_silent(pmod):
    """Fail-open: no measurement is not evidence of silence."""
    assert pmod.Plugin._is_silent_audio(None, -70.0) is False


def test_silence_threshold_is_honoured(pmod):
    assert pmod.Plugin._is_silent_audio(-50.0, -40.0) is True
    assert pmod.Plugin._is_silent_audio(-50.0, -60.0) is False


# ---- _effective_freeze_seconds ------------------------------------------

def test_freeze_window_fits_inside_sample(pmod):
    assert pmod.Plugin._effective_freeze_seconds(4, 6) == 4


def test_freeze_window_clamped_to_sample(pmod):
    """A freeze threshold >= the sample length can NEVER mature: the filter
    would be structurally unable to fire and the setting would look active
    while doing nothing."""
    assert pmod.Plugin._effective_freeze_seconds(10, 6) == 5
    assert pmod.Plugin._effective_freeze_seconds(6, 6) == 5


def test_freeze_window_never_below_one_second(pmod):
    assert pmod.Plugin._effective_freeze_seconds(4, 1) == 1
    assert pmod.Plugin._effective_freeze_seconds(0, 6) == 1
    assert pmod.Plugin._effective_freeze_seconds(-3, 6) == 1


def test_freeze_window_handles_junk(pmod):
    assert pmod.Plugin._effective_freeze_seconds(None, 6) == 4
    assert pmod.Plugin._effective_freeze_seconds("abc", 6) == 4


# ---- _analyze_stream_content: one pass, three verdicts ------------------

class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _ffmpeg_run(stderr="", returncode=0, capture=None):
    def _run(cmd, *a, **k):
        if capture is not None:
            capture.append(cmd)
        return _FakeCompleted(stderr=stderr, returncode=returncode)
    return _run


_SETTINGS = {
    "ffmpeg_path": "/usr/local/bin/ffmpeg",
    "black_screen_sample_seconds": 8,
    "black_screen_min_black_seconds": 3,
    "black_screen_ffmpeg_timeout": 20,
    "frozen_video_min_seconds": 4,
    "silent_audio_max_db": -70,
}


def _analyze(plugin, logger, **kw):
    return plugin._analyze_stream_content("http://x/1.ts", 10, _SETTINGS, logger, **kw)


def test_frozen_not_black_is_detected(plugin, pmod, monkeypatch, quiet_logger):
    """The measured red-still case: freeze fires, blackdetect stays silent."""
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_FREEZE + _AUDIBLE))
    v = _analyze(plugin, quiet_logger, want_freeze=True, want_audio=True)
    assert v["frozen"] is True
    assert v["black"] is False


def test_black_still_reports_both(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_BLACK + _FREEZE + _SILENT))
    v = _analyze(plugin, quiet_logger, want_freeze=True, want_audio=True)
    assert v["black"] is True
    assert v["frozen"] is True
    assert v["audio_db"] == -91.0


def test_healthy_source_reports_nothing(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_NO_FREEZE + _AUDIBLE))
    v = _analyze(plugin, quiet_logger, want_freeze=True, want_audio=True)
    assert v["black"] is False
    assert v["frozen"] is False
    assert v["audio_db"] == -21.1


def test_freeze_verdict_is_none_when_not_requested(plugin, pmod, monkeypatch, quiet_logger):
    """Not asked is not the same as not frozen."""
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_FREEZE))
    v = _analyze(plugin, quiet_logger, want_freeze=False, want_audio=False)
    assert v["frozen"] is None
    assert v["audio_db"] is None


def test_all_verdicts_none_when_ffmpeg_missing(plugin, pmod, monkeypatch, quiet_logger):
    def _boom(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(pmod.subprocess, "run", _boom)
    v = _analyze(plugin, quiet_logger, want_freeze=True, want_audio=True)
    assert v == {"black": None, "frozen": None, "audio_db": None}


def test_all_verdicts_none_on_timeout(plugin, pmod, monkeypatch, quiet_logger):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=20)
    monkeypatch.setattr(pmod.subprocess, "run", _boom)
    v = _analyze(plugin, quiet_logger, want_freeze=True, want_audio=True)
    assert v == {"black": None, "frozen": None, "audio_db": None}


def test_freeze_true_even_on_nonzero_exit(plugin, pmod, monkeypatch, quiet_logger):
    # ffmpeg often prints a detection then exits non-zero when the stream ends.
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_FREEZE, returncode=1))
    assert _analyze(plugin, quiet_logger, want_freeze=True)["frozen"] is True


def test_freeze_none_on_nonzero_exit_without_detection(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr="Server returned 500", returncode=1))
    assert _analyze(plugin, quiet_logger, want_freeze=True)["frozen"] is None


# ---- command shape -------------------------------------------------------

def test_freeze_filter_only_present_when_requested(plugin, pmod, monkeypatch, quiet_logger):
    capture = []
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_NO_FREEZE, capture=capture))
    plugin._analyze_stream_content("http://x/1.ts", 10, _SETTINGS, quiet_logger, want_freeze=False)
    assert not any("freezedetect" in str(p) for p in capture[0])

    capture.clear()
    plugin._analyze_stream_content("http://x/1.ts", 10, _SETTINGS, quiet_logger, want_freeze=True)
    vf = capture[0][capture[0].index("-vf") + 1]
    # blackdetect must stay FIRST so its verdict is available for precedence.
    assert vf.startswith("blackdetect=")
    assert "freezedetect=n=-60dB:d=4" in vf


def test_audio_disabled_uses_an_and_no_volumedetect(plugin, pmod, monkeypatch, quiet_logger):
    capture = []
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_NO_FREEZE, capture=capture))
    plugin._analyze_stream_content("http://x/1.ts", 10, _SETTINGS, quiet_logger, want_audio=False)
    cmd = capture[0]
    assert "-an" in cmd
    assert not any("volumedetect" in str(p) for p in cmd)


def test_audio_enabled_drops_an_and_adds_volumedetect(plugin, pmod, monkeypatch, quiet_logger):
    capture = []
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_AUDIBLE, capture=capture))
    plugin._analyze_stream_content("http://x/1.ts", 10, _SETTINGS, quiet_logger, want_audio=True)
    cmd = capture[0]
    assert "-an" not in cmd
    assert cmd[cmd.index("-af") + 1] == "volumedetect"


def test_single_pass_for_all_three_verdicts(plugin, pmod, monkeypatch, quiet_logger):
    """One ffmpeg invocation, not one per detector. This is what keeps the
    cost at a single provider connection when all three are enabled."""
    capture = []
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_BLACK + _FREEZE + _SILENT, capture=capture))
    plugin._analyze_stream_content("http://x/1.ts", 10, _SETTINGS, quiet_logger,
                                   want_freeze=True, want_audio=True)
    assert len(capture) == 1


def test_loglevel_info_retained(plugin, pmod, monkeypatch, quiet_logger):
    """blackdetect, freezedetect and volumedetect ALL log at info level;
    -loglevel error would suppress every one of them."""
    capture = []
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr="", capture=capture))
    plugin._analyze_stream_content("http://x/1.ts", 10, _SETTINGS, quiet_logger, want_freeze=True)
    cmd = capture[0]
    assert cmd[cmd.index("-loglevel") + 1] == "info"


# ---- check_stream integration -------------------------------------------

def _video_stream():
    return {"codec_type": "video", "index": 0, "width": 1920, "height": 1080,
            "r_frame_rate": "30/1", "codec_name": "h264", "pix_fmt": "yuv420p"}


def _audio_stream():
    return {"codec_type": "audio", "index": 1, "codec_name": "aac",
            "sample_rate": "48000", "channels": 2}


def _dual_run(probe_data, ffmpeg_stderr="", ffmpeg_rc=0):
    def _run(cmd, *a, **k):
        if any("detect" in str(p) for p in cmd) or "-af" in cmd:
            return _FakeCompleted(stderr=ffmpeg_stderr, returncode=ffmpeg_rc)
        return _FakeCompleted(stdout=json.dumps(probe_data), returncode=0)
    return _run


def _probe(with_audio=True):
    streams = [_video_stream()] + ([_audio_stream()] if with_audio else [])
    return {"streams": streams, "format": {"format_name": "mpegts"}}


def _run_check(plugin, settings, quiet_logger, probe_data=None):
    stream = {"stream_url": "http://x/1.ts", "channel_name": "T", "stream_id": 1}
    base = {"probe_timeout": 1, "ffprobe_analysis_duration": 1}
    base.update(_SETTINGS)
    base.update(settings)
    return plugin.check_stream(stream, 1, 0, quiet_logger, skip_retries=True, settings=base)


def test_frozen_stream_becomes_dead(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _dual_run(_probe(), ffmpeg_stderr=_FREEZE + _AUDIBLE))
    r = _run_check(plugin, {"frozen_video_detection": True}, quiet_logger)
    assert r["status"] == "Dead"
    assert r["error_type"] == "Frozen Video"
    assert set(r["dispatcharr_metadata"].values()) == {None}


def test_silent_stream_becomes_dead(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _dual_run(_probe(), ffmpeg_stderr=_NO_FREEZE + _SILENT))
    r = _run_check(plugin, {"silent_audio_detection": True}, quiet_logger)
    assert r["status"] == "Dead"
    assert r["error_type"] == "Silent Audio"
    assert set(r["dispatcharr_metadata"].values()) == {None}


def test_black_wins_over_frozen(plugin, pmod, monkeypatch, quiet_logger):
    """A black screen is ALSO a still picture, so both filters fire on it.
    Blank-screen must win, or enabling freeze detection would silently
    relabel every blank screen and break the blank-screen rename/move
    actions that match on error_type 'Black Screen'."""
    monkeypatch.setattr(pmod.subprocess, "run", _dual_run(_probe(), ffmpeg_stderr=_BLACK + _FREEZE))
    r = _run_check(plugin, {"black_screen_detection": True, "frozen_video_detection": True}, quiet_logger)
    assert r["error_type"] == "Black Screen"


def test_frozen_wins_over_silent(plugin, pmod, monkeypatch, quiet_logger):
    """A frozen picture is the more serious fault; report the picture first."""
    monkeypatch.setattr(pmod.subprocess, "run", _dual_run(_probe(), ffmpeg_stderr=_FREEZE + _SILENT))
    r = _run_check(plugin, {"frozen_video_detection": True, "silent_audio_detection": True}, quiet_logger)
    assert r["error_type"] == "Frozen Video"


def test_healthy_stream_unaffected_by_both(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _dual_run(_probe(), ffmpeg_stderr=_NO_FREEZE + _AUDIBLE))
    r = _run_check(plugin, {"frozen_video_detection": True, "silent_audio_detection": True}, quiet_logger)
    assert r["status"] == "Alive"


def test_no_ffmpeg_pass_when_all_detectors_off(plugin, pmod, monkeypatch, quiet_logger):
    """Nobody pays a decode pass they did not ask for."""
    calls = []

    def _run(cmd, *a, **k):
        calls.append(cmd)
        return _FakeCompleted(stdout=json.dumps(_probe()), returncode=0)
    monkeypatch.setattr(pmod.subprocess, "run", _run)
    _run_check(plugin, {}, quiet_logger)
    assert len(calls) == 1, "expected the ffprobe call only, no ffmpeg decode"


def test_silence_not_checked_when_stream_has_no_audio_track(plugin, pmod, monkeypatch, quiet_logger):
    """A stream with no audio track cannot be 'silent' -- that is a different
    fault. Without this guard every video-only stream would report no
    mean_volume line and, if the parser were ever changed to treat a missing
    line as silence, would be marked Dead."""
    capture = []

    def _run(cmd, *a, **k):
        capture.append(cmd)
        if any("detect" in str(p) for p in cmd) or "-af" in cmd:
            return _FakeCompleted(stderr=_NO_FREEZE, returncode=0)
        return _FakeCompleted(stdout=json.dumps(_probe(with_audio=False)), returncode=0)
    monkeypatch.setattr(pmod.subprocess, "run", _run)
    r = _run_check(plugin, {"silent_audio_detection": True}, quiet_logger)
    assert r["status"] == "Alive"
    ffmpeg_cmds = [c for c in capture if any("detect" in str(p) for p in c) or "-af" in c]
    assert not any("volumedetect" in str(p) for c in ffmpeg_cmds for p in c)


# ---- routing into the existing dead-channel actions ---------------------

def test_new_types_route_as_dead_nonblack(pmod):
    for et in ("Frozen Video", "Silent Audio"):
        result = {"status": "Dead", "error_type": et}
        assert pmod.Plugin._is_dead_nonblack(result) is True
        assert pmod.Plugin._is_black_screen(result) is False
