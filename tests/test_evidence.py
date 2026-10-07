import pytest

pytestmark = pytest.mark.evidence

from agent_bridge.auth import Forbidden, Principal
from agent_bridge.evidence import Evidence
from agent_bridge.mailbox import Mailbox

A = Principal("a", False)
B = Principal("b", False)
C = Principal("c", False)
ADMIN = Principal("operator", True)


@pytest.fixture
def evidence(tmp_path):
    box = Mailbox(store=tmp_path / "evidence.sqlite3")
    return Evidence(box)


def test_assignment_links_conversations_checkins_and_verified_artifact(evidence):
    m = evidence.box.post("a", "b", "fix", authenticated_principal="a")
    sa = evidence.register(A, {"harness": "test", "conversation_ref": "conversation:a", "configured_model": "reported"})
    sb = evidence.register(B, {"harness": "test", "conversation_ref": "conversation:b"})
    evidence.link(A, m.uid, "originated_in", "session", sa["id"])
    evidence.link(B, m.uid, "followed_up_in", "session", sb["id"])
    evidence.link(B, m.uid, "followed_up_in", "checkin", "checkin:1", inferred=True)
    evidence.outcome(B, m.uid, "completed", "commit:abc")
    evidence.outcome(B, m.uid, "verified", "commit:abc", ["test-run:123"])
    evidence.outcome(A, m.uid, "accepted", "commit:abc", ["review:456"])
    record = evidence.inspect(B, m.uid)
    assert len(record["links"]) == 3
    assert record["links"][-1]["inferred"]
    assert record["outcomes"][-1]["artifact_ref"] == "commit:abc"
    assert not sa["model_attested"]
    assert len(evidence.sessions(A)) == 1
    assert len(evidence.sessions(ADMIN)) == 2
    again = Evidence(Mailbox(store=evidence.box._store))
    assert again.inspect(A, m.uid)["outcomes"] == record["outcomes"]


def test_links_and_session_context_do_not_grant_access(evidence):
    m = evidence.box.post("a", "b", "private")
    sa = evidence.register(A, {"harness": "test", "conversation_ref": "secret"})
    for action in (lambda: evidence.inspect(C, m.uid), lambda: evidence.link(C, m.uid, "supports", "artifact", "x"), lambda: evidence.outcome(C, m.uid, "completed"), lambda: evidence.session(B, sa["id"])):
        with pytest.raises(Forbidden):
            action()
    assert evidence.sessions(C) == []
    assert "secret" not in str(evidence.directory("a"))


def test_acceptance_requires_assigner_and_current_verified_artifact(evidence):
    m = evidence.box.post("a", "b", "fix", authenticated_principal="a")
    with pytest.raises(ValueError, match="evidence_refs"):
        evidence.outcome(B, m.uid, "verified", "commit:abc")
    with pytest.raises(Forbidden):
        evidence.outcome(B, m.uid, "accepted", "commit:abc", ["test:1"])
    evidence.outcome(B, m.uid, "verified", "commit:abc", ["test:1"])
    with pytest.raises(ValueError, match="verified artifact"):
        evidence.outcome(A, m.uid, "accepted", "commit:different", ["review:1"])
    evidence.outcome(A, m.uid, "accepted", "commit:abc", ["review:1"])
    evidence.outcome(B, m.uid, "reopened", details={"reason": "new edits"})
    with pytest.raises(ValueError, match="verified artifact"):
        evidence.outcome(A, m.uid, "accepted", "commit:abc", ["review:1"])


def test_bad_registration_and_relation_are_rejected(evidence):
    with pytest.raises(ValueError):
        evidence.register(A, {"harness": "test", "conversation_ref": "x", "model_attested": True})
    m = evidence.box.post("a", "b", "fix")
    with pytest.raises(ValueError):
        evidence.link(A, m.uid, "grants_authority", "conversation", "x")


def test_reverse_links_return_only_visible_message_evidence(evidence):
    visible = evidence.box.post("a", "b", "shared")
    private = evidence.box.post("c", "c", "private")
    evidence.link(A, visible.uid, "followed_up_in", "checkin", "checkin:shared")
    evidence.link(C, private.uid, "followed_up_in", "checkin", "checkin:shared")
    assert [r["message_id"] for r in evidence.find_links(B, "checkin", "checkin:shared")] == [visible.uid]
