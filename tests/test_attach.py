"""Attaching to an existing machine instead of creating one."""

from __future__ import annotations

import pytest

from instabox.client import InstaboxClient, InstaboxError
from instabox.provider import InstaboxProvider

from mock_api import MockInstabox


@pytest.fixture
def server(tmp_path, monkeypatch):
    srv = MockInstabox(tmp_path / "vm").start()
    monkeypatch.setenv("INSTABOX_API_KEY", "ibk_" + "a" * 52)
    monkeypatch.setenv("INSTABOX_API_URL", srv.url)
    monkeypatch.delenv("TERMINAL_INSTABOX_MACHINE", raising=False)
    configure(monkeypatch)
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def client(server):
    return InstaboxClient("ibk_" + "a" * 52, base_url=server.url)


def configure(monkeypatch, **terminal):
    monkeypatch.setattr("instabox.provider._terminal_config",
                        lambda: {"instabox_file_sync": False, **terminal})


def make_env(*, task_id="default"):
    return InstaboxProvider().create_environment(
        cwd="~", timeout=30, task_id=task_id,
        container_config={"container_persistent": True})


def existing_machine(client, name="mine", *, state="running"):
    op = client.create_machine(name=name, template="agent-browser")
    machine = client.run_operation(op)["machine"]
    if state != "running":
        client.run_operation(client.machine_action(machine["id"], "suspend"))
    return machine["id"]


def create_requests(server):
    return [r for r in server.requests if r["method"] == "POST" and r["path"] == "/v1/machines"]


def test_attach_by_name_creates_nothing(server, client, monkeypatch):
    machine_id = existing_machine(client, "mine")
    before = len(create_requests(server))

    configure(monkeypatch, instabox_machine="mine")
    env = make_env(task_id="attached")
    try:
        assert env.machine_id == machine_id
        assert len(create_requests(server)) == before, "attach must not create a machine"
        assert "mine" in env.execute("pwd")["output"] or env.execute("true")["returncode"] == 0
    finally:
        env.cleanup()
    # It was running before and is left running.
    assert server.machines[machine_id]["state"] == "running"


def test_attach_by_id_and_restore_suspended(server, client, monkeypatch):
    machine_id = existing_machine(client, "paused", state="suspended")
    configure(monkeypatch, instabox_machine=machine_id)

    env = make_env(task_id="attached-id")
    try:
        assert env.machine_id == machine_id
        assert env.execute("echo attached-ok")["returncode"] == 0
    finally:
        env.cleanup()
    assert server.machines[machine_id]["state"] == "suspended"


def test_attach_missing_machine_is_actionable(server, monkeypatch):
    configure(monkeypatch, instabox_machine="ghost")
    with pytest.raises(InstaboxError) as info:
        make_env(task_id="ghost")
    message = str(info.value)
    assert "'ghost' not found" in message
    assert "instabox ls" in message
    assert not create_requests(server), "a missing attach must not fall back to creating"


def test_attach_ignores_snapshot_and_template(server, client, monkeypatch):
    machine_id = existing_machine(client, "mine")
    configure(monkeypatch, instabox_machine="mine", instabox_snapshot="does-not-exist",
              instabox_template="universal")
    env = make_env(task_id="attached-snap")
    try:
        assert env.machine_id == machine_id
    finally:
        env.cleanup()
