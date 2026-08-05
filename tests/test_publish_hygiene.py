"""Guards on what a push to this PUBLIC repository would publish.

This repository is public and main is pushed to it directly, with no orphan
branch in between (measured 2026-08-05: public, 50 stars, 4 forks). There is no
staging step, and with forks in existence a history rewrite cannot take back a
bad push. These assertions are therefore cheap insurance rather than ceremony.

WHY THESE TESTS EXIST RATHER THAN LEAVING IT TO THE SCANNER. The audit rules in
.publish-audit.json allow the literal strings "CLAUDE.md" and "superpowers",
because both appear legitimately as textual references: in the .gitignore rules
themselves, in DEVELOPMENT.md and README.md prose, and in one test docstring
citing the spec a feature came from. That allowance necessarily also silences
the scanner's file-NAME check for those strings. So the real risk, a notes file
or a design document actually becoming TRACKED, is covered here instead, where
it can be asserted exactly rather than pattern-matched.

If an allow entry is ever removed, these tests stay correct and simply become
redundant. If one of these tests is removed while the allow entries remain,
nothing is checking the file names at all. Keep them in step.
"""
import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES = PROJECT_ROOT / ".publish-audit.json"
HOOK = PROJECT_ROOT / ".githooks" / "pre-push"


def _tracked_files():
    """git ls-files is the authority on what a push publishes, not .gitignore.

    A file committed before an ignore rule was added is still tracked and is
    still published, while .gitignore says nothing about it.
    """
    out = subprocess.run(
        ["git", "ls-files"], cwd=PROJECT_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    return [line.strip() for line in out.split("\n") if line.strip()]


def test_no_notes_file_is_tracked():
    # Canonical case here, lowered at run time: these names are what the
    # test refuses to let become tracked, so it has to name them.
    names = {n.lower() for n in
             ("CLAUDE.md", "CLAUDE-HISTORY.md", "GEMINI.md", "AGENTS.md")}
    bad = [f for f in _tracked_files() if Path(f).name.lower() in names]
    assert bad == [], f"internal notes file(s) tracked and would be published: {bad}"


def test_no_design_documents_are_tracked():
    bad = [f for f in _tracked_files()
           if "superpowers" in f.lower() or f.lower().startswith("docs/specs/")]
    assert bad == [], f"design document(s) tracked and would be published: {bad}"


def test_no_agent_tooling_directory_is_tracked():
    bad = [f for f in _tracked_files()
           if f.startswith(".claude/") or f.startswith(".wolf/")]
    assert bad == [], f"agent tooling tracked and would be published: {bad}"


def test_audit_rules_file_exists():
    """Without this file the audit runs with built-in patterns only, and the
    repository-specific deny list is the half that catches real leaks here."""
    assert RULES.is_file(), ".publish-audit.json is missing"


def test_every_audit_rule_carries_a_justification():
    """An unexplained exception is how a real finding gets silenced. The audit
    script itself refuses to load a rule without one; this fails faster and in
    CI, where nobody is watching a push."""
    data = json.loads(RULES.read_text(encoding="utf-8"))
    for entry in data.get("deny", []):
        assert entry.get("why"), f"deny entry {entry.get('pattern')!r} does not say what it protects"
    for entry in data.get("allow", []):
        assert entry.get("reason"), f"allow entry {entry.get('pattern')!r} has no reason"


def test_deny_patterns_do_not_spell_their_own_secrets():
    """The rules file is the one file guaranteed to mention every string it
    looks for, so each pattern must be written with a character class. A
    pattern that matches ITSELF means the rules file became the leak.

    Checked by running each deny pattern against the rules file's own text and
    requiring that the only matches, if any, come from the pattern definition
    line rather than from prose.
    """
    import re
    raw = RULES.read_text(encoding="utf-8")
    data = json.loads(raw)
    self_matching = []
    for entry in data.get("deny", []):
        pattern = entry["pattern"]
        # Strip the JSON-escaped pattern strings themselves before scanning, so
        # a pattern is not credited with matching its own definition.
        without_definitions = raw.replace(json.dumps(pattern), "")
        if re.search(pattern, without_definitions, re.IGNORECASE):
            self_matching.append(pattern)
    assert self_matching == [], (
        "deny pattern(s) match text elsewhere in the rules file, meaning the "
        f"file spells out what it protects: {self_matching}"
    )


def test_private_remotes_is_empty():
    """origin here is a PUBLIC publication target, so it must always be
    audited. Adding it to private_remotes would switch the pre-push gate off
    entirely while leaving every other file looking correct."""
    data = json.loads(RULES.read_text(encoding="utf-8"))
    assert data.get("private_remotes", []) == [], (
        "private_remotes must stay empty: this repository has no private remote, "
        "and listing origin would disable the pre-push content audit"
    )


def test_pre_push_hook_exists_and_is_a_gate():
    """A hook that warns but returns zero is not a gate. Assert it can abort."""
    assert HOOK.is_file(), ".githooks/pre-push is missing"
    body = HOOK.read_text(encoding="utf-8")
    assert "exit 1" in body, "pre-push hook never exits non-zero, so it cannot block a push"
    assert "--ref" in body, "pre-push hook does not run the audit against the ref being pushed"
