"""Every file this plugin creates with os.open is owner-only (mode 0o600).

The Dispatcharr Plugin Hub runs CodeQL on the released archive and refuses a pull request on a high
finding (py/overly-permissive-file). Mode 0o644 and 0o660 both failed on Dispatcharr/Plugins#628;
0o600 passed. Every Dispatcharr worker runs as one user, so owner-only is enough.
"""
import ast
import os
import pathlib
import stat
import sys

import pytest

import iptv_checker.storage as storage

PKG = pathlib.Path(storage.__file__).parent
# Vendored files keep their own pins; the CodeQL rule applies to the whole released package.
SCANNED = sorted(PKG.glob("*.py"))


def _os_open_modes(path):
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    modes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) in ("os.open", "os_open"):
            if len(node.args) >= 3:
                modes.append((node.lineno, ast.unparse(node.args[2])))
            for kw in node.keywords:
                if kw.arg == "mode":
                    modes.append((node.lineno, ast.unparse(kw.value)))
    return modes


def test_the_scan_finds_os_open_calls_so_it_cannot_pass_vacuously():
    total = sum(len(_os_open_modes(p)) for p in SCANNED)
    assert total >= 5, "too few os.open calls found; the scan would pass vacuously"


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.name)
def test_no_os_open_grants_group_or_other_access(path):
    for lineno, expr in _os_open_modes(path):
        try:
            mode = ast.literal_eval(expr)
        except ValueError:
            pytest.fail("%s:%d os.open mode %s is not a literal; the scan cannot check it" % (path.name, lineno, expr))
        assert isinstance(mode, int)
        assert mode & 0o077 == 0, "%s:%d os.open mode %s grants access beyond the owner" % (path.name, lineno, expr)


def test_no_chmod_makes_anything_world_writable():
    for path in SCANNED:
        source = path.read_text(encoding="utf-8")
        assert "0o777" not in source and "0o1777" not in source, path.name


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_created_lock_file_has_no_group_or_other_bits(tmp_path):
    lock_path = str(tmp_path / "x.lock")
    old_umask = os.umask(0)
    try:
        with storage._ps._lock(lock_path):
            pass
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(os.stat(lock_path).st_mode) & 0o077 == 0
