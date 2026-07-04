# Development Workflow

How to develop, test, and release the IPTV Checker plugin. This is the
process doc; for architecture and code conventions see `CLAUDE.md`, and for
scheduler internals see `SCHEDULING_LOGIC.md`.

## Repository layout

```
iptv_checker/                  # ← the plugin (this is what gets deployed/zipped)
├── plugin.py                  #   all plugin logic (~3,900 lines, single file)
├── plugin.json                #   fields, actions, metadata — single source of truth
└── __init__.py                #   exports Plugin
tests/                         # pytest suite (runs OUTSIDE the container)
├── conftest.py                #   stubs Dispatcharr/Django modules in sys.modules
├── test_version_sync.py       #   plugin.json == plugin.py == CLAUDE.md
├── test_rate_limit_guard.py   #   429 classification + cooldown logic
├── test_bitrate_calc.py       #   packet-based bitrate + min-sample gate + audio-only->Skipped
├── test_scheduler_window.py   #   window math + pending-resume scope guards
├── test_scheduler_lock.py     #   O_EXCL election + boot-token reclaim + recycled-PID guard (POSIX concurrency)
├── test_scheduler_double_fire.py # process-shared fire-claim + status false-negative (#25 display half)
├── test_scheduler_host_eligibility.py # daphne/ASGI excluded from election (#25 root cause)
├── test_webhook.py            #   Discord/generic payload shaping + headers
├── test_black_screen.py       #   blackdetect parse + ffmpeg wrapper + check_stream
├── test_restore_and_black.py  #   restore + blank-flag predicates/planners/actions
├── test_timezone.py           #   Dispatcharr-sourced timezone resolver + UTC fallback
├── test_group_filter.py       #   include/exclude group filtering (load_groups_action)
├── test_settings_schema.py    #   plugin.json id-set freeze + no-"Black Screen"-in-labels
├── test_csv_and_status_fixes.py #  CSV no-dup header, PAL-safe low-fps, ffprobe-flags default, View-Last-Results date
└── test_plugin_helpers.py     #   cron parse/match, streamlink hosts, JSON I/O
scripts/check_version_sync.py  # standalone version-drift check (CI/pre-commit usable)
bump_version.py                # version bump across all three files
pytest.ini                     # pytest config
requirements-dev.txt           # dev/CI dependencies (pytest, pytz, ruff)
pyproject.toml                 # ruff config (tooling only, not a package)
.github/workflows/ci.yml       # CI: compile, lint, version-sync, tests; release zip on tag
.claude/hooks/                 # Claude Code guard hooks (see "Hooks" below)
.claude/skills/                # /release, /deploy, /triage-scheduler
```

There is exactly **one** copy of the plugin (`iptv_checker/`). A historical
root-level copy was removed; do not recreate it (it caused a deployed
regression when the two copies drifted — see CLAUDE.md "CSV-per-window hoist").

## Prerequisites

- Python 3.10+ on the dev machine
- `pip install -r requirements-dev.txt`
- Docker with the `dispatcharr` container running (for deploy/integration)
- `gh` CLI authenticated (for releases and issues)

## Day-to-day loop

1. **Edit** `iptv_checker/plugin.py` / `plugin.json`.
   - A Claude Code hook auto-runs `py_compile` after every edit to plugin.py.
2. **Test**: `python -m pytest tests -q` — fast (<1s), no container needed.
   The conftest injects stubs for `apps.channels.models`, `django.db`, and
   `core.utils`, so the plugin imports anywhere.
3. **Lint**: `python -m ruff check .` — errors-only ruleset (E9 + pyflakes);
   it never argues about style.
4. **Deploy to the container** (manual test): `/deploy` skill in Claude Code,
   or by hand:
   ```bash
   docker cp iptv_checker/plugin.py dispatcharr:/data/plugins/iptv_checker/plugin.py
   docker cp iptv_checker/plugin.json dispatcharr:/data/plugins/iptv_checker/plugin.json
   docker restart dispatcharr
   docker logs dispatcharr --since 2m 2>&1 | grep -i "IPTV Checker"
   ```
   Verify: no traceback, one scheduler-election winner, version matches.

## Testing

