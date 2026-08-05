"""Render the committed sample report fixture.

The fixture is committed so a rendering change FAILS a test. That is the point,
not a nuisance: a diff in tests/fixtures/sample_report.html is the review
surface for any change to how the report looks.

Regenerate deliberately:  python scripts/render_sample.py
"""
import importlib.util
import io
import os
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "ic_reports", _ROOT / "iptv_checker" / "reports.py")
reports = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reports)

# A fixed timestamp, so regenerating without a code change produces no diff.
SAMPLE_NOW = 1754400000
SAMPLE_VERSION = "0.0.0-sample"
SAMPLE_LABEL = "2026-08-05 12:00:00"

# One channel per verdict, so the fixture exercises every section and every
# glyph. Names are obviously synthetic: this file is committed to a public
# repository and must never carry a real channel name.
SAMPLE_RESULTS = [
    {"channel_id": 1, "channel_name": "Sample All Dead", "stream_id": 10,
     "status": "Dead", "error_type": "Timeout", "framerate_num": 0},
    {"channel_id": 1, "channel_name": "Sample All Dead", "stream_id": 11,
     "status": "Dead", "error_type": "Connection Refused", "framerate_num": 0},
    {"channel_id": 2, "channel_name": "Sample Working On Backup", "stream_id": 20,
     "status": "Dead", "error_type": "Timeout", "framerate_num": 0},
    {"channel_id": 2, "channel_name": "Sample Working On Backup", "stream_id": 21,
     "status": "Alive", "error_type": "N/A", "framerate_num": 50.0,
     "dispatcharr_metadata": {"resolution": "1920x1080"}},
    {"channel_id": 3, "channel_name": "Sample Provider Slate", "stream_id": 30,
     "status": "Dead", "error_type": "Black Screen", "framerate_num": 0},
    {"channel_id": 4, "channel_name": "Sample Rate Limited", "stream_id": 40,
     "status": "Dead", "error_type": "Timeout", "framerate_num": 0},
    {"channel_id": 4, "channel_name": "Sample Rate Limited", "stream_id": 41,
     "status": "Skipped", "error_type": "Rate Limited", "framerate_num": 0},
    {"channel_id": 5, "channel_name": "Sample Radio Station", "stream_id": 50,
     "status": "Skipped", "error_type": "No Video Stream", "framerate_num": 0},
    {"channel_id": 6, "channel_name": "Sample Slow Channel", "stream_id": 60,
     "status": "Alive", "error_type": "N/A", "framerate_num": 15.0,
     "dispatcharr_metadata": {"resolution": "720x576"}},
    {"channel_id": 7, "channel_name": "Sample Healthy", "stream_id": 70,
     "status": "Alive", "error_type": "N/A", "framerate_num": 50.0,
     "dispatcharr_metadata": {"resolution": "1920x1080"}},
]

SAMPLE_SETTINGS = {"black_screen_detection": True, "placeholder_file_detection": True}


def build():
    model = reports.build_model(SAMPLE_RESULTS, SAMPLE_SETTINGS,
                                now=SAMPLE_NOW, version=SAMPLE_VERSION)
    # PIN THE TIMESTAMP TEXT, not just the epoch. A real report renders LOCAL
    # time, which is right for the operator but machine-dependent: the same
    # epoch produced 08:20:00 on a Central developer machine and 13:20:00 on a
    # UTC CI runner, so the committed fixture failed the build on a difference
    # that was not a code change. time.tzset() is not available on Windows, so
    # the environment cannot simply be forced.
    model["generated_label"] = SAMPLE_LABEL
    # No plugin_dir is set, so no logo is embedded. The fixture stays small and
    # its diff stays readable.
    return reports.render_html(model)


if __name__ == "__main__":
    out = _ROOT / "tests" / "fixtures" / "sample_report.html"
    os.makedirs(str(out.parent), exist_ok=True)
    with io.open(str(out), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build())
    print("wrote %s (%d bytes)" % (out, os.path.getsize(str(out))))
