import pytest

pytestmark = pytest.mark.reports

from agent_bridge.auth import Forbidden, Principal
from agent_bridge.evidence import Evidence
from agent_bridge.mailbox import Mailbox
from agent_bridge.reports import Reports

A, B, C = [Principal(n, False) for n in ("a", "b", "c")]


@pytest.fixture
def reports(tmp_path):
    return Reports(Evidence(Mailbox(store=tmp_path / "reports.sqlite3")))


def test_telemetry_distinguishes_delivery_acknowledgment_and_outcomes(reports):
    box = reports.box
    m = box.post("a", "b", "assignment", ack_required=True, authenticated_principal="a")
    box.post("c", "c", "private")
    box.delivered(m.uid, "b", "websocket")
    first = reports.telemetry(A, overdue_after_s=0.000001)
    assert first["messages"] == 1
    assert first["pending"] == first["overdue_unacknowledged"] == [m.uid]
    assert first["durations_s"]["acknowledgment"]["samples"] == 0
    box.acknowledge(m.uid, B)
    for reason in ("Missing build", " missing   BUILD "):
        reports.evidence.outcome(B, m.uid, "blocked", details={"reason": reason})
    reports.evidence.outcome(B, m.uid, "completed")
    reports.evidence.outcome(B, m.uid, "verified", "commit:1", ["test:1"])
    reports.evidence.outcome(A, m.uid, "accepted", "commit:1", ["review:1"])
    reports.evidence.outcome(B, m.uid, "reopened")
    result = reports.telemetry(A)
    assert result["pending"] == []
    assert result["blockers"] == {"missing build": 2}
    assert result["reopened_messages"] == [m.uid]
    assert all(v["samples"] == 1 for v in result["durations_s"].values())
    assert result["events"]["delivered"] == result["events"]["acknowledged"] == 1


def test_learning_keeps_evidence_reviews_and_later_outcomes(reports):
    m = reports.box.post("a", "b", "fix")
    contradictory = reports.box.post("a", "b", "counterexample")
    item = reports.propose(A, "Build before judging logs", [m.uid], ["test:1"], [contradictory.uid])
    assert item["status"] == "candidate"
    reviewed = reports.review(B, item["id"], "accepted", ["review:1"])
    assert reviewed["status"] == "accepted"
    reports.evidence.outcome(B, m.uid, "reopened", details={"reason": "counterexample"})
    assert reports.get(A, item["id"])["status"] == "needs_revision"
    again = Reports(Evidence(Mailbox(store=reports.box._store)))
    assert again.get(A, item["id"])["reviews"] == reviewed["reviews"]
    assert again.get(A, item["id"])["contradicting_message_ids"] == [contradictory.uid]
    assert reports.list(C) == []
    with pytest.raises(Forbidden):
        reports.review(C, item["id"], "accepted", ["review:2"])


def test_reports_reject_unbounded_or_missing_evidence(reports):
    for threshold in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            reports.telemetry(A, threshold)
    with pytest.raises(ValueError):
        reports.propose(A, "unsupported", [], [])
