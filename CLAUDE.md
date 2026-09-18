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
| First run | `.venv\Scripts\agent-bridge init` (writes `config.json`, generates the token, prints the peer commands) |
| Show / rotate the token | `.venv\Scripts\agent-bridge token` / `token --rotate` |
| Start | `tools\bridge.ps1 start` |
| Status | `tools\bridge.ps1 status` |
| Stop / restart | `tools\bridge.ps1 stop` / `restart` |
| Open the LAN port | `tools\bridge.ps1 firewall` (**elevated shell**) |
| Tests | `.venv\Scripts\python.exe -m pytest tests\ -q` (warnings are errors) |
| Install | `uv venv .venv; uv pip install --python .venv\Scripts\python.exe -e .[dev]` |

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
Monitor(ws: {url: "ws://127.0.0.1:8791/notify?agent=fenrir",
             protocols: ["bridge", "bearer.<token>"]},
        description: "bridge mail for fenrir", timeout_ms: 1800000)
```

Each text frame becomes one notification in the session. Two consequences shaped
the implementation:

- **No application-level keepalive.** A heartbeat text frame would interrupt the
  listening agent on a timer, forever, carrying no information. uvicorn's
  protocol-level pings keep the socket alive instead.
- **The token rides in `Sec-WebSocket-Protocol`, not the URL.** A `Monitor` ws
  config has no header field, but it has `protocols` — and that *is* a header.
  The client offers `["bridge", "bearer.<token>"]`; the server verifies the
  second and selects the first. Same trick Kubernetes uses for `kubectl exec`.
  The older `?token=` form is gone: it put the secret in every access log
  between the peer and this box. Every HTTP route takes `Authorization: Bearer`
  and nothing else. Because of this, **the token must be made of subprotocol
  characters** (`A-Z a-z 0-9 - . _ ~` and a few others); config loading refuses
  one that is not. A 32-char hex or base64url token is fine.

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
   **Children do not inherit the server's environment.** The server was
   started from a developer shell, which is where the API keys live; an
   allowlisted `printenv` would have handed them to the peer. A child gets
   `PASSTHROUGH` (what dotnet and git need to find themselves), the
   `DOTNET_*`/`NO_COLOR` opt-outs, and the command spec's own `env` map.
4. **The firewall rule is `LocalSubnet`, not `Any`**, and the bind is on a
   private-profile LAN interface.
5. `.env`, `config.json` and credentials files are on a deny list, and
   `node_modules`/`.git`/`bin`/`obj` are skipped by list, grep and read.
6. **The one tool that writes (`avatar_png`) is confined to `output_dir`**
   (default `out/` beside `config.json`) and to `.png` names. It used to take
   an arbitrary absolute path, which made a read-only bridge able to overwrite
   any file the server's user could.

The token is a shared secret in `config.json` (gitignored), generated by
`agent-bridge init` (32 random bytes as hex). `agent-bridge token --rotate`
replaces it; then restart the bridge and re-run `claude mcp add` on every peer.
**Serving refuses to bind anything but loopback with an empty or `CHANGE_ME`
token** — a copied example cannot go live open by accident.

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
- **The MCP SDK calls a plain-function tool inline on the event loop.** A
  sync tool that walks a directory or waits on a subprocess stalls `/notify`,
  `/api/wait` and every other session until it returns. Every tool that touches
  the disk is therefore `async` and runs its body under
  `anyio.to_thread.run_sync`; ripgrep is an awaited subprocess. Keep it that
  way when adding a tool - the mailbox tools are the only ones cheap enough to
  stay sync.
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
  pure-Python scan, so `bridge_grep` reports which engine it used. Point
  `ripgrep_path` in `config.json` at a bundled `rg.exe` to use it anyway.
- **Caller-supplied regexes go through `patterns.py`, never `re`.** The
  standard library backtracks without bound — `(a|a)*$` on 28 characters takes
  half a minute — and a peer supplies the pattern. `patterns.Deadline` wraps the
  `regex` module's per-search timeout in one budget per request (20 s for grep,
  10 s for the log filter) and reports `reason: "timeout"` with what it managed
  to scan. ripgrep is a finite automaton and needs none of this.
- **The venv's `python.exe` is a trampoline.** It re-execs the uv-managed base
  interpreter, so the PID `Start-Process -PassThru` hands back is the *parent*
  of the process holding the socket. Killing the launched PID leaves the port
  bound and the next start dies on bind. `bridge.ps1` kills by **port owner**,
  and records the listener rather than the launcher.
- **Read-state writes to `mailbox.json` are coalesced (250 ms); posts are not.**
  A post is written before it returns, because losing one to a kill is a
  dropped question. Marking messages read only flips flags, happens on every
  inbox read and every socket frame, and used to rewrite the whole file each
  time; those now share one timer. A `Stop-Process -Force` inside that window
  costs one re-delivery, never a message. A clean shutdown flushes.
- **Stopping the server must poll for the port to free, not sleep.** A
  signalled process holds the socket for a moment longer, so a fixed
  `Start-Sleep` makes `restart` fail intermittently with "already listening".
- **Filter nulls out of a PID list before counting it.** A `$null` element
  leaves `.Count` non-zero while every `Stop-Process` silently no-ops, so
  `stop` reports success and kills nothing. This actually happened here.
- `Start-Process -PassThru` returns a process whose `HasExited` is the only
  quick way to tell a bind failure from a slow start; `bridge.ps1` polls
  `/api/health` rather than sleeping a fixed guess.