- **Run everything**: `python -m pytest tests -q`
- **One module**: `python -m pytest tests/test_webhook.py -q`
- Tests are pure unit tests against ffprobe/HTTP/file-IO seams
  (`subprocess.run`, `urllib.request.urlopen`, tmp-path JSON files). No
  network, no Docker, no Django.
- **Adding tests**: build a Plugin via the `plugin` fixture (it uses
  `Plugin.__new__` — no `__init__`, so no scheduler threads or `/data` I/O)
  and monkeypatch the seam your code path touches. Use `fake_clock` for
  anything time-based.
- **Convention**: every production incident gets a regression test. The
  existing modules document which shipped bug each test pins down.
- Integration checks that need the real ORM still happen in the container —
  the test suite complements, not replaces, a `/deploy` smoke test.

## Linting

Config lives in `pyproject.toml` (`[tool.ruff]`). Deliberately minimal:
`E9` (syntax) + `F` (pyflakes: undefined names, unused imports, f-string
bugs). Pre-existing cosmetic findings in `plugin.py` are suppressed via
per-file-ignores per the "do not reformat existing code" rule — don't add new
ones.

## Versioning & releases

Version scheme: calver `1.26.{DDD}{HHMM}` (UTC day-of-year + UTC hour-minute),
shared with the Lineuparr / Channel-Mapparr / EPG-Janitor cohort.

**Never hand-edit versions.** Run:

```bash
python bump_version.py            # auto, from current UTC time
python bump_version.py 1.26.1621430  # explicit
```

It updates `iptv_checker/plugin.json`, `iptv_checker/plugin.py`, and the
"Current Version" line in `CLAUDE.md`, then verifies all agree (exit ≠ 0 on
mismatch).

**Release** (or just run the `/release` skill, which does all of this):

```bash
python -m pytest tests -q && python -m ruff check .
python bump_version.py
git add -A && git commit -m "v<version> — <summary>"
git tag <version> && git push origin main --tags
```

