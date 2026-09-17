#
#  agent-bridge-mcp - Copyright(c) 2026
#

# One process, one port, three surfaces:
#
#   /mcp      streamable-HTTP MCP, for Claude Code on another machine
#   /notify   WebSocket, one text frame per message, for a live listener
#   /api/*    plain REST, the same mailbox
#
# The REST surface is not redundant with MCP. An agent already mid-session
# cannot gain a new MCP server without restarting, but it can always shell out
# to curl - so /api is how the agent on THIS machine answers a message the
# moment the bridge comes up, rather than after a restart.
#
# Auth: bearer token on HTTP. The WebSocket takes it as a query parameter
# instead, because the client that consumes /notify configures a URL and has
# nowhere to put a header. That is a real weakening (URLs land in logs), and it
# is why this binds to a LAN address and not to the internet.

import argparse
import asyncio
import hmac
import logging
import socket
from pathlib import Path

import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from agent_bridge.avatar import Avatars, OutputDenied
from agent_bridge.config import Config
from agent_bridge.execute import ExecDenied, Runner
from agent_bridge.files import Files, PathDenied
from agent_bridge.logs import Logs
from agent_bridge.mailbox import Mailbox

log = logging.getLogger("agent-bridge")

OPEN_PATHS = ("/api/health",)


# The MCP transport carries its own DNS-rebinding protection, which validates the
# Host header against an allowlist that is EMPTY by default. That default rejects
# every request that did not arrive as "localhost" with 421 Misdirected Request,
# while our own /api routes - which sit outside that middleware - keep answering.
# The result is a server that looks half-up from another machine: health responds,
# MCP does not. Hence deriving the allowlist rather than leaving it empty, and
# logging it at startup so a 421 is one glance to diagnose.
def allowed_hosts(cfg: Config) -> list[str]:
    configured = list(getattr(cfg, "allowed_hosts", []) or [])
    if configured:
        return configured

    names = {"localhost", "127.0.0.1"}
    try:
        hostname = socket.gethostname()
        names.add(hostname.lower())
        # A peer may address this box by its router-suffixed FQDN rather than by
        # IP, which is a different Host header and would otherwise 421.
        names.add(socket.getfqdn(hostname).lower())
        canonical, aliases, addresses = socket.gethostbyname_ex(hostname)
        names.add(canonical.lower())
        names.update(a.lower() for a in aliases)
        names.update(addresses)
    except OSError:
        pass

    # ":*" allows any port, so moving the bridge's port does not silently break
    # the allowlist and reintroduce exactly this bug.
    return sorted(f"{n}:*" for n in names if n)


# Mirrors the SDK's matching rule (exact, or a "host:*" any-port pattern) so
# /api/health can tell a caller whether ITS request would clear /mcp.
def _host_ok(host: str, allowed: list[str]) -> bool:
    if not host:
        return False
    if host in allowed:
        return True
    return any(host.startswith(a[:-1]) for a in allowed if a.endswith(":*"))


