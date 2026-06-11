#!/usr/bin/env python3
"""Fail if iptv_checker/plugin.json version != the version in plugin.py.

Standalone CLI mirror of tests/test_version_sync.py, shareable by CI and any
pre-commit hook. Exits non-zero on drift so a mismatched build can never be
committed or merged. Dispatcharr hot-reloads on plugin.json mtime, so a drifted
version means the UI advertises one build while another actually runs.

The deployable code is in the INNER iptv_checker/ directory, and plugin.py
stores the version as a lowercase `version = "..."` class attribute (matching
bump_version.py's PY_VERSION_RE), NOT an uppercase PLUGIN_VERSION constant.
"""

import json
import re
import sys
from pathlib import Path

# scripts/ lives at the repo root; the deployable plugin is in iptv_checker/.
PLUGIN_DIR = Path(__file__).resolve().parent.parent / "iptv_checker"

CALVER_RE = re.compile(r"^\d+\.\d+\.\d{7}$")


def main() -> int:
    plugin_json = PLUGIN_DIR / "plugin.json"
    plugin_py = PLUGIN_DIR / "plugin.py"

    json_version = json.loads(plugin_json.read_text(encoding="utf-8"))["version"]

    m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']',
                  plugin_py.read_text(encoding="utf-8"), re.MULTILINE)
    if not m:
        print("ERROR: version attribute not found in plugin.py", file=sys.stderr)
        return 2
    code_version = m.group(1)

    if json_version != code_version:
        print(f"VERSION DRIFT: plugin.json={json_version!r} != "
              f"plugin.py={code_version!r}\n"
              f"  -> run: python bump_version.py", file=sys.stderr)
        return 1

    if not CALVER_RE.match(json_version):
        print(f"ERROR: version {json_version!r} does not match calver "
              f"1.X.DDDHHMM (regex ^\\d+\\.\\d+\\.\\d{{7}}$)", file=sys.stderr)
        return 1

    print(f"version OK: {json_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
