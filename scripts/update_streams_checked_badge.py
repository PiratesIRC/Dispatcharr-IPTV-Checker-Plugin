#!/usr/bin/env python3
"""Refresh the public "Streams Checked" badge on the README.

WHAT THIS DOES. Adds up how many streams this plugin has probed, reading its own
tally inside the Dispatcharr container, and writes a Shields.io endpoint document
to a GitHub Gist. The README badge points at that Gist, so this script is what
makes the public number change.

    python scripts/update_streams_checked_badge.py            # refresh the Gist
    python scripts/update_streams_checked_badge.py --dry-run  # print, write nothing
    python scripts/update_streams_checked_badge.py --create   # first-time Gist setup

WHAT COUNTS AS ONE STREAM CHECKED. One stream probed by one pass. A channel with
four streams behind it contributes four, because the plugin checks streams and
that is what the label says. A stream probed again tomorrow night counts again:
this is a total of checks performed, not a count of distinct streams that exist,
and on this install the same few thousand streams are re-checked every night.

WHERE THE NUMBER COMES FROM. `/data/iptv_checker_stream_counts.jsonl`, one JSON
object per finished pass, written by the plugin. A windowed run that stops at its
boundary writes a line for the part it did, so two nights covering one list
contribute two lines rather than one.

THE NUMBER IS A FLOOR, AND STARTS AT THE DEPLOY. Nothing before the tally shipped
was recorded, because the results file is overwritten by every run and the dated
CSVs are pruned, so no earlier history could be reconstructed. A pass whose tally
write failed is missing too: the plugin logs a warning and carries on rather than
failing a check over a counter.

PRIVACY. The tally holds integers and the word sequential or parallel. No channel
name, no group, no URL, no hostname. Only the summed integer reaches the Gist.
The Gist is unlisted rather than private and the README names it, so treat the
number as public.
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

CONTAINER = "dispatcharr"
GIST_FILENAME = "iptv-checker-streams-checked.json"
GIST_DESCRIPTION = "IPTV Checker streams-checked badge (Shields.io endpoint)"

# gh is installed and authenticated but is NOT on PATH in either shell here, so
# `command -v gh` reports it missing and is not evidence. Pin the absolute path,
# built from LOCALAPPDATA rather than written out in full: this repository is
# public and a literal path names the Windows account for no benefit.
GH = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
                  "Packages",
                  "GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe",
                  "bin", "gh.exe")

# Where the Gist id is remembered between runs. It is committed, so a re-clone
# keeps updating the same document rather than silently creating a second one.
# The id is not a secret in any case: the README badge URL names it.
STATE_PATH = ROOT / "scripts" / ".streams_checked_badge_gist"

LEDGER_GLOB = "/data/iptv_checker_stream_counts.jsonl*"

LABEL = "Streams Checked"
COLOR = "blue"


def read_ledger_total():
    """-> (total_streams, passes, {mode: streams}).

    Reads inside the container because the tally is on a Docker volume with no
    Windows path. A line that will not parse, or whose count is not a whole
    number, is skipped rather than guessed at: a badge that is slightly low is
    better than one built on a value nobody can account for.
    """
    probe = (
        "import glob, json, sys\n"
        "total = 0\n"
        "passes = 0\n"
        "skipped = 0\n"
        "modes = {}\n"
        f"for path in sorted(glob.glob({LEDGER_GLOB!r})):\n"
        "    try:\n"
        "        fh = open(path, encoding='utf-8')\n"
        "    except OSError:\n"
        "        continue\n"
        "    with fh:\n"
        "        for line in fh:\n"
        "            if not line.strip():\n"
        "                continue\n"
        "            try:\n"
        "                row = json.loads(line)\n"
        "            except ValueError:\n"
        "                skipped += 1\n"
        "                continue\n"
        "            if not isinstance(row, dict):\n"
        "                skipped += 1\n"
        "                continue\n"
        "            count = row.get('streams')\n"
        "            if not isinstance(count, int) or isinstance(count, bool) or count < 0:\n"
        "                skipped += 1\n"
        "                continue\n"
        "            total += count\n"
        "            passes += 1\n"
        "            mode = row.get('mode')\n"
        "            if isinstance(mode, str):\n"
        "                modes[mode] = modes.get(mode, 0) + count\n"
        "sys.stdout.write(json.dumps({'total': total, 'passes': passes,\n"
        "                             'skipped': skipped, 'modes': modes}))\n"
    )
    result = subprocess.run(
        ["docker", "exec", "-u", "dispatch", "-i", CONTAINER, "python", "-"],
        input=probe, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"could not read the container: {result.stderr.strip()}")
    payload = result.stdout.strip()
    # Some container entry points print startup banners, so take the last line.
    payload = payload.splitlines()[-1] if payload else ""
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise SystemExit(
            f"unexpected output from the container: {payload[:200]!r}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("total"), int):
        raise SystemExit("unexpected output from the container: wrong shape")
    return data["total"], data.get("passes", 0), data.get("modes") or {}, data.get("skipped", 0)


def format_total(total):
    """Thousands separators, because the badge is read at a glance.

    A bare 1284302 is not readable in a 20-pixel-tall badge, and Shields does no
    formatting of its own.
    """
    return f"{total:,}"


def endpoint_document(total):
    """The Shields.io endpoint schema, and nothing else in the file.

    Extra keys are not added even though they would be convenient for a human
    reading the Gist: Shields validates this document, and a field it does not
    recognise is a way to break the badge for no benefit.
    """
    return {"schemaVersion": 1, "label": LABEL, "message": format_total(total),
            "color": COLOR}


def gh(*args, check=True):
    result = subprocess.run([GH, *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def create_gist(path):
    """Create the unlisted Gist once, and remember its id."""
    url = gh("gist", "create", str(path), "--desc", GIST_DESCRIPTION)
    gist_id = url.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{8,}", gist_id):
        raise SystemExit(f"could not read a gist id out of {url!r}")
    STATE_PATH.write_text(gist_id + "\n", encoding="utf-8")
    return gist_id, url


def raw_url(gist_id):
    """The revision-less raw URL, which always serves the newest content.

    A URL carrying a revision sha would pin the badge to the first value it ever
    had, which looks exactly like a badge that has stopped updating. The raw URL
    is cached for five minutes and a query-string cache buster does not bypass
    it, so a new number is not visible at once.
    """
    owner = gh("api", "user", "--jq", ".login")
    return (f"https://gist.githubusercontent.com/{owner}/{gist_id}/raw/"
            f"{GIST_FILENAME}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be published, write nothing")
    parser.add_argument("--create", action="store_true",
                        help="create the Gist for the first time")
    args = parser.parse_args()

    total, passes, modes, skipped = read_ledger_total()
    document = endpoint_document(total)

    by_mode = ", ".join(f"{k} {v:,}" for k, v in
                        sorted(modes.items(), key=lambda kv: (-kv[1], kv[0])))
    print(f"streams checked: {format_total(total)} over {passes} pass(es)")
    print(f"  by mode: {by_mode or 'none'}")
    if skipped:
        print(f"  unreadable lines skipped: {skipped}")

    if args.dry_run:
        print(json.dumps(document, indent=2))
        return 0

    staged = ROOT / "dist" / GIST_FILENAME
    staged.parent.mkdir(exist_ok=True)
    staged.write_text(json.dumps(document) + "\n", encoding="utf-8")

    if args.create:
        if STATE_PATH.exists():
            raise SystemExit(
                f"{STATE_PATH.name} already exists, so a Gist was created "
                f"before. Run without --create to update it.")
        gist_id, url = create_gist(staged)
        print(f"created gist {url}")
    else:
        if not STATE_PATH.exists():
            raise SystemExit(
                f"no {STATE_PATH.name}; run once with --create first.")
        gist_id = STATE_PATH.read_text(encoding="utf-8").strip()
        gh("gist", "edit", gist_id, "--filename", GIST_FILENAME, str(staged))
        print(f"updated gist {gist_id}")

    endpoint = raw_url(gist_id)
    print(f"endpoint: {endpoint}")
    print(f"badge:    https://img.shields.io/endpoint?url={endpoint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
