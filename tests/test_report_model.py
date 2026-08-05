"""The report model's judgement calls.

These are the decisions that determine whether an operator deletes a channel
they can still watch, so they are tested harder than the rendering will be.

The module under test is pure and imports nothing from Dispatcharr, so it is
loaded directly rather than through the plugin conftest stubs.
"""
import importlib.util
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "iptv_checker" / "reports.py"
_spec = importlib.util.spec_from_file_location("ic_reports", _PATH)
reports = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reports)


def _row(cid, status, error_type="N/A", fps=0, name="Ch", stream_id=None, resolution=None):
    return {
        "channel_id": cid,
        "channel_name": name,
        "stream_id": stream_id if stream_id is not None else cid * 100,
        "status": status,
        "error_type": error_type,
        "framerate_num": fps,
        "dispatcharr_metadata": {"resolution": resolution} if resolution else {},
    }


# ---- classify_channel: the whole point of the module --------------------

def test_all_streams_alive_is_working():
    rows = [_row(1, "Alive", fps=50), _row(1, "Alive", fps=50)]
    assert reports.classify_channel(rows) == "working"


def test_one_alive_one_dead_is_working_on_a_backup_not_dead():
    """The distinction the destructive actions get wrong when it is missed."""
    rows = [_row(1, "Dead", "Timeout"), _row(1, "Alive", fps=50)]
    assert reports.classify_channel(rows) == "backup_only"


def test_all_dead_ordinary_failure_is_confirmed_dead():
    rows = [_row(1, "Dead", "Timeout"), _row(1, "Dead", "Connection Refused")]
    assert reports.classify_channel(rows) == "confirmed_dead"


def test_all_dead_provider_slate_is_provider_dead():
    rows = [_row(1, "Dead", "Black Screen"), _row(1, "Dead", "Placeholder File")]
    assert reports.classify_channel(rows) == "provider_dead"


def test_mixed_slate_and_ordinary_failure_is_confirmed_dead():
    """One ordinary failure means it is not purely a provider-side slate, so it
    belongs in the group that CAN be acted on locally."""
    rows = [_row(1, "Dead", "Black Screen"), _row(1, "Dead", "Timeout")]
    assert reports.classify_channel(rows) == "confirmed_dead"


def test_dead_plus_rate_limited_is_not_judged():
    """A rate-limited stream may be perfect. The channel is unproven, and must
    never appear in a section that invites deleting it."""
    rows = [_row(1, "Dead", "Timeout"), _row(1, "Skipped", "Rate Limited")]
    assert reports.classify_channel(rows) == "not_judged"


def test_dead_plus_unvalidatable_host_is_not_judged():
    rows = [_row(1, "Dead", "Timeout"), _row(1, "Skipped", "Skipped")]
    assert reports.classify_channel(rows) == "not_judged"


def test_audio_only_is_its_own_verdict_not_a_failure():
    """A radio station. ffprobe reports no video, which is correct and not a
    fault, so it must not land among the dead."""
    rows = [_row(1, "Skipped", "No Video Stream")]
    assert reports.classify_channel(rows) == "audio_only"


def test_audio_only_with_a_dead_sibling_is_not_judged():
    """Mixed evidence: something failed and something was never judged, so no
    conclusion is available."""
    rows = [_row(1, "Skipped", "No Video Stream"), _row(1, "Dead", "Timeout")]
    assert reports.classify_channel(rows) == "not_judged"


def test_all_playable_streams_slow_is_low_framerate():
    rows = [_row(1, "Alive", fps=15), _row(1, "Alive", fps=12)]
    assert reports.classify_channel(rows) == "low_framerate"


def test_one_full_rate_stream_means_the_channel_is_not_slow():
    rows = [_row(1, "Alive", fps=15), _row(1, "Alive", fps=60)]
    assert reports.classify_channel(rows) == "backup_only" or \
        reports.classify_channel(rows) == "working"


