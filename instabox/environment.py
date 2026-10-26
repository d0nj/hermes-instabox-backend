"""InstaBox execution environment: one Firecracker microVM per task, driven over HTTPS.

Hermes runs every command through ``POST /v1/machines/{id}/exec``; the VM's filesystem is what
``read_file`` / ``write_file`` / ``patch`` / ``search_files`` see, because those tools go through
:meth:`execute` too. A persistent environment is paused (``suspend``, memory kept) on cleanup and
resumed by name next session; an ephemeral one is deleted.

Timeouts and cancellation: InstaBox caps one exec at 600 s and kills it with exit code 124, so a
longer Hermes timeout is capped to match. ``kill()`` on an in-flight command sends a second exec
that TERMs/KILLs the recorded shell and its children; the original request is abandoned either way.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import shlex
import tarfile
import threading
import time
import uuid
from pathlib import Path

from tools.environments.base import BaseEnvironment
from tools.environments.base_output import _ThreadedProcessHandle
from tools.environments.file_sync import FileSyncManager, iter_sync_files, quoted_rm_command

from .client import InstaboxAPIError, InstaboxClient, InstaboxError

logger = logging.getLogger(__name__)

#: Re-check (and wake) the machine only after this much idle time: InstaBox wakes idle-paused
#: machines on exec by itself, so a GET per command would only add latency.
_IDLE_REENSURE_SECONDS = 240.0
#: ``stdin`` on the exec API is capped at 1 MiB; more is staged as a file instead.
_MAX_INLINE_STDIN = 1 << 20
#: The API refuses ``timeoutSeconds`` above 600.
_MAX_EXEC_TIMEOUT = 600
#: Machine states that need a lifecycle call (or a wait) before commands can run.
_BUSY_STATES = frozenset({"creating", "starting", "resuming", "stopping", "suspending"})


def machine_name_for(task_id: str, *, persistent: bool) -> str:
    """A DNS-ish machine name for *task_id*: ``hermes-<task>``, or a unique one when ephemeral.

    InstaBox names are 1-63 chars of ``a-z0-9-``; the same name is how a persistent environment
    finds its VM again, so the prefix is part of the backend's on-disk identity.
    """
    cleaned = "".join(ch if (ch.isalnum() and ch.isascii()) else "-" for ch in str(task_id).lower())
    cleaned = "-".join(part for part in cleaned.split("-") if part) or "default"
    if persistent:
        name = f"hermes-{cleaned}"[:48]
    else:
        name = f"hermes-{cleaned}"[:40] + "-" + uuid.uuid4().hex[:8]
    return name.rstrip("-") or "hermes-box"


class _NullSync:
    """Stand-in for FileSyncManager when host-side file mirroring is switched off."""

    def sync(self, **_kwargs) -> None:
        return None

    def sync_back(self, **_kwargs) -> None:
        return None


class InstaboxEnvironment(BaseEnvironment):
    """Commands, files and cwd resolution live in an InstaBox machine."""

    # Base hands stdin to _run_bash as ``stdin_data``; the exec API takes it inline.
    _stdin_mode = "payload"
    _snapshot_timeout = 45

    def __init__(self, *, client: InstaboxClient, machine_name: str, cwd: str, timeout: int,
                 persistent: bool = True, template: str | None = None, snapshot: str | None = None,
                 task_id: str = "default", auto_suspend: int | None = 1800,
                 guest_user: str | None = None, file_sync: bool = True, machine: str | None = None):
        self._client = client
        self._machine_name = machine_name
        self._persistent = bool(persistent)
        self._template = template
        self._snapshot = snapshot
        self._task_id = task_id
        self._auto_suspend = auto_suspend
        self._guest_user = guest_user
        self._attach_ref = machine
        self._created = True
        self._restore_state: str | None = None
        self._machine_id = ""
        self._lock = threading.RLock()
        self._closed = False
        self._last_activity = time.monotonic()

        timings: dict[str, float] = {}
        started = time.monotonic()

        def lap(name: str) -> None:
            timings[name] = time.monotonic() - started

        machine = self._attach_or_create()
        self._machine_id = str(machine.get("id") or "")
        lap("attach")
        self._ensure_running()
        lap("ready")

        home = self._resolve_home()
        start_cwd = home if cwd in ("", "/", "~") else cwd
        super().__init__(cwd=start_cwd, timeout=timeout)
        lap("home")

        self._sync_manager = (
            FileSyncManager(
                get_files_fn=lambda: iter_sync_files(f"{home}/.hermes"),
                upload_fn=self._upload_file,
                delete_fn=self._delete_files,
                bulk_upload_fn=self._bulk_upload,
                bulk_download_fn=self._download_hermes_tree)
            if file_sync else _NullSync())
        self._sync_manager.sync(force=True)
        lap("sync")
        self.init_session()
        lap("session")
        logger.info("InstaBox: %s ready in %.1fs (attach %.1fs, ready %.1fs, home %.1fs, "
                    "sync %.1fs, session %.1fs)", self._machine_name, timings["session"],
                    timings["attach"], timings["ready"] - timings["attach"],
                    timings["home"] - timings["ready"], timings["sync"] - timings["home"],
                    timings["session"] - timings["sync"])

    # -- lifecycle -------------------------------------------------------------------------
    @property
    def machine_id(self) -> str:
        return self._machine_id

    @property
    def machine_name(self) -> str:
        return self._machine_name

    def _attach_or_create(self) -> dict:
        """Reuse the machine of a persistent environment, else create one and wait for it.

        Plans differ by machine count (free: 1, pro: 500, team: unlimited), so a create that hits
        ``quota_exceeded`` is not fatal for a persistent environment: it adopts an existing Hermes
        machine instead of failing the terminal tool. An ephemeral environment never adopts — it
        would delete a machine another session is using — and explains the way out instead.
        """
        if self._attach_ref:
            # A machine named in config is the user's: never create, never delete, restore its state.
            machine = self._client.get_machine(self._attach_ref)
            if machine is None:
                raise InstaboxError(
                    f"InstaBox machine {self._attach_ref!r} not found (`instabox ls` lists your machines). "
                    "Unset `terminal.instabox_machine` to let the backend create one.")
            self._machine_name = str(machine.get("name") or self._attach_ref)
            self._created = False
            self._restore_state = str(machine.get("state") or "")
            logger.info("InstaBox: using configured machine %s (%s)", self._machine_name, self._restore_state)
            return machine
        if self._persistent:
            existing = self._client.get_machine(self._machine_name)
            if existing:
                logger.info("InstaBox: reusing machine %s (%s)", self._machine_name, existing.get("state"))
                if self._snapshot:
                    logger.info("InstaBox: terminal.instabox_snapshot applies only to new machines; "
                                "%s already exists", self._machine_name)
                return existing
        if self._snapshot:
            # By ID or by name; a missing or not-yet-ready snapshot fails here, before any create.
            snapshot = self._client.resolve_snapshot(self._snapshot)
            self._snapshot = str(snapshot.get("id") or self._snapshot)
            logger.info("InstaBox: starting %s from snapshot %s (%s)", self._machine_name,
                        snapshot.get("name"), self._snapshot)
        try:
            op = self._client.create_machine(
                name=self._machine_name,
                # InstaBox takes snapshotId OR templateId: the snapshot already carries the software.
                template=None if self._snapshot else self._template,
                snapshot=self._snapshot,
                guest_user=self._guest_user, auto_suspend=self._auto_suspend,
                metadata={"hermes": "terminal-backend", "task": str(self._task_id)[:64]})
        except InstaboxAPIError as exc:
            if exc.code == "quota_exceeded":
                return self._handle_quota(exc)
            raise InstaboxError(self._create_hint(exc)) from exc
        try:
            done = self._client.run_operation(op, timeout=600)
        except InstaboxError:
            # A concurrent create with the same name wins the race: attach instead of failing.
            if self._persistent:
                existing = self._client.get_machine(self._machine_name)
                if existing:
                    logger.info("InstaBox: machine %s appeared during create; attaching", self._machine_name)
                    return existing
            raise
        machine = done.get("machine") or {}
        if not machine.get("id"):
            machine = self._client.get_machine(str(op.get("machineId") or self._machine_name)) or machine
        logger.info("InstaBox: created machine %s (%s)", self._machine_name, machine.get("id"))
        return machine

    def _create_hint(self, exc: InstaboxAPIError) -> str:
        """Turn a create-time API error into something the user can act on."""
        if exc.code == "template_not_allowed":
            return (f"InstaBox refused template {self._template!r}: the free plan creates only from "
                    f"`agent-browser` or a snapshot. Set `terminal.instabox_template agent-browser` "
                    f"(`hermes config set terminal.instabox_template agent-browser`). [{exc}]")
        if exc.code == "spec_not_allowed":
            return (f"InstaBox refused this machine size: the plan does not offer it "
                    f"(`instabox workspace ls --json` lists each workspace's allowedSpecs). [{exc}]")
        return str(exc)

    def _adoptable_machine(self) -> dict | None:
        """An existing Hermes machine this environment may take over, if any."""
        try:
            machines = self._client.list_machines(metadata={"hermes": "terminal-backend"}, limit=50)
        except InstaboxError as exc:
            logger.debug("InstaBox: could not list machines while handling the quota: %s", exc)
            return None
        usable = [m for m in machines if m.get("state") in {"running", "suspended", "stopped"}]
        if not usable:
            return None
        # A warm machine beats a paused one; otherwise the oldest Hermes machine is as good as any.
        return sorted(usable, key=lambda m: (m.get("state") != "running", m.get("createdAt") or ""))[0]

    def _handle_quota(self, exc: InstaboxAPIError) -> dict:
        """Adopt a machine when persistent; otherwise fail with the limit and the fix."""
        if self._persistent:
            machine = self._adoptable_machine()
            if machine:
                logger.warning(
                    "InstaBox: machine limit reached (quota_exceeded); adopting existing machine %s (%s) "
                    "instead of creating %s", machine.get("name"), machine.get("id"), self._machine_name)
                self._machine_name = str(machine.get("name") or self._machine_name)
                return machine
        raise InstaboxError(
            "InstaBox machine limit reached on this workspace (free: 1 machine, pro: 500 with 100 "
            "running, team: unlimited). Either delete an idle machine (`instabox ls`, "
            "`instabox rm <name>`), or set `terminal.container_persistent: true` so Hermes reuses one "
            "persistent machine instead of creating one per task. "
            f"[{exc}]") from exc

    def _ensure_running(self, *, timeout: float = 300.0) -> dict:
        """Make the machine ``running`` with ``runtime.ready`` true, issuing start/resume as needed."""
        deadline = time.monotonic() + timeout
        delay = 0.5
        while True:
            machine = self._client.get_machine(self._machine_id) or {}
            state = str(machine.get("state") or "")
            ready = bool((machine.get("runtime") or {}).get("ready"))
            if state == "running" and ready:
                return machine
            if state == "deleting":
                raise InstaboxError(f"InstaBox machine {self._machine_id} is being deleted")
            if state in _BUSY_STATES and machine.get("operationId"):
                remaining = max(5.0, deadline - time.monotonic())
                self._client.wait_operation(str(machine["operationId"]), timeout=min(remaining, 300))
                continue
            if state in ("stopped", "error"):
                self._client.run_operation(self._client.machine_action(self._machine_id, "start"),
                                           timeout=max(30.0, deadline - time.monotonic()))
                continue
            if state == "suspended":
                self._client.run_operation(self._client.machine_action(self._machine_id, "resume"),
                                           timeout=max(30.0, deadline - time.monotonic()))
                continue
            if time.monotonic() >= deadline:
                raise InstaboxError(
                    f"InstaBox machine {self._machine_id} not ready after {int(timeout)}s "
                    f"(state={state or 'unknown'}, ready={ready})")
            time.sleep(delay)
            delay = min(delay * 1.5, 3.0)

    def _resolve_home(self) -> str:
        """The machine's ``$HOME`` (``/data/home``), or ``/data/home`` when the probe fails."""
        try:
            result = self._exec_raw('printf %s "$HOME"', timeout=30)
        except Exception as exc:  # a probe failure must not fail environment creation
            logger.debug("InstaBox: could not resolve $HOME: %s", exc)
            return "/data/home"
        home = (result.get("stdout") or "").strip()
        return home if home.startswith("/") else "/data/home"

    # -- command execution -----------------------------------------------------------------
    def _exec_raw(self, cmd: str, *, timeout: int = 60, stdin: str | None = None) -> dict:
        """One exec, waking a stopped/suspended machine once and retrying."""
        last: Exception | None = None
        for attempt in range(2):
            try:
                return self._client.exec_command(self._machine_id, cmd=cmd, stdin=stdin, timeout=timeout)
            except InstaboxAPIError as exc:
                last = exc
                if attempt == 0 and exc.code in {"machine_not_running", "machine_paused", "operation_conflict"}:
                    self._ensure_running()
                    continue
                raise
            except InstaboxError as exc:
                last = exc
                raise
        raise last or InstaboxError("exec failed")

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None):
        """Spawn the wrapped script in the machine; the API always uses ``bash -lc`` so *login* is moot."""
        exec_id = uuid.uuid4().hex[:12]
        pid_file = f"/tmp/.hermes-exec-{exec_id}.pid"  # no-tmp: ok — inside the sandbox
        # ``exec 2>&1`` merges stderr at the source so line order survives; the API returns both
        # streams separately and only stdout reaches the process handle.
        script = f"exec 2>&1\necho $$ > {pid_file} 2>/dev/null\n{cmd_string}"

        stdin_payload: str | None = stdin_data or None
        if stdin_payload:
            if len(stdin_payload.encode("utf-8", "surrogateescape")) > _MAX_INLINE_STDIN:
                staged = self._staged_stdin_path()
                self._client.put_file(self._machine_id, staged,
                                      stdin_payload.encode("utf-8", "surrogateescape"), mode="0600")
                script = self._redirect_stdin_from_file(script, staged)
                stdin_payload = None

        def exec_fn() -> tuple[str, int]:
            try:
                result = self._exec_raw(script, timeout=timeout, stdin=stdin_payload)
            except Exception as exc:
                return (f"instabox: {exc}", 1)
            out = result.get("stdout") or ""
            err = (result.get("stderr") or "").strip()
            if err:
                out = f"{out}\n{err}" if out else err
            if result.get("truncated"):
                out += "\n[instabox: output truncated by the API]"
            code = result.get("exitCode")
            try:
                return (out, int(code if code is not None else 0))
            except (TypeError, ValueError):
                return (out, 0)

        def cancel() -> None:
            # Best effort: the request above is abandoned regardless; this stops the work.
            kill = (f"pid=$(cat {pid_file} 2>/dev/null); [ -n \"$pid\" ] || exit 0; "
                    f"pkill -TERM -P \"$pid\" 2>/dev/null; kill -TERM \"$pid\" 2>/dev/null; "
                    f"sleep 0.5; pkill -KILL -P \"$pid\" 2>/dev/null; kill -KILL \"$pid\" 2>/dev/null; "
                    f"rm -f {pid_file}; exit 0")
            try:
                with self._lock:
                    self._client.exec_command(self._machine_id, cmd=kill, timeout=15, retries=0)
            except Exception:
                logger.debug("InstaBox: cancel of %s failed", self._machine_id, exc_info=True)

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    def _before_execute(self) -> None:
        if time.monotonic() - self._last_activity > _IDLE_REENSURE_SECONDS:
            self._ensure_running()
        self._sync_manager.sync()
        self._last_activity = time.monotonic()

    # -- file sync transport ---------------------------------------------------------------
    def _upload_file(self, host_path: str, remote_path: str) -> None:
        # ``parents=true`` creates missing directories server-side: one request per file, not two.
        with open(host_path, "rb") as handle:
            self._client.put_file(self._machine_id, remote_path, handle.read(), parents=True)

    def _bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """Ship a whole sync cycle as one ``.tar.gz``: 1 upload + 1 extract, not 2 round trips per file.

        Entries are stored under their absolute remote path (minus the leading slash) and unpacked
        with ``tar -C /``, so they land exactly where the file tools expect them.
        """
        if not files:
            return
        started = time.monotonic()
        buffer = io.BytesIO()
        skipped = 0
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for host_path, remote_path in files:
                if not os.path.isfile(host_path):
                    # Sockets, FIFOs and devices cannot be archived (SSH/Daytona/Vercel skip them too).
                    skipped += 1
                    continue
                try:
                    archive.add(str(host_path), arcname=str(remote_path).lstrip("/"), recursive=False)
                except OSError as exc:
                    raise InstaboxError(f"InstaBox file sync could not read {host_path}: {exc}") from exc
        if not buffer.tell():
            logger.info("InstaBox: nothing to sync (%d non-regular file(s) skipped)", skipped)
            return
        blob = buffer.getvalue()
        remote_archive = f"/tmp/.hermes-sync-{uuid.uuid4().hex[:12]}.tar.gz"  # no-tmp: ok — sandbox path
        self._client.put_file(self._machine_id, remote_archive, blob, mode="0600", parents=True)
        quoted = shlex.quote(remote_archive)
        self._exec_raw(f"tar -xzf {quoted} -C / && rm -f {quoted}", timeout=180)
        logger.info("InstaBox: synced %d file(s) as %.1f MiB in %.1fs (one upload, one extract%s)",
                    len(files) - skipped, len(blob) / (1024 * 1024), time.monotonic() - started,
                    f", {skipped} skipped" if skipped else "")

    def _delete_files(self, remote_paths: list[str]) -> None:
        if remote_paths:
            self._exec_raw(quoted_rm_command(remote_paths), timeout=60)

    def _download_hermes_tree(self, dest: Path) -> None:
        """Tar the machine's ``~/.hermes`` and pull it over ``files/content``."""
        home = self.cwd if self.cwd.startswith("/") else "/data/home"
        base = f"{home}/.hermes"
        rel = base.lstrip("/")
        remote_tar = f"/tmp/.hermes-sync.{os.getpid()}.tar"  # no-tmp: ok — remote sandbox path
        self._exec_raw(f"tar cf {shlex.quote(remote_tar)} --exclude='*.sock' -C / {shlex.quote(rel)} "
                       f"2>/dev/null || true", timeout=120)
        try:
            data = self._client.get_file_bytes(self._machine_id, remote_tar, timeout=180)
            Path(dest).write_bytes(data)
        finally:
            with contextlib.suppress(Exception):
                self._exec_raw(f"rm -f {shlex.quote(remote_tar)}", timeout=30)

    # -- teardown --------------------------------------------------------------------------
    def cleanup(self):
        """Pause (persistent, memory kept) or delete (ephemeral) the machine."""
        lock = getattr(self, "_lock", None)
        if lock is None or getattr(self, "_closed", True):
            return  # construction failed before the environment existed
        with lock:
            if self._closed:
                return
            self._closed = True
            sync = getattr(self, "_sync_manager", None)
            if sync is not None:
                try:
                    sync.sync_back()
                except Exception as exc:
                    logger.warning("InstaBox: sync_back failed: %s", exc)
            if not self._machine_id:
                return
            try:
                if not self._created:
                    # Put a machine we only borrowed back the way we found it.
                    action = {"suspended": "suspend", "stopped": "stop"}.get(self._restore_state or "")
                    current = (self._client.get_machine(self._machine_id) or {}).get("state")
                    if action and current == "running":
                        self._client.run_operation(
                            self._client.machine_action(self._machine_id, action), timeout=180)
                    logger.info("InstaBox: left configured machine %s as it was (%s)",
                                self._machine_id, self._restore_state)
                elif self._persistent:
                    self._client.run_operation(
                        self._client.machine_action(self._machine_id, "suspend"), timeout=180)
                    logger.info("InstaBox: suspended machine %s (files kept)", self._machine_id)
                else:
                    self._client.run_operation(self._client.delete_machine(self._machine_id), timeout=180)
                    logger.info("InstaBox: deleted machine %s", self._machine_id)
            except InstaboxError as exc:
                # Already gone, already paused, or a 409 from a concurrent lifecycle change.
                logger.warning("InstaBox: cleanup of %s failed: %s", self._machine_id, exc)
            except Exception as exc:
                logger.warning("InstaBox: cleanup of %s failed: %s", self._machine_id, exc)
