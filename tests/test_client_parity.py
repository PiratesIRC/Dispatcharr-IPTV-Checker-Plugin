"""Vendored notify_client.py drift gate.

A Dispatcharr plugin is deployed as a self-contained directory into
/data/plugins/, so it cannot import the workspace-level ../_shared/ copy at
run time: that path does not exist inside the container. Vendoring is the only
mechanism, and iptv_checker is the SIXTH project to vendor this file, alongside
notifier, Sentinelarr, Stream-Mapparr, metricsarr and Lineuparr.

Two layers, because they catch different failures:

  Layer A  the committed copy must hash-match scripts/client_manifest.json.
           Catches a hand-edit to the vendored copy.
  Layer B  the committed copy must hash-match ../_shared/notify_client.py.
           Catches the manifest itself drifting from the source of truth, which
           Layer A alone cannot see because a hand-edit plus a recomputed pin
           agrees with itself.

TO LAND AN INTENDED CHANGE: edit ../_shared/notify_client.py, copy it
BYTE-IDENTICALLY over iptv_checker/notify_client.py, recompute the sha256 and
update scripts/client_manifest.json. That is a SIX-project change and needs the
operator's approval before the re-vendor, not after.

LINE ENDINGS ARE THE TRAP HERE. Copy the bytes, never the text: a text round
trip translates newlines, and a CRLF copy normalises back on a Linux checkout,
stops matching its pin, and fails CI for a reason unrelated to the code. A git
stash round trip has produced a third hash. .gitattributes pins this file to
LF. Read the file back and compare hashes rather than trusting that a copy call
returned without error, which has reported success and not persisted before.

Layer B only runs where ../_shared/ is present. It lives outside this git
repository, so a repository-only checkout such as CI does not have it, and that
half is skipped rather than failed.
"""
import hashlib
import json
import os

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENDORED = os.path.join(_REPO, "iptv_checker", "notify_client.py")
_MANIFEST = os.path.join(_REPO, "scripts", "client_manifest.json")
_SHARED = os.path.join(os.path.dirname(_REPO), "_shared", "notify_client.py")

with open(_MANIFEST, encoding="utf-8") as _fh:
    _PINS = json.load(_fh)


def _sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_vendored_client_exists():
    assert os.path.exists(_VENDORED), "vendored notify_client.py is missing"


def test_vendored_client_matches_manifest():
    """Layer A: catches a hand-edit to the vendored copy."""
    digest = _sha256(_VENDORED)
    assert digest == _PINS["notify_client.py"], (
        "iptv_checker/notify_client.py drifted from its pinned hash. If the "
        "change is intended, edit ../_shared/notify_client.py, re-vendor "
        "byte-identically, and update scripts/client_manifest.json."
    )


@pytest.mark.skipif(
    not os.path.exists(_SHARED),
    reason="workspace ../_shared/notify_client.py is not present in this checkout",
)
def test_vendored_client_matches_shared_source():
    """Layer B: catches the manifest drifting from the source of truth."""
    assert _sha256(_VENDORED) == _sha256(_SHARED), (
        "iptv_checker/notify_client.py is not byte-identical to "
        "../_shared/notify_client.py, which is the canonical source."
    )


def test_vendored_client_has_no_carriage_returns():
    """A CRLF copy normalises to LF on a Linux checkout and stops matching its
    pin, so CI fails on a file nobody touched. Asserted on the bytes, because a
    text-mode read would hide exactly the thing being checked."""
    raw = open(_VENDORED, "rb").read()
    assert b"\r" not in raw, (
        "the vendored client contains carriage returns; re-vendor in BINARY "
        "mode and check .gitattributes pins this path to eol=lf"
    )


def test_vendored_client_is_not_edited_locally():
    """The vendored copy belongs to Newsflasharr. This asserts the file still
    looks like the shared client rather than a local rewrite, so a reviewer
    reading only this repository is told not to edit it."""
    text = open(_VENDORED, encoding="utf-8").read()
    assert "def notify(" in text, "the vendored client no longer defines notify()"