def test_a_slow_channel_with_a_dead_sibling_is_still_slow():
    """A dead stream cannot be played, so it says nothing about how the channel
    looks. Only the playable streams are judged for framerate."""
    rows = [_row(1, "Alive", fps=15), _row(1, "Dead", "Timeout")]
    assert reports.classify_channel(rows) == "low_framerate"


def test_empty_and_junk_rows_are_safe():
    assert reports.classify_channel([]) == "unknown"
    assert reports.classify_channel(None) == "unknown"
    assert reports.classify_channel(["not a dict", 5]) == "unknown"


# ---- the framerate helper must be TOTAL over its inputs -----------------

@pytest.mark.parametrize("value", [
    None, "", "abc", [], {}, float("nan"), float("inf"), float("-inf"), -5, 0,
])
def test_low_framerate_never_raises_and_rejects_nonsense(value):
    assert reports._is_low_framerate(value) is False


def test_low_framerate_boundaries():
    assert reports._is_low_framerate(23.976) is True
    assert reports._is_low_framerate(24.0) is False    # film
    assert reports._is_low_framerate(25.0) is False    # PAL
    assert reports._is_low_framerate(15.0) is True


# ---- build_model ---------------------------------------------------------

_MIXED = [
    _row(1, "Dead", "Timeout", name="AllDead"),
    _row(1, "Dead", "Timeout", name="AllDead", stream_id=101),
    _row(2, "Dead", "Timeout", name="HasBackup"),
    _row(2, "Alive", fps=50, name="HasBackup", stream_id=201, resolution="1920x1080"),
    _row(3, "Dead", "Black Screen", name="Slate"),
    _row(4, "Dead", "Timeout", name="Unproven"),
    _row(4, "Skipped", "Rate Limited", name="Unproven", stream_id=401),
    _row(5, "Skipped", "No Video Stream", name="Radio"),
    _row(6, "Alive", fps=15, name="Slow"),
]


def _section(model, key):
    return next(s for s in model["sections"] if s["key"] == key)


def test_a_channel_never_appears_in_two_sections():
    model = reports.build_model(_MIXED, {}, now=0)
    seen = []
    for section in model["sections"]:
        seen.extend(r["channel_id"] for r in section["rows"])
    assert sorted(seen) == [1, 2, 3, 4, 5, 6]
    assert len(seen) == len(set(seen)), "a channel appears in more than one section"


def test_the_counts_reconcile_against_the_total():
    """A channel with nothing wrong appears in NO section, because listing every
    healthy channel is noise. Measured on the real results file from this
    install: 63 of 214 channels were listed and the other 151 were simply
    absent, with nothing letting a reader account for them. The model therefore
    publishes the arithmetic."""
    healthy = [_row(9, "Alive", fps=50, name="Fine"),
               _row(9, "Alive", fps=50, name="Fine", stream_id=901)]
    model = reports.build_model(_MIXED + healthy, {}, now=0)
    t = model["totals"]
    assert t["channels"] == 7
    assert t["channels_listed"] == 6
    assert t["channels_no_issues"] == 1
    assert t["channels_listed"] + t["channels_no_issues"] == t["channels"]


def test_reconciliation_holds_when_everything_is_healthy():
    model = reports.build_model([_row(1, "Alive", fps=50)], {}, now=0)
    t = model["totals"]
    assert t["channels_listed"] == 0
    assert t["channels_no_issues"] == 1
    assert t["channels_listed"] + t["channels_no_issues"] == t["channels"]


def test_sections_carry_the_right_channels():
    model = reports.build_model(_MIXED, {}, now=0)
    assert [r["channel_id"] for r in _section(model, reports.SECTION_CONFIRMED_DEAD)["rows"]] == [1]
    assert [r["channel_id"] for r in _section(model, reports.SECTION_BACKUP_ONLY)["rows"]] == [2]
    assert [r["channel_id"] for r in _section(model, reports.SECTION_PROVIDER_DEAD)["rows"]] == [3]
    assert [r["channel_id"] for r in _section(model, reports.SECTION_NOT_JUDGED)["rows"]] == [4]
    assert [r["channel_id"] for r in _section(model, reports.SECTION_AUDIO_ONLY)["rows"]] == [5]
    assert [r["channel_id"] for r in _section(model, reports.SECTION_LOW_FRAMERATE)["rows"]] == [6]


