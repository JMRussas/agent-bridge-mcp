#
#  agent-bridge-mcp - Copyright(c) 2026
#

import asyncio
import struct
import zlib
from pathlib import Path

import pytest

from agent_bridge.avatar import rgba_to_png
from agent_bridge.execute import ExecDenied, Runner
from agent_bridge.files import Files, PathDenied
from agent_bridge.mailbox import Mailbox


@pytest.fixture
def tree(tmp_path: Path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.cs").write_text("const int AvatarSize = 64;\nvar x = 1;\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("AvatarSize\n")
    (root / ".env").write_text("SECRET=hunter2\n")
    (tmp_path / "outside.txt").write_text("should never be readable\n")
    return root, tmp_path


# --- containment -----------------------------------------------------------

def test_relative_escape_is_refused(tree):
    root, tmp = tree
    f = Files({"proj": root})
    with pytest.raises(PathDenied):
        f.resolve("proj:../outside.txt")


def test_absolute_path_outside_root_is_refused(tree):
    root, tmp = tree
    f = Files({"proj": root})
    with pytest.raises(PathDenied):
        f.resolve(str(tmp / "outside.txt"))


def test_deny_listed_name_is_refused(tree):
    root, _ = tree
    f = Files({"proj": root})
    with pytest.raises(PathDenied):
        f.resolve("proj:.env")


def test_root_prefix_resolves_and_reads(tree):
    root, _ = tree
    f = Files({"proj": root})
    out = f.read("proj:src/main.cs")
    assert "AvatarSize" in out["text"]
    assert out["total_lines"] == 2


def test_unknown_root_names_the_known_ones(tree):
    root, _ = tree
    f = Files({"proj": root})
    with pytest.raises(PathDenied, match="proj"):
        f.resolve("nope:src/main.cs")


def test_grep_skips_denied_directories(tree):
    root, _ = tree
    f = Files({"proj": root})
    res = f._grep_python("AvatarSize", [root], "", 50, 0, False)
    assert res["count"] == 1, res
    assert "node_modules" not in res["matches"][0]


# --- mailbox ---------------------------------------------------------------

def test_inbox_marks_read_and_peek_does_not():
    box = Mailbox()
    box.post("sisyphus", "fenrir", "hello")
    assert len(box.inbox("fenrir", peek=True)) == 1
    assert len(box.inbox("fenrir", peek=True)) == 1      # still unread
    assert len(box.inbox("fenrir")) == 1
    assert box.inbox("fenrir") == []


def test_messages_are_addressed_not_broadcast():
    box = Mailbox()
    box.post("sisyphus", "fenrir", "for fenrir")
    assert box.inbox("someone-else") == []
    assert len(box.inbox("fenrir")) == 1


def test_unread_survives_capacity_pressure():
    # The eviction rule exists so a question cannot be silently dropped by
    # unrelated chatter arriving behind it.
    box = Mailbox(capacity=5)
    box.post("sisyphus", "fenrir", "the important question")
    for i in range(20):
        box.post("noise", "elsewhere", f"chatter {i}")
        box.inbox("elsewhere")                            # keep marking them read
    kept = box.inbox("fenrir", peek=True)
    assert len(kept) == 1
    assert kept[0].text == "the important question"


def test_post_requires_a_recipient_and_a_body():
    box = Mailbox()
    with pytest.raises(ValueError):
        box.post("a", "", "text")
    with pytest.raises(ValueError):
        box.post("a", "b", "   ")


def test_subscriber_receives_live_fanout():
    async def go():
        box = Mailbox()
        q = box.subscribe("fenrir")
        box.post("sisyphus", "fenrir", "live one")
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg.text == "live one"
        # A message for someone else must not reach this subscriber.
        box.post("sisyphus", "other", "not yours")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.1)

    asyncio.run(go())


# --- execution allowlist ---------------------------------------------------

CMDS = {"git-status": {"root": "*", "argv": ["git", "status"]},
        "autoplay": {"root": "proj", "argv": ["dotnet", "run"], "max_args": 2}}


def runner(tmp_path, enabled=True):
    return Runner(CMDS, {"proj": tmp_path}, enabled)


def test_unknown_command_is_refused(tmp_path):
    with pytest.raises(ExecDenied, match="allowlist"):
        asyncio.run(runner(tmp_path).run("rm-rf"))


def test_execution_can_be_disabled(tmp_path):
    with pytest.raises(ExecDenied, match="disabled"):
        asyncio.run(runner(tmp_path, enabled=False).run("git-status", root="proj"))


def test_extra_args_are_capped(tmp_path):
    with pytest.raises(ExecDenied, match="at most"):
        asyncio.run(runner(tmp_path).run("autoplay", ["1", "2", "3"], root="proj"))


@pytest.mark.parametrize("bad", ["a; rm -rf /", "$(whoami)", "--out=/etc/x", "a b", "`id`", "a|b"])
def test_shell_metacharacters_in_args_are_refused(tmp_path, bad):
    with pytest.raises(ExecDenied, match="rejected"):
        asyncio.run(runner(tmp_path).run("autoplay", [bad], root="proj"))


def test_command_pinned_to_a_root_refuses_another(tmp_path):
    with pytest.raises(ExecDenied, match="only runs in root"):
        asyncio.run(runner(tmp_path).run("autoplay", root="somewhere"))


# --- png encoder -----------------------------------------------------------

def test_rgba_to_png_round_trips_through_zlib():
    w = h = 4
    rgba = bytes([200, 100, 50, 255]) * (w * h)
    png = rgba_to_png(rgba, w, h)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, depth, ctype = struct.unpack(">IIBB", png[16:26])
    assert (width, height, depth, ctype) == (w, h, 8, 6)

    idat = png[png.index(b"IDAT") + 4:]
    raw = zlib.decompress(idat[:-12])
    # Each row is one filter byte followed by the row's pixels.
    assert len(raw) == h * (1 + w * 4)
    assert raw[1:5] == bytes([200, 100, 50, 255])


# --- MCP transport host allowlist ------------------------------------------
#
# Regression: the SDK's DNS-rebinding protection defaults to an EMPTY allowlist,
# which answers every non-localhost request with 421 Misdirected Request. Our own
# /api routes sit outside that middleware and keep working, so the server looks
# half-up from another machine - health responds, MCP does not.

from agent_bridge.config import Config
from agent_bridge.server import allowed_hosts


def test_derived_allowlist_covers_localhost_and_this_machine():
    hosts = allowed_hosts(Config({}))
    assert "localhost:*" in hosts
    assert "127.0.0.1:*" in hosts
    # Every entry allows any port, so moving the port cannot silently re-break it.
    assert all(h.endswith(":*") for h in hosts), hosts
    # Something beyond loopback must be present, or a remote peer still 421s.
    assert any(not h.startswith(("localhost", "127.")) for h in hosts), hosts


def test_configured_allowlist_wins():
    hosts = allowed_hosts(Config({"allowed_hosts": ["example.internal:*"]}))
    assert hosts == ["example.internal:*"]


def test_host_ok_mirrors_the_sdk_matching_rule():
    from agent_bridge.server import _host_ok
    allowed = ["192.168.1.174:*", "localhost:*", "exact.host"]
    assert _host_ok("192.168.1.174:8791", allowed)
    assert _host_ok("192.168.1.174:9999", allowed)      # any port
    assert _host_ok("exact.host", allowed)
    assert not _host_ok("evil.example.com", allowed)
    assert not _host_ok("", allowed)
    # A peer's OWN name is never the Host header, so it must not match.
    assert not _host_ok("sisyphus:8791", allowed)
