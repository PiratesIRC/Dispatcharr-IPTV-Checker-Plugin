#!/usr/bin/env python3
"""Bump the IPTV Checker plugin version in iptv_checker/plugin.json,
iptv_checker/plugin.py, and the "Current Version" line in CLAUDE.md.

Version format: 1.26.{DDD}{HHMM} where DDD is day-of-year (3 digits) and
HHMM is 4-digit UTC time. Matches the Lineuparr / Channel-Mapparr /
EPG-Janitor cohort convention. Pass a version string to override.

Usage:
    python3 bump_version.py              # auto, current timestamp
    python3 bump_version.py 1.26.1030900 # explicit

Exit codes: 0 on success, non-zero if the two files disagreed before/after.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_JSON = ROOT / "iptv_checker" / "plugin.json"
PLUGIN_PY = ROOT / "iptv_checker" / "plugin.py"
CLAUDE_MD = ROOT / "CLAUDE.md"

VERSION_RE = re.compile(r'^\d+\.\d+\.\d{7}$')
PY_VERSION_RE = re.compile(r'(^\s*version\s*=\s*)"([^"]+)"', re.MULTILINE)


def auto_version() -> str:
    now = datetime.now(timezone.utc)
    return f"1.26.{now.timetuple().tm_yday:03d}{now.strftime('%H%M')}"


def read_json_version() -> str:
    return json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]


def read_py_version() -> str:
    m = PY_VERSION_RE.search(PLUGIN_PY.read_text(encoding="utf-8"))
    if not m:
        raise RuntimeError("version attribute not found in plugin.py")
    return m.group(2)


def write_json_version(new: str) -> None:
    text = PLUGIN_JSON.read_text(encoding="utf-8")
    updated = re.sub(r'("version"\s*:\s*)"[^"]+"', f'\\1"{new}"', text, count=1)
    PLUGIN_JSON.write_text(updated, encoding="utf-8")


def write_py_version(new: str) -> None:
    text = PLUGIN_PY.read_text(encoding="utf-8")
    updated = PY_VERSION_RE.sub(lambda m: f'{m.group(1)}"{new}"', text, count=1)
    PLUGIN_PY.write_text(updated, encoding="utf-8")


def write_claude_md_version(new: str) -> None:
    # The line has been written both as a heading ("## Current Version: vX")
    # and as a bold line ("**Current Version: vX**"), so match neither prefix
    # nor suffix. The version stops at the first character that cannot be part
    # of one, otherwise a trailing "**" gets swallowed into the capture.
    if not CLAUDE_MD.exists():
        return
    text = CLAUDE_MD.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'(Current Version: v)[0-9][0-9.]*', f'\\g<1>{new}', text, count=1
    )
    if count == 0:
        # Do NOT fail silently. The previous version of this function guarded
        # on `updated != text`, so a pattern that stopped matching read as a
        # no-op and the release shipped a stale version line.
        print(
            f"warning: no 'Current Version: v...' line found in {CLAUDE_MD}; not updated",
            file=sys.stderr,
        )
        return
    CLAUDE_MD.write_text(updated, encoding="utf-8")


def main(argv: list[str]) -> int:
    new = argv[1] if len(argv) > 1 else auto_version()
    if not VERSION_RE.match(new):
        print(f"error: version '{new}' must match 1.X.DDDHHMM (e.g. 1.26.1021420)", file=sys.stderr)
        return 2

    before_json = read_json_version()
    before_py = read_py_version()
    if before_json != before_py:
        print(f"warning: plugin.json ({before_json}) and plugin.py ({before_py}) disagreed before bump", file=sys.stderr)

    write_json_version(new)
    write_py_version(new)
    write_claude_md_version(new)

    after_json = read_json_version()
    after_py = read_py_version()
    if after_json != after_py or after_json != new:
        print(f"error: post-bump mismatch json={after_json} py={after_py} target={new}", file=sys.stderr)
        return 1

    print(f"bumped {before_json} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
