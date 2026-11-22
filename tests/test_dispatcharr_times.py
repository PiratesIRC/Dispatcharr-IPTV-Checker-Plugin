"""Times a person reads use Dispatcharr's timezone, not the machine's clock.

Covers the results date ("Checked:", tested in test_csv_and_status_fixes.py), the
CSV file name, the "# Generated:" line and the start and end times in the CSV
header. Tokyo (UTC+9, no daylight saving) is the zone under test, so the
expectations differ from a UTC machine clock by nine hours.
"""
from datetime import datetime, timedelta

import pytest


TOKYO = "Asia/Tokyo"


@pytest.fixture
def tokyo(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_dispatcharr_timezone", lambda: TOKYO)
    return plugin


def test_now_is_in_dispatcharr_zone(tokyo):
    assert tokyo._dispatcharr_now().utcoffset() == timedelta(hours=9)


def test_unknown_zone_falls_back_to_machine_clock(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_dispatcharr_timezone", lambda: "Not/AZone")
    assert plugin._dispatcharr_tz() is None
    assert plugin._dispatcharr_now().tzinfo is None


def test_header_start_and_end_use_dispatcharr_zone(tokyo):
    tokyo.version = "1.26.0000000"
    tokyo.check_progress = {"start_time": 1_700_000_000.0, "end_time": 1_700_000_600.0}
    lines = tokyo._generate_csv_header_comments({}, [])
    assert "#   Start Time: 2023-11-15 07:13:20" in lines
    assert "#   End Time: 2023-11-15 07:23:20" in lines


def test_header_generated_line_is_dispatcharr_wall_clock(tokyo):
    tokyo.version = "1.26.0000000"
    tokyo.check_progress = {}
    lines = tokyo._generate_csv_header_comments({}, [])
    stamp = next(line for line in lines if line.startswith("# Generated: "))
    written = datetime.strptime(stamp[len("# Generated: "):], "%Y-%m-%d %H:%M:%S")
    expected = datetime.now(tokyo._dispatcharr_tz()).replace(tzinfo=None)
    assert abs((expected - written).total_seconds()) < 120


def test_csv_file_name_uses_dispatcharr_zone(tokyo, monkeypatch, tmp_path):
    import iptv_checker.plugin as pmod

    monkeypatch.setattr(pmod.PluginConfig, "EXPORTS_DIR", str(tmp_path))
    tokyo.version = "1.26.0000000"
    tokyo.check_progress = {}
    with open(tokyo.results_file, "w") as f:
        f.write('[{"channel_id": 1, "status": "Alive", "format": "HD"}]')
    out = tokyo.export_results_action({}, _Logger())
    assert out.get("status") != "error", out
    names = [n for n in tmp_path.iterdir() if n.name.startswith(pmod.PluginConfig.CSV_EXPORT_PREFIX)]
    assert len(names) == 1, names
    stamp = names[0].name[len(pmod.PluginConfig.CSV_EXPORT_PREFIX):-len(pmod.PluginConfig.CSV_EXPORT_SUFFIX)]
    written = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
    expected = datetime.now(tokyo._dispatcharr_tz()).replace(tzinfo=None)
    assert abs((expected - written).total_seconds()) < 120


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass
