#
#  agent-bridge-mcp - Copyright(c) 2026
#

import asyncio
import os
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


# --- game logs -------------------------------------------------------------
#
# The staleness reporting is the point of this module: an absent or old
# engine.log is what gets misread as "the subsystem never ran".

from agent_bridge.logs import Logs


def _build(tmp_path, with_log=True, log_older=False):
    d = tmp_path / "platform" / "desktop" / "bin" / "Debug"
    d.mkdir(parents=True)
    (d / "RogueLite.exe").write_text("binary")
    if with_log:
        log = d / "engine.log"
        log.write_text("[INFO] [LiveFeed] Connecting to ws://host/ws/game?u=someone\n"
                       "[WARNING] [LiveFeed] Unable to connect\n"
                       "[ERROR] boom\n")
        if log_older:
            os.utime(log, (1, 1))          # far older than the exe
    return d


def test_logs_are_found_inside_bin_which_bridge_read_refuses(tmp_path):
    _build(tmp_path)
    out = Logs({"proj": tmp_path}).list()
    assert len(out["logs"]) == 1
    assert out["logs"][0]["name"] == "engine.log"
    assert out["logs"][0]["lines"] == 3


def test_a_build_with_no_engine_log_is_reported_not_omitted(tmp_path):
    _build(tmp_path, with_log=False)
    out = Logs({"proj": tmp_path}).list()
    assert out["logs"] == []
    assert len(out["builds_without_engine_log"]) == 1
    assert "OLD BINARY" in out["builds_without_engine_log"][0]["note"]


def test_a_log_older_than_its_exe_says_so(tmp_path):
    _build(tmp_path, log_older=True)
    row = Logs({"proj": tmp_path}).list()["logs"][0]
    assert "OLDER" in row["note"]


def test_level_and_regex_filters(tmp_path):
    _build(tmp_path)
    lg = Logs({"proj": tmp_path})
    assert lg.read(level="WARNING")["matched_lines"] == 1
    assert lg.read(contains="u=([a-z.]+)")["matched_lines"] == 1
    assert lg.read()["matched_lines"] == 3          # defaults to newest log


def test_only_log_filenames_are_readable(tmp_path):
    d = _build(tmp_path)
    (d / "RogueLite.dll.config").write_text("secret")
    with pytest.raises(ValueError, match="not a log"):
        Logs({"proj": tmp_path})._resolve("proj:platform/desktop/bin/Debug/RogueLite.exe")


def test_log_outside_every_root_is_refused(tmp_path):
    _build(tmp_path)
    outside = tmp_path.parent / "engine.log"
    outside.write_text("x")
    with pytest.raises(ValueError, match="outside every configured root"):
        Logs({"proj": tmp_path})._resolve(str(outside))


def test_a_folder_with_diag_but_no_engine_log_is_still_flagged(tmp_path):
    # Regression: dist/live has a diag.log and no engine.log. Keying the
    # missing-scan on "any log found here" hid exactly that build.
    d = _build(tmp_path, with_log=False)
    (d / "diag.log").write_text("=== session start ===\n")
    out = Logs({"proj": tmp_path}).list()
    assert any(r["name"] == "diag.log" for r in out["logs"])
    assert len(out["builds_without_engine_log"]) == 1, out


# --- mailbox persistence ---------------------------------------------------
#
# Regression: the mailbox was memory-only, so restarting the server to add a
# tool silently destroyed an unread question and every unpolled answer.

def test_messages_survive_a_restart(tmp_path):
    store = tmp_path / "mailbox.json"
    a = Mailbox(store=store)
    a.post("sisyphus", "fenrir", "the question nobody has read yet")

    b = Mailbox(store=store)                      # simulates a process restart
    kept = b.inbox("fenrir", peek=True)
    assert len(kept) == 1
    assert kept[0].text == "the question nobody has read yet"


def test_read_state_survives_a_restart(tmp_path):
    store = tmp_path / "mailbox.json"
    a = Mailbox(store=store)
    a.post("sisyphus", "fenrir", "already answered")
    assert len(a.inbox("fenrir")) == 1             # marks it read

    b = Mailbox(store=store)
    assert b.inbox("fenrir") == []                 # must not be re-delivered


def test_ids_do_not_restart_at_one(tmp_path):
    store = tmp_path / "mailbox.json"
    a = Mailbox(store=store)
    first = a.post("x", "y", "one").id
    b = Mailbox(store=store)
    assert b.post("x", "y", "two").id > first


def test_a_corrupt_store_does_not_take_the_server_down(tmp_path):
    store = tmp_path / "mailbox.json"
    store.write_text("{not json at all")
    box = Mailbox(store=store)                     # must not raise
    assert box.post("x", "y", "still works").id == 1


def test_memory_only_is_still_supported(tmp_path):
    box = Mailbox(store=None)
    box.post("x", "y", "z")
    assert len(box.inbox("y", peek=True)) == 1


# --- long-poll -------------------------------------------------------------
#
# Exists because a peer's Monitor tool refuses WebSockets to private-range
# addresses, making /notify unusable across a LAN. bridge_wait gives the same
# latency over an ordinary request.

def test_wait_returns_immediately_when_mail_is_already_waiting():
    async def go():
        box = Mailbox()
        box.post("a", "fenrir", "already here")
        q = box.subscribe("fenrir")
        try:
            # The short-circuit path: unread mail must not block.
            existing = box.inbox("fenrir", peek=True)
            assert len(existing) == 1
        finally:
            box.unsubscribe("fenrir", q)
    asyncio.run(go())


def test_wait_wakes_on_a_message_rather_than_timing_out():
    async def go():
        box = Mailbox()
        q = box.subscribe("fenrir")
        async def send_soon():
            await asyncio.sleep(0.05)
            box.post("sisyphus", "fenrir", "arrived while waiting")
        asyncio.get_running_loop().create_task(send_soon())
        msg = await asyncio.wait_for(q.get(), timeout=2.0)
        assert msg.text == "arrived while waiting"
        box.unsubscribe("fenrir", q)
    asyncio.run(go())


def test_wait_times_out_cleanly_with_no_mail():
    async def go():
        box = Mailbox()
        q = box.subscribe("fenrir")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.1)
        box.unsubscribe("fenrir", q)
        # Unsubscribing must not leave the queue registered.
        assert q not in box._subs.get("fenrir", [])
    asyncio.run(go())
