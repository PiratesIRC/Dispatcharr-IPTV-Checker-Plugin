"""Report model for IPTV Checker: turn per-stream check results into a plain
dict that a renderer can walk without making any further judgements.

WHY THIS MODULE EXISTS SEPARATELY. plugin.py is already over 4,500 lines, and
the decisions below are the part worth reading on their own. Everything here is
pure: no Django, no filesystem, no clock. `now` is passed in.

THE JUDGEMENT THIS MODULE MAKES, AND WHY IT IS THE WHOLE POINT

Check results are recorded PER STREAM. A channel usually has several: on the
install this was built against, 1,427 of 1,440 channels carry more than one.
Dispatcharr fails over between them, so a channel is only as broken as its BEST
stream. Every count below is therefore computed per CHANNEL, from all of that
channel's rows together.

The distinctions that decide whether the operator does something destructive:

  WORKING ON A BACKUP is not DEAD. A channel whose primary failed and whose
  backup plays is completely fine. Reporting it as dead is how an operator
  deletes the channels they were fighting hardest to keep.

  NOT JUDGED is not DEAD. `Skipped` covers three unrelated situations and none
  of them is evidence of failure: the provider rate-limited us (HTTP 429), so
  the stream may be perfect; the stream is audio-only, which is a working radio
  station; or the host needs Streamlink, which ffprobe cannot validate at all.
  A channel with no working stream but at least one Skipped one has NOT been
  shown to be dead.

  PROVIDER-SIDE DEAD is not LOCALLY FIXABLE. A blank screen or a fixed-duration
  placeholder file means the provider is serving a dead channel on every
  mirror. Renaming it changes nothing; the only useful actions are to hide it
  or to complain upstream.

  NOT MEASURED is not MEASURED-AND-CLEAN. The blank-screen, frozen-video,
  silent-audio and placeholder-file detectors are each opt-in. When one is off,
  a zero in its column means nobody looked. The run-health section states which
  detectors ran so no reader can mistake one for the other.
"""

# Error types that mean the PROVIDER is serving a dead channel rather than the
# stream failing to connect. All of them are produced by a successful probe, so
# there is nothing local to retry and no failover target that would help.
PROVIDER_DEAD_ERRORS = frozenset({
    "Black Screen",
    "Placeholder File",
    "Frozen Video",
    "Silent Audio",
})

# `Skipped` reasons, kept apart because they mean different things and only one
# of them is worth acting on.
SKIP_RATE_LIMITED = "Rate Limited"
SKIP_AUDIO_ONLY = "No Video Stream"
SKIP_UNVALIDATABLE = "Skipped"

# Section keys. The renderer keys its colours and glyphs off these, so they are
# part of the contract rather than display strings.
SECTION_CONFIRMED_DEAD = "confirmed_dead"
SECTION_PROVIDER_DEAD = "provider_dead"
SECTION_BACKUP_ONLY = "backup_only"
SECTION_NOT_JUDGED = "not_judged"
SECTION_LOW_FRAMERATE = "low_framerate"
SECTION_AUDIO_ONLY = "audio_only"

LOW_FRAMERATE_THRESHOLD = 24


def _rows_by_channel(results):
    """Group per-stream rows by channel id, preserving input order."""
    grouped = {}
    for row in results or ():
        if not isinstance(row, dict):
            continue
        cid = row.get("channel_id")
        if cid is None:
            continue
        grouped.setdefault(cid, []).append(row)
    return grouped


def _status(row):
    return (row.get("status") or "").strip()


def _error_type(row):
    return (row.get("error_type") or "").strip()


