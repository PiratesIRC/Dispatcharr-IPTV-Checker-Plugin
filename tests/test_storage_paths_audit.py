"""No /data literal in the plugin code outside the path table that bind rebinds.

Every literal that starts with /data must be a row of storage.SPEC.table (bound at import) or one of
the two folders that stay where they are: DATA_DIR (unused, kept for the record) and EXPORTS_DIR,
the CSV folder other plugins share. The vendored files keep their own pins and are not scanned.
"""
import ast
import pathlib

import iptv_checker.storage as storage

PKG = pathlib.Path(storage.__file__).parent
VENDORED = {"notify_client.py", "report_chrome.py", "usage_client.py", "plugin_storage.py"}
STAYS = {"DATA_DIR", "EXPORTS_DIR"}


def _class_constants(tree, class_name):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant) \
                        and isinstance(item.value.value, str):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            yield target.id, item.value.value


def test_every_data_literal_in_plugin_config_is_in_the_table_or_stays():
    tree = ast.parse((PKG / "plugin.py").read_text(encoding="utf-8"))
    table_names = {name for name, _kind, _rel in storage.SPEC.table}
    literals = {name: value for name, value in _class_constants(tree, "PluginConfig")
                if value.startswith("/data")}
    assert literals, "no /data literal found in PluginConfig; the audit would pass vacuously"
    stray = sorted(name for name in literals if name not in table_names | STAYS)
    assert stray == [], "state path not in storage.SPEC.table: %s" % stray


def test_no_data_literal_is_used_outside_plugin_config():
    for path in sorted(PKG.glob("*.py")):
        if path.name in VENDORED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inside = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "PluginConfig":
                inside = set(range(node.lineno, node.end_lineno + 1))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/data"):
                if path.name == "plugin.py" and node.lineno in inside:
                    continue
                raise AssertionError("%s:%d holds the literal %r outside PluginConfig" % (path.name, node.lineno, node.value))


def test_the_table_names_all_exist_on_plugin_config():
    tree = ast.parse((PKG / "plugin.py").read_text(encoding="utf-8"))
    declared = {name for name, _value in _class_constants(tree, "PluginConfig")}
    missing = sorted(name for name, _kind, _rel in storage.SPEC.table if name not in declared)
    assert missing == [], missing
