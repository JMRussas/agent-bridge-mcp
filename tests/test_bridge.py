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


def test_a_slice_of_a_large_file_does_not_load_the_file(tree):
    # Regression: any count>0 read the whole file and then sliced it, so the
    # size cap only ever applied to count=0. A 2 GB file with count=1 was a
    # 2 GB read.
    import tracemalloc
    root, _ = tree
    big = root / "src" / "big.log"
    with big.open("w") as f:
        for i in range(200_000):
            f.write(f"line {i} " + "x" * 40 + "\n")     # ~10 MB
    size = big.stat().st_size
    assert size > 8_000_000

    f = Files({"proj": root}, max_read_bytes=256 * 1024)
    tracemalloc.start()
    out = f.read("proj:src/big.log", start=100_001, count=5)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert out["returned"] == 5
    assert out["text"].splitlines()[0].startswith("100001\tline 100000 ")
    assert out["total_lines"] == 200_000
    assert peak < size // 4, f"peak {peak} bytes for a 5-line slice of a {size}-byte file"


def test_whole_file_over_the_cap_is_still_refused(tree):
    root, _ = tree
    (root / "src" / "fat.txt").write_text("y" * 3000)
    f = Files({"proj": root}, max_read_bytes=2000)
    with pytest.raises(PathDenied, match="over the 2000 limit"):
        f.read("proj:src/fat.txt")


def test_a_slice_is_capped_by_bytes_not_only_by_count(tree):
    root, _ = tree
    (root / "src" / "wide.txt").write_text("\n".join("a" * 100 for _ in range(50)))
    f = Files({"proj": root}, max_read_bytes=350)
    out = f.read("proj:src/wide.txt", start=1, count=50)
    assert out["truncated"] is True
    assert out["returned"] == 3                           # 3 x 101 bytes fits, 4 does not
    assert "next line is 4" in out["note"]
    assert out["total_lines"] == 50                       # still counted to the end


def test_a_line_longer_than_the_cap_is_still_readable(tree):
    # Regression (review): a single over-cap line was dropped whole, with a note
    # pointing the caller back at the same line - an infinite loop.
    root, _ = tree
    (root / "src" / "long.txt").write_text("short\n" + "z" * 5000 + "\nafter\n")
    f = Files({"proj": root}, max_read_bytes=2000)
    out = f.read("proj:src/long.txt", start=2, count=1)
    assert out["returned"] == 1
    assert out["truncated"] is True and "longer than the 2000-byte limit" in out["note"]
    assert out["text"] == "2\t" + "z" * 2000
    assert out["total_lines"] == 3


def test_the_cap_counts_bytes_not_characters(tree):
    # Regression (review): len(str) passed a 1500-char CJK line through a
    # 2000-byte cap and returned 4500 bytes.
    root, _ = tree
    cjk = "\u4e2d" * 1000                                 # 3000 bytes per line
    (root / "src" / "cjk.txt").write_text("\n".join([cjk] * 3), encoding="utf-8")
    f = Files({"proj": root}, max_read_bytes=7000)
    out = f.read("proj:src/cjk.txt", start=1, count=3)
    assert out["returned"] == 2 and out["truncated"] is True
    assert "next line is 3" in out["note"]

    # And a cut never lands mid-character.
    out = Files({"proj": root}, max_read_bytes=1000).read("proj:src/cjk.txt", start=1, count=1)
    body = out["text"].split("\t", 1)[1]
    assert "\ufffd" not in body and len(body.encode()) <= 1000 and body == "\u4e2d" * 333


def test_crlf_files_read_the_same_as_lf(tree):
    root, _ = tree
    (root / "src" / "win.txt").write_bytes(b"one\r\ntwo\r\nthree")
    out = Files({"proj": root}).read("proj:src/win.txt")
    assert out["total_lines"] == 3
    assert out["text"] == "1\tone\n2\ttwo\n3\tthree"


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