def _framerate(row):
    try:
        return float(row.get("framerate_num") or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_low_framerate(fps):
    """Total over its input: a NaN, an infinity or a negative reads as not low.

    Only a positive framerate below the threshold counts. PAL at 25 and film at
    24, including NTSC 23.976 which rounds to 24.0, are NOT low.
    """
    try:
        fps = float(fps)
    except (TypeError, ValueError):
        return False
    # No explicit NaN guard: every comparison against NaN is False in Python,
    # so `0 < nan < threshold` already returns False. An extra `if fps != fps`
    # would read as load-bearing while changing nothing, which is worse than
    # absent. Both infinities fall out of the range check the same way.
    return 0 < fps < LOW_FRAMERATE_THRESHOLD


def classify_channel(rows):
    """One channel's stream rows -> a verdict string.

    Returns exactly one of:
      "working"        at least one stream plays and nothing is flagged
      "backup_only"    plays, but only because a non-primary stream survived
      "low_framerate"  plays, but every playable stream is below the threshold
      "audio_only"     no video anywhere, and the reason is an audio-only feed
      "provider_dead"  nothing plays, and every failure is a provider slate
      "confirmed_dead" nothing plays, and at least one failure is an ordinary one
      "not_judged"     nothing plays, but something was Skipped, so unproven
      "unknown"        no usable rows at all

    ORDER MATTERS AND IS NOT ARBITRARY. Anything that plays is reported as
    playing before any failure is considered, because the operator's destructive
    actions work on channels and a channel that plays must never appear in a
    section that invites deleting it.
    """
    rows = [r for r in (rows or ()) if isinstance(r, dict)]
    if not rows:
        return "unknown"

    alive = [r for r in rows if _status(r) == "Alive"]
    dead = [r for r in rows if _status(r) == "Dead"]
    skipped = [r for r in rows if _status(r) == "Skipped"]

    if alive:
        # Every playable stream is slow, so the channel really is slow. If even
        # one plays at full rate, Dispatcharr can use it and the channel is not.
        if all(_is_low_framerate(_framerate(r)) for r in alive):
            return "low_framerate"
        if dead or skipped:
            return "backup_only"
        return "working"

    # Nothing plays. An audio-only feed is a WORKING radio station that ffprobe
    # reports as having no video, so it is judged before any failure is.
    if skipped and all(_error_type(r) == SKIP_AUDIO_ONLY for r in skipped) and not dead:
        return "audio_only"

    # A Skipped stream is not evidence of failure, so the channel is unproven.
    if skipped:
        return "not_judged"

    if dead:
        if all(_error_type(r) in PROVIDER_DEAD_ERRORS for r in dead):
            return "provider_dead"
        return "confirmed_dead"

    return "unknown"


def _channel_row(cid, rows, verdict):
    """One rendered table row. Plain values only, no formatting decisions."""
    alive = [r for r in rows if _status(r) == "Alive"]
    reasons = sorted({_error_type(r) for r in rows
                      if _status(r) in ("Dead", "Skipped") and _error_type(r) not in ("", "N/A")})
    fps = [_framerate(r) for r in alive if _framerate(r) > 0]
    return {
        "channel_id": cid,
        "channel_name": rows[0].get("channel_name") or "",
        "streams_total": len(rows),
        "streams_alive": len(alive),
        "reasons": reasons,
        "framerate": min(fps) if fps else None,
        "resolution": next((r.get("dispatcharr_metadata", {}).get("resolution")
                            for r in alive
                            if isinstance(r.get("dispatcharr_metadata"), dict)), None),
        "verdict": verdict,
    }


# Each section states what it holds and what the operator should DO about it.
# The action line is not decoration: a section that cannot say what to do about
# its contents does not earn a place in the report.
SECTION_SPECS = [
    (SECTION_CONFIRMED_DEAD, "Confirmed dead", ("confirmed_dead",),
     "Every stream on these channels failed to connect, and none was merely rate limited. "
     "This is the only group the dead-channel rename, move and delete actions should act on.",
     "Rename, move or delete them."),
    (SECTION_PROVIDER_DEAD, "Dead at the provider", ("provider_dead",),
     "Every stream connected and returned a blank picture, a frozen picture, silence, or a "
     "fixed-duration placeholder file. The provider is serving a dead channel on every mirror, "
     "so there is no failover target and nothing local to retry.",
     "Hide them, or raise it with the provider. Renaming changes nothing."),
    (SECTION_NOT_JUDGED, "Not judged", ("not_judged",),
     "No stream played, but at least one was skipped rather than tested: rate limited by the "
     "provider, or on a host this checker cannot validate. These channels have NOT been shown "
     "to be broken.",
     "Re-run the check later, or with fewer parallel workers. Do not delete these."),
    (SECTION_BACKUP_ONLY, "Working on a backup stream", ("backup_only",),
     "At least one stream failed but another plays, so the channel works. It appears here only "
     "so the failed streams are visible and the channel is not mistaken for a broken one.",
     "Nothing. Optionally reorder the streams so the working one is first."),
    (SECTION_LOW_FRAMERATE, "Low framerate", ("low_framerate",),
     "Every playable stream on these channels runs below 24 frames per second. Film at 24 and "
     "PAL at 25 are not counted.",
     "Tag or move them if choppy playback matters to you."),
    (SECTION_AUDIO_ONLY, "Audio only", ("audio_only",),
     "These carry sound and no video. That is normal for radio stations and is not a fault.",
     "Nothing. They are listed so they are never mistaken for channels with no picture."),
]


def build_model(results, settings=None, now=None, version="", run_health=None):
    """Per-stream check results -> a plain dict for the renderers.

    Pure: no I/O and no clock. `now` is an epoch float supplied by the caller.
    Every value is a plain type, so a renderer never has to decide anything.
    """
    settings = settings if isinstance(settings, dict) else {}
    grouped = _rows_by_channel(results)

    verdicts = {}
    rows_by_verdict = {}
    for cid, rows in grouped.items():
        verdict = classify_channel(rows)
        verdicts[cid] = verdict
        rows_by_verdict.setdefault(verdict, []).append(_channel_row(cid, rows, verdict))

    sections = []
    for key, title, wanted, description, action in SECTION_SPECS:
        items = []
        for verdict in wanted:
            items.extend(rows_by_verdict.get(verdict, ()))
        items.sort(key=lambda r: (r["channel_name"] or "", r["channel_id"]))
        sections.append({
            "key": key,
            "title": title,
            "count": len(items),
            "description": description,
            "action": action,
            "rows": items,
        })

    all_rows = [r for r in (results or ()) if isinstance(r, dict)]
    # A channel with nothing wrong appears in NO section, because a list of
    # every healthy channel is noise. That leaves a reader unable to reconcile
    # the section counts against the total, so the arithmetic is published
    # instead: listed + no_issues == channels, always.
    listed = sum(len(s["rows"]) for s in sections)
    totals = {
        "channels": len(grouped),
        "streams": len(all_rows),
        "streams_alive": sum(1 for r in all_rows if _status(r) == "Alive"),
        "streams_dead": sum(1 for r in all_rows if _status(r) == "Dead"),
        "streams_skipped": sum(1 for r in all_rows if _status(r) == "Skipped"),
        "channels_working": sum(1 for v in verdicts.values()
                                if v in ("working", "backup_only", "low_framerate", "audio_only")),
        "channels_listed": listed,
        "channels_no_issues": len(grouped) - listed,
    }

    # Which detectors actually ran. A zero from a detector that was off means
    # nobody looked, and the renderer must be able to say so.
    detectors = {
        "black_screen": bool(settings.get("black_screen_detection")),
        "frozen_video": bool(settings.get("frozen_video_detection")),
        "silent_audio": bool(settings.get("silent_audio_detection")),
        "placeholder_file": bool(settings.get("placeholder_file_detection")),
    }

    health = dict(run_health or {})
    health.setdefault("rate_limited_streams",
                      sum(1 for r in all_rows if _error_type(r) == SKIP_RATE_LIMITED))
    health["detectors"] = detectors
    health["trustworthy"] = health["rate_limited_streams"] == 0

    return {
        "generated_at": now,
        "version": version,
        "totals": totals,
        "sections": sections,
        "run_health": health,
    }


# =========================================================================
# Rendering
# =========================================================================
#
# ONE self-contained file. Inline CSS, no <link>, no CDN, no webfont, no
# remote image. It is opened off disk as a file:// URL, mailed as an
# attachment, and read on a television browser with no route to the internet.
#
# render_html has NO safety net: write_report below catches OSError only, so a
# TypeError or a division by zero in here escapes to the caller. Every helper
# must therefore be TOTAL over its inputs.
#
# THE CSS AND SVG LIVE IN MODULE-LEVEL CONSTANTS, not inside an f-string. A
# literal brace inside an f-string becomes a format field and raises at render
# time, which is a runtime failure in the one function that has no net.

import base64
import csv
import html
import io
import os
import time

REPORT_HTML = "report.html"
ARCHIVE_LIMIT = 8

# The logo is embedded as a data URI, never linked: a relative path resolves
# against nothing in an emailed attachment, and a remote URL is blocked by
# default in most mail clients. It is CAPPED because this plugin's logo.png is
# 310 KB, which is 414 KB once base64 encoded, and that would ride on every
# emailed copy of every report. Over the cap the header renders with NO image
# at all, which is the same degradation as a missing file.
LOGO_MAX_ENCODED_BYTES = 96 * 1024

# Colour is never the only carrier of meaning. Each section gets a dot class, a
# word, and a glyph, and the GLYPH IS KEYED ON THE CLASS rather than the title,
# so a glyph can never disagree with the colour beside it. Every class here maps
# to exactly one glyph.
_SECTION_DOT = {
    SECTION_CONFIRMED_DEAD: "dot-dead",
    SECTION_PROVIDER_DEAD: "dot-provider",
    SECTION_NOT_JUDGED: "dot-unproven",
    SECTION_BACKUP_ONLY: "dot-backup",
    SECTION_LOW_FRAMERATE: "dot-slow",
    SECTION_AUDIO_ONLY: "dot-audio",
}

_DOT_GLYPH = {
    "dot-dead": "\N{WASTEBASKET}",
    "dot-provider": "\N{WARNING SIGN}",
    "dot-unproven": "\N{HOURGLASS WITH FLOWING SAND}",
    "dot-backup": "\N{WHITE HEAVY CHECK MARK}",
    "dot-slow": "\N{TURTLE}",
    "dot-audio": "\N{SPEAKER WITH THREE SOUND WAVES}",
}

# A spacing scale and a grey ramp, both as tokens. Every margin, padding and gap
# picks a step. Text hierarchy uses the ramp and NEVER `opacity`: an opacity
# value paints a different colour on every surface, so the contrast ratio moves
# whenever a background changes, and the fade applies to everything nested
# inside. Light and dark differ ONLY in token values, which is what makes
# `!important` unnecessary anywhere in this sheet.
#
# Type is sized for reading a television across a room. Do not shrink it.
_CSS = """
:root {
  --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s5: 24px;
  --bg: #ffffff; --surface: #f6f7f9; --border: #d6dae0;
  --ink: #14181d; --ink-muted: #4a5560; --ink-dim: #66727e;
  --dead: #b3261e; --provider: #8a4c00; --unproven: #1a5fb4;
  --backup: #1a7f37; --slow: #8a4c00; --audio: #1a5fb4;
  --focus: #1a5fb4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14181d; --surface: #1d232a; --border: #333c46;
    --ink: #eef1f4; --ink-muted: #b6c0ca; --ink-dim: #99a5b1;
    --dead: #ff8a80; --provider: #ffb86b; --unproven: #8ab4f8;
    --backup: #7ee2a0; --slow: #ffb86b; --audio: #8ab4f8;
    --focus: #8ab4f8;
  }
}
:root[data-theme="dark"] {
  --bg: #14181d; --surface: #1d232a; --border: #333c46;
  --ink: #eef1f4; --ink-muted: #b6c0ca; --ink-dim: #99a5b1;
  --dead: #ff8a80; --provider: #ffb86b; --unproven: #8ab4f8;
  --backup: #7ee2a0; --slow: #ffb86b; --audio: #8ab4f8;
  --focus: #8ab4f8;
}
:root[data-theme="light"] {
  --bg: #ffffff; --surface: #f6f7f9; --border: #d6dae0;
  --ink: #14181d; --ink-muted: #4a5560; --ink-dim: #66727e;
  --dead: #b3261e; --provider: #8a4c00; --unproven: #1a5fb4;
  --backup: #1a7f37; --slow: #8a4c00; --audio: #1a5fb4;
  --focus: #1a5fb4;
}
body {
  margin: 0; padding: var(--s5);
  background: var(--bg); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 20px; line-height: 1.55;
}
header { display: flex; align-items: center; gap: var(--s4); margin-bottom: var(--s5); }
header img { width: 64px; height: 64px; }
h1 { font-size: 30px; margin: 0 0 var(--s1) 0; }
.meta { color: var(--ink-muted); font-size: 17px; }
.totals { display: flex; flex-wrap: wrap; gap: var(--s3); margin-bottom: var(--s5); }
.tile {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: var(--s3) var(--s4); min-width: 128px;
}
.tile .n { font-size: 28px; font-weight: 600; }
.tile .k { color: var(--ink-muted); font-size: 16px; }
.chart { margin-bottom: var(--s5); }
details {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; margin-bottom: var(--s3); padding: var(--s3) var(--s4);
}
summary { cursor: pointer; font-size: 22px; font-weight: 600; }
summary:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
.dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: var(--s2); }
.dot-dead { background: var(--dead); }
.dot-provider { background: var(--provider); }
.dot-unproven { background: var(--unproven); }
.dot-backup { background: var(--backup); }
.dot-slow { background: var(--slow); }
.dot-audio { background: var(--audio); }
.glyph { margin-right: var(--s2); }
.count {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 999px; padding: 0 var(--s2); margin-left: var(--s2);
  font-size: 17px; color: var(--ink-muted);
}
.sub { color: var(--ink-muted); font-size: 17px; margin: var(--s2) 0 var(--s1) 0; }
.act { color: var(--ink); font-size: 17px; margin: 0 0 var(--s2) 0; font-weight: 600; }
.hint { color: var(--ink-dim); font-size: 15px; margin: 0 0 var(--s3) 0; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 17px; }
th, td { text-align: left; padding: var(--s2) var(--s3); border-bottom: 1px solid var(--border); }
th { color: var(--ink-muted); font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.empty { color: var(--ink-dim); font-size: 17px; margin: var(--s2) 0; }
.bar-label { fill: var(--ink-muted); font-size: 13px; }
.bar-dead { fill: var(--dead); }
.bar-provider { fill: var(--provider); }
.bar-unproven { fill: var(--unproven); }
.bar-backup { fill: var(--backup); }
.bar-slow { fill: var(--slow); }
.bar-audio { fill: var(--audio); }
footer {
  margin-top: var(--s5); padding-top: var(--s4);
  border-top: 1px solid var(--border);
  color: var(--ink-muted); font-size: 16px;
}
footer a { color: var(--focus); }
"""

_FIND_HINT = ("Expand this section before using your browser find on this page. "
              "Text inside a collapsed section is not searchable in some browsers.")

REPO_URL = "https://github.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin"
ISSUES_URL = REPO_URL + "/issues"


def _esc(value):
    """HTML-escape any value. Total: None and non-strings become text first."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _logo_data_uri(plugin_dir, max_encoded=LOGO_MAX_ENCODED_BYTES):
    """Base64 data URI for the plugin logo, or None.

    None means the header renders with NO image element at all. A logo that
    cannot be read, or that is too large to ride on every emailed copy, must
    never fail a build.
    """
    try:
        path = os.path.join(plugin_dir or "", "logo.png")
        with open(path, "rb") as fh:
            raw = fh.read()
    except (OSError, TypeError, ValueError):
        return None
    try:
        encoded = base64.b64encode(raw).decode("ascii")
    except Exception:
        return None
    if len(encoded) > max_encoded:
        return None
    return "data:image/png;base64," + encoded


def _fmt_fps(value):
    """Framerate for display. Total: anything unusable renders as empty."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return ""
    return "%.1f" % number


def _section_html(section):
    """One report section as a collapsible details element.

    The details element needs no JavaScript, and a client that does not
    implement it renders the content EXPANDED, so the failure mode is
    everything visible rather than content lost. Every section starts
    COLLAPSED, and the count in the heading is the number of rows in the table
    beneath it, never the size of any wider population.
    """
    section = section if isinstance(section, dict) else {}
    dot = _SECTION_DOT.get(section.get("key"), "dot-unproven")
    glyph = _DOT_GLYPH.get(dot, "")
    rows = section.get("rows") or []
    out = [
        "<details><summary>",
        '<span class="dot ', dot, '" aria-hidden="true"></span>',
    ]
    if glyph:
        out.extend(['<span class="glyph" aria-hidden="true">', glyph, "</span>"])
    out.extend([
        _esc(section.get("title")),
        '<span class="count">', str(len(rows)), "</span>",
        "</summary>",
        '<p class="sub">', _esc(section.get("description")), "</p>",
        '<p class="act">What to do: ', _esc(section.get("action")), "</p>",
        '<p class="hint">', _esc(_FIND_HINT), "</p>",
    ])
    if not rows:
        out.append('<p class="empty">Nothing in this group.</p>')
    else:
        out.append('<div class="scroll"><table><thead><tr>'
                   "<th>Channel</th><th>Streams</th><th>Playing</th>"
                   "<th>Resolution</th><th>Framerate</th><th>Reasons</th>"
                   "</tr></thead><tbody>")
        for row in rows:
            row = row if isinstance(row, dict) else {}
            out.extend([
                "<tr><td>", _esc(row.get("channel_name")), "</td>",
                '<td class="num">', _esc(row.get("streams_total", 0)), "</td>",
                '<td class="num">', _esc(row.get("streams_alive", 0)), "</td>",
                "<td>", _esc(row.get("resolution") or ""), "</td>",
                '<td class="num">', _fmt_fps(row.get("framerate")), "</td>",
                "<td>", _esc(", ".join(str(x) for x in (row.get("reasons") or []))), "</td></tr>",
            ])
        out.append("</tbody></table></div>")
    out.append("</details>")
    return "".join(out)


def _bar_chart(sections):
    """Inline SVG bar chart of the section counts.

    Colour is applied by a CSS CLASS, never by a fill attribute holding a
    custom property: support for that is patchy and it fails silently to BLACK,
    which is an invisible chart on the dark surface.

    Total over its input: an all-zero set of counts must not divide by zero.
    """
    items = []
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        count = len(section.get("rows") or [])
        cls = _SECTION_DOT.get(section.get("key"), "dot-unproven").replace("dot-", "bar-")
        items.append((section.get("title") or "", count, cls))
    if not items:
        return ""
    widest = max(n for _, n, _ in items)
    if widest <= 0:
        return ""
    row_h, gap, label_w, bar_max = 26, 6, 250, 380
    height = len(items) * (row_h + gap)
    width = label_w + bar_max + 70
    out = ['<svg class="chart" role="img" aria-label="Channel counts by group" ',
           'viewBox="0 0 %d %d" width="100%%" height="%d">' % (width, height, height)]
    for index, (title, count, cls) in enumerate(items):
        y = index * (row_h + gap)
        bar_w = int(bar_max * count / widest)
        out.append('<text class="bar-label" x="0" y="%d">%s</text>' % (y + 18, _esc(title)))
        out.append('<rect class="%s" x="%d" y="%d" width="%d" height="%d" rx="3"></rect>'
                   % (cls, label_w, y, bar_w, row_h))
        out.append('<text class="bar-label" x="%d" y="%d">%d</text>'
                   % (label_w + bar_w + 8, y + 18, count))
    out.append("</svg>")
    return "".join(out)


def _tile(number, label):
    return ('<div class="tile"><div class="n">%s</div><div class="k">%s</div></div>'
            % (_esc(number), _esc(label)))


def _detector_sentence(detectors):
    """State which optional detectors ran, so a zero can be read correctly.

    A zero from a detector that was OFF means nobody looked. That is not the
    same as measured and found none, and the reader must be able to tell.
    """
    detectors = detectors if isinstance(detectors, dict) else {}
    names = {
        "black_screen": "blank screen",
        "frozen_video": "frozen video",
        "silent_audio": "silent audio",
        "placeholder_file": "placeholder file",
    }
    ran = sorted(names[k] for k, v in detectors.items() if v and k in names)
    off = sorted(names[k] for k, v in detectors.items() if not v and k in names)
    parts = []
    if ran:
        parts.append("Detectors that ran: " + ", ".join(ran) + ".")
    if off:
        parts.append("Not measured, so a zero here means nobody looked: "
                     + ", ".join(off) + ".")
    return " ".join(parts)


def render_html(model):
    """A complete, self-contained HTML page.

    No safety net above this: write_report catches OSError only. Every helper
    called here is total over its inputs, and the page is assembled by joining
    a list rather than by one large formatted string, so a literal brace in the
    CSS cannot become a format field.
    """
    model = model if isinstance(model, dict) else {}
    totals = model.get("totals") if isinstance(model.get("totals"), dict) else {}
    health = model.get("run_health") if isinstance(model.get("run_health"), dict) else {}
    sections = model.get("sections") or []

    generated = model.get("generated_at")
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(generated)))
    except (TypeError, ValueError):
        stamp = "unknown time"

    logo = _logo_data_uri(model.get("plugin_dir"))

    out = ["<title>IPTV Checker report</title>", "<style>", _CSS, "</style>", "<header>"]
    if logo:
        out.append('<img src="%s" alt="">' % logo)
    out.extend([
        "<div><h1>IPTV Checker report</h1>",
        '<div class="meta">Generated ', _esc(stamp),
        " . Plugin version ", _esc(model.get("version") or "unknown"),
        "</div></div></header>",
        '<div class="totals">',
        _tile(totals.get("channels", 0), "channels checked"),
        _tile(totals.get("channels_working", 0), "channels playing"),
        _tile(totals.get("channels_listed", 0), "listed below"),
        _tile(totals.get("channels_no_issues", 0), "no issues found"),
        _tile(totals.get("streams", 0), "streams tested"),
        "</div>",
    ])

    reconciles = (totals.get("channels_listed", 0) + totals.get("channels_no_issues", 0)
                  == totals.get("channels", 0))
    out.append('<p class="sub">')
    out.append(_esc(
        "Channels with nothing wrong are counted but not listed, so the groups below hold "
        "%s of %s channels." % (totals.get("channels_listed", 0), totals.get("channels", 0))
        if reconciles else
        "The group counts below do not reconcile against the channel total, which is a bug."))
    out.append("</p>")

    if not health.get("trustworthy", True):
        out.append('<p class="act">')
        out.append(_esc(
            "The provider rate limited %s stream request(s) during this run, so some results "
            "may be wrong. Re-run before acting on anything below."
            % health.get("rate_limited_streams", 0)))
        out.append("</p>")

    detector_text = _detector_sentence(health.get("detectors"))
    if detector_text:
        out.extend(['<p class="hint">', _esc(detector_text), "</p>"])

    out.append(_bar_chart(sections))
    for section in sections:
        out.append(_section_html(section))

    out.extend([
        "<footer>",
        'Built by the IPTV Checker plugin. ',
        '<a href="', REPO_URL, '">Source</a> . ',
        '<a href="', ISSUES_URL, '">Report a problem</a>.',
        "</footer>",
    ])
    return "".join(out)


