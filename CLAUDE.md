# agent-bridge-mcp

An MCP server that lets a Claude agent on another machine talk to the agent on
this one, read the source trees this one works in, and probe the viewer-avatar
decode path that spans both.

Built for the FENRIR <-> SISYPHUS split: the Rogue-Lite game client and the
Sluzzygames server *source* live on FENRIR, while the GifterBoard server and its
ffmpeg actually *run* on SISYPHUS. The avatar contract spans that gap and
nothing checks it, which is what this bridge exists to fix.

## Quick Reference

| What | Command |
|------|---------|
| Start | `tools\bridge.ps1 start` |
| Status | `tools\bridge.ps1 status` |
| Stop / restart | `tools\bridge.ps1 stop` / `restart` |
| Open the LAN port | `tools\bridge.ps1 firewall` (**elevated shell**) |
| Tests | `.venv\Scripts\python.exe -m pytest tests\ -q` |
| Install | `uv venv .venv; uv pip install --python .venv\Scripts\python.exe -e .` |

Logs are `server.log` / `server.err` next to `config.json`. A silent failure to
start is almost always the port already being held — `status` says so.

## Layout

```
src/agent_bridge/
  server.py     FastMCP tools + the WS and REST routes; the only file that knows about HTTP
  mailbox.py    durable message store + live fan-out
  files.py      scoped read/list/grep over the configured roots
  execute.py    allowlisted command runner
  avatar.py     the decode probes and a dependency-free PNG encoder
  config.py     config.json loader
tools/bridge.ps1  start/stop/status/firewall
config.json       machine-specific, gitignored — holds the token
```

## The three surfaces

One process, one port (**8791**), three ways in:

| Path | Protocol | Who uses it |
|---|---|---|
| `/mcp` | streamable HTTP | Claude Code on another machine |
| `/notify` | WebSocket, one text frame per message | a live listener that wants to be *told*, not to poll |
| `/api/*` | plain REST | an agent already mid-session, via `curl` |

`/api` is **not** redundant with `/mcp`. A Claude Code session cannot gain a new
MCP server without restarting, so the agent on this machine would otherwise be
unable to answer a message until the next session. `curl` always works.

## Connecting from another machine

On SISYPHUS, with FENRIR at `192.168.1.174`:

```powershell
claude mcp add --transport http fenrir `
  http://192.168.1.174:8791/mcp `
  --header "Authorization: Bearer <token from FENRIR's config.json>"
```

Check it first without Claude Code — `/api/health` needs no token and is the
fastest way to tell "firewall" apart from "wrong token":

```powershell
curl http://192.168.1.174:8791/api/health
```

**ICMP is blocked on both boxes**, so `ping sisyphus` times out on a machine
that is perfectly reachable. Test with SMB or the health endpoint, never ping.

## Receiving messages without polling

The point of `/notify` is that a message *arrives* rather than being asked for.
In Claude Code, the `Monitor` tool consumes it directly:

```
Monitor(ws: {url: "ws://127.0.0.1:8791/notify?agent=fenrir&token=<token>"},
        persistent: true)
