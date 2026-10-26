"""Snapshots: save a machine, then start new ones from it via plugin config."""

from __future__ import annotations

import json

import pytest

from instabox.client import InstaboxClient, InstaboxError
from instabox.provider import InstaboxProvider, configured_snapshot

from mock_api import MockInstabox


@pytest.fixture
def server(tmp_path, monkeypatch):
    srv = MockInstabox(tmp_path / "vm").start()
    monkeypatch.setenv("INSTABOX_API_KEY", "ibk_" + "a" * 52)
    monkeypatch.setenv("INSTABOX_API_URL", srv.url)
    monkeypatch.delenv("TERMINAL_INSTABOX_SNAPSHOT", raising=False)
    configure(monkeypatch)
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def client(server):
    return InstaboxClient("ibk_" + "a" * 52, base_url=server.url)


def configure(monkeypatch, **terminal):
    """Point the provider at this mock's terminal config (file sync off keeps the tests quick)."""
    monkeypatch.setattr("instabox.provider._terminal_config",
                        lambda: {"instabox_file_sync": False, **terminal})


def make_env(*, task_id="default", persistent=True):
    return InstaboxProvider().create_environment(
        cwd="~", timeout=30, task_id=task_id,
        container_config={"container_persistent": persistent})


def last_create_body(server: MockInstabox) -> dict:
    bodies = [json.loads(r["body"]) for r in server.requests
              if r["method"] == "POST" and r["path"] == "/v1/machines"]
    assert bodies, "no machine create was sent"
    return bodies[-1]


def bake(client: InstaboxClient, task_id: str, name: str, *, template: str | None = None) -> dict:
    """Run a machine, write a marker file, save it as a snapshot."""
    env = make_env(task_id=task_id)
    try:
        assert env.execute("echo baked-in > baked.txt")["returncode"] == 0
        return client.create_snapshot(env.machine_id, name=name,
                                      description="test snapshot")
    finally:
        env.cleanup()


def test_snapshot_is_off_by_default(server):
    assert configured_snapshot() is None


def test_snapshot_client_roundtrip(server, client):
    snapshot = bake(client, "src", "baked")
    assert snapshot["status"] == "ready"
    assert snapshot["name"] == "baked"

    assert client.get_snapshot(snapshot["id"])["name"] == "baked"
    assert [s["name"] for s in client.list_snapshots()] == ["baked"]
    # Resolution accepts the human name as well as the ID.
    assert client.resolve_snapshot("baked")["id"] == snapshot["id"]
    assert client.resolve_snapshot(snapshot["id"])["id"] == snapshot["id"]

    client.delete_snapshot(snapshot["id"])
    assert client.list_snapshots() == []


def test_machine_starts_from_configured_snapshot(server, client, monkeypatch):
    snapshot = bake(client, "src", "baked")
    configure(monkeypatch, instabox_snapshot="baked")

    env = make_env(task_id="from-snapshot")
    try:
        # Files under /data (home included) travel with the snapshot.
        assert "baked-in" in env.execute("cat baked.txt")["output"]
        body = last_create_body(server)
        assert body["snapshotId"] == snapshot["id"]
        assert "templateId" not in body, "InstaBox takes a snapshot or a template, never both"
        assert server.machines[env.machine_id]["template"]["id"] == snapshot["template"]["id"]
    finally:
        env.cleanup()


def test_snapshot_can_be_given_by_id(server, client, monkeypatch):
    snapshot = bake(client, "src", "baked")
    configure(monkeypatch, instabox_snapshot=snapshot["id"])
    env = make_env(task_id="by-id")
    try:
        assert "baked-in" in env.execute("cat baked.txt")["output"]
        assert last_create_body(server)["snapshotId"] == snapshot["id"]
    finally:
        env.cleanup()


def test_snapshot_overrides_the_configured_template(server, client, monkeypatch):
    bake(client, "src", "baked")
    configure(monkeypatch, instabox_snapshot="baked", instabox_template="universal")
    env = make_env(task_id="precedence")
    try:
        body = last_create_body(server)
        assert body["snapshotId"]
        assert "templateId" not in body
    finally:
        env.cleanup()


def test_snapshot_bypasses_the_free_plan_template_rule(server, client, monkeypatch):
    """Free may create only from agent-browser — or from a snapshot of anything."""
    configure(monkeypatch, instabox_template="universal")
    bake(client, "src", "baked")
    server.allowed_templates = {"agent-browser"}  # a free workspace from here on
    configure(monkeypatch, instabox_snapshot="baked", instabox_template="universal")

    env = make_env(task_id="free-snapshot")
    try:
        assert env.machine_id in server.machines
        assert server.machines[env.machine_id]["template"]["id"] == "universal"
    finally:
        env.cleanup()


def test_unknown_snapshot_is_actionable(server, monkeypatch):
    configure(monkeypatch, instabox_snapshot="nope")
    with pytest.raises(InstaboxError) as info:
        make_env(task_id="missing")
    message = str(info.value)
    assert "'nope' not found" in message
    assert "instabox snapshot ls" in message or "Available snapshots" in message


def test_unknown_snapshot_lists_the_available_ones(server, client, monkeypatch):
    bake(client, "src", "baked")
    configure(monkeypatch, instabox_snapshot="nope")
    with pytest.raises(InstaboxError) as info:
        make_env(task_id="missing")
    assert "Available snapshots: baked" in str(info.value)


def test_snapshot_not_ready_is_actionable(server, monkeypatch):
    server.snapshots["sn_pending000000000000000000"] = {
        "id": "sn_pending000000000000000000", "name": "half", "status": "pending",
        "sourceMachineId": "m_source", "template": {"id": "agent-browser"},
        "rootSizeBytes": 0, "dataSizeBytes": 0, "createdAt": "2026-10-25T00:00:00Z",
        "_dir": str(server.root.parent / "snapshots" / "sn_pending000000000000000000"),
    }
    configure(monkeypatch, instabox_snapshot="half")
    with pytest.raises(InstaboxError) as info:
        make_env(task_id="pending")
    message = str(info.value)
    assert "pending" in message
    assert "ready" in message