def build(cfg: Config):
    # A relative store or output directory sits beside config.json, not beside
    # whatever directory the service happened to be started from.
    def beside_config(p: str) -> Path | None:
        if not p:
            return None
        return Path(p) if Path(p).is_absolute() else Path(__file__).resolve().parents[2] / p

    box = Mailbox(capacity=int(cfg.inbox_max), store=beside_config(cfg.mailbox_store))
    files = Files(cfg.roots, int(cfg.max_read_bytes))
    runner = Runner(cfg.commands, cfg.roots, cfg.exec_enabled, cfg.exec_timeout)
    avatars = Avatars(cfg.gifterboard, cfg.roots, output_dir=beside_config(cfg.output_dir))
    logs = Logs(cfg.roots)

    hosts = allowed_hosts(cfg)
    mcp = FastMCP(
        name=f"agent-bridge@{cfg.self_name}",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            # Origin is absent on a Claude Code request; the middleware passes an
            # absent Origin, so an empty list here is permissive-by-absence, not
            # a block on legitimate clients.
            allowed_origins=[],
        ),
        instructions=(
            f"You are talking to the machine '{cfg.self_name}', where the Rogue-Lite game "
            f"client and the Sluzzygames server source both live.\n\n"
            f"CALL bridge_capabilities() FIRST. It lists every tool, the live WebSocket "
            f"subscription that removes the need to poll, the REST fallback, and the "
            f"hazards that have already caused false diagnoses here.\n\n"
            f"Use bridge_send to ask the agent here a question; it is delivered live and "
            f"also persisted to disk, so it survives that agent being mid-turn AND this "
            f"server restarting. Poll bridge_inbox for replies, or subscribe to /notify "
            f"and be told instead.\n\n"
            f"Most questions do not need a human or another agent: bridge_read and "
            f"bridge_grep expose both source trees, logs_read serves the game's engine.log "
            f"(with staleness warnings), and avatar_contract/avatar_probe answer 'do the "
            f"two sides still agree on the byte count' without anyone reading code."
        ),
    )

    # --- mailbox -----------------------------------------------------------

    @mcp.tool()
    def bridge_whoami() -> dict:
        """Identify this machine and summarise what the bridge exposes."""
        return {
            "self_name": cfg.self_name,
            "roots": {k: str(v) for k, v in cfg.roots.items()},
            "exec_enabled": cfg.exec_enabled,
            "commands": sorted(cfg.commands),
            "gifterboard_url": avatars.url or "(unset)",
            "peers": box.peers(),
        }

    @mcp.tool()
    def bridge_send(to: str, text: str, sender: str = "", thread: str = "") -> dict:
        """Send a message to an agent on another machine.

        Delivered live to any listener and queued durably, so it is read even if
        the recipient was mid-turn. Name yourself in `sender` so a reply can be
        addressed back. Use `thread` to keep one investigation together.
        """
        try:
            msg = box.post(sender or "remote", to, text, thread)
        except ValueError as e:
            return {"error": str(e)}
        return {"sent": True, "id": msg.id, "to": msg.to, "sender": msg.sender,
                "queued_for_recipient": box.unread_count(msg.to)}

    @mcp.tool()
    def bridge_inbox(agent: str, limit: int = 20, peek: bool = False, thread: str = "") -> dict:
        """Read unread messages addressed to `agent`, marking them read.

        Pass peek=true to look without consuming.
        """
        msgs = box.inbox(agent, limit=limit, peek=peek, thread=thread)
        return {"agent": agent, "count": len(msgs),
                "still_unread": box.unread_count(agent),
                "messages": [m.as_dict() for m in msgs]}

    @mcp.tool()
    async def bridge_wait(agent: str, timeout: float = 25.0, peek: bool = False) -> dict:
        """Block until a message arrives for `agent`, or until timeout.

        Use this instead of polling bridge_inbox on a timer. It returns the
        instant a message lands, so a reply costs a round trip rather than half
        a poll interval. Returns immediately if mail is already waiting. On
        timeout it returns an empty list, which is not an error - just call it
        again. This is the WebSocket's latency without the WebSocket, for a
        client that cannot open one.
        """
        waited = max(1.0, min(float(timeout), 120.0))

        # Anything already unread short-circuits: a caller must never block
        # while a question sits in its own inbox.
        existing = box.inbox(agent, limit=20, peek=peek)
        if existing:
            return {"agent": agent, "count": len(existing), "timed_out": False,
                    "messages": [m.as_dict() for m in existing]}

        q = box.subscribe(agent)
        try:
            await asyncio.wait_for(q.get(), timeout=waited)
        except asyncio.TimeoutError:
            return {"agent": agent, "count": 0, "timed_out": True,
                    "waited_s": waited, "messages": []}
        finally:
            box.unsubscribe(agent, q)

        # Re-read through the mailbox rather than returning the queued object, so
        # read state is recorded exactly as bridge_inbox would record it.
        msgs = box.inbox(agent, limit=20, peek=peek)
        return {"agent": agent, "count": len(msgs), "timed_out": False,
                "messages": [m.as_dict() for m in msgs]}

    @mcp.tool()
    def bridge_history(agent: str = "", limit: int = 50, thread: str = "") -> dict:
        """Recent traffic, read or not, for context on an ongoing thread."""
        return {"messages": [m.as_dict() for m in box.history(agent, limit, thread)]}

    @mcp.tool()
    def bridge_peers() -> dict:
        """Which agents have used this bridge, and what is waiting for each."""
        return {"peers": box.peers(), "self": cfg.self_name}

    @mcp.tool()
    async def bridge_capabilities() -> dict:
        """Everything this bridge offers: tools, live subscription, REST, hazards.

        Start here. The tool list is derived from what is actually registered
        rather than hand-maintained, so it cannot drift from reality.
        """
        registered = await mcp.list_tools()
        groups: dict[str, list] = {}
        for t in registered:
            group = ("mailbox" if t.name.startswith("bridge_") and
                     t.name.split("_")[1] in ("send", "inbox", "history", "peers",
                                              "whoami", "capabilities")
                     else "source" if t.name in ("bridge_read", "bridge_grep",
                                                 "bridge_list", "bridge_roots")
                     else "execution" if t.name in ("bridge_run", "bridge_commands")
                     else "logs" if t.name.startswith("logs_")
                     else "avatar" if t.name.startswith("avatar_")
                     else "other")
            groups.setdefault(group, []).append({
                "name": t.name,
                "purpose": (t.description or "").strip().splitlines()[0],
            })

        host = f"{cfg.self_name} ({cfg.host}:{cfg.port})"
        return {
            "self": cfg.self_name,
            "endpoint_host": host,
            "tools": groups,
            "live_subscription": {
                "what": "A WebSocket that pushes each message addressed to you as "
                        "ONE text frame, so you are told rather than polling "
                        "bridge_inbox. Any peer may subscribe under any agent name.",
                "url": f"ws://<this-host>:{cfg.port}/notify?agent=<your-name>&token=<token>",
                "note": "The token goes in the query string because a WebSocket "
                        "client config has nowhere to put a header. Unread "
                        "messages are replayed on connect, so subscribing late "
                        "does not miss what prompted you to connect. There is no "
                        "application keepalive by design - every text frame is a "
                        "real message.",
                "in_claude_code": "Monitor(ws={url: '...'}, persistent: true)",
                "IF THAT IS BLOCKED": "Claude Code's Monitor refuses WebSockets to private-range addresses, which makes /notify unusable across a LAN. Use bridge_wait() instead - it blocks until a message arrives and returns the same latency without a socket.",
            },
            "rest": {
                "GET  /api/health": "unauthenticated; reports host_seen, host_allowed, "
                                    "your_address - use it to tell a firewall problem "
                                    "from a token problem from a Host-allowlist 421",
                "GET  /api/inbox?agent=&limit=&peek=": "same mailbox",
                "POST /api/send": '{"sender","to","text","thread"}',
                "GET  /api/peers": "who has used this bridge",
                "GET  /api/wait?agent=&timeout=": "long-poll; returns the instant mail arrives, or empty on timeout. The curl twin of bridge_wait.",
                "why": "An agent already mid-session cannot gain a new MCP server "
                       "without restarting, but it can always shell out to curl.",
            },
            "roots": {k: str(v) for k, v in cfg.roots.items()},
            "commands": sorted(cfg.commands) if cfg.exec_enabled else [],
            "peers": box.peers(),
            "hazards": [
                "BACKSLASHES: Windows paths have been corrupted repeatedly in "
                "messages through this bridge (\\a and \\t eaten as escapes), which "
                "caused a real false diagnosis. Send paths with forward slashes, or "
                "JSON-escape them. Never trust a pasted Windows path here.",
                "STALE BUILDS: an absent engine.log usually means the binary predates "
                "Log.Path, not that a subsystem is silent. logs_list reports "
                "builds_without_engine_log and compares each log to the exe beside "
                "it - read those fields before concluding anything from an absence.",
                "ANY PEER CAN READ ANY MAILBOX: the token authorises use of the "
                "bridge, not an identity. Subscribing or reading as another agent's "
                "name is not prevented. Do not put secrets in messages.",
            ],
        }

    # --- read-only source access -------------------------------------------

    @mcp.tool()
    def bridge_roots() -> dict:
        """The source trees readable through this bridge."""
        return {"roots": {k: str(v) for k, v in cfg.roots.items()},
                "usage": "Address files as 'root:relative/path', e.g. "
                         "'rogue-lite:game/live/ViewerRegistry.cs'."}

    # The SDK calls a plain-function tool inline on the event loop, so every
    # tool below that touches the disk runs its body in a worker thread. Left
    # synchronous, one slow directory walk stalls the WebSocket, the long-poll
    # and every other session until it finishes.

    @mcp.tool()
    async def bridge_list(root: str = "", glob: str = "**/*", limit: int = 200) -> dict:
        """List files in a configured root, filtered by glob."""
        return await anyio.to_thread.run_sync(files.list, root, glob, limit)

    @mcp.tool()
    async def bridge_read(path: str, start: int = 1, count: int = 0) -> dict:
        """Read a file as numbered lines. Address it as 'root:relative/path'.

        `start`/`count` read a slice, which is required for large files.
        """
        try:
            return await anyio.to_thread.run_sync(files.read, path, start, count)
        except (PathDenied, OSError) as e:
            return {"error": str(e)}

    @mcp.tool()
    async def bridge_grep(pattern: str, root: str = "", glob: str = "", limit: int = 100,
                          context: int = 0, ignore_case: bool = False) -> dict:
        """Search the configured roots with a regular expression (ripgrep)."""
        return await files.grep(pattern, root, glob, limit, context, ignore_case)

    # --- allowlisted execution ---------------------------------------------

    @mcp.tool()
    def bridge_commands() -> dict:
        """The commands this bridge will run, and where each runs."""
        return {"enabled": cfg.exec_enabled, "commands": runner.describe()}

    @mcp.tool()
    async def bridge_run(name: str, args: list[str] | None = None, root: str = "") -> dict:
        """Run one allowlisted command by name and return its output.

        `name` must come from bridge_commands. You cannot compose a command line;
        `args` appends a small number of plain values where the entry allows it.
        """
        try:
            return await runner.run(name, args, root)
        except ExecDenied as e:
            return {"error": str(e)}

    # --- game logs ----------------------------------------------------------

    @mcp.tool()
    async def logs_list() -> dict:
        """Every engine.log / diag.log on this machine, newest first.

        Also lists builds that have NO engine.log, because that absence is the
        trap: a binary published before Log.Path was set never writes one, so
        "no log" means old binary, not "the subsystem never ran".
        """
        return await anyio.to_thread.run_sync(logs.list)

    @mcp.tool()
    async def logs_read(target: str = "", lines: int = 200, contains: str = "",
                        level: str = "") -> dict:
        """Read a game log, newest lines last. Defaults to the most recent one.

        `contains` is a regex filter, `level` keeps one of INFO/WARNING/ERROR.
        Every result carries the log's age and how it compares to the executable
        beside it, so a stale file cannot be read as current.
        """
        try:
            return await anyio.to_thread.run_sync(logs.read, target, lines, contains, level)
        except ValueError as e:
            return {"error": str(e)}

    # --- avatar / decode probes --------------------------------------------

    @mcp.tool()
    async def avatar_contract() -> dict:
        """Compare the avatar byte contract as written on BOTH sides.

        Reads AvatarSize from ViewerRegistry.cs and AVATAR_SIZE from game-feed.js
        and reports whether they still agree. A disagreement is silent at runtime.
        """
        return await anyio.to_thread.run_sync(avatars.expectations)

    @mcp.tool()
    async def avatar_probe(uid: str, size: int = 0, creator: str = "") -> dict:
        """Fetch one viewer's avatar from the running server and judge the bytes.

        Reports the received length against the length the game requires, what
        the body actually looks like if it is wrong, and whether the pixels are
        blank or transparent if it is right.
        """
        try:
            return await avatars.probe(uid, size, creator)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    @mcp.tool()
    async def avatar_png(uid: str, out_path: str, size: int = 0, creator: str = "") -> dict:
        """Write a viewer's decoded avatar to a PNG so it can be looked at.

        `out_path` is relative to this bridge's output directory and must end
        in .png; nothing outside that directory is ever written.
        """
        try:
            return await avatars.to_png(uid, out_path, size, creator)
        except OutputDenied as e:
            return {"error": str(e), "output_dir": str(avatars.output_dir)}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    # --- HTTP app ----------------------------------------------------------

    def authorised(request_or_ws) -> bool:
        if not cfg.token:
            return True
        header = request_or_ws.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        supplied = supplied or request_or_ws.query_params.get("token", "")
        return hmac.compare_digest(supplied, cfg.token)

    class Auth(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.url.path in OPEN_PATHS or authorised(request):
                return await call_next(request)
            return JSONResponse({"error": "bad or missing token"}, status_code=401)

    async def health(request: Request):
        # host_seen and allowed_hosts are here so a peer that cannot reach /mcp
        # can diagnose it from ITS OWN side in one request. The Host header is
        # the address the caller dialled - this machine - never the caller's own
        # name, which is the thing that makes the allowlist confusing.
        return JSONResponse({
            "ok": True,
            "self": cfg.self_name,
            "peers": box.peers(),
            "roots": sorted(cfg.roots),
            "host_seen": request.headers.get("host", ""),
            "allowed_hosts": hosts,
            "host_allowed": _host_ok(request.headers.get("host", ""), hosts),
            "your_address": request.client.host if request.client else "",
        })

    async def api_send(request: Request):
        body = await request.json()
        try:
            msg = box.post(body.get("sender", "local"), body.get("to", ""),
                           body.get("text", ""), body.get("thread", ""))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"sent": True, "id": msg.id})

    async def api_inbox(request: Request):
        q = request.query_params
        peek = q.get("peek", "0") not in ("0", "", "false")
        msgs = box.inbox(q.get("agent", cfg.self_name), int(q.get("limit", 20)),
                         peek, q.get("thread", ""))
        return JSONResponse({"count": len(msgs), "messages": [m.as_dict() for m in msgs]})

    async def api_peers(request: Request):
        return JSONResponse({"peers": box.peers()})

    # The curl-shaped twin of bridge_wait, for a peer whose WebSocket client is
    # restricted (Claude Code's Monitor refuses private-range addresses, which
    # makes /notify unusable across a LAN even though the endpoint is fine).
    async def api_wait(request: Request):
        q_params = request.query_params
        agent = q_params.get("agent", cfg.self_name)
        waited = max(1.0, min(float(q_params.get("timeout", 25)), 120.0))
        peek = q_params.get("peek", "0") not in ("0", "", "false")

        existing = box.inbox(agent, limit=20, peek=peek)
        if existing:
            return JSONResponse({"count": len(existing), "timed_out": False,
                                 "messages": [m.as_dict() for m in existing]})

        q = box.subscribe(agent)
        try:
            await asyncio.wait_for(q.get(), timeout=waited)
        except asyncio.TimeoutError:
            return JSONResponse({"count": 0, "timed_out": True, "messages": []})
        finally:
            box.unsubscribe(agent, q)

        msgs = box.inbox(agent, limit=20, peek=peek)
        return JSONResponse({"count": len(msgs), "timed_out": False,
                             "messages": [m.as_dict() for m in msgs]})

    async def notify(ws: WebSocket):
        if not authorised(ws):
            await ws.close(code=4401)
            return
        agent = (ws.query_params.get("agent") or cfg.self_name).lower()
        await ws.accept()
        q = box.subscribe(agent)

        # Anything already waiting is replayed first, so connecting late does not
        # mean missing the message that prompted someone to connect.
        #
        # Peeked, then marked read only AFTER the frame is actually on the wire.
        # Leaving it unread meant a listener that reads its mail from this socket
        # never consumed anything, so every reconnect replayed the same backlog -
        # and this server reconnects often. Marking read before sending would be
        # the opposite bug: a send that fails would drop the message silently.
        backlog = box.inbox(agent, limit=20, peek=True)
        try:
            for m in backlog:
                await ws.send_text(_frame(m, pending=True))
                m.read = True
            if backlog:
                box.flush()
            # No application-level keepalive on purpose. Every text frame this
            # socket sends becomes a notification in the listening agent's
            # session, so a heartbeat would interrupt it on a timer for no
            # information. uvicorn sends protocol-level pings already, which
            # keep the connection alive without waking anyone.
            while True:
                await ws.send_text(_frame(await q.get()))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            box.unsubscribe(agent, q)

    app = mcp.streamable_http_app()
    app.routes.append(WebSocketRoute("/notify", notify))
    app.routes.append(Route("/api/health", health, methods=["GET"]))
    app.routes.append(Route("/api/send", api_send, methods=["POST"]))
    app.routes.append(Route("/api/inbox", api_inbox, methods=["GET"]))
    app.routes.append(Route("/api/peers", api_peers, methods=["GET"]))
    app.routes.append(Route("/api/wait", api_wait, methods=["GET"]))
    app.add_middleware(Auth)
    return app


def _frame(msg, pending: bool = False) -> str:
    # One line of prose first: whatever consumes this shows the frame to a human
    # or to a model, and a bare JSON blob buries the actual question.
    head = f"[bridge] {msg.sender} -> {msg.to}"
    if msg.thread:
        head += f" ({msg.thread})"
    if pending:
        head += " [unread backlog]"
    return f"{head}: {msg.text}"


def main() -> None:
    ap = argparse.ArgumentParser(prog="agent-bridge-mcp")
    ap.add_argument("--config", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    cfg = Config.load(args.config)
    if not cfg.token:
        log.warning("no token set - every peer on the LAN can use this bridge")

    host = args.host or cfg.host
    port = args.port or int(cfg.port)
    log.info("agent-bridge '%s' on http://%s:%d  (mcp=/mcp  ws=/notify  rest=/api)",
             cfg.self_name, host, port)
    for name, path in cfg.roots.items():
        log.info("  root %-14s %s", name, path)
    log.info("  allowed Host headers: %s", ", ".join(allowed_hosts(cfg)))

    uvicorn.run(build(cfg), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
