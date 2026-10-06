import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_bridge.auth import Forbidden, Principal
from agent_bridge.evidence import Evidence
from agent_bridge.files import Files
from agent_bridge.leases import Leases, LeaseConflict
from agent_bridge.mailbox import Mailbox
from agent_bridge.runtime import Runtime

A, B = Principal("a", False), Principal("b", False)
ADMIN = Principal("operator", True)


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    evidence = Evidence(Mailbox(store=tmp_path / "leases.sqlite3"))
    leases = Leases(evidence, Files({"repo": root}))
    sa = evidence.register(A, {"harness": "test", "conversation_ref": "a"})["id"]
    sb = evidence.register(B, {"harness": "test", "conversation_ref": "b"})["id"]
    return evidence, leases, sa, sb, root


def test_overlaps_case_renewal_release_and_expiry(setup, monkeypatch):
    evidence, leases, sa, sb, root = setup
    first = leases.acquire(A, sa, "repo", ".", ["src/Main.py"])
    for path in ("SRC/main.PY", "src", "."):
        with pytest.raises(LeaseConflict) as exc:
            leases.acquire(B, sb, "repo", ".", [path])
        assert exc.value.holders[0]["id"] == first["id"]
    with pytest.raises(Forbidden):
        leases.change(B, first["id"])
    assert leases.change(A, first["id"], 1000)["expires_at"] > first["expires_at"]
    released = leases.change(A, first["id"])
    assert leases.change(A, first["id"])["released_at"] == released["released_at"]
    second = leases.acquire(B, sb, "repo", ".", ["src"])
    again = Leases(Evidence(Mailbox(store=evidence.box._store)), Files({"repo": root}))
    assert again.list(A)[0]["id"] == second["id"]
    monkeypatch.setattr("agent_bridge.leases.time.time", lambda: second["expires_at"] + 1)
    assert leases.list(A) == []
    with pytest.raises(ValueError, match="expired"):
        leases.change(B, second["id"], 10)
    leases.acquire(A, sa, "repo", ".", ["src"])


def test_symlink_alias_and_invalid_paths(setup):
    evidence, leases, sa, sb, root = setup
    (root / "actual").mkdir()
    try:
        (root / "alias").symlink_to(root / "actual", target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit symlinks")
    leases.acquire(A, sa, "repo", ".", ["actual/file"])
    with pytest.raises(LeaseConflict):
        leases.acquire(B, sb, "repo", ".", ["alias/file"])
    for path in ("../outside", "/absolute", "C:\\escape", "src/**"):
        with pytest.raises(ValueError):
            leases.acquire(B, sb, "repo", ".", [path])


def test_concurrent_work_claims_are_exclusive_and_require_operator_recovery(setup):
    evidence, leases, sa, sb, root = setup
    m = evidence.box.post("a", "b", "assignment", ack_required=True)
    one = Runtime(evidence)
    two = Runtime(Evidence(Mailbox(store=evidence.box._store)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: pair[0].claim(B, m.uid, pair[1]), [(one, "one"), (two, "two")]))
    assert sum(r["acquired"] for r in results) == 1
    with pytest.raises(Forbidden):
        one.claim(B, m.uid, "one", "reset", {"reason": "inspect"})
    winner = next(r["claim"]["worker_id"] for r in results if r["acquired"])
    with pytest.raises(ValueError):
        one.claim(B, m.uid, "other", "completed", {"artifact_ref": "result:1"})
    one.claim(ADMIN, m.uid, winner, "reset", {"reason": "process stopped"})
    assert two.claim(B, m.uid, "replacement")["acquired"]
    two.claim(B, m.uid, "replacement", "completed", {"artifact_ref": "result:2"})
    assert not one.claim(B, m.uid, "third")["acquired"]
