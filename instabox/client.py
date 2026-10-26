"""Minimal, dependency-free client for the InstaBox API.

Docs: https://instabox.sh/docs/api.md  ·  OpenAPI: https://api.instabox.sh/v1/openapi.json

Only the calls a terminal backend needs: machine lifecycle (create / start / stop / suspend /
resume / delete, each answered as an operation), command execution, and file upload/download.
Authentication is ``X-API-Key``. Lifecycle calls send an ``Idempotency-Key``; exec does not.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.instabox.sh"

# Errors the API flags ``retryable`` and that mean "the request was not applied".
_RETRYABLE_STATUSES = frozenset({429, 503})


class InstaboxError(RuntimeError):
    """Any failure talking to InstaBox (transport, protocol or an API error)."""


class InstaboxAPIError(InstaboxError):
    """An error response from the API, carrying its machine-readable ``code``."""

    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False,
                 request_id: str | None = None, operation_id: str | None = None):
        self.status, self.code, self.message = status, code, message
        self.retryable = retryable
        self.request_id, self.operation_id = request_id, operation_id
        super().__init__(f"InstaBox API error {status} {code}: {message}")


def _operation_error(op: dict) -> str:
    err = op.get("error") or {}
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "unknown error")
    return str(err)


class InstaboxClient:
    """Thin synchronous HTTP client. Thread-safe: no mutable per-request state."""

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 60.0):
        if not api_key:
            raise InstaboxError("no InstaBox API key: set INSTABOX_API_KEY")
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    # -- transport -------------------------------------------------------------------------
    def _request(self, method: str, path: str, *, body: dict | None = None, raw: bytes | None = None,
                 headers: dict | None = None, timeout: float | None = None,
                 retries: int = 0) -> tuple[int, dict, bytes]:
        url = f"{self.base_url}{path}"
        data: bytes | None = None
        hdrs = {"X-API-Key": self.api_key, "Accept": "application/json",
                "User-Agent": "hermes-instabox-backend/1.0"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif raw is not None:
            data = raw
            hdrs["Content-Type"] = "application/octet-stream"
        if headers:
            hdrs.update(headers)

        attempts = max(0, retries) + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    payload = resp.read()
                    return resp.status, dict(resp.headers), payload
            except urllib.error.HTTPError as exc:
                payload = exc.read() or b""
                code, message, retryable, request_id, operation_id = (None, "", False, None, None)
                try:
                    err = (json.loads(payload.decode("utf-8")) or {}).get("error") or {}
                    code = err.get("code")
                    message = str(err.get("message") or "")
                    retryable = bool(err.get("retryable"))
                    request_id = err.get("requestId")
                    operation_id = err.get("operationId")
                except (ValueError, AttributeError):
                    message = payload.decode("utf-8", "replace")[:300]
                error = InstaboxAPIError(exc.code, str(code or f"http_{exc.code}"), message or exc.reason or "",
                                         retryable=retryable, request_id=request_id,
                                         operation_id=operation_id)
                if attempt + 1 < attempts and (retryable or exc.code in _RETRYABLE_STATUSES):
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    delay = float(retry_after) if (retry_after or "").replace(".", "", 1).isdigit() else 0.5 * 2 ** attempt
                    time.sleep(min(delay, 10.0) + random.uniform(0, 0.25))
                    last_exc = error
                    continue
                raise error from None
            except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                raise InstaboxError(f"cannot reach InstaBox at {self.base_url}: {exc}") from exc

        raise InstaboxError(f"InstaBox request failed: {last_exc}")

    def _json(self, method: str, path: str, **kwargs) -> tuple[int, dict]:
        status, _hdrs, payload = self._request(method, path, **kwargs)
        if not payload:
            return status, {}
        try:
            return status, json.loads(payload.decode("utf-8"))
        except ValueError as exc:
            raise InstaboxError(f"InstaBox returned non-JSON for {method} {path}: {exc}") from exc

    @staticmethod
    def _idempotency() -> dict:
        return {"Idempotency-Key": uuid.uuid4().hex}

    # -- machines --------------------------------------------------------------------------
    def get_machine(self, ref: str) -> dict | None:
        """A machine by ID or name; ``None`` when it does not exist."""
        try:
            _status, machine = self._json("GET", f"/v1/machines/{urllib.parse.quote(ref, safe='')}")
        except InstaboxAPIError as exc:
            if exc.status == 404:
                return None
            raise
        return machine or None

    def list_machines(self, *, metadata: dict | None = None, limit: int | None = None) -> list[dict]:
        query: list[tuple[str, str]] = []
        for key, value in (metadata or {}).items():
            query.append(("metadata", f"{key}:{value}"))
        if limit:
            query.append(("limit", str(limit)))
        path = "/v1/machines" + (("?" + urllib.parse.urlencode(query)) if query else "")
        _status, payload = self._json("GET", path)
        return list(payload.get("machines") or [])

    def create_machine(self, *, name: str | None = None, template: str | None = None,
                       snapshot: str | None = None, guest_user: str | None = None,
                       auto_suspend: int | None = None, metadata: dict | None = None) -> dict:
        """Create a machine; returns the ``create`` operation (its ``machineId`` is the new machine)."""
        body: dict = {}
        if name:
            body["name"] = name
        if snapshot:
            body["snapshotId"] = snapshot
        elif template:
            body["templateId"] = template
        if guest_user:
            body["guestUser"] = guest_user
        if auto_suspend is not None:
            body["autoSuspendAfterSeconds"] = int(auto_suspend)
        if metadata:
            body["metadata"] = {str(k): str(v) for k, v in metadata.items()}
        _status, op = self._json("POST", "/v1/machines", body=body, headers=self._idempotency())
        return op

    def machine_action(self, machine: str, action: str) -> dict:
        """``start`` / ``stop`` / ``suspend`` / ``resume``; returns the operation."""
        if action not in {"start", "stop", "suspend", "resume"}:
            raise ValueError(f"unknown machine action {action!r}")
        path = f"/v1/machines/{urllib.parse.quote(machine, safe='')}/{action}"
        _status, op = self._json("POST", path, body={}, headers=self._idempotency())
        return op

    def delete_machine(self, machine: str) -> dict:
        path = f"/v1/machines/{urllib.parse.quote(machine, safe='')}"
        _status, op = self._json("DELETE", path, headers={**self._idempotency(), "X-Confirm-Delete": machine})
        return op

    # -- operations ------------------------------------------------------------------------
    def get_operation(self, op_id: str, *, wait: int | None = None) -> dict:
        path = f"/v1/operations/{urllib.parse.quote(op_id, safe='')}"
        timeout = self.timeout
        if wait:
            path += f"?wait={int(wait)}s"
            timeout = max(self.timeout, int(wait) + 20)
        _status, op = self._json("GET", path, timeout=timeout)
        return op

    def wait_operation(self, op_id: str, *, timeout: float = 300.0) -> dict:
        """Follow an operation to a terminal state; long-polls (``?wait=``) so no sleep loop is needed."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstaboxError(f"InstaBox operation {op_id} did not finish within {int(timeout)}s")
            op = self.get_operation(op_id, wait=max(1, min(25, int(remaining))))
            state = (op or {}).get("state")
            if state == "succeeded":
                return op
            if state == "failed":
                raise InstaboxError(f"InstaBox {op.get('type') or 'operation'} failed: {_operation_error(op)}")
            if state not in {"queued", "running"}:
                logger.debug("InstaBox operation %s in state %r", op_id, state)
                raise InstaboxError(f"InstaBox operation {op_id} ended in unexpected state {state!r}")

    def run_operation(self, op: dict, *, timeout: float = 300.0) -> dict:
        """Wait out an operation returned by another call; returns the finished operation."""
        op_id = (op or {}).get("id")
        if not op_id:
            raise InstaboxError(f"InstaBox did not return an operation: {op!r}")
        return self.wait_operation(op_id, timeout=timeout)

    # -- snapshots -------------------------------------------------------------------------
    def list_snapshots(self) -> list[dict]:
        _status, payload = self._json("GET", "/v1/snapshots")
        return list(payload.get("snapshots") or [])

    def get_snapshot(self, ref: str) -> dict | None:
        """A snapshot by ID; ``None`` when it does not exist."""
        try:
            _status, snapshot = self._json("GET", f"/v1/snapshots/{urllib.parse.quote(ref, safe='')}")
        except InstaboxAPIError as exc:
            if exc.status == 404:
                return None
            raise
        return snapshot or None

    def resolve_snapshot(self, ref: str) -> dict:
        """A ready snapshot by ID **or by name**, or an error naming what is available."""
        ref = str(ref or "").strip()
        if not ref:
            raise InstaboxError("empty InstaBox snapshot reference")
        snapshot = self.get_snapshot(ref)
        if snapshot is None:
            snapshot = next((s for s in self.list_snapshots() if s.get("name") == ref), None)
        if snapshot is None:
            names = sorted({str(s["name"]) for s in self.list_snapshots() if s.get("name")})
            available = (f" Available snapshots: {', '.join(names)}." if names
                         else " You have no snapshots yet (`instabox snapshot ls`).")
            raise InstaboxError(f"InstaBox snapshot {ref!r} not found.{available}")
        status = str(snapshot.get("status") or "")
        if status != "ready":
            raise InstaboxError(
                f"InstaBox snapshot {snapshot.get('name') or ref} is {status or 'not ready'}; "
                f"a machine can only be created from a `ready` snapshot (`instabox snapshot ls`).")
        return snapshot

    def create_snapshot(self, machine: str, *, name: str, description: str | None = None,
                        root_only: bool = False) -> dict:
        """Save *machine* as a snapshot; a stopped machine answers ``201`` with ``status: ready``."""
        body: dict = {"name": name}
        if description:
            body["description"] = description
        if root_only:
            body["rootOnly"] = True
        path = f"/v1/machines/{urllib.parse.quote(machine, safe='')}/snapshots"
        _status, snapshot = self._json("POST", path, body=body, headers=self._idempotency())
        return snapshot

    def wait_snapshot(self, snap_id: str, *, timeout: float = 600.0) -> dict:
        """Poll a ``pending`` snapshot until it is ``ready`` (a running machine's save is async)."""
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.get_snapshot(snap_id) or {}
            status = str(snapshot.get("status") or "")
            if status == "ready":
                return snapshot
            if status == "failed":
                raise InstaboxError(f"InstaBox snapshot {snap_id} failed: {snapshot.get('error') or 'unknown error'}")
            if time.monotonic() >= deadline:
                raise InstaboxError(f"InstaBox snapshot {snap_id} still {status or 'pending'} after {int(timeout)}s")
            time.sleep(min(3.0, max(0.5, deadline - time.monotonic())))

    def delete_snapshot(self, ref: str) -> None:
        path = f"/v1/snapshots/{urllib.parse.quote(ref, safe='')}"
        self._request("DELETE", path, headers={**self._idempotency(), "X-Confirm-Delete": ref})

    # -- commands --------------------------------------------------------------------------
    def exec_command(self, machine: str, *, cmd: str | None = None, argv: list[str] | None = None,
                     cwd: str | None = None, envs: dict | None = None, stdin: str | None = None,
                     timeout: int = 120, retries: int = 1) -> dict:
        """Run one command; returns ``{stdout, stderr, exitCode, timedOut, truncated}``."""
        if bool(cmd) == bool(argv):
            raise ValueError("pass exactly one of cmd / argv")
        body: dict = {"cmd": cmd} if cmd is not None else {"argv": list(argv or [])}
        if cwd:
            body["cwd"] = cwd
        if envs:
            body["envs"] = {str(k): str(v) for k, v in envs.items()}
        if stdin:
            body["stdin"] = stdin
        body["timeoutSeconds"] = max(1, min(int(timeout), 600))
        path = f"/v1/machines/{urllib.parse.quote(machine, safe='')}/exec"
        _status, payload = self._json("POST", path, body=body, timeout=body["timeoutSeconds"] + 30,
                                      retries=retries)
        return payload

    # -- files -----------------------------------------------------------------------------
    def put_file(self, machine: str, path: str, data: bytes, *, mode: str | None = None,
                 parents: bool = True) -> dict:
        query = {"path": path}
        if mode:
            query["mode"] = mode
        if parents:
            query["parents"] = "true"
        url = f"/v1/machines/{urllib.parse.quote(machine, safe='')}/files?{urllib.parse.urlencode(query)}"
        _status, _hdrs, payload = self._request("PUT", url, raw=data)
        return json.loads(payload.decode("utf-8")) if payload else {}

    def get_file_bytes(self, machine: str, path: str, *, timeout: float | None = None) -> bytes:
        query = urllib.parse.urlencode({"path": path})
        url = f"/v1/machines/{urllib.parse.quote(machine, safe='')}/files/content?{query}"
        _status, _hdrs, payload = self._request("GET", url, timeout=timeout)
        return payload

    def get_file_text(self, machine: str, path: str) -> dict:
        """``GET /files`` — a directory listing or a text file, read live as root."""
        query = urllib.parse.urlencode({"path": path})
        _status, payload = self._json("GET", f"/v1/machines/{urllib.parse.quote(machine, safe='')}/files?{query}")
        return payload