def test_an_oversize_message_is_refused():
    box = Mailbox(max_message_bytes=100)
    with pytest.raises(ValueError, match="limit is 100"):
        box.post("a", "b", "x" * 101)
    box.post("a", "b", "\u4e2d" * 33)                    # 99 bytes: bytes, not chars
    with pytest.raises(ValueError, match="limit is 100"):
        box.post("a", "b", "\u4e2d" * 34)                # 102 bytes


def test_retention_is_by_bytes_as_well_as_count():
    box = Mailbox(capacity=1000, max_bytes=1000, max_message_bytes=500)
    for i in range(5):
        box.post("a", "b", f"{i}" + "x" * 299)          # 300 bytes each
    box.inbox("b")                                       # all read
    box.post("a", "b", "final" + "y" * 295)
    assert box.bytes_used <= 1000
    texts = [m.text[:5] for m in box.history()]
    assert texts[-1] == "final" and "0xxxx" not in texts   # oldest read went first


def test_the_last_unread_message_is_never_evicted():
    box = Mailbox(capacity=1000, max_bytes=100, max_message_bytes=500)
    box.post("a", "b", "z" * 200)                        # over the byte cap on its own
    assert box.unread_count("b") == 1


async def test_read_state_writes_are_coalesced_but_posts_are_not(tmp_path, monkeypatch):
    # AC (B6): ten inbox reads in a row write the store at most once; a post
    # writes immediately because losing it would be a dropped question.
    box = Mailbox(store=tmp_path / "m.json", debounce_s=0.05)
    writes = []
    real = box._persist
    monkeypatch.setattr(box, "_persist", lambda: writes.append(1) or real())

    for i in range(10):
        box.post("a", "b", f"m{i}")
    assert len(writes) == 10                             # one per post, at once

    writes.clear()
    for _ in range(10):
        box.inbox("b", limit=1)
    assert writes == []                                  # nothing yet ...
    await asyncio.sleep(0.15)
    assert len(writes) == 1                              # ... then exactly one
    assert Mailbox(store=tmp_path / "m.json").unread_count("b") == 0


async def test_close_flushes_a_pending_write(tmp_path):
    store = tmp_path / "m.json"
    box = Mailbox(store=store, debounce_s=10)
    box.post("a", "b", "hello")
    box.inbox("b")
    assert Mailbox(store=store).unread_count("b") == 1   # not yet on disk
    box.close()
    assert Mailbox(store=store).unread_count("b") == 0


def test_without_a_loop_read_state_is_written_immediately(tmp_path):
    store = tmp_path / "m.json"
    box = Mailbox(store=store)
    box.post("a", "b", "hello")
    box.inbox("b")
    assert Mailbox(store=store).unread_count("b") == 0


def test_post_requires_a_recipient_and_a_body():
    box = Mailbox()
    with pytest.raises(ValueError):
        box.post("a", "", "text")
    with pytest.raises(ValueError):
        box.post("a", "b", "   ")


async def test_subscriber_receives_live_fanout():
    box = Mailbox()
    q = box.subscribe("fenrir")
    box.post("sisyphus", "fenrir", "live one")
    msg = await asyncio.wait_for(q.get(), timeout=1)
    assert msg.text == "live one"
    # A message for someone else must not reach this subscriber.
    box.post("sisyphus", "other", "not yours")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.1)


# --- execution allowlist ---------------------------------------------------

CMDS = {"git-status": {"root": "*", "argv": ["git", "status"]},
        "autoplay": {"root": "proj", "argv": ["dotnet", "run"], "max_args": 2}}


def runner(tmp_path, enabled=True):
    return Runner(CMDS, {"proj": tmp_path}, enabled)


async def test_unknown_command_is_refused(tmp_path):
    with pytest.raises(ExecDenied, match="allowlist"):
        await runner(tmp_path).run("rm-rf")


async def test_execution_can_be_disabled(tmp_path):
    with pytest.raises(ExecDenied, match="disabled"):
        await runner(tmp_path, enabled=False).run("git-status", root="proj")


