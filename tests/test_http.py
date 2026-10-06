#
#  agent-bridge-mcp - Copyright(c) 2026
#

# The auth boundary, exercised over HTTP rather than by calling functions. A
# regression here is a security bug, and the unit tests cannot see it.

from pathlib import Path

import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse

from agent_bridge.config import Config
from agent_bridge.server import build

TOKEN = "test-token-not-secret"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("hello\n")
    cfg = Config({
        "self_name": "here",
        "token": TOKEN,
        "roots": {"proj": str(root)},
        "mailbox_store": "",
        "allowed_hosts": ["testserver:*", "localhost:*"],
    })
    with TestClient(build(cfg)) as c:
        yield c


# --- header vs query string --------------------------------------------------
#
# Regression: the query-string token was accepted on every route, so it worked
# in a plain GET and landed in access logs. It is for the WebSocket only.

def test_health_needs_no_token_and_says_only_what_reachability_needs(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    # S7: names, unread counts and roots are behind auth. An unauthenticated
    # caller learns how to diagnose reachability and nothing about traffic.
    assert set(r.json()) == {"ok", "self", "host_seen", "allowed_hosts", "host_allowed", "your_address"}
    assert client.get("/api/agents").status_code == 401
    assert client.get("/api/agents", headers=BEARER).status_code == 200


def test_rest_refuses_without_token(client):
    assert client.get("/api/inbox?agent=x").status_code == 401
    assert client.post("/api/send", json={"to": "x", "text": "hi"}).status_code == 401


def test_rest_accepts_the_header(client):
    assert client.get("/api/inbox?agent=x", headers=BEARER).status_code == 200


def test_rest_refuses_the_token_in_the_query_string(client):
    assert client.get(f"/api/inbox?agent=x&token={TOKEN}").status_code == 401
    assert client.post(f"/api/send?token={TOKEN}",
                       json={"to": "x", "text": "hi"}).status_code == 401


def test_websocket_accepts_the_token_as_a_subprotocol(client):
    with client.websocket_connect("/notify?agent=x",
                                  subprotocols=["bridge", f"bearer.{TOKEN}"]) as ws:
        assert ws.accepted_subprotocol == "bridge"
        r = client.post("/api/send", json={"sender": "a", "to": "x", "text": "ping"},
                        headers=BEARER)
        assert r.status_code == 200
        assert ws.receive_text().endswith("a -> x: ping")


def test_a_live_frame_is_consumed_and_not_replayed_on_reconnect(client):
    # Regression: only the backlog was marked read after send. A message
    # delivered live stayed unread, so every reconnect replayed it as
    # "[unread backlog]".
    with client.websocket_connect("/notify?agent=x", headers=BEARER) as ws:
        client.post("/api/send", json={"sender": "a", "to": "x", "text": "live one"},
                    headers=BEARER)
        assert ws.receive_text().endswith("a -> x: live one")

    assert client.get("/api/inbox?agent=x&peek=1", headers=BEARER).json()["count"] == 0
    # Reconnect: nothing pending, so a fresh message is the first frame.
    with client.websocket_connect("/notify?agent=x", headers=BEARER) as ws:
        client.post("/api/send", json={"sender": "a", "to": "x", "text": "second"},
                    headers=BEARER)
        frame = ws.receive_text()
        assert "second" in frame and "backlog" not in frame


def test_agent_name_is_normalised_on_the_socket(client):
    with client.websocket_connect("/notify?agent=X%20", headers=BEARER) as ws:
        client.post("/api/send", json={"sender": "a", "to": "x", "text": "trim"},
                    headers=BEARER)
        assert "trim" in ws.receive_text()
    assert client.get("/api/inbox?agent=x&peek=1", headers=BEARER).json()["count"] == 0


def test_a_wildcard_listener_does_not_consume_another_agents_mail(client):
    with client.websocket_connect("/notify?agent=*", headers=BEARER) as ws:
        client.post("/api/send", json={"sender": "a", "to": "y", "text": "for y"},
                    headers=BEARER)
        assert "for y" in ws.receive_text()
    # y still has it.
    assert client.get("/api/inbox?agent=y&peek=1", headers=BEARER).json()["count"] == 1


def test_websocket_refuses_the_token_in_the_query_string(client):
    with pytest.raises(WebSocketDenialResponse) as exc:
        with client.websocket_connect(f"/notify?agent=x&token={TOKEN}"):
            pass
    assert exc.value.status_code == 401


def test_websocket_still_accepts_the_header(client):
    with client.websocket_connect("/notify?agent=x", headers=BEARER) as ws:
        client.post("/api/send", json={"sender": "a", "to": "x", "text": "ping"},
                    headers=BEARER)
        assert "ping" in ws.receive_text()


def test_websocket_refuses_a_bad_token_with_a_401_handshake(client):
    # A pre-accept close becomes an anonymous 403 by the time uvicorn is done
    # with it; denying the handshake is what a real client actually sees.
    with pytest.raises(WebSocketDenialResponse) as exc:
        with client.websocket_connect("/notify?agent=x&token=wrong"):
            pass
    assert exc.value.status_code == 401
    assert "Bearer" in exc.value.json()["hint"]


def test_401_body_says_where_the_token_goes(client):
    r = client.get(f"/api/inbox?agent=x&token={TOKEN}")
    assert r.status_code == 401
    assert "bearer." in r.json()["hint"]


def test_a_token_the_subprotocol_grammar_cannot_carry_is_refused_at_load():
    with pytest.raises(SystemExit, match="subprotocol"):
        Config({"token": "has a space"})
    with pytest.raises(SystemExit, match="subprotocol"):
        Config({"token": "has=equals"})
    Config({"token": "0123abcd-._~"})          # hex, base64url and unreserved: fine


# --- per-peer credentials (S1) --------------------------------------------------
#
# The token is the identity. A peer's token names the peer: it sends as itself
# whatever the body says, and it reads, waits on and listens to its own mailbox
# only. The admin token keeps the old, unrestricted semantics.

PEER = "peer-token-not-secret"
AS_PEER = {"Authorization": f"Bearer {PEER}"}


@pytest.fixture
def peered(tmp_path: Path):
    cfg = Config({
        "self_name": "here", "token": TOKEN, "roots": {}, "mailbox_store": "",
        "agents": {"sisyphus": {"token": PEER}},
        "allowed_hosts": ["testserver:*"],
    })
    with TestClient(build(cfg)) as c:
        yield c


def test_a_peer_sends_as_itself_whatever_the_body_says(peered):
    r = peered.post("/api/send", json={"sender": "here", "to": "x", "text": "hi"}, headers=AS_PEER)
    assert r.status_code == 200 and r.json()["sender"] == "sisyphus"
    # The admin may still name a sender, and defaults to this machine's name.
    r = peered.post("/api/send", json={"sender": "script", "to": "x", "text": "hi"}, headers=BEARER)
    assert r.json()["sender"] == "script"
    r = peered.post("/api/send", json={"to": "x", "text": "hi"}, headers=BEARER)
    assert r.json()["sender"] == "here"


def test_a_peer_reads_only_its_own_mailbox_over_rest(peered):
    peered.post("/api/send", json={"to": "sisyphus", "text": "yours"}, headers=BEARER)
    peered.post("/api/send", json={"to": "other", "text": "not yours"}, headers=BEARER)

    r = peered.get("/api/inbox?agent=other", headers=AS_PEER)
    assert r.status_code == 403 and "may not read the mailbox of 'other'" in r.json()["error"]
    assert peered.get("/api/wait?agent=other&timeout=1", headers=AS_PEER).status_code == 403

    # No agent means "me", for a peer.
    r = peered.get("/api/inbox", headers=AS_PEER)
    assert r.json()["agent"] == "sisyphus" and [m["text"] for m in r.json()["messages"]] == ["yours"]
    # The admin may read anyone's, and "other" still has its message.
    assert peered.get("/api/inbox?agent=other", headers=BEARER).json()["count"] == 1


def test_a_peer_listens_as_itself_and_may_not_listen_as_another(peered):
    with peered.websocket_connect("/notify", subprotocols=["bridge", f"bearer.{PEER}"]) as ws:
        peered.post("/api/send", json={"to": "sisyphus", "text": "ping"}, headers=BEARER)
        assert ws.receive_text().endswith("here -> sisyphus: ping")

    for other in ("other", "*"):
        with pytest.raises(WebSocketDenialResponse) as exc:
            with peered.websocket_connect(f"/notify?agent={other}", headers=AS_PEER):
                pass
        assert exc.value.status_code == 403


def test_the_admins_default_mailbox_is_normalised(tmp_path: Path):
    # Regression: Credentials kept self_name as written, so with "Fenrir" the
    # admin's /notify subscribed as "Fenrir", m.to == agent never matched
    # "fenrir", and every live frame came back as backlog on reconnect.
    cfg = Config({"self_name": "Fenrir", "token": TOKEN, "mailbox_store": "",
                  "allowed_hosts": ["testserver:*"]})
    with TestClient(build(cfg)) as c:
        with c.websocket_connect("/notify", headers=BEARER) as ws:
            c.post("/api/send", json={"sender": "a", "to": "fenrir", "text": "live"}, headers=BEARER)
            assert ws.receive_text().endswith("a -> fenrir: live")
        assert c.get("/api/inbox?agent=fenrir&peek=1", headers=BEARER).json()["count"] == 0
        assert c.get("/api/inbox", headers=BEARER).json()["agent"] == "fenrir"


def test_a_placeholder_admin_token_does_not_authenticate_when_peers_exist(tmp_path: Path):
    cfg = Config({"self_name": "here", "token": "CHANGE_ME", "mailbox_store": "",
                  "agents": {"sisyphus": {"token": PEER}}, "allowed_hosts": ["testserver:*"]})
    with TestClient(build(cfg)) as c:
        assert c.get("/api/inbox", headers={"Authorization": "Bearer CHANGE_ME"}).status_code == 401
        assert c.get("/api/inbox", headers=AS_PEER).status_code == 200


def test_a_clean_shutdown_flushes_the_coalesced_read_state(tmp_path: Path):
    from agent_bridge.mailbox import Mailbox
    store = tmp_path / "mailbox.json"
    cfg = Config({"self_name": "here", "token": TOKEN, "roots": {},
                  "mailbox_store": str(store), "allowed_hosts": ["testserver:*"],
                  "mailbox_debounce_s": 30})                 # so the timer cannot win the race
    with TestClient(build(cfg)) as c:
        c.post("/api/send", json={"sender": "a", "to": "x", "text": "hi"}, headers=BEARER)
        c.get("/api/inbox?agent=x", headers=BEARER)          # read: coalesced, not yet on disk
        assert Mailbox(store=store).unread_count("x") == 1
    # Leaving the context runs the lifespan shutdown, which must flush it.
    assert Mailbox(store=store).unread_count("x") == 0


def test_explicit_socket_delivery_replays_until_authorized_ack(peered):
    sent = peered.post("/api/send", json={"to": "sisyphus", "text": "durable", "ack_required": True}, headers=BEARER).json()
    for _ in range(2):
        with peered.websocket_connect("/notify?ack=explicit&format=json", headers=AS_PEER) as ws:
            message = ws.receive_json()
            assert message["uid"] == sent["uid"]
    # Even a legacy consuming inbox cannot consume ack-required assignments.
    assert peered.get("/api/inbox", headers=AS_PEER).json()["count"] == 1
    first = peered.post("/api/ack", json={"message_id": sent["uid"]}, headers=AS_PEER).json()["message"]
    second = peered.post("/api/ack", json={"message_id": str(sent["id"])}, headers=AS_PEER).json()["message"]
    assert first["acknowledged_at"] == second["acknowledged_at"]
    assert peered.get("/api/inbox", headers=AS_PEER).json()["count"] == 0
    assert peered.get("/api/history", headers=AS_PEER).json()["messages"][0]["uid"] == sent["uid"]


def test_ack_and_history_do_not_cross_role_boundary(peered):
    sent = peered.post("/api/send", json={"to": "other", "text": "private", "ack_required": True}, headers=BEARER).json()
    assert peered.post("/api/ack", json={"message_id": sent["uid"]}, headers=AS_PEER).status_code == 403
    assert peered.get("/api/history?agent=other", headers=AS_PEER).status_code == 403
    assert peered.get("/api/history", headers=AS_PEER).json()["messages"] == []


def test_provenance_distinguishes_operator_impersonation(peered):
    peered.post("/api/send", json={"sender": "lead", "to": "sisyphus", "text": "operator"}, headers=BEARER)
    peered.post("/api/send", json={"sender": "lead", "to": "sisyphus", "text": "agent"}, headers=AS_PEER)
    rows = peered.get("/api/history", headers=AS_PEER).json()["messages"]
    assert rows[0]["sender"] == "lead" and rows[0]["authenticated_principal"] == "here"
    assert rows[0]["admin"] and rows[0]["impersonated"]
    assert rows[1]["sender"] == rows[1]["authenticated_principal"] == "sisyphus"
    assert not rows[1]["admin"] and not rows[1]["impersonated"]


def test_rest_session_and_evidence_flow(peered):
    session = peered.post("/api/sessions", json={"context": {"harness": "test", "conversation_ref": "conversation:private", "configured_model": "reported"}}, headers=AS_PEER).json()["result"]
    sent = peered.post("/api/send", json={"to": "sisyphus", "text": "fix", "ack_required": True}, headers=BEARER).json()
    assert peered.post("/api/links", json={"message_id": sent["uid"], "relation": "followed_up_in", "target_type": "session", "target_ref": session["id"]}, headers=AS_PEER).status_code == 200
    assert peered.post("/api/outcomes", json={"message_id": sent["uid"], "kind": "verified", "artifact_ref": "commit:abc", "evidence_refs": ["test:123"]}, headers=AS_PEER).status_code == 200
    assert peered.post("/api/outcomes", json={"message_id": sent["uid"], "kind": "accepted", "artifact_ref": "commit:abc", "evidence_refs": ["review:456"]}, headers=AS_PEER).status_code == 403
    assert peered.post("/api/outcomes", json={"message_id": sent["uid"], "kind": "accepted", "artifact_ref": "commit:abc", "evidence_refs": ["review:456"]}, headers=BEARER).status_code == 200
    result = peered.get("/api/evidence", params={"message_id": sent["uid"]}, headers=AS_PEER).json()["result"]
    assert len(result["links"]) == 1 and result["outcomes"][-1]["kind"] == "accepted"
    assert peered.get("/api/sessions", headers=AS_PEER).json()["result"][0]["id"] == session["id"]


def test_session_reference_cannot_be_spoofed_in_metadata(peered):
    session = peered.post("/api/sessions", json={"context": {"harness": "test", "conversation_ref": "operator:private"}}, headers=BEARER).json()["result"]
    assert peered.post("/api/send", json={"to": "x", "text": "hi", "session_id": session["id"]}, headers=AS_PEER).status_code == 403
    assert peered.post("/api/send", json={"to": "x", "text": "hi", "meta": {"session_id": session["id"]}}, headers=AS_PEER).status_code == 400


def test_telemetry_and_reviewed_learning_rest(peered):
    sent = peered.post("/api/send", json={"to": "sisyphus", "text": "fix", "ack_required": True}, headers=BEARER).json()
    candidate = peered.post("/api/learnings", json={"text": "Build before judging logs", "message_ids": [sent["uid"]], "evidence_refs": ["test:1"]}, headers=AS_PEER).json()["result"]
    assert candidate["status"] == "candidate"
    reviewed = peered.post("/api/learning-reviews", json={"learning_id": candidate["id"], "status": "accepted", "evidence_refs": ["review:1"]}, headers=BEARER).json()["result"]
    assert reviewed["status"] == "accepted"
    assert peered.get("/api/learnings", headers=AS_PEER).json()["result"][0]["id"] == candidate["id"]
    metrics = peered.get("/api/telemetry", headers=AS_PEER).json()["result"]
    assert metrics["messages"] == 1 and metrics["pending"] == [sent["uid"]]
    assert peered.get("/api/telemetry?overdue_after_s=nan", headers=AS_PEER).status_code == 400
