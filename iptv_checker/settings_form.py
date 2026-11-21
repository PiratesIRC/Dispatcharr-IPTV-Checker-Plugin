"""Settings form layout for Dispatcharr 0.34 and later.

Dispatcharr 0.34.0 added collapsible section fields, multiselect pick lists and
a rows height for text fields. All of them need manifest_version 1 in
plugin.json, and Dispatcharr 0.33 and earlier drop a field of a type they do
not know. So the flat form stays in plugin.json, which every version reads, and
this module builds the 0.34 form from it for Plugin.get_fields, which only 0.34
and later call. Pure functions: no Django, no filesystem, no clock.
"""

import copy

# Pick lists stored under a NEW id. A multiselect needs a list value, and a
# stored comma string under the old id would break the form, so the old text
# setting keeps its id (0.33 still edits it) and legacy_settings() writes the
# list back into it at run time. Every parser then reads one text form.
LIST_SETTINGS = {
    # new id: old id
    "video_format_suffix_list": "video_format_suffixes",
}
NEW_ID_BY_OLD_ID = {old: new for new, old in LIST_SETTINGS.items()}

VIDEO_FORMATS = ("UHD", "FHD", "HD", "SD", "Unknown")

NEW_FIELDS = {
    "video_format_suffix_list": {
        "label": "🎬 Add Video Format Suffixes",
        "help_text": "The formats to add as a suffix, for example [HD], to channel names by the Add Video Format Suffix action and its scheduled step. Pick one or more.",
        "options": VIDEO_FORMATS,
    },
}

# Text settings drawn as a taller box in the 0.34 form, under the same id. The
# value stays a string, so nothing is written back. Value: starting height.
TEXT_ROWS = {
    "scheduled_times": 3,
}

# The info fields that head each group of settings in the flat form.
SECTION_PREFIX = "_section_"

# Groups a typical user rarely opens start closed.
COLLAPSED_SECTIONS = frozenset((
    "_section_black_screen",
    "_section_placeholder_file",
    "_section_frozen_video",
    "_section_silent_audio",
    "_section_closed_captions",
    "_section_post_check",
    "_section_dead",
    "_section_black",
    "_section_low_fps",
    "_section_format",
    "_section_restore",
    "_section_auto_rename_move",
    "_section_auto_delete",
    "_section_advanced",
))

# Id suffix of the info field that keeps a section header's explanation.
ABOUT_SUFFIX = "_about"
HELP_SUFFIX = "_help_"

