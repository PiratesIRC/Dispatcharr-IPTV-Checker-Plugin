"""Regression tests for the 2026-06-23 CSV-audit fixes:

- bug-csv-dup-monitoring-col: exported CSV header listed ffprobe_monitoring_seconds twice
- low-framerate threshold lowered below PAL (25fps) so European broadcasts aren't flagged
- CSV preamble "FFprobe Flags" fallback default must match the probe's real default
- audio-only streams (radio) return "No video stream found" -> Skipped, not Dead
- "View Last Results" must show when the check was produced
"""
import json
from datetime import datetime


# --- bug-csv-dup-monitoring-col -------------------------------------------------

def test_csv_fieldnames_no_duplicate_column(plugin):
    results = [{
        'channel_id': 1,
        'ffprobe_monitoring_seconds': 8,
        'ffprobe_calculated_bitrate_kbps': 3000,
        'ffprobe_packet_count': 600,
    }]
    fields = plugin._compute_csv_fieldnames(results)
    assert len(fields) == len(set(fields)), f"duplicate column(s) in header: {fields}"
    assert fields.count('ffprobe_monitoring_seconds') == 1
    assert 'ffprobe_calculated_bitrate_kbps' in fields
    assert 'ffprobe_packet_count' in fields


# --- low-framerate threshold (PAL-safe) ----------------------------------------

def test_low_framerate_helper_is_pal_safe(plugin):
    assert plugin._is_low_framerate(25) is False   # PAL
    assert plugin._is_low_framerate(24) is False   # film
    assert plugin._is_low_framerate(23) is True     # genuinely choppy
    assert plugin._is_low_framerate(20) is True
    assert plugin._is_low_framerate(0) is False     # unknown/dead, never "low"
    assert plugin._is_low_framerate(30) is False
    assert plugin._is_low_framerate(60) is False


def test_pal_25fps_not_flagged_in_preamble(plugin):
    plugin.check_progress = {}
    results = [{'status': 'Alive', 'format': 'HD', 'framerate_num': 25}]
    lines = plugin._generate_csv_header_comments({}, results)
    # The statistics count line ("Low Framerate Streams (<Nfps): X"), not the
    # always-present settings lines ("Low Framerate Rename Format", etc.).
    count_lines = [ln for ln in lines if 'Low Framerate Streams' in ln]
    assert count_lines == [], f"25fps PAL wrongly counted as low framerate: {count_lines}"


# --- CSV preamble ffprobe-flags default consistency (#7) ------------------------

def test_csv_preamble_ffprobe_flags_default_includes_packets(plugin):
    plugin.check_progress = {}
    results = [{'status': 'Alive', 'format': 'HD', 'framerate_num': 30}]
    lines = plugin._generate_csv_header_comments({}, results)
    flag_lines = [ln for ln in lines if 'FFprobe Flags:' in ln]
    assert flag_lines, "no FFprobe Flags line in preamble"
    assert '-show_packets' in flag_lines[0], (
        f"preamble default disagrees with probe default: {flag_lines[0]!r}")


# Note: audio-only (radio) -> Skipped is covered idiomatically by
# tests/test_bitrate_calc.py::test_no_video_stream_is_skipped.


# --- date on "View Last Results" -----------------------------------------------

def test_view_results_includes_checked_date(plugin, quiet_logger, monkeypatch):
    results = [{'status': 'Alive', 'format': 'HD', 'framerate_num': 30}]
    with open(plugin.results_file, 'w') as f:
        json.dump(results, f)
    ts = 1_700_000_000.0
    monkeypatch.setattr(plugin, '_load_progress', lambda: {'status': 'idle', 'end_time': ts})
    out = plugin.view_results_action({}, quiet_logger)
    expected = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    assert expected in out['message'], f"no checked-date in: {out['message']!r}"
