"""Optional closed-caption presence reporting.

The verdict comes from the same ffmpeg decode pass as the other content
detectors: the showinfo filter prints one "side data" line for each frame that
carries CEA-608/708 caption data. ffprobe -show_streams cannot answer this, because
its closed_captions field was absent on a clip known to carry captions.

Every stderr fixture is real output captured from ffmpeg 8.1.2 in the Dispatcharr
container on synthetic clips (no network, no provider connection), run with
blackdetect, freezedetect and showinfo in one chain. The clip with captions had a
GA94 SEI added to each frame. Addresses and long values are shortened.

The result is report-only. It must never change a stream's status, and it reports
that caption data is present, not that the data contains text.
"""
import json


_CONFIG_ONLY = (
    "[Parsed_showinfo_2 @ 0x7fa84c003c40] config in time_base: 1/90000, frame_rate: 30/1\n"
    "[Parsed_showinfo_2 @ 0x7fa84c003c40] config out time_base: 0/0, frame_rate: 0/0\n"
)
_FRAME = (
    "[Parsed_showinfo_2 @ 0x7fa84c003c40] n:   0 pts:      0 pts_time:0       "
    "duration:   3000 duration_time:0.0333333 fmt:yuv420p cl:left sar:1/1 s:320x240\n"
)
_CAPTION_LINE = "[Parsed_showinfo_2 @ 0x7fa84c003c40]   side data - ATSC A53 Part 4 Closed Captions: (6 bytes)\n"
_OTHER_SIDE_DATA = (
    "[Parsed_showinfo_2 @ 0x719cf0003c40]   side data - H.26[45] User Data Unregistered SEI message: "
    "UUID=dc45e9bd-e6d9-48b7-962c-d820d923eeef\n"
)
_BLACK = "[Parsed_blackdetect_0 @ 0x7bcf94003700] black_start:0 black_end:4 black_duration:4\n"

_WITH_CAPTIONS = _CONFIG_ONLY + _FRAME + _CAPTION_LINE + _OTHER_SIDE_DATA
_WITHOUT_CAPTIONS = _CONFIG_ONLY + _FRAME + _OTHER_SIDE_DATA

_SETTINGS = {
    "ffmpeg_path": "/usr/local/bin/ffmpeg",
    "black_screen_sample_seconds": 8,
    "black_screen_min_black_seconds": 3,
    "black_screen_ffmpeg_timeout": 20,
    "frozen_video_min_seconds": 4,
    "silent_audio_max_db": -70,
}


class _Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _ffmpeg(stderr="", returncode=0, capture=None):
    def _run(cmd, *a, **k):
        if capture is not None:
            capture.append(cmd)
        return _Completed(stderr=stderr, returncode=returncode)
    return _run


def _vf(cmd):
    return cmd[cmd.index("-vf") + 1]


def _analyze(plugin, logger, **kw):
    return plugin._analyze_stream_content("http://x/1.ts", 10, _SETTINGS, logger, **kw)


def _probe(subtitles=False):
    streams = [
        {"codec_type": "video", "index": 0, "width": 1920, "height": 1080,
         "r_frame_rate": "30/1", "codec_name": "h264", "pix_fmt": "yuv420p"},
        {"codec_type": "audio", "index": 1, "codec_name": "aac", "sample_rate": "48000", "channels": 2},
    ]
    if subtitles:
        streams.append({"codec_type": "subtitle", "index": 2, "codec_name": "dvb_subtitle"})
    return {"streams": streams, "format": {"format_name": "mpegts"}}


def _dual(probe, stderr="", returncode=0, calls=None):
    def _run(cmd, *a, **k):
        if any("detect" in str(p) for p in cmd):
            if calls is not None:
                calls.append(cmd)
            return _Completed(stderr=stderr, returncode=returncode)
        return _Completed(stdout=json.dumps(probe), returncode=0)
    return _run


def _check(plugin, settings, logger):
    stream = {"stream_url": "http://x/1.ts", "channel_name": "T", "stream_id": 1}
    merged = {"probe_timeout": 1, "ffprobe_analysis_duration": 1}
    merged.update(_SETTINGS)
    merged.update(settings)
    return plugin.check_stream(stream, 1, 0, logger, skip_retries=True, settings=merged)


ON = {"closed_caption_detection": True}


# ---- _parse_caption_presence --------------------------------------------

def test_a_frame_with_caption_side_data_is_present(pmod):
    assert pmod.Plugin._parse_caption_presence(_WITH_CAPTIONS) is True


def test_frames_with_other_side_data_are_absent(pmod):
    assert pmod.Plugin._parse_caption_presence(_WITHOUT_CAPTIONS) is False


def test_filter_setup_lines_are_not_evidence_of_frames(pmod):
    """No frame was examined, so absence has not been measured."""
    assert pmod.Plugin._parse_caption_presence(_CONFIG_ONLY) is None


def test_empty_output_is_unknown(pmod):
    assert pmod.Plugin._parse_caption_presence("") is None
    assert pmod.Plugin._parse_caption_presence(None) is None


# ---- _analyze_stream_content --------------------------------------------

