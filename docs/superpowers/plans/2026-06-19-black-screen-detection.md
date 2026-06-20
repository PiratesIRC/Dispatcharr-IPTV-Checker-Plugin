# Black-Screen Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect Alive-by-ffprobe streams that decode to a pure black screen and reclassify them as Dead so existing rename/move/delete actions clean them up.

**Architecture:** After a stream passes the existing ffprobe check, an opt-in second pass decodes a few seconds with `ffmpeg`'s `blackdetect` filter. A pure parser turns ffmpeg stderr into black segments; a thin subprocess wrapper returns True/False/None; the `check_stream` call site converts a True verdict into a Dead result with null metadata (identical to every other Dead stream except `error_type='Black Screen'`). Any ffmpeg problem fails open — the stream stays Alive.

**Tech Stack:** Python 3, `subprocess`, `ffmpeg` (Linux container), pytest. No new dependencies.

## Global Constraints

- **Deployable code is the inner folder** `iptv_checker/iptv_checker/`. Edit `plugin.py` and `plugin.json` there.
- **No type hints, minimal docstrings** in `plugin.py` (tests are exempt).
- **Do not reorganize imports or reformat** — ruff is errors-only.
- `re` and `subprocess` are already imported at module level in `plugin.py`; do not re-import.
- **Logs prefixed** `[Black Screen]` within the existing `[IPTV Checker]` logger.
- **Action/return shape:** `check_stream` returns a dict with at least `status`, `error`, `error_type`, `dispatcharr_metadata` (and the other keys already present in `default_return`).
- **Fail-open is mandatory:** a missing/erroring/timing-out ffmpeg must never produce a Dead verdict.
- **Versioning:** calver `1.26.{DDD}{HHMM}` via `python bump_version.py` only. Never hand-edit versions. Bump touches `plugin.json`, `plugin.py`, and `CLAUDE.md` together.
- **Tests live only in `tests/`.** New file: `tests/test_black_screen.py`.
- Validation loop: `python -m pytest tests -q` then `python -m ruff check .` then `python -m py_compile iptv_checker/plugin.py`.

## File Structure

- **Modify** `iptv_checker/iptv_checker/plugin.py`:
  - Add `_parse_blackdetect_output(stderr)` (staticmethod) — pure parser.
  - Add `_check_black_screen(self, url, timeout, settings, logger)` — subprocess wrapper.
  - Add the black-screen call site inside `check_stream` (after `stream_format = self._get_stream_format(resolution)` at line ~3281, before the `✓ ALIVE` log at ~3282).
- **Modify** `iptv_checker/iptv_checker/plugin.json`:
  - New `_section_black_screen` info block + 4 settings, inserted before the `_section_dead` block.
  - New `ffmpeg_path` string setting in the existing `_section_advanced`, right after `ffprobe_path`.
- **Create** `tests/test_black_screen.py` — parser, wrapper, integration, and settings-schema tests.
- **Modify** `CLAUDE.md` (architecture bullet) — folded into the final task; version line is handled by `bump_version.py`.

---

### Task 1: Pure blackdetect-output parser

**Files:**
- Modify: `iptv_checker/iptv_checker/plugin.py` (add `_parse_blackdetect_output` staticmethod to the `Plugin` class, e.g. directly above `check_stream` at line ~3027)
- Test: `tests/test_black_screen.py`

