"""End-to-end backend tests against a local fake of the InstaBox API.

The fake runs real commands with ``bash -lc`` inside a throwaway machine root, so everything below
exercises the whole stack: HTTP client, operation polling, session snapshot, cwd markers, stdin,
file upload/download and the base environment's execute() wrapping.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from instabox import environment as env_module
from instabox.client import InstaboxError
from instabox.provider import InstaboxProvider

from mock_api import MockInstabox


@pytest.fixture
def server(tmp_path, monkeypatch):
    srv = MockInstabox(tmp_path / "vm").start()
    monkeypatch.setenv("INSTABOX_API_KEY", "ibk_" + "a" * 52)
    monkeypatch.setenv("INSTABOX_API_URL", srv.url)
    # Mirroring the host's ~/.hermes into the fake machine is not what these tests are about.
    monkeypatch.setattr("instabox.provider.configured_file_sync", lambda: False)
    try:
        yield srv
    finally:
        srv.stop()


def make_env(server: MockInstabox, *, task_id: str = "default", persistent: bool = True, cwd: str = "~"):
    return InstaboxProvider().create_environment(
        cwd=cwd, timeout=30, task_id=task_id,
        container_config={"container_persistent": persistent})


def test_execute_roundtrip(server):
    env = make_env(server, task_id="exec")
    try:
        result = env.execute("echo hello")
        assert result["returncode"] == 0
        assert "hello" in result["output"]

        # stderr is merged at the source, and a non-zero exit code survives.
        result = env.execute("echo oops >&2; exit 3")
        assert result["returncode"] == 3
        assert "oops" in result["output"]
    finally:
        env.cleanup()


def test_cwd_persists_between_commands(server, tmp_path):
    env = make_env(server, task_id="cwd")
    try:
        home = Path(env.cwd)
        assert home.is_dir()
        (home / "sub").mkdir()
        env.execute("cd sub")
        assert Path(env.execute("pwd")["output"].strip()) == home / "sub"
        assert env.cwd == str(home / "sub")
    finally:
        env.cleanup()


def test_stdin_payload(server):
    env = make_env(server, task_id="stdin")
    try:
        result = env.execute("cat", stdin_data="piped through the exec API\n")
        assert result["returncode"] == 0
        assert "piped through the exec API" in result["output"]
    finally:
        env.cleanup()


def test_persistent_machine_is_reused_and_keeps_files(server):
    first = make_env(server, task_id="persist")
    machine_id = first.machine_id
    try:
        assert first.execute("echo kept > note.txt")["returncode"] == 0
    finally:
        first.cleanup()

    assert server.machines[machine_id]["state"] == "suspended"

    second = make_env(server, task_id="persist")
    try:
        assert second.machine_id == machine_id
        assert "kept" in second.execute("cat note.txt")["output"]
    finally:
        second.cleanup()


def test_ephemeral_machine_is_deleted(server):
    env = make_env(server, task_id="scratch", persistent=False)
    machine_id = env.machine_id
    assert machine_id in server.machines
    env.cleanup()
    assert machine_id not in server.machines


def test_timeout_is_enforced(server):
    env = make_env(server, task_id="timeout")
    try:
        started = time.monotonic()
        result = env.execute("sleep 30", timeout=2)
        elapsed = time.monotonic() - started
        assert result["returncode"] == 124
        assert elapsed < 10
    finally:
        env.cleanup()


def test_lifecycle_calls_are_idempotent_but_exec_is_not(server):
    env = make_env(server, task_id="idem")
    try:
        env.execute("true")
    finally:
        env.cleanup()
    lifecycle = [r for r in server.requests
                 if r["method"] in {"POST", "DELETE"} and "/exec" not in r["path"]]
    execs = [r for r in server.requests if r["path"].endswith("/exec")]
    assert lifecycle and all("idempotency-key" in r["headers"] for r in lifecycle)
    assert execs and all("idempotency-key" not in r["headers"] for r in execs)
    # timeoutSeconds is always inside the API's 1..600 window.
    assert all(1 <= int(__import__("json").loads(r["body"])["timeoutSeconds"]) <= 600 for r in execs)


def test_file_sync_uploads_host_cache(server, tmp_path, monkeypatch):
    source = tmp_path / "host-cache.txt"
    source.write_text("from the host\n")

    extra = tmp_path / "second.txt"
    extra.write_text("second\n")

    def fake_sync_files(_base):
        machine_root = Path(next(iter(server.machines.values()))["_root"])
        return [(str(source), f"{machine_root}/.hermes/cache/host-cache.txt"),
                (str(extra), f"{machine_root}/.hermes/cache/nested/second.txt")]

    monkeypatch.setattr(env_module, "iter_sync_files", fake_sync_files)
    monkeypatch.setattr("instabox.provider.configured_file_sync", lambda: True)

    env = make_env(server, task_id="sync")
    try:
        root = Path(env.cwd)
        assert (root / ".hermes/cache/host-cache.txt").read_text() == "from the host\n"
        # Parallel bulk upload creates missing parent directories server-side.
        assert (root / ".hermes/cache/nested/second.txt").read_text() == "second\n"
        puts = [r for r in server.requests if r["method"] == "PUT"]
        assert len(puts) == 1, f"the whole cycle should be one tarball upload, saw {len(puts)} PUTs"
    finally:
        env.cleanup()


def test_fetch_file_reads_through_the_exec_channel(server, tmp_path):
    env = make_env(server, task_id="fetch")
    try:
        env.execute("printf 'artifact\\n' > artifact.txt")
        dest = tmp_path / "artifact.txt"
        env.fetch_file(f"{env.cwd}/artifact.txt", dest, max_bytes=1024)
        assert dest.read_text() == "artifact\n"
    finally:
        env.cleanup()


def test_free_plan_quota_adopts_the_existing_machine(server):
    """Free allows one machine: a second persistent task shares it instead of failing."""
    server.max_machines = 1
    first = make_env(server, task_id="one")
    first_id = first.machine_id
    try:
        assert first.execute("echo shared > shared.txt")["returncode"] == 0
    finally:
        first.cleanup()

    second = make_env(server, task_id="two")
    try:
        assert second.machine_id == first_id
        assert "shared" in second.execute("cat shared.txt")["output"]
    finally:
        second.cleanup()


def test_ephemeral_quota_fails_with_a_way_out(server):
    """An ephemeral environment must never delete a machine another session is using."""
    server.max_machines = 1
    first = make_env(server, task_id="one")
    try:
        with pytest.raises(InstaboxError) as info:
            make_env(server, task_id="scratch", persistent=False)
        message = str(info.value)
        assert "machine limit" in message
        assert "container_persistent" in message
        assert "instabox rm" in message
        # Nothing was adopted, and the first session's machine is still there.
        assert first.machine_id in server.machines
    finally:
        first.cleanup()


def test_template_not_allowed_says_what_to_change(server, monkeypatch):
    server.allowed_templates = {"agent-browser"}
    monkeypatch.setattr("instabox.provider.configured_template", lambda: "universal")
    with pytest.raises(InstaboxError) as info:
        make_env(server, task_id="tpl")
    message = str(info.value)
    assert "agent-browser" in message
    assert "terminal.instabox_template" in message