# Dispatcharr draws help text as one plain paragraph. In the 0.34 form a long
# help text is cut to a short form and the rest follows as titled info fields
# right below the field. The flat form keeps the full text.
# Value: (short help, [(title, paragraph), ...]).
HELP_PARAGRAPHS = {
    "channel_groups": (
        "The channel groups this plugin should act on. Leave empty for all groups. How this list is read depends on the Channel Groups Mode setting directly below.",
        [
            ("Wildcards", "Wildcards are supported: * matches any characters, ? matches one, and [abc] matches a character from the set. Matching is case-sensitive."),
            ("Names that match nothing", "A group name or pattern that matches nothing is reported as an error by Validate Settings in both modes, because a typo in exclude mode would check the group you meant to skip."),
        ],
    ),
    "channel_groups_mode": (
        "Decides whether the Channel Groups list names the groups to check or the groups to skip. Leaving the Channel Groups box empty checks every group either way.",
        [
            ("Choosing a mode", "Use the second option when a group should be left alone, for example pay-per-view or event channels. The second option also means a group you create later is checked automatically, whereas listing groups to check means a new group is skipped until you add it."),
            ("Older settings", "This replaces the separate Group(s) to Check and Group(s) to EXCLUDE settings; if you had values in those, they are read automatically until you save this one."),
        ],
    ),
    "placeholder_file_detection": (
        "When ON, any stream reporting a fixed container duration is marked Dead under the `Placeholder File` error type, so the dead-channel rename/move/delete actions handle it. Adds no probe time.",
        [
            ("Evidence", "Measured against a full sweep of 156 provider channels: all 138 healthy channels reported no duration, while all 19 that did were serving a placeholder file. The duration and container bitrate are recorded in the CSV for every stream even when this is OFF, so you can check the evidence before turning it on."),
            ("When to leave it OFF", "Leave OFF if any channel here is fed by a finite file you want to keep."),
        ],
    ),
    "frozen_video_detection": (
        "When ON, the decoded sample is checked for a continuous run of identical frames and matching streams are marked Dead under the `Frozen Video` error type, so the dead-channel rename/move/delete actions handle them.",
        [
            ("Blank screens", "A blank screen is also a still picture, so blank-screen detection takes precedence when both are ON and both fire. Uses the Blank-Screen Sample length for how much video to decode."),
            ("When ffmpeg fails", "Fail-open: if ffmpeg is missing or errors, the stream stays Alive."),
        ],
    ),
    "silent_audio_detection": (
        "When ON, the mean volume of the decoded sample is measured and streams at or below the threshold are marked Dead under the `Silent Audio` error type.",
        [
            ("Streams it skips", "Skipped entirely for streams with no audio track, which cannot be silent. Fail-open: if the volume cannot be measured the stream stays Alive, because no measurement is not evidence of silence."),
            ("Video still plays", "This marks a stream Dead even though its video is fine, so the dead-channel delete action would remove a channel that still has a picture."),
        ],
    ),
    "closed_caption_detection": (
        "When ON, the decoded sample is checked for closed-caption data (CEA-608 or CEA-708) and each stream gets Yes or No in the ffprobe_has_captions column of the results CSV. A separate subtitle track also counts as Yes.",
        [
            ("What Yes means", "Yes describes the stream the provider sends: an output profile that passes only video and audio, such as a ticker overlay, removes a separate subtitle track before it reaches the player. Yes means caption data is present, not that it contains text."),
            ("What No and blank mean", "A blank cell means the check could not decide. A stream can carry captions during only some programs, so a No applies to the sample that was checked. Uses the Blank-Screen Sample length for how much video to decode."),
        ],
    ),
    "scheduled_times": (
        "One or more cron expressions: 'minute hour day month weekday' (weekday: 0=Sunday, 6=Saturday). Separate several expressions with a semicolon or a new line. Leave blank to disable scheduling. After editing this field, click Save Schedule below to apply it.",
        [
            ("Examples", "'0 4 * * *' is daily at 4 AM, '0 0,8,16 * * *' is daily at midnight, 8 AM and 4 PM, '0 3 * * 0' is Sundays at 3 AM, and '0 2 */2 * *' is every 2 days at 2 AM. Two expressions together: '0 4 * * * ; 0 3 1 * *'."),
        ],
    ),
    "scheduler_email_report": (
        "When ON, each scheduled check builds the HTML report and queues it for delivery by email through Newsflasharr. Runs beside the CSV export and on the same terms, so a window that closes part way through the channel list still leaves a report.",
        [
            ("What it needs", "Requires the Newsflasharr plugin, its SMTP settings, and a routing rule matching source iptv_checker and event usage_report with channel smtp. Without that rule the report is delivered somewhere else, so the plugin checks for it first and refuses rather than reporting a success you cannot verify."),
        ],
    ),
    "scheduler_trigger_stream_mapparr": (
        "When ON, a scheduled check that reached the end of its channel list hands off to the Stream-Mapparr plugin, which then runs its own scheduled steps against the fresh results.",
        [
            ("What it needs", "Stream-Mapparr must be installed and enabled and must have its 'Run After IPTV Checker Scan' setting on; otherwise this logs a line and does nothing. Stream-Mapparr runs Sort Alternate Streams and Match and Assign, whichever it has ticked."),
            ("When it runs", "Runs after the rename, move and delete steps, and not at all for a window that closed part way through the list."),
        ],
    ),
    "ffprobe_flags": (
        "Comma-separated ffprobe flags for stream analysis. Default: -show_streams,-show_packets,-loglevel error.",
        [
            ("Options", "-show_streams (basic validation), -show_packets (per-packet data; required for video bitrate calculation on live MPEG-TS streams), -loglevel error (errors only)."),
            ("Do not add -show_frames", "When both -show_frames and -show_packets are passed, ffprobe returns a combined packets_and_frames array instead of separate packets[], which stops the bitrate fallback running."),
        ],
    ),
    "share_usage_counts": (
        "Sends this plugin's Streams Checked total and a random id for this plugin on this install to the plugin author's counter at plugin-stats.dpas.workers.dev, at most once an hour, so the README badges count every install that runs at least one action. No names, channels, streams, providers or settings are sent.",
        [
            ("When it sends", "It sends when the plugin runs, at most once an hour, or ten minutes after the last report when the plugin has just recorded new work. The server stores the id with the total and the date of the last report."),
            ("Your IP address", "The connection shows the server your public IP address; the server uses it only to limit abuse and does not store it in its database, though when an install first registers it keeps a salted one-way hash of it (of its /64 block for IPv6) for up to three days. Cloudflare, which hosts the server, keeps its own request logs."),
            ("Turning it off", "Untick to stop sending; this install's figures are deleted from the server the next time the plugin runs after you untick."),
        ],
    ),
}


