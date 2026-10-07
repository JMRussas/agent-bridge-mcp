"""On-demand startup and calls over real stdio and HTTP MCP transports."""

import asyncio
import json
import socket
import subprocess
import sys
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from agent_bridge.launcher import Launcher, build_launcher

pytestmark = pytest.mark.mcp


@pytest.fixture
def launcher(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "self_name": "test-bridge", "host": "127.0.0.1", "port": port,
        "token": "test-admin", "agents": {"a": {"token": "test-agent-a"}},
        "mailbox_store": str(tmp_path / "mailbox.sqlite3"),
    }))
    return Launcher(str(config), "a")


def test_requires_agent_role(launcher):
    with pytest.raises(ValueError, match="existing agent role"):
        Launcher(str(launcher.path), "test-bridge")


def test_refuses_existing_unhealthy_service(launcher):
    with patch.object(launcher, "status", return_value={"ready": False, "state": "credential_rejected"}), patch("subprocess.Popen") as spawn:
        with pytest.raises(RuntimeError, match="credential_rejected"):
            launcher.ensure_running()
        spawn.assert_not_called()


async def test_real_start_concurrent_reuse_identity_and_recovery(launcher):
    children = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    try:
        assert launcher.status()["state"] == "unreachable"
        with patch("agent_bridge.launcher.subprocess.Popen", side_effect=spawn):
            results = await asyncio.gather(*[asyncio.to_thread(launcher.ensure_running) for _ in range(3)])
            assert sum(result["started"] for result in results) == 1
            assert len(children) == 1
            catalog = await launcher.tools()
            assert "bridge_send" in {item["name"] for item in catalog["tools"]}
            result = await launcher.call("bridge_whoami", {})
            assert not result.isError
            assert json.loads(result.content[0].text)["you"] == {"name": "a", "admin": False}
            denied = await launcher.call("bridge_inbox", {"agent": "someone-else"})
            assert "may not read" in denied.content[0].text
            children[0].terminate()
            children[0].wait(timeout=10)
            result = await launcher.call("bridge_whoami", {})
            assert not result.isError
            assert len(children) == 2
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)


async def test_stdio_tools_available_while_bridge_down(launcher):
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "agent_bridge", "launcher", "--config", str(launcher.path), "--agent", "a",
    ])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            catalog = await session.list_tools()
            assert {tool.name for tool in catalog.tools} == {
                "bridge_status", "bridge_ensure_running", "bridge_tools", "bridge_call",
            }
            status = await session.call_tool("bridge_status", {})
            assert json.loads(status.content[0].text)["state"] == "unreachable"
    assert launcher.status()["ready"] is False


async def test_forwarded_error_is_preserved(launcher):
    from mcp import types
    from unittest.mock import AsyncMock
    result = types.CallToolResult(content=[types.TextContent(type="text", text="denied")], isError=True)
    with patch.object(launcher, "call", new=AsyncMock(return_value=result)) as call:
        mcp = build_launcher(launcher)
        forwarded = await mcp.call_tool("bridge_call", {"name": "bridge_run", "arguments": {"name": "x"}})
        assert forwarded.isError
        assert forwarded.content[0].text == "denied"
        call.assert_awaited_once_with("bridge_run", {"name": "x"})


async def test_start_failure_is_actionable(launcher):
    from unittest.mock import Mock
    failed = Mock()
    failed.poll.return_value = 1
    failed.returncode = 1
    with patch("agent_bridge.launcher.subprocess.Popen", return_value=failed):
        with pytest.raises(RuntimeError, match="exited with code 1.*server.err"):
            await asyncio.to_thread(launcher.ensure_running)


def test_occupied_non_http_port_is_not_replaced(launcher):
    with socket.socket() as listener:
        listener.bind((launcher.host, launcher.cfg.port))
        listener.listen()
        with patch("agent_bridge.launcher.subprocess.Popen") as spawn:
            with pytest.raises(RuntimeError, match="unhealthy|occupied"):
                launcher.ensure_running()
            spawn.assert_not_called()
