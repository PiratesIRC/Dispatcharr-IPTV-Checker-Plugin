"""The vendored plugin_storage.py matches its pinned hash, and _shared when that folder is present.

The pin needs no _shared folder, so this runs in CI and on the released archive. When the workspace
_shared folder is present, the copy must also be byte-identical to it.
"""
import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDORED = ROOT / "iptv_checker" / "plugin_storage.py"
MANIFEST = ROOT / "scripts" / "storage_manifest.json"
SHARED = ROOT.parent / "_shared" / "plugin_storage.py"


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_vendored_copy_matches_the_pinned_hash():
    pinned = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert pinned == {"plugin_storage.py": _sha256(VENDORED)}


def test_the_vendored_copy_is_byte_identical_to__shared_when_present():
    if not SHARED.exists():
        import pytest
        pytest.skip("_shared is not checked out next to this repository")
    assert _sha256(VENDORED) == _sha256(SHARED)
