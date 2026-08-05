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
