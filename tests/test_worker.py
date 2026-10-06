import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_bridge.config import Config
from agent_bridge.server import build
from agent_bridge.worker import Worker, exclusive_lock, BridgeClient


class Client:
    def __init__(self, http):
        self.http = http
        self.fail_ack = False

    def request(self, path, data=None):
        if path == "/api/ack" and self.fail_ack:
            self.fail_ack = False
            raise OSError("simulated network failure")
        response = self.http.get(path, headers={"Authorization": "Bearer role"}) if data is None else self.http.post(path, json=data, headers={"Authorization": "Bearer role"})
        if response.status_code != 200:
            raise ValueError(response.json())
        return response.json()


@pytest.fixture
def client(tmp_path):
    app = build(Config({"token": "operator", "self_name": "host", "agents": {"worker": {"token": "role"}}, "mailbox_store": str(tmp_path / "bridge.sqlite3"), "allowed_hosts": ["testserver:*"]}))
    with TestClient(app) as http:
        yield Client(http)


def send(client, text="assignment"):
    response = client.http.post("/api/send", json={"to": "worker", "text": text, "ack_required": True}, headers={"Authorization": "Bearer operator"})
    return client.http.get("/api/history", headers={"Authorization": "Bearer role"}).json()["messages"][-1]


def harness(counter):
    script = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print(sys.stdin.read())"
    return [sys.executable, "-c", script, str(counter)]


def test_real_process_handoff_result_and_ack_retry_survive_restart(client, tmp_path):
    m = send(client, "assignment; $(do not execute)")
    counter = tmp_path / "launches"
    state = tmp_path / "state"
    w = Worker(client, state, harness(counter), tmp_path)
    client.fail_ack = True
    with pytest.raises(OSError):
        w.process(m)
    assert counter.read_text() == "x"
    assert w.db.execute("SELECT status FROM jobs").fetchone()[0] == "completed"
    w.db.close()
    resumed = Worker(client, state, harness(counter), tmp_path)
    assert resumed.process(m) == "acknowledged"
    assert counter.read_text() == "x"  # never launches the completed job twice
    assert "do not execute" in (state / (m["uid"] + ".log")).read_text()
    assert client.request("/api/inbox?peek=1")["count"] == 0
    resumed.db.close()


def test_different_workers_never_launch_same_assignment(client, tmp_path):
    m = send(client)
    w = Worker(client, tmp_path / "one", harness(tmp_path / "one-count"), tmp_path)
    w.claim(m)
    w.save(m, "running", 1)
    w.db.close()
    restarted = Worker(client, tmp_path / "one", harness(tmp_path / "one-count"), tmp_path)
    assert restarted.process(m) == "uncertain"
    other = Worker(client, tmp_path / "two", harness(tmp_path / "two-count"), tmp_path)
    assert other.process(m) == "claimed_elsewhere"
    assert not (tmp_path / "two-count").exists()
    assert client.request("/api/inbox?peek=1")["count"] == 1
    restarted.db.close()
    other.db.close()


def test_failed_and_timed_out_processes_leave_mail_pending(client, tmp_path):
    m = send(client)
    failed = Worker(client, tmp_path / "failed", [sys.executable, "-c", "raise SystemExit(1)"], tmp_path, max_attempts=1)
    assert failed.process(m) == "failed"
    assert failed.process(m) == "failed"
    slow = Worker(client, tmp_path / "slow", [sys.executable, "-c", "import time;time.sleep(2)"], tmp_path, timeout_s=0.05)
    assert slow.process(m) == "uncertain"
    assert client.request("/api/inbox?peek=1")["count"] == 1
    failed.db.close()
    slow.db.close()


def test_lock_and_endpoint_validation(tmp_path):
    with exclusive_lock(tmp_path / "worker.lock"):
        with pytest.raises(ValueError, match="another worker"):
            with exclusive_lock(tmp_path / "worker.lock"):
                pass
    for url in ("file:///tmp", "http://name:secret@example.com", "http://example.com/?token=secret"):
        with pytest.raises(ValueError):
            BridgeClient(url, "role")


def test_result_persistence_failure_is_uncertain_not_relaunched(client, tmp_path, monkeypatch):
    m = send(client)
    counter = tmp_path / "launches"
    w = Worker(client, tmp_path / "state", harness(counter), tmp_path)
    def fail_sync(fd):
        raise OSError("disk failure")
    monkeypatch.setattr("agent_bridge.worker.os.fsync", fail_sync)
    assert w.process(m) == "uncertain"
    assert w.process(m) == "uncertain"
    assert counter.read_text() == "x"
    assert client.request("/api/inbox?peek=1")["count"] == 1
    w.db.close()


def test_worker_state_cannot_switch_worktree_or_harness(client, tmp_path):
    state = tmp_path / "state"
    w = Worker(client, state, harness(tmp_path / "launches"), tmp_path)
    w.db.close()
    with pytest.raises(ValueError, match="another bridge"):
        Worker(client, state, [sys.executable, "-c", "print('changed')"], tmp_path)
