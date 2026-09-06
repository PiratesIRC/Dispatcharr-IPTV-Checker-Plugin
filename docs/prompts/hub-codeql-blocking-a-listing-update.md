# Hand-off prompt: a CodeQL result is blocking a Dispatcharr Plugin Hub listing update

Written 2026-08-06 after clearing exactly this on IPTV Checker (Dispatcharr/Plugins pull request
216). Copy everything below the line into the other plugin's coding agent. It is written to be
self-contained, so it repeats context that agent will not have.

---

## The task

The Dispatcharr Plugin Hub pull request that updates this plugin's listing is failing its
`codeql-analyze` check, and the `Plugin PR Check` gate fails because it depends on that result.
Every other check may well be passing. Clear the CodeQL findings properly, cut a release, and point
the Hub listing at it.

## Read this before you conclude the failure is not yours

The Hub scans **the plugin's release archive, not the Hub repository's own tree**, whenever the
listing uses `source_type: "external"`. Measured in `.github/workflows/validate-plugin.yml` in
`Dispatcharr/Plugins`: the `codeql-analyze` job has a step named "Populate external plugin source
for analysis" that downloads the release ZIP named by `source_url` and extracts it into
`plugins/<slug>/` before the scan runs.

Two consequences that wasted a day on IPTV Checker:

1. **The reported path contains a directory that does not exist in the Hub repository.** A finding
   at `plugins/<slug>/<inner_package>/plugin.py:491` looks like someone else's file, because the
   Hub repository only has `plugins/<slug>/plugin.py`. That extra segment is the layout inside the
   release archive. It is your code.
2. **Switching a listing from full-source to external mode exposes the plugin to CodeQL for the
   first time.** So a pull request that changes no Python at all can legitimately start failing. Do
   not conclude from "this pull request touches no Python" that the failure is unrelated. That
   argument was made on pull request 216 and it was wrong.

Also: **the validation bot comment names the rule and the exact file and line.** Read it before
saying you cannot tell which code is involved. For external plugins the location is rendered as
plain text rather than a link, because the path is not in that repository, so it is easy to skim
past.

## How to fix it, in order

### 1. Identify the finding from the bot comment

Get the rule id, file and line from the most recent `Plugin Validation Results` comment on the pull
request:

```
gh pr view <PR_NUMBER> --repo Dispatcharr/Plugins --json comments \
  --jq '.comments[-1].body'
```

Map the reported path back to your repository by removing the `plugins/<slug>/` prefix.

### 2. Find every site the rule applies to, not only the reported ones

This is the part that matters most. CodeQL reports the sites it can see statically; the same defect
usually exists in nearby code that it cannot flag.

On IPTV Checker the rule was `py/overly-permissive-file` and it reported two calls of the form
`os.open(path, flags, 0o644)`. Searching the surrounding function found a **third** file created by
the same routine through the builtin `open(path, 'w')`, which takes the process umask rather than
an explicit mode. CodeQL cannot flag that one because there is no literal mode in the call.
Restricting only the two reported calls would have left the third writing a world-readable file
over the restricted one it had just replaced, so the change would have looked complete and fixed
nothing on that path.

Generalise the search rather than editing the two lines you were handed. For file permissions that
means: every `os.open` with an explicit mode, every `os.chmod`, and every builtin `open()` that
creates a file whose contents you would not publish.

### 3. Choose the mode from what the file actually holds

Do not pick a value to satisfy the scanner. Ask who reads the file. If it is only ever read and
written by the plugin's own processes inside the Dispatcharr container, `0o600` is right and costs
nothing. If something else genuinely needs it, say so in a comment and keep the wider mode, and be
ready to justify that to a reviewer.

### 4. Write the test before the change, and assert on the argument, not on stat()

Two traps here, both hit on IPTV Checker:

- **`stat()` assertions are meaningless on Windows.** The filesystem carries no POSIX permission
  bits and `st_mode` always reads `0o666` or `0o444`, so a mode test either passes vacuously or has
  to be skipped, which means a Windows development machine never runs it. Assert instead on the
  **mode argument passed to `os.open`**, by monkeypatching `os.open` in the module under test to
  record `(path, mode)` and delegate to the real one. That runs everywhere and tests exactly the
  thing the scanner objects to.
- **Requiring the file to be created through `os.open` is itself a useful assertion**, because it
  is what catches a site that quietly reverts to the builtin `open()`.

Shape that worked:

```python
def _record_open_modes(pmod, monkeypatch):
    calls = []
    real_open = pmod.os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        calls.append((str(path), mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(pmod.os, "open", spy)
    return calls
```

Then assert `mode & 0o077 == 0` for every recorded path, plus that the expected paths appear at all
(`assert created, "the code created no file, so this test proves nothing"`).

### 5. Prove the tests are not vacuous

Revert each site **one at a time** back to its original form, run the suite, and confirm it fails
each time. Then reword a comment only and confirm the suite still passes, as a control. A test that
passes both before and after the change is not a test.

One trap: if the worktree uses CRLF, a multi-line search-and-replace written with bare `\n` silently
matches nothing, and the mutant reports as surviving when it was never applied. That happened here.
Verify each mutation actually changed the file before trusting its result.

### 6. Release, then point the listing at the new release

Follow this plugin's own release runbook. In outline, and in this order:

1. Run the publish audit before anything leaves the machine:
   `python ../.claude/skills/pre-publish-audit/audit_publish.py --ref <tag> --rules .publish-audit.json`
2. Bump the version with `python scripts/bump_version.py`. Never hand-edit versions.
3. Commit, tag, push. Confirm continuous integration built and attached the release asset, and that
   the asset URL returns HTTP 200.
4. Update `version` in `plugins/<slug>/plugin.json` on the Hub pull request branch. `source_url`
   normally contains `{version}` and needs no edit.

### 7. Watch the checks, and read the bot comment again

`codeql-analyze` should go green, then `report`, then `Plugin PR Check`. The bot posts a fresh
validation comment ending in "All validation checks passed".

## A mistake to avoid when editing the Hub manifest through the API

Do not pipe a file through Windows Python using an MSYS path such as `/tmp/hub.json`. Windows Python
cannot resolve it, the edit step fails, and if the upload command runs anyway it sends the file back
**unchanged**, producing an empty commit whose message claims a bump that did not happen. Use MSYS
tools throughout, or a Windows path, and verify the pushed file by reading it back before moving on.

## Tone for any comment you post on the pull request

If you argued earlier that the failure was not caused by the change, say plainly that you were wrong
and what the actual cause was. State what you measured and where you measured it. Keep it short.