def test_the_count_equals_the_rows_beneath_it():
    """The invariant the renderer relies on: a section heading's number is the
    number of rows in its table, never the size of some wider population."""
    model = reports.build_model(_MIXED, {}, now=0)
    for section in model["sections"]:
        assert section["count"] == len(section["rows"]), section["key"]


def test_every_section_says_what_to_do_about_its_contents():
    """A section that cannot say what to do about its rows does not earn a
    place. A conditional note cannot serve as a section's description."""
    model = reports.build_model(_MIXED, {}, now=0)
    for section in model["sections"]:
        assert section["description"].strip(), section["key"]
        assert section["action"].strip(), section["key"]


def test_totals_count_channels_and_streams_separately():
    model = reports.build_model(_MIXED, {}, now=0)
    t = model["totals"]
    assert t["channels"] == 6
    assert t["streams"] == 9
    # Two Alive rows: channel 2's backup and channel 6's slow stream. Channel
    # 5 is a Skipped audio-only row, which is a WORKING radio station but is
    # not an Alive stream, and the two counts must not be conflated.
    assert t["streams_alive"] == 2
    assert t["streams_dead"] == 5
    assert t["streams_skipped"] == 2


def test_working_channel_count_includes_backup_only_and_audio_only():
    """Channels 2 (backup), 5 (radio) and 6 (slow) all give the operator a
    picture or a sound, so they count as working."""
    model = reports.build_model(_MIXED, {}, now=0)
    assert model["totals"]["channels_working"] == 3


def test_detector_state_is_recorded_so_a_zero_can_be_read_correctly():
    """A zero from a detector that was OFF means nobody looked. That is not the
    same as measured and clean, and the report must be able to say which."""
    off = reports.build_model(_MIXED, {}, now=0)
    assert off["run_health"]["detectors"]["black_screen"] is False
    on = reports.build_model(_MIXED, {"black_screen_detection": True}, now=0)
    assert on["run_health"]["detectors"]["black_screen"] is True


def test_rate_limiting_marks_the_run_untrustworthy():
    model = reports.build_model(_MIXED, {}, now=0)
    assert model["run_health"]["rate_limited_streams"] == 1
    assert model["run_health"]["trustworthy"] is False

    clean = reports.build_model([_row(1, "Alive", fps=50)], {}, now=0)
    assert clean["run_health"]["trustworthy"] is True


def test_empty_results_produce_a_valid_empty_model():
    model = reports.build_model([], {}, now=0)
    assert model["totals"]["channels"] == 0
    assert len(model["sections"]) == len(reports.SECTION_SPECS)
    assert all(s["count"] == 0 for s in model["sections"])


def test_build_model_is_total_over_junk_input():
    for junk in (None, [], [None], ["x"], [{}], [{"channel_id": None}]):
        model = reports.build_model(junk, {}, now=0)
        assert isinstance(model["totals"]["channels"], int)


def test_rows_are_sorted_by_channel_name():
    rows = [_row(2, "Dead", "Timeout", name="Zulu"), _row(1, "Dead", "Timeout", name="Alpha")]
    model = reports.build_model(rows, {}, now=0)
    names = [r["channel_name"] for r in _section(model, reports.SECTION_CONFIRMED_DEAD)["rows"]]
    assert names == ["Alpha", "Zulu"]


def test_row_reports_stream_counts_so_failover_is_visible():
    model = reports.build_model(_MIXED, {}, now=0)
    row = _section(model, reports.SECTION_BACKUP_ONLY)["rows"][0]
    assert row["streams_total"] == 2
    assert row["streams_alive"] == 1
    assert "Timeout" in row["reasons"]
