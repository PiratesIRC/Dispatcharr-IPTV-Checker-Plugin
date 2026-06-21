"""Timezone sourced from Dispatcharr: _coerce_timezone + _dispatcharr_timezone."""
import sys
import types


def test_coerce_valid(pmod):
    assert pmod.Plugin._coerce_timezone("America/New_York") == "America/New_York"
    assert pmod.Plugin._coerce_timezone("  Europe/London  ") == "Europe/London"


def test_coerce_invalid_blank_none(pmod):
    P = pmod.Plugin
    default = pmod.PluginConfig.DEFAULT_TIMEZONE
    assert P._coerce_timezone("Not/AZone") == default
    assert P._coerce_timezone("") == default
    assert P._coerce_timezone("   ") == default
    assert P._coerce_timezone(None) == default
    assert P._coerce_timezone(12345) == default


def test_default_timezone_is_utc(pmod):
    assert pmod.PluginConfig.DEFAULT_TIMEZONE == "UTC"


def test_dispatcharr_timezone_fallback_when_core_absent(plugin, pmod):
    # No core.models stub installed -> lazy import raises -> fallback default.
    sys.modules.pop("core.models", None)
    assert plugin._dispatcharr_timezone() == pmod.PluginConfig.DEFAULT_TIMEZONE


def test_dispatcharr_timezone_reads_coresettings(plugin, pmod, monkeypatch):
    mod = types.ModuleType("core.models")

    class CoreSettings:
        @staticmethod
        def get_system_time_zone():
            return "Asia/Tokyo"

    mod.CoreSettings = CoreSettings
    monkeypatch.setitem(sys.modules, "core.models", mod)
    assert plugin._dispatcharr_timezone() == "Asia/Tokyo"


def test_dispatcharr_timezone_coerces_bad_value(plugin, pmod, monkeypatch):
    mod = types.ModuleType("core.models")

    class CoreSettings:
        @staticmethod
        def get_system_time_zone():
            return "garbage/zone"

    mod.CoreSettings = CoreSettings
    monkeypatch.setitem(sys.modules, "core.models", mod)
    assert plugin._dispatcharr_timezone() == pmod.PluginConfig.DEFAULT_TIMEZONE