async def test_extra_args_are_capped(tmp_path):
    with pytest.raises(ExecDenied, match="at most"):
        await runner(tmp_path).run("autoplay", ["1", "2", "3"], root="proj")


@pytest.mark.parametrize("bad", ["a; rm -rf /", "$(whoami)", "--out=/etc/x", "a b", "`id`", "a|b"])
async def test_shell_metacharacters_in_args_are_refused(tmp_path, bad):
    with pytest.raises(ExecDenied, match="rejected"):
        await runner(tmp_path).run("autoplay", [bad], root="proj")


async def test_command_pinned_to_a_root_refuses_another(tmp_path):
    with pytest.raises(ExecDenied, match="only runs in root"):
        await runner(tmp_path).run("autoplay", root="somewhere")


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

async def test_wait_returns_immediately_when_mail_is_already_waiting():
    box = Mailbox()
    box.post("a", "fenrir", "already here")
    # The short-circuit path: unread mail must not block.
    msgs, timed_out = await box.wait("fenrir", 5.0)
    assert not timed_out and len(msgs) == 1


async def test_wait_wakes_on_a_message_rather_than_timing_out():
    box = Mailbox()

    async def send_soon():
        await asyncio.sleep(0.05)
        box.post("sisyphus", "fenrir", "arrived while waiting")
    asyncio.get_running_loop().create_task(send_soon())
    msgs, timed_out = await box.wait("fenrir", 2.0)
    assert not timed_out and msgs[0].text == "arrived while waiting"


async def test_wait_times_out_cleanly_with_no_mail():
    box = Mailbox()
    msgs, timed_out = await box.wait("fenrir", 0.1)
    assert timed_out and msgs == []
    # Unsubscribing must not leave the queue registered.
    assert box._subs.get("fenrir", []) == []


async def test_wait_returns_the_message_even_if_a_socket_consumed_it_first():
    # Regression: /notify and wait share the fan-out. When the socket's send
    # completed and marked the message read before the waiter re-read the
    # inbox, the waiter answered "mail arrived" with an empty list.
    box = Mailbox()
    task = asyncio.create_task(box.wait("x", 2.0))
    await asyncio.sleep(0)                       # the waiter is now subscribed
    msg = box.post("a", "x", "hi")
    box.mark_read(msg)                           # what the socket does after send
    msgs, timed_out = await task
    assert not timed_out
    assert msgs == [msg]


async def test_wait_does_not_adopt_someone_elses_message_from_a_wildcard_wake():
    box = Mailbox()
    task = asyncio.create_task(box.wait("*", 0.3))
    await asyncio.sleep(0)
    box.post("a", "y", "for y")
    msgs, timed_out = await task
    # Woken, but "*" has no inbox of its own and must not claim y's mail.
    assert not timed_out and msgs == []
    assert box.unread_count("y") == 1


def test_ws_backlog_marked_read_is_not_replayed_after_restart(tmp_path):
    # Regression: the /notify backlog peeked without consuming, so a listener
    # that reads its mail from the socket never marked anything read and every
    # reconnect replayed the same messages.
    store = tmp_path / "mailbox.json"
    box = Mailbox(store=store)
    box.post("sisyphus", "fenrir", "delivered over the socket")

    backlog = box.inbox("fenrir", peek=True)
    assert len(backlog) == 1
    box.mark_read(*backlog)    # what notify() does once the frame is sent

    assert Mailbox(store=store).inbox("fenrir", peek=True) == []


# --- avatar_png output containment -----------------------------------------
#
# Regression: to_png used to write wherever the caller pointed it. On a bridge
# whose whole posture is "read-only plus an allowlist", one tool that writes to
# an arbitrary absolute path is a write primitive over the server's whole disk.

from agent_bridge.avatar import Avatars, OutputDenied


