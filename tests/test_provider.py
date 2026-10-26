"""Provider-level tests: identity, classification flags, cheap health checks."""

from __future__ import annotations

import pytest

from instabox.provider import (InstaboxProvider, api_base_url, configured_auto_suspend, configured_template,
                               configured_file_sync)


def test_identity_and_classification():
    provider = InstaboxProvider()
    assert provider.name == "instabox"
    assert provider.display_name == "InstaBox"
    assert provider.is_remote is True
    assert provider.is_container is True
    # Disposable cloud VM: dangerous-command approvals are skipped, as for docker/modal.
    assert provider.skip_container_guards is True
    assert provider.supports_streams is False
    assert provider.default_cwd == "~"
    assert provider.cache_path_base == "~/.hermes"
    assert "INSTABOX_API_KEY" in provider.strip_env_keys
    assert "InstaBox" in provider.description


def test_is_available_follows_the_key(monkeypatch):
    from hermes_cli import config as hermes_config
    monkeypatch.delenv("INSTABOX_API_KEY", raising=False)
    monkeypatch.setattr(hermes_config, "get_env_value", lambda _k: None)
    monkeypatch.setattr(hermes_config, "load_env", lambda: {})
    assert InstaboxProvider().is_available() is False
    assert InstaboxProvider().probe()[0] == "needs_setup"
    assert InstaboxProvider().check_requirements({}) is False

    monkeypatch.setenv("INSTABOX_API_KEY", "ibk_test")
    assert InstaboxProvider().is_available() is True
    assert InstaboxProvider().check_requirements({}) is True
    assert "ibk_" in " ".join(InstaboxProvider().setup_instructions())


def test_defaults(monkeypatch):
    from hermes_cli import config as hermes_config
    monkeypatch.setattr(hermes_config, "load_config_readonly", lambda: {})
    monkeypatch.setattr(hermes_config, "get_env_value", lambda _k: None)
    assert api_base_url() == "https://api.instabox.sh"
    assert configured_template() == "agent-browser"
    assert configured_auto_suspend() == 1800
    assert configured_file_sync() is False


def test_config_overrides(monkeypatch):
    from hermes_cli import config as hermes_config
    monkeypatch.setattr(hermes_config, "load_config_readonly", lambda: {"terminal": {
        "instabox_api_url": "http://127.0.0.1:9/", "instabox_template": "universal",
        "instabox_auto_suspend": "600", "instabox_file_sync": "off"}})
    assert api_base_url() == "http://127.0.0.1:9"
    assert configured_template() == "universal"
    assert configured_auto_suspend() == 600
    assert configured_file_sync() is False


def test_bad_auto_suspend_falls_back(monkeypatch):
    from hermes_cli import config as hermes_config
    monkeypatch.setattr(hermes_config, "load_config_readonly", lambda: {"terminal": {"instabox_auto_suspend": "soon"}})
    assert configured_auto_suspend() == 1800


@pytest.mark.parametrize("task,persistent,expected_prefix", [
    ("default", True, "hermes-default"),
    ("My Task/1", True, "hermes-my-task-1"),
    ("", True, "hermes-default"),
    ("x" * 200, True, "hermes-"),
])
def test_machine_names(task, persistent, expected_prefix):
    from instabox.environment import machine_name_for
    name = machine_name_for(task, persistent=persistent)
    assert name.startswith(expected_prefix), name
    assert len(name) <= 63
    assert name[0].isalnum()
    assert all(ch.islower() or ch.isdigit() or ch == "-" for ch in name)


def test_ephemeral_names_are_unique():
    from instabox.environment import machine_name_for
    a = machine_name_for("default", persistent=False)
    b = machine_name_for("default", persistent=False)
    assert a != b
    assert a.startswith("hermes-default-")
