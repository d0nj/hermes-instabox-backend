"""The machine (and friends) can come from the plugin's own settings form."""

from __future__ import annotations

import pytest

from instabox import provider as prov


def fake_config(monkeypatch, *, terminal=None, settings=None):
    cfg = {"plugins": {"entries": {"instabox": {"settings": settings or {}}}}}
    if terminal is not None:
        cfg["terminal"] = terminal
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)
    monkeypatch.setattr(prov, "_env_value", lambda _name: "")
    for name in ("TERMINAL_INSTABOX_MACHINE", "TERMINAL_INSTABOX_SNAPSHOT", "TERMINAL_INSTABOX_TEMPLATE"):
        monkeypatch.delenv(name, raising=False)


def test_machine_from_plugin_settings(monkeypatch):
    fake_config(monkeypatch, settings={"machine": "base-dev"})
    assert prov.configured_machine() == "base-dev"


def test_terminal_key_wins_over_plugin_settings(monkeypatch):
    fake_config(monkeypatch, terminal={"instabox_machine": "from-terminal"},
                settings={"machine": "from-plugin"})
    assert prov.configured_machine() == "from-terminal"


def test_snapshot_and_template_from_plugin_settings(monkeypatch):
    fake_config(monkeypatch, settings={"snapshot": "baked", "template": "universal",
                                       "auto_suspend": 600, "file_sync": False})
    assert prov.configured_snapshot() == "baked"
    assert prov.configured_template() == "universal"
    assert prov.configured_auto_suspend() == 600
    assert prov.configured_file_sync() is False


def test_defaults_when_nothing_is_set(monkeypatch):
    fake_config(monkeypatch)
    assert prov.configured_machine() is None
    assert prov.configured_snapshot() is None
    assert prov.configured_template() == "agent-browser"
    assert prov.configured_auto_suspend() == 1800
    assert prov.configured_file_sync() is False
