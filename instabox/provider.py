"""The ``instabox`` terminal backend provider: classification, health checks, environment creation."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from agent.terminal_env_provider import TerminalEnvironmentProvider

from .client import DEFAULT_BASE_URL, InstaboxClient, InstaboxError
from .environment import InstaboxEnvironment, machine_name_for

logger = logging.getLogger(__name__)

_KEY_URL = "https://dashboard.instabox.sh/dashboard/api-keys"
_DOCS_URL = "https://instabox.sh/docs/start.md"
#: The template InstaBox creates from on every plan (the free plan allows only this one).
DEFAULT_TEMPLATE = "agent-browser"
#: Idle minutes before the machine pauses itself; 0 turns it off on paid plans.
DEFAULT_AUTO_SUSPEND = 1800


def _terminal_config() -> dict:
    from hermes_cli.config import load_config_readonly
    terminal = load_config_readonly().get("terminal")
    return terminal if isinstance(terminal, dict) else {}


def _plugin_settings() -> dict:
    """``plugins.entries.instabox.settings`` — the plugin's own settings form."""
    from hermes_cli.config import load_config_readonly
    plugins = load_config_readonly().get("plugins")
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get("instabox") if isinstance(entries, dict) else None
    settings = entry.get("settings") if isinstance(entry, dict) else None
    return settings if isinstance(settings, dict) else {}


def _setting(name: str, env: str = "") -> str:
    """A plugin setting: terminal.* first, then the plugin's settings form, then an env var."""
    value = _terminal_config().get(f"instabox_{name}")
    value = str(value or "").strip() or str(_plugin_settings().get(name) or "").strip()
    value = value or (_env_value(env) if env else "")
    return str(value or "").strip()


def _env_value(name: str) -> str:
    from hermes_cli.config import get_env_value
    return str(get_env_value(name) or "").strip()


def api_key() -> str:
    """``INSTABOX_API_KEY`` from the process environment or the profile's ``.env``."""
    return os.getenv("INSTABOX_API_KEY", "").strip() or _env_value("INSTABOX_API_KEY")


def api_base_url() -> str:
    """``instabox_api_url`` (terminal.*, plugin settings, ``INSTABOX_API_URL``), else the public API."""
    return (_setting("api_url", "INSTABOX_API_URL") or DEFAULT_BASE_URL).rstrip("/")


def configured_template() -> str:
    return _setting("template", "TERMINAL_INSTABOX_TEMPLATE") or DEFAULT_TEMPLATE


def configured_auto_suspend() -> Optional[int]:
    """Idle seconds before InstaBox pauses the machine (``0`` disables it on paid plans)."""
    raw = _terminal_config().get("instabox_auto_suspend", _plugin_settings().get("auto_suspend"))
    if raw is None or str(raw).strip() == "":
        return DEFAULT_AUTO_SUSPEND
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning("terminal.instabox_auto_suspend=%r is not an integer; using %d", raw, DEFAULT_AUTO_SUSPEND)
        return DEFAULT_AUTO_SUSPEND


def configured_guest_user() -> Optional[str]:
    return _setting("guest_user") or None


def configured_snapshot() -> Optional[str]:
    """Snapshot ID or name to start machines from, instead of a template."""
    return _setting("snapshot", "TERMINAL_INSTABOX_SNAPSHOT") or None


def configured_machine() -> Optional[str]:
    """An existing machine (name or ID) to use instead of creating one.

    ``terminal.instabox_machine`` or ``plugins.entries.instabox.settings.machine`` (or
    ``TERMINAL_INSTABOX_MACHINE``): with this set, every session uses that machine and the backend
    never creates, adopts or deletes one.
    """
    return _setting("machine", "TERMINAL_INSTABOX_MACHINE") or None


def configured_file_sync() -> bool:
    """Mirror the host's ``~/.hermes`` (skills, cache, credential files) into the machine.

    Off by default: a typical ``~/.hermes`` is ~700 files / 6 MB and the API answers one file per
    round trip (~0.6 s each), so the first command would otherwise wait minutes. Turn it on when
    the machine really needs host skills/credentials.
    """
    raw = _terminal_config().get("instabox_file_sync", _plugin_settings().get("file_sync"))
    if raw is None:
        return False
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw)


