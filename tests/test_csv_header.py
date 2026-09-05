"""The comment preamble at the top of an exported CSV.

This file is opened by a person in a spreadsheet, so the preamble has to say
what it is before it says how the run was configured, and it must survive being
opened under a different codepage.

Three defects prompted these tests, each measured on 2026-09-05:

  - it raised ZeroDivisionError when the results list was empty, because it
    computes a percentage of the total without checking the total;
  - it printed the Python values True and False, and rendered the same setting
    two different ways depending on whether Dispatcharr had stored it as a
    boolean or as the string "true";
  - it described the configuration in detail and never said what the run did,
    and the counts that answer that sat at the bottom under the settings.
"""

import pytest


@pytest.fixture
def header_for(pmod):
    """Build a real preamble from a Plugin that was never initialised."""
    def build(settings=None, results=None):
        inst = pmod.Plugin.__new__(pmod.Plugin)
        inst.version = "1.26.0000000"
        inst.check_progress = {}
        inst._dispatcharr_timezone = lambda: "America/Chicago"
        return inst._generate_csv_header_comments(settings or {}, results or [])
    return build


def _line(lines, prefix):
    for line in lines:
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no preamble line starts with {prefix!r}")


_RESULTS = [
    {"status": "Alive", "format": "FHD", "framerate_num": 30},
    {"status": "Alive", "format": "HD", "framerate_num": 60},
    {"status": "Dead", "error_type": "Timeout"},
    {"status": "Skipped", "error_type": "Rate Limited"},
]


# --- it must not fall over ---------------------------------------------------

def test_an_empty_results_list_does_not_raise(header_for):
    """It divided by the total without checking the total was not zero."""
    lines = header_for({}, [])
    assert lines, "an empty run should still produce a preamble"


def test_an_empty_results_list_says_nothing_was_checked(header_for):
    text = "\n".join(header_for({}, []))
    assert "0" in text


# --- it must say what it is before it says how it was set up -----------------

def test_the_preamble_says_what_the_file_is_first(header_for):
    opening = "\n".join(header_for({}, _RESULTS)[:12]).lower()
    assert "report" in opening or "export" in opening
    assert "hash" in opening
    assert any(word in opening for word in ("skip", "ignore", "not data", "explanation")), \
        "a reader is never told the hash lines are a preamble rather than data"


def test_every_preamble_line_is_commented(header_for):
    """One uncommented line would be read as data by a spreadsheet import."""
    for line in header_for({}, _RESULTS):
        assert not line.strip() or line.startswith("#"), line


def test_what_the_run_did_comes_before_how_it_was_configured(header_for):
    lines = header_for({}, _RESULTS)
    text = "\n".join(lines)
    did = text.lower().index("what this run did")
    settings_at = text.index("Plugin Settings")
    assert did < settings_at, "the counts are buried under the configuration"


def test_what_the_run_did_reports_the_real_counts(header_for):
    lines = header_for({}, _RESULTS)
    checked = _line(lines, "#   Streams checked:")
    assert "4" in checked
    assert "2" in _line(lines, "#   Playing:")
    assert "1" in _line(lines, "#   Not playing:")
    assert "1" in _line(lines, "#   Not judged:")


# --- settings must read as English -------------------------------------------

def test_a_boolean_reads_as_yes_or_no(header_for):
    lines = header_for({"only_visible_channels": True}, _RESULTS)
    assert "Yes" in _line(lines, "#   Only Visible Channels:")


def test_a_boolean_stored_as_a_string_reads_the_same_way(header_for):
    """Dispatcharr stores some booleans as the strings true and false."""
    lines = header_for({"only_visible_channels": "true"}, _RESULTS)
    assert "Yes" in _line(lines, "#   Only Visible Channels:")


def test_a_boolean_that_is_off_reads_as_no(header_for):
    lines = header_for({"only_visible_channels": "false"}, _RESULTS)
    assert "No" in _line(lines, "#   Only Visible Channels:")


