# Black-Screen Detection — Design Spec

**Date:** 2026-06-19
**Status:** Approved (revised after QA review; pending implementation plan)
**Feature:** Detect Alive-by-ffprobe streams that decode to a black screen and reclassify them as Dead.

## Problem

The checker validates streams with `ffprobe`, which reads container/stream
*metadata* only — codec, resolution, framerate, bitrate. It never decodes
pixels. A stream emitting a valid but entirely black video signal (1920×1080,
H264, 30fps, real bitrate) is indistinguishable from a working channel and is
reported **Alive**. Users want these marked **Dead** so the existing
rename/move/delete actions clean them up.

## Scope

- **In scope:** Pure black-screen detection via `ffmpeg blackdetect`.
- **Out of scope:** Frozen/static-image detection, non-black error-slate
  detection (user explicitly opted out). Per-channel reference-image matching.

## Detection Mechanism

New helper `_check_black_screen(url, timeout, settings, logger)` runs:

```
ffmpeg -hide_banner -nostats -loglevel info \
       -user_agent "VLC/3.0.21 LibVLC/3.0.21" -rw_timeout <timeout_us> \
       -i <url> -t <sample_seconds> -an \
       -vf blackdetect=d=<min_black_seconds>:pic_th=0.98 -f null -
```

Critical command details (all verified against ffmpeg semantics during QA):

- **Input options must precede `-i`.** `-user_agent` and `-rw_timeout` are
  *input* options; placed after `-i` they are silently ignored.
- **Use `-rw_timeout` (µs), not `-timeout`.** `-timeout` is protocol-specific
  and its meaning varies; `-rw_timeout` is the general "abort if no I/O
  progress" input option. (The existing ffprobe call uses `-timeout` at
  plugin.py:3123; do not copy that verbatim to ffmpeg.)
- **`-loglevel info` is required — never `-loglevel error`.** blackdetect emits
  its results at `info` level. Suppressing below `info` yields empty stderr and
  the detector silently never fires. `-nostats` removes ffmpeg's progress spam
  so stderr stays small.
- `-t <sample_seconds>` bounds the decode to the sample window.
- `-an` drops audio.
- `blackdetect=d=<min_black_seconds>` reports a black segment only when black
  runs continuously for ≥ that many seconds.
- `pic_th=0.98` — a frame is "black" when ≥98% of pixels are below the luma
  threshold (ffmpeg default; non-configurable in v1).
- `-f null -` discards decoded output (Linux container target).

### Wall-clock safety

`-t` and `-rw_timeout` are **not** sufficient to bound runtime (a connection
that opens then stalls mid-decode can hang). The subprocess is therefore wrapped
in an explicit wall-clock cap, mirroring the existing ffprobe call at
plugin.py:3175:

```
subprocess.run(cmd, capture_output=True, text=True,
               timeout=settings['black_screen_ffmpeg_timeout'])
```

`subprocess.TimeoutExpired` → return `None` (fail-open).

### Parsing

Output is parsed by a pure, Django-free function
`_parse_blackdetect_output(stderr)` that scans stderr for lines like:

```
[blackdetect @ 0x...] black_start:0 black_end:6.0 black_duration:6.0
```

It returns the list of parsed `(start, end, duration)` segments (empty if none).

### Return contract

`_check_black_screen` returns:

- `True`  — `_parse_blackdetect_output` found ≥1 qualifying black segment.
  This holds **even if ffmpeg exits non-zero**, because blackdetect commonly
  prints the segment and then exits non-zero on a stream that ends early — a
  reported black segment is authoritative.
- `False` — ffmpeg produced parseable output (or clean exit) with **no** black
  segment → real video. Also covers audio-only streams (no video → blackdetect
  never instantiates → no segment) and streams shorter than the sample.
- `None`  — could not decide: `ffmpeg` missing (`FileNotFoundError`),
  `TimeoutExpired`, or non-zero exit with empty/unparseable stderr.

## Fail-Open Safety

`None` ⇒ **leave the stream Alive** and log a warning. A missing/broken
`ffmpeg`, a decode timeout, or a parse failure must never falsely kill a working
channel. Mirrors the plugin's existing graceful-degradation patterns.

## Integration Point

Inside `check_stream`, immediately before the existing
`return {'status': 'Alive', ...}`:

1. **Disabled** (`black_screen_detection` false) → return Alive unchanged.
   `ffmpeg` is never invoked (zero behavior change when off).
2. **Stop check** → if `self._stop_event.is_set()`, skip the black pass and
   return Alive (cancellation is checked *before* launching ffmpeg; the decode
   itself is blocking and is bounded by the subprocess wall-clock timeout, not
   interrupted mid-flight).
3. **Enabled** → call `_check_black_screen`. On `True`, return a Dead result
   built exactly like any other Dead stream:
   - `status = 'Dead'`
   - `error_type = 'Black Screen'`
   - `error = 'Stream decodes to a black screen'`
   - **`dispatcharr_metadata` set to the all-null shape** (same as
     `default_return`).

### Why null metadata (QA issue #4)

