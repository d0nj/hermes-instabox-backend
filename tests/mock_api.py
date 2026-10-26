"""A local fake of the InstaBox HTTP API, for tests.

It speaks the documented shapes (https://instabox.sh/docs/api.md) and runs commands for real with
``bash -lc`` inside a throwaway "machine root" used as the command's cwd and ``$HOME``. That is
enough to exercise the whole backend — transport, operation polling, session snapshot, cwd markers,
stdin, file upload/download — without an InstaBox account.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_MAX_STDOUT = 1_000_000


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class MockInstabox:
    """One org with machines, operations and an exec endpoint."""

    def __init__(self, root: Path, port: int = 0, max_machines: int | None = None,
                 allowed_templates: set[str] | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_machines = max_machines
        self.allowed_templates = allowed_templates
        self.machines: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        self.ops: dict[str, dict] = {}
        self.requests: list[dict] = []
        self._polls: dict[str, int] = {}
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._httpd.mock = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    # -- server ----------------------------------------------------------------------------
    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "MockInstabox":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    # -- helpers ---------------------------------------------------------------------------
    @staticmethod
    def public(machine: dict) -> dict:
        return {k: v for k, v in machine.items() if not k.startswith("_")}

    def machine(self, ref: str) -> dict | None:
        if ref in self.machines:
            return self.machines[ref]
        return next((m for m in self.machines.values() if m["name"] == ref), None)

    def _new_op(self, kind: str, machine: dict, apply) -> dict:
        op = {"id": "op_" + uuid.uuid4().hex[:26], "machineId": machine["id"], "type": kind,
              "state": "queued", "phase": "queued", "error": None,
              "createdAt": _iso(), "updatedAt": _iso()}
        self.ops[op["id"]] = {"op": op, "apply": apply}
        machine["operationId"] = op["id"]
        return op

    def _error(self, status: int, code: str, message: str, retryable: bool = False):
        return status, {"error": {"code": code, "message": message, "retryable": retryable,
                                  "requestId": "req_test", "operationId": None}}

    def _create_machine(self, body: dict):
        name = body.get("name") or "box-" + uuid.uuid4().hex[:8]
        if body.get("templateId") and body.get("snapshotId"):
            return self._error(400, "bad_request", "give templateId or snapshotId, not both")
        snapshot = None
        if body.get("snapshotId"):
            snapshot = self.snapshots.get(str(body["snapshotId"])) or next(
                (s for s in self.snapshots.values() if s["name"] == body["snapshotId"]), None)
            if snapshot is None:
                return self._error(400, "bad_request", f"unknown snapshot {body['snapshotId']!r}")
            if snapshot.get("status") != "ready":
                return self._error(400, "bad_request", f"snapshot is {snapshot.get('status')}, not ready")
        if self.allowed_templates is not None and (body.get("templateId") or "agent-browser") not in self.allowed_templates \
                and snapshot is None:
            return self._error(403, "template_not_allowed",
                               f"template {body.get('templateId')!r} is not allowed on this plan")
        if self.max_machines is not None and len(self.machines) >= self.max_machines:
            return self._error(429, "quota_exceeded",
                               f"machine limit reached: this workspace allows {self.max_machines} machine(s)",
                               retryable=True)
        if self.machine(name):
            return self._error(409, "operation_conflict", f"a machine named {name} already exists")
        template_id = body.get("templateId") or ("agent-browser" if snapshot is None else snapshot["template"]["id"])
        machine = {
            "id": "m_" + uuid.uuid4().hex[:26], "name": name, "state": "creating",
            "desiredState": "running", "revision": 1, "operationId": None,
            "template": {"id": template_id, "version": "test", "digest": "sha256:test"},
            "spec": {"vcpus": 4, "memMiB": 8192, "persistentDiskGiB": 20, "rootDiskGiB": 16},
            "storage": {"sizeGiB": 20, "mountPath": "/data", "persistent": True, "synced": True},
            "runtime": {"ready": False},
            "autoSuspendAfterSeconds": body.get("autoSuspendAfterSeconds"),
            "metadata": body.get("metadata") or {}, "guestUser": body.get("guestUser") or "user",
            "createdAt": _iso(), "updatedAt": _iso(),
            "_root": str(self.root / name),
        }
        Path(machine["_root"]).mkdir(parents=True, exist_ok=True)
        if snapshot is not None:
            # A snapshot carries the machine's files: seed the new machine from it.
            saved = Path(snapshot["_dir"])
            if saved.is_dir():
                shutil.copytree(saved, machine["_root"], dirs_exist_ok=True)
        self.machines[machine["id"]] = machine

        def apply() -> None:
            machine.update(state="running", runtime={"ready": True})

        return 202, self._new_op("create", machine, apply)

    def _save_snapshot(self, machine: dict, body: dict):
        name = str(body.get("name") or "").strip()
        if not name:
            return self._error(400, "bad_request", "name is required")
        if any(s["name"] == name for s in self.snapshots.values()):
            return self._error(400, "bad_request", f"a snapshot named {name!r} already exists")
        snap_id = "sn_" + uuid.uuid4().hex[:26]
        snap_dir = self.root.parent / "snapshots" / snap_id
        shutil.copytree(machine["_root"], snap_dir, dirs_exist_ok=True)
        snapshot = {
            "id": snap_id, "name": name, "status": "ready",
            "sourceMachineId": machine["id"], "sourceSnapshotSeq": 1,
            "template": dict(machine["template"]),
            "rootSizeBytes": 1024, "dataSizeBytes": 1024, "createdAt": _iso(),
            "_dir": str(snap_dir),
        }
        self.snapshots[snap_id] = snapshot
        return 201, self.public(snapshot)

    def _lifecycle(self, machine: dict, action: str):
        target = {"start": "running", "stop": "stopped", "suspend": "suspended", "resume": "running"}[action]
        if machine["state"] == target and (machine.get("runtime") or {}).get("ready"):
            return self._error(409, "operation_conflict", f"machine is already {target}")
        if machine["state"] == "deleting":
            return self._error(409, "operation_conflict", "machine is being deleted")
        machine["state"] = {"start": "starting", "stop": "stopping",
                            "suspend": "suspending", "resume": "resuming"}[action]

        def apply() -> None:
            machine.update(state=target, runtime={"ready": target == "running"})

        return 202, self._new_op(action, machine, apply)

    def _delete(self, machine: dict):
        machine["state"] = "deleting"

        def apply() -> None:
            self.machines.pop(machine["id"], None)

        return 202, self._new_op("delete", machine, apply)

    def _operation(self, op_id: str):
        entry = self.ops.get(op_id)
        if not entry:
            return self._error(404, "not_found", f"no operation {op_id}")
        op = entry["op"]
        if op["state"] in {"queued", "running"}:
            self._polls[op_id] = self._polls.get(op_id, 0) + 1
            if self._polls[op_id] >= 2:
                try:
                    entry["apply"]()
                    op.update(state="succeeded", updatedAt=_iso())
                    machine = self.machines.get(op["machineId"])
                    if machine:
                        machine["operationId"] = None
                        op["machine"] = self.public(machine)
                except Exception as exc:  # pragma: no cover - surfaced to the client
                    op.update(state="failed", error={"code": "internal", "message": str(exc)})
            else:
                op.update(state="running", phase="working", updatedAt=_iso())
        return 200, op

    def _exec(self, machine: dict, body: dict):
        if machine["state"] != "running" or not (machine.get("runtime") or {}).get("ready"):
            return self._error(409, "machine_not_running", "machine is stopped; start it first")
        cmd, argv = body.get("cmd"), body.get("argv")
        if (cmd is None) == (argv is None):
            return self._error(400, "bad_request", "set exactly one of cmd or argv")
        cwd = body.get("cwd") or machine["_root"]
        env = {k: v for k, v in os.environ.items() if k != "INSTABOX_API_KEY"}
        env["HOME"] = machine["_root"]
        env.update({str(k): str(v) for k, v in (body.get("envs") or {}).items()})
        args = ["bash", "-lc", cmd] if cmd is not None else [str(a) for a in argv]
        timeout = int(body.get("timeoutSeconds") or 60)
        started = time.monotonic()
        try:
            proc = subprocess.run(args, cwd=cwd, env=env, input=body.get("stdin") or "",
                                  capture_output=True, text=True, timeout=timeout, check=False)
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            code, timed_out = 124, True
        except FileNotFoundError as exc:
            stdout, stderr, code, timed_out = "", str(exc), 127, False
        truncated = False
        if len(stdout) > _MAX_STDOUT:
            stdout, truncated = stdout[:_MAX_STDOUT], True
        return 200, {"stdout": stdout, "stderr": stderr, "exitCode": code,
                     "timedOut": timed_out, "truncated": truncated,
                     "durationMs": int((time.monotonic() - started) * 1000)}

    def _put_file(self, body: bytes, path: str, query: dict) -> tuple[int, dict]:
        target = Path(path)
        if not target.parent.is_dir():
            if query.get("parents") in ("true", "1"):
                target.parent.mkdir(parents=True, exist_ok=True)
            else:
                return self._error(404, "missing_parent", f"no directory {target.parent}")
        existed = target.exists()
        target.write_bytes(body)
        if query.get("mode"):
            os.chmod(target, int(str(query["mode"]), 8))
        return (200 if existed else 201), {"path": str(target), "size": len(body),
                                           "sha256": hashlib.sha256(body).hexdigest(),
                                           "mode": query.get("mode") or "0644", "created": not existed}

    def _get_file(self, path: str) -> tuple[int, dict]:
        target = Path(path)
        if target.is_dir():
            entries = [{"name": p.name, "type": "dir" if p.is_dir() else "file",
                        "size": 0 if p.is_dir() else p.stat().st_size, "mode": "0644",
                        "modTime": _iso()} for p in sorted(target.iterdir())[:5000]]
            return 200, {"path": str(target), "type": "dir", "size": 0, "mode": "0755",
                         "modTime": _iso(), "entries": entries}
        if not target.is_file():
            return self._error(404, "not_found", f"no file {target}")
        return 200, {"path": str(target), "type": "file", "size": target.stat().st_size,
                     "mode": "0644", "modTime": _iso(),
                     "content": target.read_text("utf-8", "replace")[:256 * 1024]}

    # -- routing ---------------------------------------------------------------------------
    def route(self, method: str, path: str, query: dict, headers: dict, body: bytes):
        with self._lock:
            self.requests.append({"method": method, "path": path, "query": query,
                                  "headers": {k.lower(): v for k, v in headers.items()}, "body": body})
            parts = [p for p in path.strip("/").split("/") if p]
            if parts[:2] == ["v1", "snapshots"]:
                if len(parts) == 2 and method == "GET":
                    return 200, {"snapshots": [self.public(s) for s in self.snapshots.values()]}
                if len(parts) == 3:
                    snapshot = self.snapshots.get(parts[2]) or next(
                        (s for s in self.snapshots.values() if s["name"] == parts[2]), None)
                    if snapshot is None:
                        return self._error(404, "not_found", f"no snapshot {parts[2]}")
                    if method == "GET":
                        return 200, self.public(snapshot)
                    if method == "DELETE":
                        if not headers.get("Idempotency-Key"):
                            return self._error(400, "bad_request", "Idempotency-Key header is required")
                        self.snapshots.pop(snapshot["id"], None)
                        return 204, b""
            if parts[:2] == ["v1", "machines"]:
                if len(parts) == 2:
                    if method == "GET":
                        machines = list(self.machines.values())
                        wanted = query.get("metadata")
                        if wanted and ":" in wanted:
                            key, _, value = wanted.partition(":")
                            machines = [m for m in machines
                                        if str((m.get("metadata") or {}).get(key)) == value]
                        return 200, {"machines": [self.public(m) for m in machines], "nextCursor": None}
                    if method == "POST":
                        if not headers.get("Idempotency-Key"):
                            return self._error(400, "bad_request", "Idempotency-Key header is required")
                        return self._create_machine(_json(body))
                ref = parts[2]
                machine = self.machine(ref)
                if machine is None:
                    return self._error(404, "not_found", f"no machine {ref}")
                if len(parts) == 3:
                    if method == "GET":
                        return 200, self.public(machine)
                    if method == "DELETE":
                        if not headers.get("Idempotency-Key"):
                            return self._error(400, "bad_request", "Idempotency-Key header is required")
                        return self._delete(machine)
                if len(parts) == 4 and method == "POST":
                    action = parts[3]
                    if action in {"start", "stop", "suspend", "resume"}:
                        if not headers.get("Idempotency-Key"):
                            return self._error(400, "bad_request", "Idempotency-Key header is required")
                        return self._lifecycle(machine, action)
                    if action == "exec":
                        return self._exec(machine, _json(body))
                    if action == "snapshots":
                        if not headers.get("Idempotency-Key"):
                            return self._error(400, "bad_request", "Idempotency-Key header is required")
                        return self._save_snapshot(machine, _json(body))
                if len(parts) == 4 and parts[3] == "files":
                    if method == "PUT":
                        return self._put_file(body, query.get("path", ""), query)
                    if method == "GET":
                        return self._get_file(query.get("path", "/"))
                if len(parts) == 5 and parts[3:] == ["files", "content"] and method == "GET":
                    target = Path(query.get("path", ""))
                    if target.is_dir():
                        return self._error(409, "not_a_file", "path is a directory")
                    if not target.is_file():
                        return self._error(404, "not_found", f"no file {target}")
                    return 200, target.read_bytes()
            if parts[:2] == ["v1", "operations"] and len(parts) == 3 and method == "GET":
                return self._operation(parts[2])
            return self._error(404, "not_found", f"no route {method} {path}")


def _json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        return {}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # keep pytest output clean
        return None

    def _handle(self, method: str) -> None:
        mock: MockInstabox = self.server.mock  # type: ignore[attr-defined]
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, payload = mock.route(method, parsed.path, query, dict(self.headers), body)
        if isinstance(payload, (dict, list)):
            raw = json.dumps(payload).encode("utf-8")
            ctype = "application/json"
        else:
            raw = payload or b""
            ctype = "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")
