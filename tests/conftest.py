"""Test bootstrap: make the plugin package and the Hermes core importable."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _ensure_hermes_core() -> None:
    """Import Hermes' ``tools.environments.base``; locate a dev checkout when it is not installed."""
    try:
        import tools.environments.base  # noqa: F401
        return
    except ImportError:
        pass
    candidates = [os.getenv("HERMES_AGENT_ROOT"), str(Path.home() / ".hermes" / "hermes-agent")]
    for candidate in candidates:
        if candidate and (Path(candidate) / "tools" / "environments" / "base.py").is_file():
            sys.path.insert(0, candidate)
            try:
                import tools.environments.base  # noqa: F401
                return
            except ImportError:
                sys.path.pop(0)
    raise RuntimeError(
        "Hermes core is not importable. Run the tests from a Hermes checkout, or set "
        "HERMES_AGENT_ROOT to the hermes-agent source directory.")


_ensure_hermes_core()

import pytest


@pytest.fixture(autouse=True)
def _isolate_real_config(monkeypatch):
    """Never let a test read the developer's real config.yaml (it may pin a machine/snapshot)."""
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config_readonly", lambda: {})
    for name in ("TERMINAL_INSTABOX_MACHINE", "TERMINAL_INSTABOX_SNAPSHOT", "TERMINAL_INSTABOX_TEMPLATE"):
        monkeypatch.delenv(name, raising=False)
    yield
