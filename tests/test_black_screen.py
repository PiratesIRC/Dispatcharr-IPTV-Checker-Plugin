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