```

Each text frame becomes one notification in the session. Two consequences shaped
the implementation:

- **No application-level keepalive.** A heartbeat text frame would interrupt the
  listening agent on a timer, forever, carrying no information. uvicorn's
  protocol-level pings keep the socket alive instead.
- **The token goes in the query string**, because a `Monitor` ws config accepts
  a URL and has nowhere to put a header. URLs land in logs, which is a real
  weakening — and the reason this binds to the LAN and not the internet.

## Tools

| Tool | Purpose |
|---|---|
| `bridge_whoami` / `bridge_peers` / `bridge_roots` | what this machine is and what it exposes |
| `bridge_send` / `bridge_inbox` / `bridge_history` | the mailbox |
| `bridge_list` / `bridge_read` / `bridge_grep` | read-only source access |
| `bridge_commands` / `bridge_run` | allowlisted execution |
| `avatar_contract` / `avatar_probe` / `avatar_png` | the decode path |

Files are addressed as `root:relative/path`, e.g.
`rogue-lite:game/live/ViewerRegistry.cs`.

## The avatar contract, and why these tools exist

`avatar_contract` reads `AvatarSize` out of `ViewerRegistry.cs` and
`AVATAR_SIZE` out of `game-feed.js` and reports whether they still agree. It
reads both rather than asserting a remembered number, because the whole failure
mode is the two drifting apart.

The reason a drift is worth tooling: **it is silent on both sides.**

- `decodeToRgba()` resolves `null` whenever ffmpeg returns anything other than
  exactly `size*size*4` bytes.
- `ViewerRegistry.BeginDownload` discards the response unless it is exactly
  `AvatarBytes`, and swallows every exception, because "this viewer has no
  picture" is a normal outcome.

So a mismatch does not raise, log, or fail a test. It renders a plain monster —
indistinguishable from a viewer who genuinely has no avatar. `avatar_probe`
turns that into a sentence: it reports the received length against the required
length, sniffs what the body actually is when it is wrong (an undecoded JPEG
reads very differently from a JSON error), and checks for all-transparent or
all-black pixels when the length is right but the picture is still missing.

## Security posture

This server reads source and runs commands, so the containment is the design,
not a wrapper around it:

1. **Paths are resolved before they are checked.** A caller-supplied path is
   made absolute and then required to sit under a configured root. Checking the
   string first and resolving after would pass both `..\..\Windows` and a
   junction pointing out of the tree.
2. **The exec allowlist is keyed by name, not by prefix.** A remote agent asks
   for `viewers`; it never composes a command line. Prefix matching is the
   version of this that looks equivalent and is not — allowing `git log` as a
   prefix also allows `git log; rm -rf`.
3. **Nothing runs through a shell.** `argv` lists, `shell=False`. Extra
   arguments are additionally filtered to `[A-Za-z0-9._=-]`.
4. **The firewall rule is `LocalSubnet`, not `Any`**, and the bind is on a
   private-profile LAN interface.
5. `.env`, `config.json` and credentials files are on a deny list, and
   `node_modules`/`.git`/`bin`/`obj` are skipped by list, grep and read.
6. **The one tool that writes (`avatar_png`) is confined to `output_dir`**
   (default `out/` beside `config.json`) and to `.png` names. It used to take
   an arbitrary absolute path, which made a read-only bridge able to overwrite
   any file the server's user could.

The token is a shared secret in `config.json` (gitignored). Rotating it means
editing that file and re-running `claude mcp add` on every peer.

## Gotchas

- **The MCP transport has its own Host allowlist, and the SDK's default is
  empty.** `TransportSecurityMiddleware` (DNS-rebinding protection) answers every
  request whose `Host` header is not on the list with **421 Misdirected
  Request** — and it wraps only the MCP route. `/api/*` sits outside it and keeps
  answering, so from another machine the server looks *half up*: health responds,
  MCP does not, and the peer reports "health is the only endpoint I can see."
  `allowed_hosts()` derives this machine's names, FQDN and IPs at startup and
  logs them; override with `allowed_hosts` in `config.json`. Every entry uses the
  `:*` any-port form so changing the port cannot silently re-break it.
  Diagnose with:
  ```powershell
  curl -i -X POST http://<host>:8791/mcp -H "Accept: application/json, text/event-stream" `
       -H "Content-Type: application/json" -H "Authorization: Bearer <token>" `
       -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
  ```
  `421` is the host allowlist, `401` is the token, `200` with an SSE body is healthy.

  **A peer does not go in this list.** `allowed_hosts` is matched against the
  `Host` header, which is the address the caller *dialled* — this machine —
  not the caller's own name. SISYPHUS asking for `http://192.168.1.174:8791`
  sends `Host: 192.168.1.174:8791`; the name "sisyphus" never appears. What
  authorises a peer is the bearer token. `/api/health` reports `host_seen`,
  `host_allowed` and `your_address` so a peer can settle this from its own
  side in one unauthenticated request.
- **`Files` defines a method named `list`**, which shadows the builtin *inside
  the class body*. An eagerly evaluated `list[Path]` annotation there resolves
  to the method and raises `TypeError` at import. `from __future__ import
  annotations` is load-bearing, not tidiness.
- **uvicorn has no WebSocket support by default.** Without `websockets` or
  `wsproto` installed, `/notify` returns "Expected 101 status code" and the only
  clue is a `WARNING: No supported WebSocket library detected` line in
  `server.err`. `websockets` is a pinned dependency for this reason.
- **ripgrep is not on this machine's PATH** — it ships inside VS Code and Unity
  bundles behind versioned directory names that rot. `files.py` falls back to a
  pure-Python scan, so `bridge_grep` reports which engine it used.
- **The venv's `python.exe` is a trampoline.** It re-execs the uv-managed base
  interpreter, so the PID `Start-Process -PassThru` hands back is the *parent*
  of the process holding the socket. Killing the launched PID leaves the port
  bound and the next start dies on bind. `bridge.ps1` kills by **port owner**,
  and records the listener rather than the launcher.
- **Stopping the server must poll for the port to free, not sleep.** A
  signalled process holds the socket for a moment longer, so a fixed
  `Start-Sleep` makes `restart` fail intermittently with "already listening".
- **Filter nulls out of a PID list before counting it.** A `$null` element
  leaves `.Count` non-zero while every `Stop-Process` silently no-ops, so
  `stop` reports success and kills nothing. This actually happened here.
- `Start-Process -PassThru` returns a process whose `HasExited` is the only
  quick way to tell a bind failure from a slow start; `bridge.ps1` polls
  `/api/health` rather than sleeping a fixed guess.