`_update_dispatcharr_metadata` (plugin.py:3383) branches on
`all_none = all(v is None for v in metadata.values())` (line 3400). Only when
`all_none` is true does it clear the channel's `stream_stats` jsonb; otherwise
it **writes** the stats into the DB. A Dead result carrying real metadata would
therefore push 1080p/H264/bitrate stats onto a channel we just declared dead.
Returning null metadata makes a black stream behave identically to every other
Dead stream: its stats get cleared. The CSV/webhook still convey *why* via
`error_type='Black Screen'`; blank resolution columns are consistent with all
other dead rows.

The black pass runs on **every** Alive stream whenever the toggle is on, for
both manual ("Run Now") and scheduled/windowed checks. It executes inside the
existing parallel/sequential check paths, inheriting worker concurrency and
window-end behavior.

## New Settings (`plugin.json`)

Schema matches existing fields (`id`, `label` with emoji, `type` =
`number`/`boolean`/`string`, `default`, `help_text`). Grouped under a new
`_section_black_screen` info block.

| id | type | default | purpose |
|---|---|---|---|
| `black_screen_detection` | boolean | `false` | master toggle — off = no behavior change |
| `black_screen_sample_seconds` | number | `6` | seconds of video to decode per stream |
| `black_screen_min_black_seconds` | number | `3` | continuous black run required to flag |
| `black_screen_ffmpeg_timeout` | number | `20` | wall-clock cap on the ffmpeg subprocess (connection + decode) |
| `ffmpeg_path` | string | `/usr/local/bin/ffmpeg` | sibling of existing `ffprobe_path` |

`help_text` for `black_screen_detection` must state: the per-stream cost
(~5–10s per Alive stream), the fail-open behavior, and that very-dark-but-not-
black slates (dark grey, a logo on black) will read as not-black and stay Alive.

### Default tuning (QA issue #3)

The first revision used a 5s sample with a 4s black requirement — only 1s of
headroom. Connection latency, GOP/keyframe alignment, and PTS gaps mean a
genuinely-black stream could yield <4s of contiguous decodable black and report
**no** segment (false negative). Defaults are widened to **6s sample / 3s
continuous black**, giving ~3s of latency headroom while still requiring the
sample to be essentially all black (a working stream won't show 3s of
continuous black). Both remain user-tunable.

## Downstream Impact

None beyond the metadata-null handling above. Flagged streams carry
`status='Dead'`, so rename/move/delete (filter on `status == 'Dead'`),
Alive/Dead/Skipped counts, CSV export (`error_type` column), and webhook all
flow through with no action-code changes.

## Cost & Performance

- Adds ~5–10s per **Alive** stream when enabled; Dead/Skipped streams are
  unaffected (black pass only runs after a stream passes ffprobe).
- Per-stream, inside existing concurrency — scales with `parallel_workers`.
- **Windowed-schedule interaction:** the added per-stream time materially
  reduces how many streams a window clears (e.g. 500 alive streams ≈ +20–40 min
  at low worker counts). The black pass runs within the existing loop, which
  already breaks on `_past_window_end()` between streams, so a window won't
  overshoot — it just covers fewer streams per window. Keep the default sample
  low; document the trade-off in help text.

## Tests (`tests/test_black_screen.py`)

1. `_parse_blackdetect_output`:
   - stderr with one black segment → returns it.
   - stderr with multiple black segments → returns all, ordered.
   - clean stderr, no black → empty list.
   - malformed/garbage stderr → empty list, no exception.
   - `-loglevel error`-style empty stderr → empty list.
2. `_check_black_screen` (subprocess mocked):
   - black stderr, exit 0 → `True`.
   - black stderr, **non-zero exit** → `True` (segment is authoritative).
   - no-black stderr → `False`.
   - audio-only / short-stream (no segment) → `False`.
   - `FileNotFoundError` (ffmpeg missing) → `None`.
   - `subprocess.TimeoutExpired` → `None`.
   - non-zero exit, empty/unparseable stderr → `None`.
3. `check_stream` integration (subprocess mocked):
   - ffprobe Alive + ffmpeg black → `status='Dead'`,
     `error_type='Black Screen'`, **metadata all-null**.
   - toggle off → ffmpeg never invoked, stream stays Alive.
   - ffmpeg `None` (error/timeout) → stream stays Alive (fail-open).
   - `_stop_event` set → ffmpeg never invoked, stays Alive.
4. `_update_dispatcharr_metadata` path: a Black-Screen Dead result hits the
   `all_none` clear branch (stats cleared), not the write branch.

## Version / Release

Calver bump via `bump_version.py`. Deploy from the inner `iptv_checker/` copy
(both `plugin.py` and `plugin.json`). Implementation plan must include a quick
container check that `ffmpeg` exists at the configured path before relying on
it. Release notes document the new opt-in setting and its cost.

## Known Limitations

- Detects **pure** black only. Near-black error slates, dark scenes, or static
  non-black "no signal" cards are not caught (by design, v1).
- Keyframe/PTS latency at decode start is the main false-negative risk;
  mitigated by the headroom in the default tuning.
- Cancellation does not interrupt an in-flight decode; it is bounded by
  `black_screen_ffmpeg_timeout`.