CSV_COLUMNS = ["group", "channel_id", "channel_name", "streams_total",
               "streams_playing", "resolution", "framerate", "reasons"]


def render_csv(model):
    """The same rows as the HTML, as CSV. Convenience export, not the product."""
    model = model if isinstance(model, dict) else {}
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for section in model.get("sections") or []:
        if not isinstance(section, dict):
            continue
        title = section.get("title") or ""
        for row in section.get("rows") or []:
            if not isinstance(row, dict):
                continue
            writer.writerow([
                title,
                row.get("channel_id", ""),
                row.get("channel_name", ""),
                row.get("streams_total", 0),
                row.get("streams_alive", 0),
                row.get("resolution") or "",
                _fmt_fps(row.get("framerate")),
                ", ".join(str(x) for x in (row.get("reasons") or [])),
            ])
    return buf.getvalue()


def _atomic_write(path, text):
    """Write via a temporary file in the SAME directory, then replace.

    Same directory because os.replace is only atomic within one filesystem. A
    partially written report must never be readable at the live path.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _prune_archives(directory, prefix, suffix, keep=ARCHIVE_LIMIT):
    """Keep the newest `keep` archives by name. Never raises."""
    try:
        names = sorted(n for n in os.listdir(directory)
                       if n.startswith(prefix) and n.endswith(suffix))
    except OSError:
        return
    for name in names[:-keep] if keep > 0 else names:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def write_report(model, report_dir, csv_dir, now):
    """Write the HTML report and the CSV. NEVER RAISES.

    The HTML report is the product; the CSV is a convenience export. If the CSV
    directory is unwritable, the HTML write must still succeed and be returned,
    and only csv_path degrades to None.

    Returns {"html_path", "csv_path", "archive_path", "error"}. A falsy
    html_path is the ONLY honest signal that nothing was published: counts are
    computed before the write, so a caller that reports success on the counts
    alone will report a healthy summary for a run that wrote nothing. Gate on
    html_path, and verify by the artifact's mtime rather than by this return
    value.
    """
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    except (TypeError, ValueError):
        stamp = "unknown"
    out = {"html_path": None, "csv_path": None, "archive_path": None, "error": None}

    try:
        os.makedirs(report_dir, exist_ok=True)
        html_text = render_html(model)
        live = os.path.join(report_dir, REPORT_HTML)
        _atomic_write(live, html_text)
        archive = os.path.join(report_dir, "report-%s.html" % stamp)
        _atomic_write(archive, html_text)
        out["html_path"] = live
        out["archive_path"] = archive
        _prune_archives(report_dir, "report-", ".html")
    except OSError as exc:
        out["error"] = "could not write the report: %s" % exc
        return out

    try:
        os.makedirs(csv_dir, exist_ok=True)
        csv_path = os.path.join(csv_dir, "report-%s.csv" % stamp)
        _atomic_write(csv_path, render_csv(model))
        out["csv_path"] = csv_path
        _prune_archives(csv_dir, "report-", ".csv")
    except OSError as exc:
        out["error"] = "the report was written but the CSV was not: %s" % exc

    return out
