"""instabox — Hermes terminal backend that runs commands in an InstaBox cloud machine.

InstaBox (https://instabox.sh) gives each agent its own Firecracker microVM: Debian 13, 4 vCPU /
8 GiB by default, a persistent 20 GiB disk at ``/data``, paused when idle and resumed by name.

Select it with ``terminal.backend: instabox``. The API key comes from ``INSTABOX_API_KEY`` (process
environment or the profile's ``.env``); the template from ``terminal.instabox_template``. See
README.md.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

from .provider import InstaboxProvider

logger = logging.getLogger(__name__)


def _core_ships_instabox() -> bool:
    """True on a Hermes core that already has an ``instabox`` backend built in.

    Such a core reserves the name (a registration would be refused) and serves the backend itself,
    so the plugin stays inactive instead of failing at import.
    """
    from tools.environments.base import BaseEnvironment
    return (Path(inspect.getfile(BaseEnvironment)).parent / "instabox.py").is_file()


def register(ctx) -> None:
    if _core_ships_instabox():
        logger.info("instabox: this Hermes core ships an InstaBox backend built in; the plugin stays inactive")
        return
    ctx.register_terminal_environment_provider(InstaboxProvider())