class InstaboxProvider(TerminalEnvironmentProvider):
    """One Firecracker microVM per task, reached over the InstaBox HTTPS API."""

    # Commands run in another machine's filesystem: host OS/home/cwd hints are wrong, host-looking
    # cwds get sanitized, and the disposable VM skips dangerous-command approval prompts.
    is_remote = True
    is_container = True
    # The screen, computer_use and the browser stay on the host: exec is a request/response API
    # with no long-lived stdin/stdout channel to hand them.
    supports_streams = False

    @property
    def name(self) -> str:
        return "instabox"

    @property
    def display_name(self) -> str:
        return "InstaBox"

    @property
    def description(self) -> str:
        return "Run commands in an InstaBox cloud machine (Firecracker microVM) from instabox.sh."

    @property
    def env_description(self) -> str:
        return "an InstaBox cloud machine (Debian Linux)"

    # A stopped/suspended VM is resumed by name; without this two ephemeral sessions could share
    # (and delete) one machine. Ephemeral environments use unique names, so the base default holds.
    session_isolated_when_nonpersistent = False

    @property
    def default_cwd(self) -> Optional[str]:
        # Resolved per machine ($HOME, /data/home) in InstaboxEnvironment.__init__.
        return "~"

    @property
    def cache_path_base(self) -> Optional[str]:
        # FileSyncManager lands ~/.hermes under the machine's home; the remote shell expands ~.
        return "~/.hermes"

    @property
    def strip_env_keys(self) -> frozenset:
        return frozenset({"INSTABOX_API_KEY"})

    # -- health ----------------------------------------------------------------------------
    def is_available(self) -> bool:
        """Cheap and offline (runs on every UI paint): the key is the only local prerequisite."""
        return bool(api_key())

    def check_requirements(self, config: dict[str, Any]) -> bool:
        if not api_key():
            logger.error(
                "InstaBox backend selected but INSTABOX_API_KEY is not set. Create a key at %s "
                "and add it to %s as INSTABOX_API_KEY=ibk_...", _KEY_URL,
                os.path.join(os.getenv("HERMES_HOME", "~/.hermes"), ".env"))
            return False
        return True

    def probe(self) -> tuple[str, str]:
        if self.is_available():
            source = f"snapshot {configured_snapshot()}" if configured_snapshot() else f"template {configured_template()}"
            return ("ready", f"{api_base_url()} · {source}")
        return ("needs_setup", f"INSTABOX_API_KEY is not set — create one at {_KEY_URL}")

    def setup_instructions(self) -> list[str]:
        lines = [f"Create an API key at {_KEY_URL}, then add INSTABOX_API_KEY=ibk_... to your "
                 "profile's .env (or export it)."]
        if api_key():
            lines.append(f"API key found. Endpoint: {api_base_url()}")
            snapshot = configured_snapshot()
            if snapshot:
                lines.append(f"Starting from snapshot: {snapshot} (terminal.instabox_snapshot). "
                             "The snapshot decides the installed software; the template is not used.")
            else:
                lines.append(f"Template: {configured_template()} "
                             "(change with `hermes config set terminal.instabox_template <template>`; "
                             "`universal` is the same image without the preinstalled desktop).")
            lines.append("Start from your own saved machine with "
                         "`hermes config set terminal.instabox_snapshot <name-or-id>` "
                         "(`instabox snapshot ls` lists them; save one with `instabox snapshot save <machine> --name N`).")
            lines.append("`instabox templates` lists the templates your plan can create from.")
        else:
            lines.append(f"Docs: {_DOCS_URL}")
        return lines

    def doctor_checks(self) -> list[tuple[bool, str, str]]:
        key = api_key()
        rows = [(bool(key), "InstaBox API key", "(set)" if key else f"(missing — {_KEY_URL})")]
        if not key:
            return rows
        client = InstaboxClient(key, base_url=api_base_url(), timeout=5)
        started = time.monotonic()
        try:
            machines = client.list_machines(limit=1)
        except InstaboxError as exc:
            rows.append((False, "InstaBox API", f"({exc})"))
        else:
            rows.append((True, "InstaBox API",
                         f"({api_base_url()} reachable in {int((time.monotonic() - started) * 1000)}ms, "
                         f"{len(machines)} machine(s) on this page)"))
        wanted = configured_snapshot()
        if wanted:
            try:
                snapshot = client.resolve_snapshot(wanted)
            except InstaboxError as exc:
                rows.append((False, "InstaBox snapshot", f"({exc})"))
            else:
                rows.append((True, "InstaBox snapshot",
                             f"({snapshot.get('name')} · {snapshot.get('id')} · ready)"))
        return rows

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "cloud",
            "tag": "Firecracker microVM · Debian 13",
            "env_vars": [{"key": "INSTABOX_API_KEY", "prompt": "InstaBox API key (ibk_...)", "url": _KEY_URL}],
        }

    # -- environment -----------------------------------------------------------------------
    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default",
                           image: Optional[str] = None, container_config: Optional[dict[str, Any]] = None,
                           **kwargs: Any) -> InstaboxEnvironment:
        cc = container_config or {}
        whole = "" if image is None else str(image)
        if whole:
            # Batch runs register a per-prompt image; InstaBox has no custom images, so an explicit
            # value is ignored. Use a snapshot (terminal.instabox_snapshot) for a custom machine.
            logger.debug("InstaBox: ignoring image override %r (use terminal.instabox_snapshot instead)", whole)
        client = InstaboxClient(api_key(), base_url=api_base_url(), timeout=60)
        snapshot = configured_snapshot()
        # InstaBox takes a snapshot OR a template, never both: a configured snapshot wins.
        template = None if snapshot else configured_template()
        if snapshot and configured_template() != DEFAULT_TEMPLATE:
            logger.info("InstaBox: terminal.instabox_snapshot=%s is set; ignoring terminal.instabox_template=%s",
                        snapshot, configured_template())
        return InstaboxEnvironment(
            client=client,
            machine_name=machine_name_for(task_id, persistent=bool(cc.get("container_persistent", True))),
            cwd=cwd, timeout=timeout, persistent=bool(cc.get("container_persistent", True)),
            template=template, snapshot=snapshot, task_id=task_id,
            machine=configured_machine(),
            auto_suspend=configured_auto_suspend(), guest_user=configured_guest_user(),
            file_sync=configured_file_sync())
