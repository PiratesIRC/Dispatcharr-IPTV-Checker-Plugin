# IPTV Checker v1.26.1721834 — Release Notes

Cosmetic / UX release: clearer terminology and a reorganized settings screen. No
functional behavior changes.

## "Black Screen" → "Blank Screen" in the UI

Every user-facing reference to "Black Screen" / "Black-Screen" in the settings and
actions now reads **"Blank Screen" / "Blank-Screen"** (the detection toggle, the
sample/required/timeout fields, the handling section, the rename/move actions and
their buttons, and the scheduler toggles).

**Kept stable on purpose** (so nothing breaks): the internal setting/action IDs, the
detection logic, the `[Blank]` rename tag, and the destination group default
**"Black Screens"**. The classification value is still `error_type = 'Black Screen'`
in code — so the **results table and CSV still show "Black Screen"** (it's the stored
error code). That divergence is intentional, not a missed rename.

## Settings screen reorganized

The settings tab was getting unwieldy. Fields are now grouped by lifecycle:

```
Group Selection → Check Behavior → Blank-Screen Detection
→ 🏷️ Post-Check Actions  (Dead, Blank, Low-Framerate, Format, Restore — all together)
→ Webhook → Scheduling & Automation  (+ an "Auto-run after scheduled checks" sub-section)
→ Advanced  (ffprobe flags / analysis duration / streamlink hosts moved here)
```

The two scattered ⬛ sections are now distinct and sensibly placed (detection up in
the scan area, handling in Post-Check Actions); the previously-orphaned Restore
section sits at the end of the Post-Check block; and the ~11 scheduler toggles are
grouped under their own sub-header. Reordering is purely cosmetic — Dispatcharr keys
settings by id, so saved values are unaffected.

Also fixed a stale help-text reference ("Scheduler Timezone above" → "Dispatcharr's
timezone"; that field was removed in v1.26.1721651).

## Tests

- New `tests/test_settings_schema.py`: freezes the field/action id set (a reorder can
  never silently drop/rename an id) and asserts no "Black Screen"/"Black-Screen"
  vocabulary leaks back into a user-facing label/description/button. Full suite:
  **141 passing**, ruff clean.

## Upgrade notes

No migration, no saved-setting changes. Deploy both `plugin.py` and `plugin.json`
from `iptv_checker/` and restart the container.
