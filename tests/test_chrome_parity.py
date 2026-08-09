"""Vendored report_chrome.py drift gate.

report_chrome.py holds the report page furniture: the colour tokens with their
three theme states, and the helpers for escaping, the logo data URI, masthead,
stat tiles, bar chart, collapsible section, table, footer and page shell. It is
shared with Stream-Mapparr, which is the whole point: the two report pages had
their own copies of the same stylesheet and those copies had already drifted.

A Dispatcharr plugin is deployed as a self-contained directory into
/data/plugins/, so it cannot import the workspace-level ../_shared/ copy at run
time: that path does not exist inside the container. Vendoring is the only
mechanism, exactly as for notify_client.py.

Two layers, because they catch different failures:

  Layer A  the committed copy must hash-match scripts/chrome_manifest.json.
           Catches a hand-edit to the vendored copy.
  Layer B  the committed copy must hash-match ../_shared/report_chrome.py.
           Catches the manifest itself drifting from the source of truth, which
           Layer A alone cannot see because a hand-edit plus a recomputed pin
           agrees with itself.

TO LAND AN INTENDED CHANGE: edit ../_shared/report_chrome.py, copy it
BYTE-IDENTICALLY over iptv_checker/report_chrome.py, recompute the sha256 and
update scripts/chrome_manifest.json. That is a TWO-project change today and
grows with every plugin that adopts it.

LINE ENDINGS ARE THE TRAP. Copy the bytes, never the text: a text round trip
translates newlines, and a CRLF copy normalises back on a Linux checkout, stops
matching its pin, and fails CI for a reason unrelated to the code. Read the file
back and compare hashes rather than trusting that a copy call returned without
error, which has reported success and not persisted before.

Layer B only runs where ../_shared/ is present. It lives outside this git
repository, so a repository-only checkout such as CI does not have it, and that
half is skipped rather than failed.
"""
import hashlib
import importlib.util
import json
import os
import pathlib

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENDORED = os.path.join(_REPO, "iptv_checker", "report_chrome.py")
_MANIFEST = os.path.join(_REPO, "scripts", "chrome_manifest.json")
_SHARED = os.path.join(os.path.dirname(_REPO), "_shared", "report_chrome.py")

with open(_MANIFEST, encoding="utf-8") as _fh:
    _PINS = json.load(_fh)


def _sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_vendored_chrome_exists():
    assert os.path.exists(_VENDORED), "vendored report_chrome.py is missing"


def test_vendored_chrome_matches_manifest():
    """Layer A: catches a hand-edit to the vendored copy."""
    assert _sha256(_VENDORED) == _PINS["report_chrome.py"], (
        "iptv_checker/report_chrome.py drifted from its pinned hash. If the "
        "change is intended, edit ../_shared/report_chrome.py, re-vendor "
        "byte-identically, and update scripts/chrome_manifest.json."
    )


def test_vendored_chrome_uses_unix_line_endings():
    assert b"\r\n" not in open(_VENDORED, "rb").read()


@pytest.mark.skipif(
    not os.path.exists(_SHARED),
    reason="workspace ../_shared/report_chrome.py is not present in this checkout",
)
def test_vendored_chrome_matches_the_shared_source():
    """Layer B: catches the manifest drifting from the source of truth."""
    assert _sha256(_VENDORED) == _sha256(_SHARED), (
        "iptv_checker/report_chrome.py differs from ../_shared/report_chrome.py. "
        "Re-vendor by copying the BYTES, then update the manifest."
    )


def _reports_module():
    """Loaded by path, matching how the other report tests here load it."""
    path = pathlib.Path(_REPO) / "iptv_checker" / "reports.py"
    spec = importlib.util.spec_from_file_location("ic_reports_chrome_parity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_stylesheet_this_plugin_renders_is_built_from_its_own_accents():
    """The shared builder must own only the neutral tokens and structure. This
    plugin's six verdict colours stay here, and they are what produce the
    .dot-dead / .bar-dead style class names its sections and chart emit."""
    reports_module = _reports_module()
    assert set(reports_module.ACCENTS) == {
        "dead", "provider", "unproven", "backup", "slow", "audio"}
    css = reports_module._CSS
    for name in reports_module.ACCENTS:
        assert ".dot-%s {" % name in css
        assert ".bar-%s {" % name in css
