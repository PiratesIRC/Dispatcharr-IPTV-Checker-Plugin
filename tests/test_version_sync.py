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


def test_claude_md_current_version_matches():
    if not CLAUDE_MD.exists():
        return
    m = re.search(r"## Current Version: v(\S+)", CLAUDE_MD.read_text(encoding="utf-8"))
    assert m, "Current Version line not found in CLAUDE.md"
    assert m.group(1) == _json_version()


def test_version_has_no_v_prefix():
    """The doubled-'v' bug (d2f7d8c): version strings must be bare numbers."""
    assert not _json_version().startswith("v")
    assert not _py_version().startswith("v")