**Interfaces:**
- Produces: `Plugin._parse_blackdetect_output(stderr: str) -> list[tuple[float, float, float]]` — list of `(start, end, duration)` segments, `[]` when none. Static method (callable as `plugin._parse_blackdetect_output(s)` or `pmod.Plugin._parse_blackdetect_output(s)`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_black_screen.py`:

```python
"""Black-screen detection: blackdetect parsing, ffmpeg wrapper, and the
check_stream integration that reclassifies a black Alive stream as Dead.

Feature spec: docs/superpowers/specs/2026-06-19-black-screen-detection-design.md
"""
import json


# ---- Task 1: _parse_blackdetect_output ----------------------------------

# A realistic ffmpeg -loglevel info stderr fragment with one black segment.
_ONE_SEGMENT = (
    "Input #0, mpegts, from 'http://x/1.ts':\n"
    "  Duration: N/A, start: 1.400000, bitrate: N/A\n"
    "[blackdetect @ 0x55e0] black_start:0 black_end:6.0 black_duration:6\n"
)

_TWO_SEGMENTS = (
    "[blackdetect @ 0x1] black_start:0 black_end:2.5 black_duration:2.5\n"
    "[blackdetect @ 0x1] black_start:3.0 black_end:6.0 black_duration:3\n"
)

_NO_SEGMENT = (
    "Input #0, mpegts, from 'http://x/1.ts':\n"
    "  Stream #0:0: Video: h264, yuv420p, 1920x1080, 30 fps\n"
    "frame=  180 fps=0.0 q=-0.0 Lsize=N/A time=00:00:06.00 bitrate=N/A\n"
)


def test_parse_single_black_segment(pmod):
    segs = pmod.Plugin._parse_blackdetect_output(_ONE_SEGMENT)
    assert segs == [(0.0, 6.0, 6.0)]


def test_parse_multiple_black_segments_in_order(pmod):
    segs = pmod.Plugin._parse_blackdetect_output(_TWO_SEGMENTS)
    assert segs == [(0.0, 2.5, 2.5), (3.0, 6.0, 3.0)]


def test_parse_no_black_segment(pmod):
    assert pmod.Plugin._parse_blackdetect_output(_NO_SEGMENT) == []


def test_parse_empty_or_garbage(pmod):
    assert pmod.Plugin._parse_blackdetect_output("") == []
    assert pmod.Plugin._parse_blackdetect_output("totally unrelated text") == []
    assert pmod.Plugin._parse_blackdetect_output(None) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_black_screen.py -q`
Expected: FAIL — `AttributeError: type object 'Plugin' has no attribute '_parse_blackdetect_output'`.

- [ ] **Step 3: Implement the parser**

In `iptv_checker/iptv_checker/plugin.py`, add this method to the `Plugin` class immediately above `def check_stream(` (line ~3027). Match surrounding 4-space indentation:

```python
    @staticmethod
    def _parse_blackdetect_output(stderr):
        # Parse ffmpeg blackdetect stderr into a list of (start, end, duration)
        # float tuples. Returns [] when no black segments are present.
        segments = []
        if not stderr:
            return segments
        pattern = re.compile(
            r'black_start:(?P<start>[\d.]+)\s+'
            r'black_end:(?P<end>[\d.]+)\s+'
            r'black_duration:(?P<dur>[\d.]+)'
        )
        for m in pattern.finditer(stderr):
            try:
                segments.append((
                    float(m.group('start')),
                    float(m.group('end')),
                    float(m.group('dur')),
                ))
            except (ValueError, TypeError):
                continue
        return segments
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_black_screen.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_black_screen.py iptv_checker/iptv_checker/plugin.py
git commit -m "feat: add blackdetect stderr parser"
```

---

### Task 2: ffmpeg black-screen wrapper

**Files:**
- Modify: `iptv_checker/iptv_checker/plugin.py` (add `_check_black_screen` to the `Plugin` class, directly below `_parse_blackdetect_output`)
- Test: `tests/test_black_screen.py`

**Interfaces:**
- Consumes: `Plugin._parse_blackdetect_output` (Task 1).
- Produces: `Plugin._check_black_screen(self, url, timeout, settings, logger) -> True | False | None`
  - `True`  → blackdetect reported ≥1 segment (authoritative, even on non-zero exit).
  - `False` → ffmpeg exited 0 with no segment (real video).
  - `None`  → undecidable (ffmpeg missing, `TimeoutExpired`, or non-zero exit with no segment) → caller fails open.
  - Reads settings: `ffmpeg_path` (default `/usr/local/bin/ffmpeg`), `black_screen_sample_seconds` (6), `black_screen_min_black_seconds` (3), `black_screen_ffmpeg_timeout` (20).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_black_screen.py`:

```python
# ---- Task 2: _check_black_screen ----------------------------------------

import subprocess  # noqa: E402


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _ffmpeg_run(stderr="", returncode=0, capture=None):
    """Return a fake subprocess.run that records the ffmpeg command."""
    def _run(cmd, *a, **k):
        if capture is not None:
            capture.append(cmd)
        return _FakeCompleted(stderr=stderr, returncode=returncode)
    return _run


_BS_SETTINGS = {
    "ffmpeg_path": "/usr/local/bin/ffmpeg",
    "black_screen_sample_seconds": 6,
    "black_screen_min_black_seconds": 3,
    "black_screen_ffmpeg_timeout": 20,
}


def test_check_black_true_on_segment(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_ONE_SEGMENT, returncode=0))
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is True


def test_check_black_true_even_on_nonzero_exit(plugin, pmod, monkeypatch, quiet_logger):
    # blackdetect often prints a segment then ffmpeg exits non-zero (stream ends).
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_ONE_SEGMENT, returncode=1))
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is True


def test_check_black_false_no_segment_clean_exit(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_NO_SEGMENT, returncode=0))
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is False


def test_check_black_none_on_nonzero_without_segment(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr="Server returned 500", returncode=1))
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is None


def test_check_black_none_when_ffmpeg_missing(plugin, pmod, monkeypatch, quiet_logger):
    def _boom(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(pmod.subprocess, "run", _boom)
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is None


def test_check_black_none_on_timeout(plugin, pmod, monkeypatch, quiet_logger):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=20)
    monkeypatch.setattr(pmod.subprocess, "run", _boom)
    assert plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger) is None


def test_check_black_command_shape(plugin, pmod, monkeypatch, quiet_logger):
    capture = []
    monkeypatch.setattr(pmod.subprocess, "run", _ffmpeg_run(stderr=_NO_SEGMENT, capture=capture))
    plugin._check_black_screen("http://x/1.ts", 10, _BS_SETTINGS, quiet_logger)
    cmd = capture[0]
    # input options precede -i; uses -rw_timeout (not -timeout); info loglevel.
    assert cmd[0] == "/usr/local/bin/ffmpeg"
    assert "-rw_timeout" in cmd and "-timeout" not in cmd
    i_idx = cmd.index("-i")
    assert cmd.index("-rw_timeout") < i_idx
    assert cmd.index("-user_agent") < i_idx
    assert "-loglevel" in cmd and cmd[cmd.index("-loglevel") + 1] == "info"
    assert any(p.startswith("blackdetect=d=3:pic_th=0.98") for p in cmd)
    assert cmd[-3:] == ["-f", "null", "-"]
    assert cmd[i_idx + 1] == "http://x/1.ts"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_black_screen.py -k check_black -q`
Expected: FAIL — `AttributeError: 'Plugin' object has no attribute '_check_black_screen'`.

- [ ] **Step 3: Implement the wrapper**

In `plugin.py`, add directly below `_parse_blackdetect_output`:

```python
    def _check_black_screen(self, url, timeout, settings, logger):
        # Decode a few seconds of an Alive stream and detect a pure black
        # picture. Returns True (black), False (has video), or None
        # (undecidable -> caller leaves the stream Alive). Never raises.
        s = settings or {}
        ffmpeg_path = s.get('ffmpeg_path', '/usr/local/bin/ffmpeg')
        sample_seconds = s.get('black_screen_sample_seconds', 6)
        min_black = s.get('black_screen_min_black_seconds', 3)
        ffmpeg_timeout = s.get('black_screen_ffmpeg_timeout', 20)

        # Input options (-user_agent, -rw_timeout) MUST precede -i or ffmpeg
        # silently ignores them. -loglevel info is required: blackdetect logs
        # its results at info level, so -loglevel error would suppress them.
        cmd = [
            ffmpeg_path,
            '-hide_banner', '-nostats', '-loglevel', 'info',
            '-user_agent', 'VLC/3.0.21 LibVLC/3.0.21',
            '-rw_timeout', str(int(timeout) * 1000000),
            '-i', url,
            '-t', str(sample_seconds),
            '-an',
            '-vf', f'blackdetect=d={min_black}:pic_th=0.98',
            '-f', 'null', '-',
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=ffmpeg_timeout
            )
        except FileNotFoundError:
            logger.warning(f"[Black Screen] ffmpeg not found at {ffmpeg_path}; leaving stream Alive")
            return None
        except subprocess.TimeoutExpired:
            logger.warning(f"[Black Screen] ffmpeg timed out after {ffmpeg_timeout}s; leaving stream Alive")
            return None
        except Exception as e:
            logger.warning(f"[Black Screen] ffmpeg error ({e}); leaving stream Alive")
            return None

        segments = self._parse_blackdetect_output(result.stderr or '')
        if segments:
            return True
        if result.returncode == 0:
            return False
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_black_screen.py -k check_black -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_black_screen.py iptv_checker/iptv_checker/plugin.py
git commit -m "feat: add ffmpeg blackdetect wrapper (_check_black_screen)"
```

---

### Task 3: Wire black-screen check into check_stream

**Files:**
- Modify: `iptv_checker/iptv_checker/plugin.py` (`check_stream`, between line ~3281 and ~3282)
- Test: `tests/test_black_screen.py`

**Interfaces:**
- Consumes: `Plugin._check_black_screen` (Task 2), the existing `default_return` dict (built at line ~3074), `self._stop_event`.
- Produces: when `black_screen_detection` is true and the stream decodes black, `check_stream` returns `status='Dead'`, `error_type='Black Screen'`, `error='Stream decodes to a black screen'`, and **all-null `dispatcharr_metadata`** (so `_update_dispatcharr_metadata`'s `all_none` branch clears stats instead of writing them).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_black_screen.py`:

```python
# ---- Task 3: check_stream integration -----------------------------------

def _video_stream():
    return {
        "codec_type": "video", "index": 0,
        "width": 1920, "height": 1080, "r_frame_rate": "30/1",
        "codec_name": "h264", "pix_fmt": "yuv420p",
    }


def _dual_run(probe_data, ffmpeg_stderr="", ffmpeg_rc=0, capture=None):
    """Fake subprocess.run that answers ffprobe with JSON and ffmpeg
    (any command containing a blackdetect filter) with stderr."""
    def _run(cmd, *a, **k):
        if capture is not None:
            capture.append(cmd)
        is_ffmpeg = any("blackdetect" in str(p) for p in cmd)
        if is_ffmpeg:
            return _FakeCompleted(stderr=ffmpeg_stderr, returncode=ffmpeg_rc)
        return _FakeCompleted(stdout=json.dumps(probe_data), returncode=0)
    return _run


def _probe():
    return {"streams": [_video_stream()], "format": {"format_name": "mpegts"}}


def _run_check(plugin, settings, quiet_logger):
    stream = {"stream_url": "http://x/1.ts", "channel_name": "T", "stream_id": 1}
    base = {"probe_timeout": 1, "ffprobe_analysis_duration": 1}
    base.update(settings)
    return plugin.check_stream(stream, 1, 0, quiet_logger, skip_retries=True, settings=base)


def test_black_stream_becomes_dead(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run",
                        _dual_run(_probe(), ffmpeg_stderr=_ONE_SEGMENT, ffmpeg_rc=0))
    result = _run_check(plugin, {"black_screen_detection": True}, quiet_logger)
    assert result["status"] == "Dead"
    assert result["error_type"] == "Black Screen"
    # Metadata must be all-null so _update_dispatcharr_metadata clears stats.
    assert all(v is None for v in result["dispatcharr_metadata"].values())


def test_alive_stream_stays_alive_when_not_black(plugin, pmod, monkeypatch, quiet_logger):
    monkeypatch.setattr(pmod.subprocess, "run",
                        _dual_run(_probe(), ffmpeg_stderr=_NO_SEGMENT, ffmpeg_rc=0))
    result = _run_check(plugin, {"black_screen_detection": True}, quiet_logger)
    assert result["status"] == "Alive"


def test_toggle_off_never_invokes_ffmpeg(plugin, pmod, monkeypatch, quiet_logger):
    capture = []
    monkeypatch.setattr(pmod.subprocess, "run",
                        _dual_run(_probe(), ffmpeg_stderr=_ONE_SEGMENT, capture=capture))
    result = _run_check(plugin, {"black_screen_detection": False}, quiet_logger)
    assert result["status"] == "Alive"
    assert not any(any("blackdetect" in str(p) for p in c) for c in capture)


def test_ffmpeg_error_fails_open_to_alive(plugin, pmod, monkeypatch, quiet_logger):
    # ffmpeg returns None verdict (non-zero, no segment) -> stays Alive.
    monkeypatch.setattr(pmod.subprocess, "run",
                        _dual_run(_probe(), ffmpeg_stderr="boom", ffmpeg_rc=1))
    result = _run_check(plugin, {"black_screen_detection": True}, quiet_logger)
    assert result["status"] == "Alive"


def test_stop_event_skips_black_check(plugin, pmod, monkeypatch, quiet_logger):
    capture = []
    monkeypatch.setattr(pmod.subprocess, "run",
                        _dual_run(_probe(), ffmpeg_stderr=_ONE_SEGMENT, capture=capture))
    plugin._stop_event.set()
    result = _run_check(plugin, {"black_screen_detection": True}, quiet_logger)
    assert result["status"] == "Alive"
    assert not any(any("blackdetect" in str(p) for p in c) for c in capture)
    plugin._stop_event.clear()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_black_screen.py -k "black_stream or stays_alive or toggle_off or fails_open or stop_event" -q`
Expected: FAIL — black stream returns `status='Alive'` (no call site yet).

- [ ] **Step 3: Add the call site**

In `plugin.py`, find these two lines inside `check_stream` (line ~3281–3282):

```python
                        stream_format = self._get_stream_format(resolution)
                        logger.info(f"✓ '{channel_name}' ALIVE - {stream_format} {resolution} {framerate_num:.1f}fps")
```

Insert the black-screen check **between** them, so the block reads:

```python
                        stream_format = self._get_stream_format(resolution)

                        # Optional black-screen verification. An Alive-by-ffprobe
                        # stream can still decode to a pure black picture; mark it
                        # Dead so destructive actions clean it up. Fail-open: any
                        # ffmpeg problem (None) leaves the stream Alive. Null
                        # metadata mirrors every other Dead stream so the DB stats
                        # get cleared (see _update_dispatcharr_metadata all_none).
                        if (settings and settings.get('black_screen_detection')
                                and not self._stop_event.is_set()):
                            if self._check_black_screen(url, timeout, settings, logger) is True:
                                logger.info(f"✗ '{channel_name}' DEAD - Black Screen ({resolution})")
                                black_return = dict(default_return)
                                black_return['error'] = 'Stream decodes to a black screen'
                                black_return['error_type'] = 'Black Screen'
                                return black_return

                        logger.info(f"✓ '{channel_name}' ALIVE - {stream_format} {resolution} {framerate_num:.1f}fps")
```

Note: `default_return` already has `status='Dead'` and all-null `dispatcharr_metadata`; `dict(default_return)` is a sufficient shallow copy because only top-level keys are reassigned.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_black_screen.py -q`
Expected: PASS (all black-screen tests).

- [ ] **Step 5: Run the full suite + lint + compile (no regressions)**

Run:
```bash
python -m pytest tests -q
python -m ruff check .
python -m py_compile iptv_checker/plugin.py
```
Expected: all pass, no ruff errors.

- [ ] **Step 6: Commit**

```bash
git add tests/test_black_screen.py iptv_checker/iptv_checker/plugin.py
git commit -m "feat: reclassify black-screen streams as Dead in check_stream"
```

---

### Task 4: Add settings to plugin.json

**Files:**
- Modify: `iptv_checker/iptv_checker/plugin.json`
- Test: `tests/test_black_screen.py`

**Interfaces:**
- Produces settings consumed by Tasks 2–3: `black_screen_detection` (boolean, default false), `black_screen_sample_seconds` (number, 6), `black_screen_min_black_seconds` (number, 3), `black_screen_ffmpeg_timeout` (number, 20), `ffmpeg_path` (string, `/usr/local/bin/ffmpeg`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_black_screen.py`:

```python
# ---- Task 4: settings schema --------------------------------------------

import io  # noqa: E402
from pathlib import Path  # noqa: E402


def _load_fields():
    p = Path(__file__).resolve().parents[1] / "iptv_checker" / "plugin.json"
    data = io.open(p, encoding="utf-8").read()
    return {f["id"]: f for f in json.loads(data)["fields"] if "id" in f}


def test_black_screen_settings_present_with_defaults():
    fields = _load_fields()
    expected = {
        "black_screen_detection": ("boolean", False),
        "black_screen_sample_seconds": ("number", 6),
        "black_screen_min_black_seconds": ("number", 3),
        "black_screen_ffmpeg_timeout": ("number", 20),
        "ffmpeg_path": ("string", "/usr/local/bin/ffmpeg"),
    }
    for fid, (ftype, default) in expected.items():
        assert fid in fields, f"missing setting {fid}"
        assert fields[fid]["type"] == ftype, f"{fid} type"
        assert fields[fid]["default"] == default, f"{fid} default"
        assert fields[fid].get("label"), f"{fid} needs a label"
        assert fields[fid].get("help_text"), f"{fid} needs help_text"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_black_screen.py -k settings -q`
Expected: FAIL — `missing setting black_screen_detection`.

- [ ] **Step 3: Add the section + 4 settings**

In `iptv_checker/iptv_checker/plugin.json`, find the `_section_dead` block:

```json
    {
      "id": "_section_dead",
      "label": "✏️ Dead Channel Handling",
```

Insert this block **immediately before** it (note the trailing comma after the closing `}` of the new block, before the `{` of `_section_dead`):

```json
    {
      "id": "_section_black_screen",
      "label": "⬛ Black-Screen Detection",
      "type": "info",
      "description": "Optionally decode a few seconds of each Alive stream with ffmpeg and mark it Dead if it is a pure black screen. Costs extra CPU/time per Alive stream."
    },
    {
      "id": "black_screen_detection",
      "label": "⬛ Detect Black-Screen Streams",
      "type": "boolean",
      "default": false,
      "help_text": "When ON, every stream that passes ffprobe is decoded for a few seconds with ffmpeg's blackdetect filter; pure-black streams are marked Dead (error_type 'Black Screen') so rename/move/delete actions handle them. Adds ~5-10s per Alive stream. Fail-open: if ffmpeg is missing or errors, the stream stays Alive. Very-dark-but-not-black 'no signal' slates are NOT detected."
    },
    {
      "id": "black_screen_sample_seconds",
      "label": "⬛ Black-Screen Sample (seconds)",
      "type": "number",
      "default": 6,
      "help_text": "How many seconds of video to decode when testing for a black screen. Longer = more reliable but slower. Default: 6"
    },
    {
      "id": "black_screen_min_black_seconds",
      "label": "⬛ Continuous Black Required (seconds)",
      "type": "number",
      "default": 3,
      "help_text": "Minimum continuous run of black video (within the sample) required to flag a stream as black. Should be a few seconds less than the sample to allow for connection/keyframe latency. Default: 3"
    },
    {
      "id": "black_screen_ffmpeg_timeout",
      "label": "⬛ Black-Screen ffmpeg Timeout (seconds)",
      "type": "number",
      "default": 20,
      "help_text": "Hard wall-clock cap on the ffmpeg black-screen decode (connection + sampling). If exceeded, the stream is left Alive. Default: 20"
    },
```

- [ ] **Step 4: Add `ffmpeg_path` to the Advanced section**

Find the `ffprobe_path` field (last field in the `fields` array):

```json
    {
      "id": "ffprobe_path",
      "label": "📍 FFprobe Path",
      "type": "string",
      "default": "/usr/local/bin/ffprobe",
      "placeholder": "/usr/local/bin/ffprobe",
      "help_text": "Full path to the ffprobe executable. Default: /usr/local/bin/ffprobe (Dispatcharr's default location)"
    }
```

Add a comma after its closing `}` and insert this field directly after it:

```json
    {
      "id": "ffmpeg_path",
      "label": "📍 FFmpeg Path",
      "type": "string",
      "default": "/usr/local/bin/ffmpeg",
      "placeholder": "/usr/local/bin/ffmpeg",
      "help_text": "Full path to the ffmpeg executable, used for black-screen detection. Default: /usr/local/bin/ffmpeg (Dispatcharr's default location)"
    }
```

- [ ] **Step 5: Verify the JSON is valid and the test passes**

Run:
```bash
python -c "import io,json; json.loads(io.open('iptv_checker/plugin.json',encoding='utf-8').read()); print('json ok')"
python -m pytest tests/test_black_screen.py -k settings -q
```
Expected: `json ok`, then test PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_black_screen.py iptv_checker/iptv_checker/plugin.json
git commit -m "feat: add black-screen detection settings to plugin.json"
```

---

### Task 5: Version bump, docs, and release prep

**Files:**
- Modify (auto): `iptv_checker/plugin.json`, `iptv_checker/plugin.py`, `CLAUDE.md` (version line) via `bump_version.py`
- Modify: `CLAUDE.md` (architecture bullet)

- [ ] **Step 1: Add an architecture bullet to CLAUDE.md**

In `iptv_checker/CLAUDE.md`, under `## Key Architecture`, add a bullet (place it after the `**Stream status**` bullet):

```markdown
- **Black-screen detection** (opt-in): when `black_screen_detection=true`, every stream that passes ffprobe gets a second pass — `ffmpeg ... -vf blackdetect=d=N:pic_th=0.98` over `black_screen_sample_seconds` of decode — and is reclassified `status='Dead'`, `error_type='Black Screen'` if a continuous black run ≥ `black_screen_min_black_seconds` is found. `_check_black_screen` (subprocess wrapper) + `_parse_blackdetect_output` (pure parser) live just above `check_stream`. The Dead result reuses `default_return`'s all-null metadata so `_update_dispatcharr_metadata`'s `all_none` branch CLEARS stats (a Dead result carrying real metadata would instead WRITE stats onto the dead channel). Fail-open: ffmpeg missing/timeout/unparseable → verdict `None` → stream stays Alive. Uses `-rw_timeout` (before `-i`) and `-loglevel info` (blackdetect logs at info level; `-loglevel error` would suppress its output). Bounded by `black_screen_ffmpeg_timeout`. Settings/`ffmpeg_path` in plugin.json.
```

- [ ] **Step 2: Run the version bump**

Run: `python bump_version.py`
Expected: prints the new calver version; updates `iptv_checker/plugin.json`, `iptv_checker/plugin.py`, and the `## Current Version` line in `CLAUDE.md` in sync.

- [ ] **Step 3: Final validation**

Run:
```bash
python -m pytest tests -q
python -m ruff check .
python -m py_compile iptv_checker/plugin.py
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add iptv_checker/plugin.json iptv_checker/plugin.py CLAUDE.md
git commit -m "chore: bump version + document black-screen detection"
```

- [ ] **Step 5: Deploy + verify in container (manual, with the user)**

The plugin runs inside Dispatcharr; black detection needs a real `ffmpeg`. With the user:
1. Confirm ffmpeg exists in the container: `docker exec <dispatcharr> /usr/local/bin/ffmpeg -version` (or `which ffmpeg`). If the path differs, set `ffmpeg_path` in the UI.
2. Deploy via the `deploy` skill (copies **both** `plugin.py` and `plugin.json` from the inner `iptv_checker/` folder; hot-reload fires on `plugin.json` mtime).
3. Enable **Detect Black-Screen Streams** in the plugin settings.
4. Run a check against a known-black channel (the `beIN SP RTS 1` example) plus a known-good channel; confirm the black one reports Dead / `Black Screen` and the good one stays Alive. Check the CSV `error_type` column.

---

## Self-Review

**Spec coverage:**
- Detection mechanism / command details (rw_timeout before -i, loglevel info, -nostats, blackdetect d/pic_th, -f null -, wall-clock timeout) → Task 2 (impl + `test_check_black_command_shape`). ✓
- Parser + return contract → Tasks 1 & 2. ✓
- Fail-open (None → Alive) → Task 2 (missing/timeout/non-zero) + Task 3 (`test_ffmpeg_error_fails_open_to_alive`). ✓
- Integration point + stop-event guard + null metadata (issue #4) → Task 3. ✓
- Settings schema (id/label/type number|boolean|string/default/help_text + section) → Task 4. ✓
- Downstream (status='Dead' flows through) → covered by null-metadata Dead result; no action-code change needed. ✓
- Default tuning 6s/3s → Task 2 defaults + Task 4 defaults + command-shape test asserts `d=3`. ✓
- Docs/version → Task 5. ✓
- Manual container ffmpeg check → Task 5 Step 5. ✓

**Placeholder scan:** No TBD/TODO; every code/step shows real code and exact commands. ✓

**Type consistency:** `_parse_blackdetect_output` returns `list[tuple]` (Task 1) and is consumed via truthiness in `_check_black_screen` (Task 2). `_check_black_screen` returns `True|False|None` and Task 3 checks `is True`. Settings ids match across Tasks 2/3/4. ✓