def test_showinfo_is_added_only_when_requested(plugin, pmod, monkeypatch, quiet_logger):
    seen = []
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg(_WITHOUT_CAPTIONS, capture=seen))
    _analyze(plugin, quiet_logger)
    _analyze(plugin, quiet_logger, want_captions=True)
    assert "showinfo" not in _vf(seen[0])
    assert "showinfo" in _vf(seen[1])


def test_blackdetect_stays_first_and_showinfo_last(plugin, pmod, monkeypatch, quiet_logger):
    seen = []
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg(_WITHOUT_CAPTIONS, capture=seen))
    _analyze(plugin, quiet_logger, want_freeze=True, want_captions=True)
    filters = _vf(seen[0]).split(",")
    assert filters[0].startswith("blackdetect")
    assert filters[-1] == "showinfo"


def test_captions_share_the_single_decode_pass(plugin, pmod, monkeypatch, quiet_logger):
    seen = []
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg(_WITH_CAPTIONS, capture=seen))
    _analyze(plugin, quiet_logger, want_freeze=True, want_audio=True, want_captions=True)
    assert len(seen) == 1


def test_verdict_is_none_unless_requested(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg(_WITH_CAPTIONS))
    assert _analyze(plugin, quiet_logger)["captions"] is None


def test_verdict_true_when_caption_lines_are_present(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg(_WITH_CAPTIONS))
    assert _analyze(plugin, quiet_logger, want_captions=True)["captions"] is True


def test_verdict_false_on_a_clean_pass_without_captions(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg(_WITHOUT_CAPTIONS))
    assert _analyze(plugin, quiet_logger, want_captions=True)["captions"] is False


def test_failed_pass_cannot_claim_absence(plugin, pmod, monkeypatch, quiet_logger):
    """A pass that died after a few frames did not measure the whole sample."""
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg(_WITHOUT_CAPTIONS, returncode=1))
    assert _analyze(plugin, quiet_logger, want_captions=True)["captions"] is None


def test_failed_pass_still_reports_captions_it_saw(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg(_WITH_CAPTIONS, returncode=1))
    assert _analyze(plugin, quiet_logger, want_captions=True)["captions"] is True


# ---- check_stream -------------------------------------------------------

def test_stream_with_captions_reports_yes(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _dual(_probe(), _WITH_CAPTIONS))
    r = _check(plugin, ON, quiet_logger)
    assert r["status"] == "Alive"
    assert r["ffprobe_data"]["has_captions"] == "Yes"


def test_stream_without_captions_reports_no_and_stays_alive(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _dual(_probe(), _WITHOUT_CAPTIONS))
    r = _check(plugin, ON, quiet_logger)
    assert r["status"] == "Alive"
    assert r["error_type"] == "N/A"
    assert r["ffprobe_data"]["has_captions"] == "No"


def test_unmeasured_stream_leaves_the_column_blank(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _dual(_probe(), "", returncode=1))
    r = _check(plugin, ON, quiet_logger)
    assert r["status"] == "Alive"
    assert "has_captions" not in r["ffprobe_data"]


def test_a_separate_subtitle_track_reports_yes(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _dual(_probe(subtitles=True), _WITHOUT_CAPTIONS))
    assert _check(plugin, ON, quiet_logger)["ffprobe_data"]["has_captions"] == "Yes"


def test_a_subtitle_track_reports_yes_when_the_decode_fails(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _dual(_probe(subtitles=True), "", returncode=1))
    assert _check(plugin, ON, quiet_logger)["ffprobe_data"]["has_captions"] == "Yes"


def test_setting_off_runs_no_decode_pass(plugin, pmod, monkeypatch, quiet_logger):
    calls = []
    monkeypatch.setattr(pmod.subprocess, "run", _dual(_probe(), _WITH_CAPTIONS, calls=calls))
    r = _check(plugin, {}, quiet_logger)
    assert calls == []
    assert "has_captions" not in r["ffprobe_data"]


def test_stored_string_false_counts_as_off(plugin, pmod, monkeypatch, quiet_logger):
    calls = []
    monkeypatch.setattr(pmod.subprocess, "run", _dual(_probe(), _WITH_CAPTIONS, calls=calls))
    _check(plugin, {"closed_caption_detection": "false"}, quiet_logger)
    assert calls == []


def test_captions_alone_does_not_enable_blank_screen_marking(plugin, pmod, monkeypatch, quiet_logger):
    """blackdetect is always in the chain; its verdict needs its own setting."""
    monkeypatch.setattr(pmod.subprocess, "run", _dual(_probe(), _BLACK + _WITHOUT_CAPTIONS))
    r = _check(plugin, ON, quiet_logger)
    assert r["status"] == "Alive"


def test_blank_screen_still_wins_when_both_are_on(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _dual(_probe(), _BLACK + _WITHOUT_CAPTIONS))
    r = _check(plugin, dict(ON, black_screen_detection=True), quiet_logger)
    assert r["status"] == "Dead"
    assert r["error_type"] == "Black Screen"


def test_result_is_not_written_to_dispatcharr_stream_stats(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _dual(_probe(), _WITH_CAPTIONS))
    r = _check(plugin, ON, quiet_logger)
    assert "has_captions" not in r["dispatcharr_metadata"]