Pushing the tag triggers the CI release job: it re-validates, checks the tag
matches `plugin.json`, builds `iptv_checker-v<version>.zip` (no
`__pycache__`) with Linux `zip` (forward-slash separators), validates it with
`scripts/validate_zip.py` (bug-087 guard — a backslash-separator zip, as Windows
`Compress-Archive` / .NET `ZipFile.CreateFromDirectory` produce, fails install on
Dispatcharr's Linux host), and creates/uploads the GitHub release.

Upstream marketplace (`Dispatcharr/Plugins`) submission rules are in
`README.md` → "To the upstream marketplace".

## CI

`.github/workflows/ci.yml`, on every push/PR to `main` and on tags:

| Step | Catches |
|------|---------|
| `py_compile` | syntax errors |
| `ruff check` | undefined names, unused imports, f-string bugs |
| `test_version_sync.py` | plugin.json / plugin.py / CLAUDE.md version drift |
| full pytest | regressions in rate-limit, bitrate, window, webhook, black-screen logic |
| release job (tags only) | tag/version mismatch; builds + attaches the zip |

## Claude Code automation

**Hooks** (`.claude/settings.json` + `.claude/hooks/`):

- `post-edit-compile.py` — PostToolUse: `py_compile` after any edit to
  `iptv_checker/plugin.py` or `bump_version.py`; feeds errors straight back.
- `pre-commit-version-sync.py` — PreToolUse: blocks `git commit` when
  plugin.json / plugin.py / CLAUDE.md versions disagree.

**Skills** (`.claude/skills/`):

- `/release` — the full release checklist above, gated on green tests.
- `/deploy` — copy into the container, restart, verify logs.
- `/triage-scheduler` — step-by-step diagnosis of scheduler election, stale
  progress, pending-resume, and reload-flag state in the container.

OpenWolf hooks (`.wolf/`) handle session memory/context and are independent
of the above.

## Container reference

| What | Where |
|------|-------|
| Plugin install dir | `/data/plugins/iptv_checker/` |
| Data files | `/data/iptv_checker_*.json` |
| Channel state (restore) | `/data/iptv_checker_channel_state.json` (orig group per channel; captured on Move, consumed by Restore) |
| CSV exports | `/data/exports/` |
| Scheduler lock | `/data/iptv_checker_scheduler.pid` (2 lines: pid + boot token) |
| ffprobe | `/usr/local/bin/ffprobe` (configurable) |
| ffmpeg | `/usr/local/bin/ffmpeg` (configurable; only used by opt-in black-screen detection) |

## Gotchas (the expensive ones)

- `-show_frames` must NOT be added to default `ffprobe_flags` — combined with
  `-show_packets`, ffprobe emits `packets_and_frames` and the bitrate
  fallback silently dies (tested in `test_bitrate_calc.py`). The default lives
  in one place: `PluginConfig.DEFAULT_FFPROBE_FLAGS` (used by both `check_stream`
  and the CSV preamble — keep them sharing it, v1.26.1741204+).
- CSV header columns come from `_compute_csv_fieldnames`: any `base_fieldnames`
  entry that also starts with `ffprobe_` (i.e. `ffprobe_monitoring_seconds`) is
  excluded from the `ffprobe_`-prefix auto-collector, else it appears twice
  (`bug-csv-dup-monitoring-col`, v1.26.1741204+).
- Low-framerate eligibility goes through `_is_low_framerate(fps)` /
  `PluginConfig.LOW_FRAMERATE_THRESHOLD` (=24, PAL/film-safe), NOT a hardcoded
  `< 30` — route every new low-fps site through the helper (v1.26.1741204+).
- Dead-channel actions act only on `status == 'Dead'`; `Skipped` (Streamlink
  hosts, HTTP 429, and audio-only/`No Video Stream` — v1.26.1741204+) must stay
  untouched. **Black/blank exception (v1.26.1721554+):**
  the Dead *rename/move* filters use `_is_dead_nonblack` (excludes
  `error_type == 'Black Screen'`) — black channels are renamed/moved by the
  dedicated black actions. Dead *delete* still includes them.
- Restore (`restore_channels_action`) acts on `status == 'Alive'` channels that were
  previously marked (have stored state OR a status tag). It strips tags via
  `_derive_strippable_tags` and moves back to the captured original group. The
  state file is read-modify-written last-writer-wins across processes — a lost
  capture only forfeits the exact-group restore (name still restored), same
  accepted model as `pending_resume.json`.
- Black-screen detection (opt-in) must **fail open**: a missing/erroring/timing-out
  ffmpeg returns `None` and leaves the stream Alive — never Dead. The Dead result
  it produces uses all-`None` `dispatcharr_metadata` so `_update_dispatcharr_metadata`
  hits the `all_none` clear branch instead of writing `0x0` stats. Use `ffmpeg`'s
  `-loglevel info` (blackdetect logs at info level) and `-rw_timeout` *before* `-i`.
- `/data` must be a local volume — the scheduler election relies on
  `os.open(O_CREAT|O_EXCL)` + `os.replace` atomicity (v1.26.1751208+; was POSIX
  rename). The winner is whoever atomically *creates* the lock; never reintroduce
  a "rename then read-back my own PID" confirmation — it's a TOCTOU that let two
  processes win on a restart and double-fire the cron (`bug-sched-double-election`,
  2026-06-24). Stale-lock reclaim is serialized under the `<lock>.reclaim` guard
  in `_reclaim_scheduler_lock`. The concurrency regression test
  (`test_scheduler_lock.py`) is **POSIX-only** — Windows can't delete/replace a
  file open for reading, so verify that path in the Linux container, not on dev.
- Scheduler timezone comes from Dispatcharr (`core.models.CoreSettings.get_system_time_zone()`)
  via `_dispatcharr_timezone()`, NOT a plugin setting (removed v1.26.1721651). The lazy
  `from core.models import CoreSettings` lives inside the resolver so the module still imports
  in tests (conftest doesn't stub `core.models` → ImportError → `_coerce_timezone` falls back to
  `PluginConfig.DEFAULT_TIMEZONE` = `UTC`). Don't reintroduce a `scheduler_timezone` field.
- Windows dev machines: always pass `encoding="utf-8"` when reading/writing
  the plugin files from scripts; the cp1252 default truncates on the emoji in
  plugin.py/plugin.json (this bit `bump_version.py` once).