@pytest.fixture
def avatars(tmp_path: Path):
    a = Avatars({"url": "http://unused", "avatar_size": 2}, {}, output_dir=tmp_path / "out")

    async def fake_fetch(uid, size=0, creator=""):
        return (bytes([9, 9, 9, 255]) * 4,
                {"url": "u", "status": 200, "content_type": "", "requested_size": 2})

    a.fetch = fake_fetch
    return a


async def test_png_lands_under_the_output_dir(avatars, tmp_path):
    res = await avatars.to_png("uid", "faces/one.png")
    assert res["written"] is True
    assert Path(res["path"]) == (tmp_path / "out" / "faces" / "one.png").resolve()
    assert Path(res["path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("bad, would_land", [
    ("../../x.png", "x.png"),                  # relative escape -> tmp_path.parent
    (r"..\..\escaped.png", "escaped.png"),     # backslash spelling of the same
    ("{abs}/elsewhere.png", "elsewhere.png"),  # absolute path outside the dir
    ("inside.cs", None),                       # right place, wrong kind of file
    ("inside.png.ps1", None),                  # suffix that only looks like png
    ("evil.ps1:x.png", None),                  # NTFS alternate data stream
    ("", None),
])
async def test_writes_outside_or_not_png_are_refused(avatars, tmp_path, bad, would_land):
    with pytest.raises(OutputDenied):
        await avatars.to_png("uid", bad.format(abs=tmp_path.as_posix()))
    # Check where each escape would actually have resolved to, not a guess.
    if would_land:
        assert not (tmp_path.parent / would_land).exists()
        assert not (tmp_path / would_land).exists()
    out = tmp_path / "out"
    assert not out.exists() or not list(out.rglob("*")), "something landed in out/"


async def test_denied_path_never_reaches_the_network(avatars):
    async def explode(*a, **k):
        raise AssertionError("fetch was called for a path that should have been refused")
    avatars.fetch = explode
    with pytest.raises(OutputDenied):
        await avatars.to_png("uid", "../nope.png")


async def test_absolute_path_inside_output_dir_is_fine(avatars, tmp_path):
    res = await avatars.to_png("uid", str(tmp_path / "out" / "abs.png"))
    assert res["written"] is True


async def test_no_output_dir_means_no_writes_at_all(tmp_path):
    a = Avatars({"url": "http://unused"}, {}, output_dir=None)
    with pytest.raises(OutputDenied, match="disabled"):
        a._output_path("anything.png")


# --- the event loop stays free during slow I/O ------------------------------
#
# Regression: the SDK calls a plain-function tool inline on the loop, so a grep
# or directory walk stalled the WebSocket, the long-poll and every other session
# until it finished. The I/O tools now run their bodies off the loop.

import shutil
import time


async def test_a_slow_grep_does_not_stall_the_mailbox(tree, monkeypatch):
    root, _ = tree
    f = Files({"proj": root})
    monkeypatch.setattr("agent_bridge.files.shutil.which", lambda _: None)

    def slow_scan(*a, **k):
        time.sleep(0.6)
        return {"matches": [], "count": 0, "truncated": False, "engine": "python"}
    monkeypatch.setattr(f, "_grep_python", slow_scan)

    box = Mailbox()
    q = box.subscribe("x")
    grep_task = asyncio.create_task(f.grep("anything"))
    await asyncio.sleep(0.05)                      # the scan is now in its thread
    box.post("a", "x", "hello")
    # Blocked loop: this would only resolve once the 0.6 s scan returned.
    msg = await asyncio.wait_for(q.get(), timeout=0.2)
    assert msg.text == "hello"
    assert not grep_task.done()
    assert (await grep_task)["engine"] == "python"


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep not on PATH")
async def test_ripgrep_path_is_awaited(tree):
    root, _ = tree
    res = await Files({"proj": root}).grep("AvatarSize")
    assert res["engine"] == "ripgrep"
    assert res["count"] == 1 and "node_modules" not in res["matches"][0]
