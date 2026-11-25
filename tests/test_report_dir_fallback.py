"""The report falls back to /data when /config cannot be written.

Dispatcharr's plugin guide (Plugins.md, read 2026-10-17) asks plugins to write
only under /data or /app/data, and a stock Dispatcharr image has no writable
/config. This installation bind-mounts /config to the host, which is why the
report lives there. _writable_dir keeps REPORT_DIR when it can be written and
otherwise uses REPORT_FALLBACK_DIR, so Email Report and the scheduled report
still produce a file on a stock installation.
"""
import ast
import pathlib
import re

import iptv_checker.plugin as pm
import iptv_checker.storage as storage

PLUGIN = pathlib.Path(pm.__file__)


def test_the_preferred_directory_is_used_when_writable(tmp_path):
    preferred = str(tmp_path / "config" / "iptv_checker")
    fallback = str(tmp_path / "data" / "iptv_checker" / "reports")
    assert pm._writable_dir(preferred, fallback) == preferred


def test_the_fallback_is_used_when_the_preferred_directory_cannot_be_written(tmp_path):
    fallback = str(tmp_path / "data" / "iptv_checker" / "reports")
    assert pm._writable_dir("/nonexistent\x00dir", fallback) == fallback


def test_the_preferred_directory_is_returned_when_neither_can_be_used():
    """The caller's own write then fails and is reported as before."""
    assert pm._writable_dir("/nonexistent\x00a", "/nonexistent\x00b") == "/nonexistent\x00a"


def test_the_fallback_literal_is_under_data_and_never_under_logos():
    """The legacy literal is the old /data folder. bind moves the fallback to the shared Newsflasharr
    folder (test_storage_bind.py), and that target must never be under /logos either."""
    literal = re.search(r'REPORT_FALLBACK_DIR = "([^"]+)"', PLUGIN.read_text(encoding="utf-8")).group(1)
    assert literal.startswith("/data/")
    assert "/logos" not in literal
    assert storage.SPEC.reports_constant == "REPORT_FALLBACK_DIR"
    assert "logos" not in storage.SPEC.reports_subdir


def test_the_report_is_written_through_the_fallback_chooser():
    tree = ast.parse(PLUGIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "reports.write_report":
            args = [ast.unparse(a) for a in node.args[1:3]]
            assert args == ["report_dir", "report_dir"], args
            break
    else:
        raise AssertionError("no reports.write_report call in plugin.py")
    assert "_writable_dir(PluginConfig.REPORT_DIR, PluginConfig.REPORT_FALLBACK_DIR)" in PLUGIN.read_text(encoding="utf-8")
