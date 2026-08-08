"""The stream tally that the public README badge is built from.

WHY THE FILE EXISTS AT ALL. `/data/iptv_checker_results.json` is overwritten by
every pass and the dated CSV reports are pruned to the last few, so nothing else
this plugin writes can be added up into a lifetime total. The tally is an
append-only line per finished pass.

WHAT THESE TESTS PROTECT.

  IT MUST NEVER RAISE. It is called from the `finally` of both stream
  processing paths. An exception there would replace whatever the run was about
  to report with a traceback about a counter.

  IT MUST APPEND. Several Dispatcharr processes hold this module. A
  read-modify-write total loses an increment whenever two of them overlap.

  IT MUST CARRY NOTHING IDENTIFYING. A public badge is built from this file, so
  a channel name, a group or a URL landing in it would be published. The line
  holds three integers-and-a-word and nothing else.

  BOTH PROCESSING PATHS MUST RECORD. The two paths are separate methods and
  either one can be the one that runs, decided by the enable_parallel_checking
  setting. A tally wired into only one of them undercounts silently, and which
  half is missing depends on a setting nobody would think to check.
"""
import ast
import json
import os
import pathlib

import pytest


PLUGIN_SOURCE = (pathlib.Path(__file__).resolve().parent.parent
                 / "iptv_checker" / "plugin.py")


@pytest.fixture
def ledger(plugin, pmod, tmp_path, monkeypatch):
    """Point the tally at tmp_path and hand back a reader for its lines."""
    path = tmp_path / "stream_counts.jsonl"
    monkeypatch.setattr(pmod.PluginConfig, "STREAM_COUNT_LEDGER_FILE", str(path))

    def lines():
        if not path.exists():
            return []
        return [json.loads(raw) for raw in
                path.read_text(encoding="utf-8").splitlines() if raw.strip()]

    lines.path = path
    return lines


def test_records_the_count(plugin, ledger):
    assert plugin._record_streams_checked(2691, "parallel") is True
    rows = ledger()
    assert len(rows) == 1
    assert rows[0]["streams"] == 2691
    assert rows[0]["mode"] == "parallel"


def test_appends_rather_than_overwriting(plugin, ledger):
    plugin._record_streams_checked(10, "sequential")
    plugin._record_streams_checked(5, "parallel")
    plugin._record_streams_checked(1, "parallel")
    assert [row["streams"] for row in ledger()] == [10, 5, 1]


def test_a_line_carries_only_the_three_expected_keys(plugin, ledger):
    """A public badge is built from this file. Nothing identifying may be in it."""
    plugin._record_streams_checked(7, "parallel")
    assert set(ledger()[0]) == {"ts", "streams", "mode"}


def test_the_timestamp_is_an_integer_epoch(plugin, ledger):
    plugin._record_streams_checked(7, "parallel")
    assert isinstance(ledger()[0]["ts"], int)


def test_zero_is_recorded_rather_than_dropped(plugin, ledger):
    """A pass that probed nothing is a fact about that pass.

    Dropping it would make the ledger silently sparse, so a reader could not
    tell a night with no work from a night the tally failed to write.
    """
    assert plugin._record_streams_checked(0, "sequential") is True
    assert ledger()[0]["streams"] == 0


def test_an_unwritable_path_is_survivable(plugin, pmod, tmp_path, monkeypatch):
    """The call sites are `finally` blocks. This may not raise, ever."""
    unwritable = tmp_path / "no-such-directory" / "counts.jsonl"
    monkeypatch.setattr(pmod.PluginConfig, "STREAM_COUNT_LEDGER_FILE",
                        str(unwritable))
    assert plugin._record_streams_checked(12, "parallel") is False
    assert not os.path.exists(str(unwritable))


@pytest.mark.parametrize("bad", [None, "many", object(), float("nan")])
def test_a_count_that_is_not_a_whole_number_writes_nothing(plugin, ledger, bad):
    assert plugin._record_streams_checked(bad, "parallel") is False
    assert ledger() == []


def test_a_negative_count_writes_nothing(plugin, ledger):
    """A negative total would make the badge go backwards."""
    assert plugin._record_streams_checked(-5, "parallel") is False
    assert ledger() == []


# --- The two call sites -----------------------------------------------------
#
# Read structurally out of the source rather than by running the two processing
# methods, which need a ThreadPoolExecutor, a channel map and live ffprobe. A
# substring search over the file would pass while the call sat in dead code, so
# these assert the call is inside the `finally` of the named function.

def _function_named(name):
    tree = ast.parse(PLUGIN_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in plugin.py")


def _records_in_a_finally(func):
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        for statement in node.finalbody:
            for inner in ast.walk(statement):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "_record_streams_checked"):
                    return True
    return False


@pytest.mark.parametrize("path", ["_process_streams_sequential",
                                  "_process_streams_parallel"])
def test_the_processing_path_records_its_tally_in_a_finally(path):
    """In `finally` so a cancelled or window-truncated pass still counts.

    A windowed run stopping at its boundary is the normal case on the install
    this was written for, not a failure, so counting only passes that reach the
    end of the try block would undercount by most of them.
    """
    assert _records_in_a_finally(_function_named(path))


def test_the_parallel_path_counts_the_dict_the_workers_fill():
    """`results` is built from results_dict near the END of the try block.

    Counting `results` in the parallel path reports zero whenever an exception
    lands before that line, for a pass that probed thousands of streams.
    """
    source = ast.get_source_segment(
        PLUGIN_SOURCE.read_text(encoding="utf-8"),
        _function_named("_process_streams_parallel"))
    finally_tail = source[source.rindex("finally:"):]
    assert "results_dict.values()" in finally_tail
    assert "Cancelled" in finally_tail
