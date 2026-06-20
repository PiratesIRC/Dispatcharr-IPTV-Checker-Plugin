# IPTV Checker v1.26.1702112 — Release Notes

## Black-Screen Detection (opt-in)

Some IPTV streams return a perfectly valid signal — correct resolution, codec,
framerate, and bitrate — yet only ever display a **black screen**. `ffprobe`
reads stream *metadata*, not pixels, so it reports these as **Alive**. This
release adds an optional second pass that decodes actual video and catches them.

### What it does

When **Detect Black-Screen Streams** is enabled, every stream that passes
ffprobe is decoded for a few seconds with `ffmpeg`'s `blackdetect` filter:

```
ffmpeg -hide_banner -nostats -loglevel info \
       -user_agent "VLC/3.0.21 LibVLC/3.0.21" -rw_timeout <µs> \
       -i <url> -t <sample_seconds> -an \
       -vf blackdetect=d=<min_black_seconds>:pic_th=0.98 -f null -
```

If a continuous black run of at least `Continuous Black Required (seconds)` is
found within the sample, the stream is reclassified **Dead** with
`error_type = Black Screen`, and its `stream_stats` are cleared. From there it
flows through rename / move / delete, CSV export, and webhook exactly like any
other dead stream — no new status, no action-code changes.

### Fail-open by design

If `ffmpeg` is missing, errors, produces unparseable output, or exceeds
**Black-Screen ffmpeg Timeout (seconds)**, the stream is left **Alive**. A
tooling glitch must never falsely kill a working channel — destructive actions
only ever act on confirmed-Dead streams.

### New settings

| Setting | Default | Notes |
|---|---|---|
| Detect Black-Screen Streams | `false` | Master toggle. Off = zero behavior change; ffmpeg is never invoked. |
| Black-Screen Sample (seconds) | `6` | Seconds of video to decode per alive stream. |
| Continuous Black Required (seconds) | `3` | Continuous black run needed to flag. Kept below the sample to absorb connection/keyframe latency. |
| Black-Screen ffmpeg Timeout (seconds) | `20` | Hard wall-clock cap on the decode. |
| FFmpeg Path | `/usr/local/bin/ffmpeg` | Under **Advanced**; sibling of the existing FFprobe Path. |

### Requirements

`ffmpeg` must be available in the Dispatcharr container (it already ships at
`/usr/local/bin/ffmpeg`; verified with ffmpeg 8.1, which includes the
`blackdetect` filter). Set **FFmpeg Path** if yours differs.

### Cost & scope

Adds ~5–10 s per **alive** stream when enabled; dead/skipped streams are
unaffected (the black pass only runs after a stream passes ffprobe). It runs on
both manual and scheduled checks and respects the windowed-schedule boundary and
cancellation (stop) signal.

### Known limitations

- Detects **pure** black only. Dark-grey or non-black "no signal" / error-card
  slates are not caught.
- A channel that is legitimately black for several seconds (fade-from-black
  intro, station ident) can be flagged. Raise **Continuous Black Required** or
  **Sample** seconds to compensate.

## Other

- New test suite `tests/test_black_screen.py` (parser, ffmpeg wrapper,
  `check_stream` integration, settings schema). Full suite: 96 passing.
- Corrected stale FFprobe defaults in the README settings table
  (`-show_streams,-show_packets,-loglevel error`; analysis duration `8`).

## Upgrade notes

No migration. The feature is **off by default** — existing deployments behave
identically until you enable the toggle. Deploy both `plugin.py` and
`plugin.json` from the `iptv_checker/` folder (hot-reload fires on
`plugin.json` mtime), then enable the setting and re-run a check.
