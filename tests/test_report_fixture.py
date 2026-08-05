"""The committed sample report must match what the renderer produces now.

A rendering change FAILING this test is the point, not a nuisance: the diff in
tests/fixtures/sample_report.html is the review surface for a change to how the
report looks. Regenerate deliberately with `python scripts/render_sample.py`
and read the diff before committing it.
"""
import importlib.util
import io
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FIXTURE = _ROOT / "tests" / "fixtures" / "sample_report.html"

_spec = importlib.util.spec_from_file_location(
    "ic_render_sample", _ROOT / "scripts" / "render_sample.py")
sample = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sample)


def test_fixture_matches_the_current_renderer():
    assert _FIXTURE.exists(), (
        "the sample fixture is missing; run python scripts/render_sample.py")
    committed = io.open(str(_FIXTURE), encoding="utf-8").read()
    assert sample.build() == committed, (
        "the rendered report changed. If that is intended, run "
        "python scripts/render_sample.py and review the diff.")


def test_fixture_carries_no_real_channel_name():
    """This file is committed to a PUBLIC repository."""
    text = io.open(str(_FIXTURE), encoding="utf-8").read()
    import re
    names = re.findall(r"<tr><td>([^<]*)</td>", text)
    assert names, "the fixture rendered no rows, so it proves nothing"
    for name in names:
        assert name.startswith("Sample "), name


def test_fixture_exercises_every_section():
    text = io.open(str(_FIXTURE), encoding="utf-8").read()
    assert text.count("<details>") >= 6
    assert "Nothing in this group." not in text, (
        "every section should have at least one sample row, or the fixture "
        "stops exercising the row rendering for that section")


def test_the_fixture_does_not_depend_on_the_machine_timezone():
    """A real report renders LOCAL time, which is right for the operator but
    machine-dependent. The committed fixture must not inherit that: the same
    epoch rendered 08:20:00 on a Central developer machine and 13:20:00 on a
    UTC CI runner, and the build failed on a difference that was not a code
    change. The generator therefore pins the timestamp TEXT.

    time.tzset() does not exist on Windows, so the environment cannot simply be
    forced; this asserts the pin instead, which is what makes the fixture
    reproducible everywhere.
    """
    assert getattr(sample, "SAMPLE_LABEL", None), \
        "the sample generator must pin the timestamp text"
    assert sample.SAMPLE_LABEL in sample.build()


def test_a_real_report_still_renders_local_time():
    """The pin must not have changed what an actual report shows."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ic_reports_tz", _ROOT / "iptv_checker" / "reports.py")
    reports = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reports)
    import time
    model = reports.build_model([], {}, now=sample.SAMPLE_NOW, version="x")
    expected = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sample.SAMPLE_NOW))
    assert expected in reports.render_html(model), \
        "a model with no explicit label must still render local time"
