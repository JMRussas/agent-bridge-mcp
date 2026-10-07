#
#  agent-bridge-mcp - Copyright(c) 2026
#

# Identity through the MCP transport itself. The unit tests can prove that
# Credentials resolves a token to a name; only a real streamable-HTTP session
# proves the name reaches the tool, because that depends on the SDK putting
# the Starlette request on the tool's context. So this runs uvicorn on a free
# loopback port and drives it with the SDK's own client.

import asyncio
import json
import socket
import threading

import httpx
import pytest

pytestmark = pytest.mark.mcp
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_bridge.auth import Credentials, Forbidden
from agent_bridge.config import Config
from agent_bridge.server import build

ADMIN = "admin-token-not-secret"
PEER_A = "a-token-not-secret"
PEER_B = "b-token-not-secret"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def mcp_url():
    port = _free_port()
    cfg = Config({"self_name": "here", "token": ADMIN, "mailbox_store": "",
                  "agents": {"a": {"token": PEER_A, "description": "Claude Code in Rogue-Lite"},
                             "b": {"token": PEER_B, "description": "Codex CLI in Rogue-Lite"}},
                  "allowed_hosts": ["127.0.0.1:*"]})
    server = uvicorn.Server(uvicorn.Config(build(cfg), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        await asyncio.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(10)


async def call(url: str, token: str, tool: str, **args) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=30) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
                return json.loads(result.content[0].text)


# --- the credential is the identity -------------------------------------------

async def test_a_peer_token_names_the_peer(mcp_url):
    assert (await call(mcp_url, PEER_A, "bridge_whoami"))["you"] == {"name": "a", "admin": False}
    assert (await call(mcp_url, ADMIN, "bridge_whoami"))["you"] == {"name": "here", "admin": True}


async def test_a_peer_cannot_claim_another_sender(mcp_url):
    sent = await call(mcp_url, PEER_A, "bridge_send", to="b", text="hi", sender="here")
    assert sent["sender"] == "a"
    got = await call(mcp_url, PEER_B, "bridge_inbox")
    assert [m["sender"] for m in got["messages"]] == ["a"]


async def test_the_admin_may_still_name_a_sender(mcp_url):
    sent = await call(mcp_url, ADMIN, "bridge_send", to="a", text="hi", sender="script")
    assert sent["sender"] == "script"
    sent = await call(mcp_url, ADMIN, "bridge_send", to="a", text="hi")
    assert sent["sender"] == "here"


async def test_a_peer_reads_only_its_own_mailbox(mcp_url):
    await call(mcp_url, ADMIN, "bridge_send", to="b", text="for b")
    denied = await call(mcp_url, PEER_A, "bridge_inbox", agent="b")
    assert "may not read the mailbox of 'b'" in denied["error"]
    denied = await call(mcp_url, PEER_A, "bridge_wait", agent="b", timeout=1)
    assert "may not read" in denied["error"]
    # Still there for b, and the admin may look too.
    assert (await call(mcp_url, ADMIN, "bridge_inbox", agent="b", peek=True))["count"] == 1
    assert (await call(mcp_url, PEER_B, "bridge_inbox"))["count"] == 1


async def test_a_peers_history_is_its_own_traffic(mcp_url):
    await call(mcp_url, ADMIN, "bridge_send", to="b", text="admin to b")
    await call(mcp_url, PEER_A, "bridge_send", to="b", text="a to b")
    await call(mcp_url, PEER_B, "bridge_send", to="a", text="b to a")
    seen = (await call(mcp_url, PEER_A, "bridge_history"))["messages"]
    assert sorted(m["text"] for m in seen) == ["a to b", "b to a"]
    assert "may not read" in (await call(mcp_url, PEER_A, "bridge_history", agent="b"))["error"]
    everything = (await call(mcp_url, ADMIN, "bridge_history"))["messages"]
    assert len(everything) == 3


# Two agents in the same repo on the same machine - the case that made the
# credential a ROLE rather than a machine. The bridge cannot see where either
# runs; what it guarantees is that a reply to one is invisible to the other.
async def test_two_agents_in_one_repo_do_not_see_each_others_mail(mcp_url):
    await call(mcp_url, PEER_A, "bridge_send", to="here", text="from a: what is AvatarSize?")
    await call(mcp_url, PEER_B, "bridge_send", to="here", text="from b: build is red")
    await call(mcp_url, ADMIN, "bridge_send", to="a", text="64")
    await call(mcp_url, ADMIN, "bridge_send", to="b", text="known, fixing")

    a_sees = (await call(mcp_url, PEER_A, "bridge_inbox"))["messages"]
    b_sees = (await call(mcp_url, PEER_B, "bridge_inbox"))["messages"]
    assert [m["text"] for m in a_sees] == ["64"]
    assert [m["text"] for m in b_sees] == ["known, fixing"]
    # The admin's own mailbox has both questions, each provably from its author.
    ours = (await call(mcp_url, ADMIN, "bridge_inbox"))["messages"]
    assert [(m["sender"], m["text"][:6]) for m in ours] == [("a", "from a"), ("b", "from b")]


async def test_the_directory_says_who_to_ask(mcp_url):
    await call(mcp_url, ADMIN, "bridge_send", to="b", text="hi", sender="script")
    agents = (await call(mcp_url, PEER_A, "bridge_agents"))["agents"]
    by_name = {e["name"]: e for e in agents}
    assert by_name["a"]["description"] == "Claude Code in Rogue-Lite" and by_name["a"]["credentialed"]
    assert by_name["b"]["unread"] == 1 and by_name["b"]["credentialed"]
    assert by_name["here"]["admin"] and by_name["here"]["credentialed"]
    # A name that only ever appeared in traffic is listed, and flagged.
    assert by_name["script"]["credentialed"] is False


# --- the pure part ---------------------------------------------------------------

def test_an_instance_of_a_role_is_the_roles_own_mailbox():
    # Reserved for S1b: "rl-claude#3f2a" is one session of rl-claude. It shares
    # the role's credential, so it is the role's to read; nobody else's.
    c = Credentials("here", ADMIN, {"a": {"token": PEER_A}, "b": {"token": PEER_B}})
    a, b = c.identify(PEER_A), c.identify(PEER_B)
    assert c.mailbox_for(a, "a#3f2a") == "a#3f2a"
    with pytest.raises(Forbidden):
        c.mailbox_for(b, "a#3f2a")
    with pytest.raises(Forbidden):
        c.mailbox_for(b, "ab")        # a prefix is not an instance


def test_credentials_resolve_in_constant_shape():
    c = Credentials("here", ADMIN, {"a": {"token": PEER_A}})
    assert c.identify(ADMIN).admin and c.identify(ADMIN).name == "here"
    assert c.identify(PEER_A).name == "a" and not c.identify(PEER_A).admin
    assert c.identify("nope") is None and c.identify("") is None


def test_a_placeholder_admin_token_is_not_a_credential():
    c = Credentials("here", "CHANGE_ME", {"a": {"token": PEER_A}})
    assert c.identify("CHANGE_ME") is None
    assert c.identify(PEER_A).name == "a"
    assert not c.open


def test_the_admin_name_is_normalised():
    c = Credentials(" Fenrir ", ADMIN, {})
    assert c.identify(ADMIN).name == "fenrir"
    assert c.mailbox_for(c.identify(ADMIN), "") == "fenrir"


def test_no_credentials_at_all_means_everyone_is_the_admin():
    c = Credentials("here", "", {})
    assert c.open
    assert c.identify("").admin and c.identify("anything").admin


def test_mailbox_for_defaults_to_self_and_refuses_others():
    c = Credentials("here", ADMIN, {"a": {"token": PEER_A}})
    a, admin = c.identify(PEER_A), c.identify(ADMIN)
    assert c.mailbox_for(a, "") == "a"
    assert c.mailbox_for(a, " A ") == "a"
    with pytest.raises(Forbidden, match="mailbox of 'b'"):
        c.mailbox_for(a, "b")
    with pytest.raises(Forbidden, match="every mailbox"):
        c.mailbox_for(a, "*")
    assert c.mailbox_for(admin, "") == "here"
    assert c.mailbox_for(admin, "b") == "b"
    assert c.mailbox_for(admin, "*") == "*"


async def test_evidence_protocol_preserves_real_mcp_identity(mcp_url):
    session = (await call(mcp_url, PEER_A, "bridge_register_session", context={"harness": "test", "conversation_ref": "conversation:a"}))["result"]
    assert session["owner"] == "a"
    sent = await call(mcp_url, PEER_A, "bridge_send", to="b", text="assignment", ack_required=True, session_id=session["id"])
    assert (await call(mcp_url, PEER_B, "bridge_inbox"))["count"] == 1
    assert "error" in await call(mcp_url, PEER_A, "bridge_ack", message_id=sent["uid"])
    await call(mcp_url, PEER_B, "bridge_ack", message_id=sent["uid"])
    await call(mcp_url, PEER_B, "bridge_link", message_id=sent["uid"], relation="followed_up_in", target_type="checkin", target_ref="checkin:1")
    await call(mcp_url, PEER_B, "bridge_outcome", message_id=sent["uid"], kind="verified", artifact_ref="commit:1", evidence_refs=["test:1"])
    denied = await call(mcp_url, PEER_B, "bridge_outcome", message_id=sent["uid"], kind="accepted", artifact_ref="commit:1", evidence_refs=["review:1"])
    assert "error" in denied
    accepted = await call(mcp_url, PEER_A, "bridge_outcome", message_id=sent["uid"], kind="accepted", artifact_ref="commit:1", evidence_refs=["review:1"])
    assert accepted["result"]["actor"] == "a"
    assert (await call(mcp_url, PEER_B, "bridge_links", target_type="checkin", target_ref="checkin:1"))["result"][0]["message_id"] == sent["uid"]
    metrics = (await call(mcp_url, PEER_B, "bridge_telemetry"))["result"]
    assert metrics["pending"] == [] and metrics["durations_s"]["acknowledgment"]["samples"] == 1


async def test_slow_report_does_not_block_mail_delivery(mcp_url, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def slow_report(self, who, overdue_after_s=3600):
        started.set()
        release.wait(5)
        return {"messages": 0}
    monkeypatch.setattr("agent_bridge.reports.Reports.telemetry", slow_report)
    task = asyncio.create_task(call(mcp_url, PEER_A, "bridge_telemetry"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        sent = await asyncio.wait_for(call(mcp_url, PEER_B, "bridge_send", to="a", text="still responsive"), 2)
        assert sent["sent"] and not task.done()
    finally:
        release.set()
        await task
