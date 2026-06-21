# IPTV Checker v1.26.1721733 — Release Notes

## Exclude channel groups from checking

A new **Group(s) to EXCLUDE** field (comma-separated, wildcards supported) lets you
skip groups that would otherwise be checked. It's the inverse of the existing
**Group(s) to Check** field, and it **composes** with it:

- Check `US-*` but skip pay-per-view → Check `US-*`, Exclude `US-PPV-*`
- Check everything except one group → leave Check blank, Exclude `Adult`

### Behavior

- Exclude is applied **after** the include filter.
- Same case-sensitive wildcard rules as the include field (`*`, `?`, `[...]`).
- If a group matches **both** fields, **exclude wins**.
- If the filters leave nothing to check, the load reports a clear error instead
  of silently falling back to all groups.
- The CSV audit header now reports a `Group(s) Excluded:` line, and the windowed
  scheduler's scope-drift guard accounts for the exclude filter.

Opt-in: leave the field blank for unchanged behavior.

## Other

- Removed a benign startup log line (`⏰ WINDOW: pending state exists but its
  window already closed — discarding dead pending state`). The stale pending file
  is still discarded — just silently.

## Tests

- New `_match_group_names` unit tests (exact / wildcard / multi / no-match /
  case-sensitivity) and four integration tests driving exclusion through
  `load_groups_action` (exclude-wins, exclude-from-all, all-excluded error,
  blank-exclude no-op). Fingerprint tests updated for the new scope key. Full
  suite: **134 passing**, ruff clean.

## Upgrade note

A windowed run that was mid-window across the upgrade discards its pending state
once (the stored scope fingerprint lacks the new exclude key) and starts fresh —
self-heals on the next window. No other migration.

Deploy both `plugin.py` and `plugin.json` from the `iptv_checker/` folder
(hot-reload fires on `plugin.json` mtime), then restart the container.