def is_section_header(field):
    """True for an info field whose id marks the start of a settings group."""
    return (isinstance(field, dict) and field.get("type") == "info"
            and str(field.get("id", "")).startswith(SECTION_PREFIX))


def split_list(text):
    """A comma-separated setting as a list of trimmed, non-empty entries."""
    if isinstance(text, (list, tuple)):
        return [str(v).strip() for v in text if str(v).strip()]
    if text is None:
        return []
    return [part.strip() for part in str(text).split(",") if part.strip()]


def legacy_settings(settings):
    """A copy of settings with each pick-list value written back into its old
    text setting. Only a new id holding a list counts; anything else leaves the
    old setting alone. The input is not modified."""
    out = dict(settings or {})
    for new_id, old_id in LIST_SETTINGS.items():
        value = out.get(new_id)
        if isinstance(value, list):
            out[old_id] = ", ".join(split_list(value))
    return out


def _options(names, extra=()):
    """Pick-list options: names in their given order, then each extra value not
    already present (case-insensitive). An extra value is never left out,
    because Dispatcharr rejects a save holding a value that is not an option."""
    seen = {}
    for name in list(names or ()) + list(extra or ()):
        name = str(name or "").strip()
        if name and "," not in name:
            seen.setdefault(name.casefold(), name)
    return [{"label": n, "value": n} for n in seen.values()]


def _canonical(values, names):
    """Each value spelled as the matching name (case-insensitive), else as typed."""
    by_key = {str(n).casefold(): str(n) for n in names or ()}
    return [by_key.get(v.casefold(), v) for v in values]


def pick_list_fields(flat_fields, settings=None):
    """The flat form with each LIST_SETTINGS text field replaced by a pick list
    under its new id. The default is built from the saved old text value, so an
    install that never saved the new field keeps its choices. The input is not
    modified."""
    settings = settings or {}
    out = []
    for field in flat_fields or []:
        if not isinstance(field, dict) or field.get("id") not in NEW_ID_BY_OLD_ID:
            out.append(copy.deepcopy(field) if isinstance(field, dict) else field)
            continue
        old_id = field["id"]
        new_id = NEW_ID_BY_OLD_ID[old_id]
        meta = NEW_FIELDS[new_id]
        names = meta["options"]
        default = _canonical(split_list(settings.get(old_id, field.get("default", ""))), names)
        stored = settings.get(new_id)
        stored = split_list(stored) if isinstance(stored, list) else []
        out.append({
            "id": new_id, "label": meta["label"], "type": "multiselect",
            "default": default, "options": _options(names, default + stored),
            "help_text": meta["help_text"],
        })
    return out


def sectioned_fields(flat_fields):
    """The flat form rearranged into collapsible sections.

    Each section header becomes a section marker with the same id, and every
    field after it, up to the next header, gets "section": <that id>. The
    header's explanation moves to an info field at the top of its section,
    because a marker shows only its label. Fields before the first header stay
    ungrouped. The input is not modified.
    """
    out = []
    current = None
    for field in flat_fields or []:
        if not isinstance(field, dict):
            continue
        if is_section_header(field):
            current = field["id"]
            out.append({
                "id": current,
                "label": field.get("label") or current,
                "type": "section",
                "collapsed": current in COLLAPSED_SECTIONS,
            })
            text = field.get("description") or field.get("help_text")
            if text:
                out.append({
                    "id": current + ABOUT_SUFFIX, "label": "", "type": "info",
                    "description": text, "section": current,
                })
            continue
        item = copy.deepcopy(field)
        if current is not None:
            item["section"] = current
        if item.get("id") in TEXT_ROWS and item.get("type") in ("string", "text"):
            item["type"] = "text"
            item["rows"] = TEXT_ROWS[item["id"]]
        out.append(item)
        out.extend(_split_help(item, current))
    return out


def _split_help(item, section):
    """Cut a long help text to its short form (in place) and return the rest as
    titled info fields that follow the field."""
    spec = HELP_PARAGRAPHS.get(item.get("id"))
    if not spec:
        return []
    short, paragraphs = spec
    item["description" if item.get("type") == "info" else "help_text"] = short
    extra = []
    for number, (title, text) in enumerate(paragraphs, start=1):
        info = {"id": f"{item['id']}{HELP_SUFFIX}{number}", "label": title,
                "type": "info", "description": text}
        if section is not None:
            info["section"] = section
        extra.append(info)
    return extra


def build_form(flat_fields, settings=None):
    """The complete 0.34 form: pick lists first, then sections."""
    return sectioned_fields(pick_list_fields(flat_fields, settings))
