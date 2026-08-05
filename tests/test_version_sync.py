"""Version consistency guards.

These tests turn the manual release checklist ("bump the version in BOTH
files, and the Current Version line in CLAUDE.md") into assertions. They have
caught real bugs before they existed: the doubled-"v" prefix and the
plugin.json/plugin.py mismatch both shipped from hand-edited releases.
"""
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JSON = PROJECT_ROOT / "iptv_checker" / "plugin.json"
PLUGIN_PY = PROJECT_ROOT / "iptv_checker" / "plugin.py"
CLAUDE_MD = PROJECT_ROOT / "CLAUDE.md"

CALVER_RE = re.compile(r"^\d+\.\d+\.\d{7}$")


def _json_version():
    return json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]


def _py_version():
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', PLUGIN_PY.read_text(encoding="utf-8"), re.MULTILINE)
    assert m, "version attribute not found in plugin.py"
    return m.group(1)


def test_plugin_json_and_plugin_py_versions_match():
    assert _json_version() == _py_version()


def test_version_is_calver_format():
    assert CALVER_RE.match(_json_version()), (
        f"version {_json_version()!r} does not match calver 1.X.DDDHHMM"
    )


# The Current Version line has been written both as a heading
# ("## Current Version: v1.2.3") and as a bold line ("**Current Version:
# v1.2.3**"). Match either, and stop at the first character that cannot be
# part of a version so trailing markdown is not captured.
CURRENT_VERSION_RE = re.compile(r"Current Version: v([0-9][0-9.]*)")


def test_claude_md_current_version_matches():
    if not CLAUDE_MD.exists():
        return
    m = CURRENT_VERSION_RE.search(CLAUDE_MD.read_text(encoding="utf-8"))
    assert m, "Current Version line not found in CLAUDE.md"
    assert m.group(1) == _json_version()


def test_bump_version_can_actually_rewrite_claude_md():
    """bump_version.write_claude_md_version silently did nothing when its
    pattern stopped matching after CLAUDE.md was restructured: the guard is
    `if updated != text`, so a miss is indistinguishable from a no-op and the
    next release would have shipped a stale version line. Assert the rewrite
    really lands rather than trusting the bump's exit code."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("bump_version", PROJECT_ROOT / "bump_version.py")
    bump = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bump)

    original = CLAUDE_MD.read_text(encoding="utf-8")
    try:
        bump.write_claude_md_version("9.99.9999999")
        rewritten = CLAUDE_MD.read_text(encoding="utf-8")
        m = CURRENT_VERSION_RE.search(rewritten)
        assert m, "Current Version line vanished after a bump"
        assert m.group(1) == "9.99.9999999", "bump_version did not rewrite the version line"
    finally:
        CLAUDE_MD.write_text(original, encoding="utf-8", newline="")


def test_version_has_no_v_prefix():
    """The doubled-'v' bug (d2f7d8c): version strings must be bare numbers."""
    assert not _json_version().startswith("v")
    assert not _py_version().startswith("v")
