"""write_report: it must never raise, and it must never claim success falsely.

The counts in any summary are computed BEFORE the write, and write_report
degrades rather than raising, so a caller that reports success from the counts
alone will report a healthy summary for a run that wrote nothing at all. A
falsy html_path is the only honest signal, and the artifact's mtime is the only
proof.
"""
import importlib.util
import os
import pathlib
import time

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "iptv_checker" / "reports.py"
_spec = importlib.util.spec_from_file_location("ic_reports_write", _PATH)
reports = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reports)


@pytest.fixture
def model():
    rows = [
        {"channel_id": 1, "channel_name": "Dead One", "stream_id": 10,
         "status": "Dead", "error_type": "Timeout", "framerate_num": 0},
        {"channel_id": 2, "channel_name": "Fine", "stream_id": 20,
         "status": "Alive", "error_type": "N/A", "framerate_num": 50},
    ]
    return reports.build_model(rows, {}, now=1754400000, version="9.9.9")


def test_writes_live_report_archive_and_csv(tmp_path, model):
    out = reports.write_report(model, str(tmp_path), str(tmp_path), 1754400000)
    assert out["html_path"] and os.path.exists(out["html_path"])
    assert out["archive_path"] and os.path.exists(out["archive_path"])
    assert out["csv_path"] and os.path.exists(out["csv_path"])
    assert out["error"] is None
    assert os.path.basename(out["html_path"]) == reports.REPORT_HTML


def test_live_report_and_archive_have_identical_content(tmp_path, model):
    out = reports.write_report(model, str(tmp_path), str(tmp_path), 1754400000)
    assert open(out["html_path"], encoding="utf-8").read() == \
        open(out["archive_path"], encoding="utf-8").read()


def test_stable_filename_always_holds_the_latest_run(tmp_path, model):
    reports.write_report(model, str(tmp_path), str(tmp_path), 1754400000)
    first = open(tmp_path / reports.REPORT_HTML, encoding="utf-8").read()
    other = reports.build_model([], {}, now=1754400001, version="9.9.9")
    reports.write_report(other, str(tmp_path), str(tmp_path), 1754400001)
    second = open(tmp_path / reports.REPORT_HTML, encoding="utf-8").read()
    assert first != second


def test_no_temporary_file_is_left_behind(tmp_path, model):
    reports.write_report(model, str(tmp_path), str(tmp_path), 1754400000)
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]


def test_a_partial_write_is_never_readable_at_the_live_path(tmp_path, model):
    """The temporary file must sit in the SAME directory, because os.replace is
    only atomic within one filesystem."""
    seen = {}
    real_replace = os.replace

    def spy(src, dst):
        seen.setdefault("pairs", []).append((os.path.dirname(src), os.path.dirname(dst)))
        return real_replace(src, dst)

    reports.os.replace = spy
    try:
        reports.write_report(model, str(tmp_path), str(tmp_path), 1754400000)
    finally:
        reports.os.replace = real_replace
    assert seen["pairs"], "os.replace was never called, so the write was not atomic"
    for src_dir, dst_dir in seen["pairs"]:
        assert src_dir == dst_dir, "temporary file was not in the destination directory"


def test_archives_are_pruned_to_the_limit(tmp_path, model):
    for i in range(reports.ARCHIVE_LIMIT + 5):
        reports.write_report(model, str(tmp_path), str(tmp_path), 1754400000 + i * 3600)
    archives = [n for n in os.listdir(tmp_path)
                if n.startswith("report-") and n.endswith(".html")]
    assert len(archives) == reports.ARCHIVE_LIMIT


def test_pruning_keeps_the_newest(tmp_path, model):
    stamps = [1754400000 + i * 3600 for i in range(reports.ARCHIVE_LIMIT + 3)]
    for stamp in stamps:
        reports.write_report(model, str(tmp_path), str(tmp_path), stamp)
    kept = sorted(n for n in os.listdir(tmp_path)
                  if n.startswith("report-") and n.endswith(".html"))
    newest = "report-%s.html" % time.strftime("%Y%m%d-%H%M%S", time.localtime(stamps[-1]))
    assert newest in kept
    oldest = "report-%s.html" % time.strftime("%Y%m%d-%H%M%S", time.localtime(stamps[0]))
    assert oldest not in kept


def test_csv_archives_are_pruned_independently(tmp_path, model):
    """Pruning is keyed on the suffix, so the two streams do not collide."""
    for i in range(reports.ARCHIVE_LIMIT + 4):
        reports.write_report(model, str(tmp_path), str(tmp_path), 1754400000 + i * 3600)
    csvs = [n for n in os.listdir(tmp_path) if n.endswith(".csv")]
    htmls = [n for n in os.listdir(tmp_path)
             if n.startswith("report-") and n.endswith(".html")]
    assert len(csvs) == reports.ARCHIVE_LIMIT
    assert len(htmls) == reports.ARCHIVE_LIMIT


# ---- degradation: never raise, and never claim success falsely ----------

def test_never_raises_when_the_report_directory_cannot_be_made(tmp_path, model):
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory")
    out = reports.write_report(model, str(blocker / "sub"), str(tmp_path), 1754400000)
    assert out["html_path"] is None, "a failed write must report a falsy html_path"
    assert out["error"], "a failed write must say why"


def test_a_csv_failure_does_not_lose_the_html_report(tmp_path, model):
    """The HTML report is the product; the CSV is a convenience export."""
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory")
    out = reports.write_report(model, str(tmp_path), str(blocker / "sub"), 1754400000)
    assert out["html_path"] and os.path.exists(out["html_path"])
    assert out["csv_path"] is None
    assert out["error"]


def test_never_raises_on_a_junk_model(tmp_path):
    for junk in (None, {}, {"sections": None}, {"sections": [None]}):
        out = reports.write_report(junk, str(tmp_path), str(tmp_path), 1754400000)
        assert isinstance(out, dict)
        assert "html_path" in out


def test_never_raises_on_a_junk_timestamp(tmp_path, model):
    for stamp in (None, "yesterday", float("nan")):
        out = reports.write_report(model, str(tmp_path), str(tmp_path), stamp)
        assert isinstance(out, dict)


def test_the_artifact_mtime_moves_which_is_the_only_real_proof(tmp_path, model):
    """A green return value is not evidence that anything was published."""
    out = reports.write_report(model, str(tmp_path), str(tmp_path), 1754400000)
    first = os.path.getmtime(out["html_path"])
    time.sleep(0.01)
    reports.write_report(model, str(tmp_path), str(tmp_path), 1754400060)
    assert os.path.getmtime(out["html_path"]) >= first
