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
        "output_dir": str(tmp_path / "out"),
        "allowed_hosts": ["testserver:*", "localhost:*"],
    })
    with TestClient(build(cfg)) as c:
        yield c


# --- header vs query string --------------------------------------------------
#
# Regression: the query-string token was accepted on every route, so it worked
# in a plain GET and landed in access logs. It is for the WebSocket only.

def test_health_needs_no_token(client):
    assert client.get("/api/health").status_code == 200


def test_rest_refuses_without_token(client):
    assert client.get("/api/inbox?agent=x").status_code == 401
    assert client.post("/api/send", json={"to": "x", "text": "hi"}).status_code == 401


def test_rest_accepts_the_header(client):
    assert client.get("/api/inbox?agent=x", headers=BEARER).status_code == 200


def test_rest_refuses_the_token_in_the_query_string(client):
    assert client.get(f"/api/inbox?agent=x&token={TOKEN}").status_code == 401
    assert client.post(f"/api/send?token={TOKEN}",
                       json={"to": "x", "text": "hi"}).status_code == 401


def test_websocket_accepts_the_token_in_the_query_string(client):
    with client.websocket_connect(f"/notify?agent=x&token={TOKEN}") as ws:
        r = client.post("/api/send", json={"sender": "a", "to": "x", "text": "ping"},
                        headers=BEARER)
        assert r.status_code == 200
        assert ws.receive_text().endswith("a -> x: ping")


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
    assert "/notify" in r.json()["hint"]