def test_the_preamble_never_prints_a_python_boolean(header_for):
    text = "\n".join(header_for(
        {"only_visible_channels": True, "enable_parallel_checking": False}, _RESULTS))
    assert "True" not in text
    assert "False" not in text


# --- a bare number tells a reader nothing ------------------------------------

def test_a_timeout_says_what_its_units_are(header_for):
    assert "second" in _line(header_for({}, _RESULTS), "#   Connection Timeout:")


def test_the_retry_count_says_what_it_counts(header_for):
    line = _line(header_for({}, _RESULTS), "#   Dead Connection Retries:")
    assert "attempt" in line.lower() or "before" in line.lower()


def test_the_worker_count_says_what_a_higher_number_does(header_for):
    line = _line(header_for({}, _RESULTS), "#   Parallel Workers:")
    assert "connection" in line.lower() or "at once" in line.lower()


# --- it has to survive a spreadsheet ----------------------------------------

def test_the_preamble_is_plain_ascii(header_for):
    """An arrow or a dash arrives as mojibake under a different codepage."""
    text = "\n".join(header_for({"only_visible_channels": True}, _RESULTS))
    bad = sorted({c for c in text if ord(c) > 127})
    assert not bad, [hex(ord(c)) for c in bad]


# --- the pure helper ---------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (True, "Yes"), (False, "No"),
    ("true", "Yes"), ("True", "Yes"), ("TRUE", "Yes"), (" true ", "Yes"),
    ("yes", "Yes"), ("1", "Yes"), ("on", "Yes"),
    ("false", "No"), ("no", "No"), ("0", "No"), ("", "No"),
    (1, "Yes"), (0, "No"),
])
def test_yes_no_renders_every_shape_dispatcharr_stores(pmod, value, expected):
    assert pmod._yes_no(value) == expected


def test_yes_no_falls_back_to_the_default_when_unset(pmod):
    """An unset setting must read as its default, not as No."""
    assert pmod._yes_no(None, True) == "Yes"
    assert pmod._yes_no(None, False) == "No"


# --- the record has to say what was armed -----------------------------------

DETECTORS = ("black_screen_detection", "placeholder_file_detection",
             "frozen_video_detection", "silent_audio_detection")

AUTOMATIC_CHANGES = ("scheduler_restore_channels", "scheduler_rename_dead_channels",
                     "scheduler_rename_black_screen_channels",
                     "scheduler_rename_low_framerate_channels",
                     "scheduler_add_video_format_suffix",
                     "scheduler_move_dead_channels",
                     "scheduler_move_black_screen_channels",
                     "scheduler_move_low_framerate_channels",
                     "scheduler_delete_dead_channels")


def test_the_record_says_which_detectors_were_on(header_for):
    """A zero from a detector that was off cannot be read as none found.

    All four are opt-in, so a report listing no blank screens means either that
    none were found or that nobody looked, and the file did not say which.
    """
    text = "\n".join(header_for({d: True for d in DETECTORS}, _RESULTS))
    for word in ("Blank Screen", "Placeholder File", "Frozen Video", "Silent Audio"):
        assert word in text, f"the record does not say whether {word} detection ran"


def test_the_record_says_which_automatic_changes_were_armed(header_for):
    """This file is the record kept when a destructive action follows it."""
    text = "\n".join(header_for({c: True for c in AUTOMATIC_CHANGES}, _RESULTS))
    assert "Automatic Channel Changes" in text
    for word in ("Delete", "Restore", "Rename", "Move"):
        assert word in text, f"the record does not say whether {word} was armed"


def test_automatic_deletion_is_recorded_even_when_it_is_off(header_for):
    """Off is the answer a reader needs most, so it cannot be omitted."""
    text = "\n".join(header_for({}, _RESULTS))
    line = _line(text.split("\n"), "#   Delete Dead Channels:")
    assert "No" in line
