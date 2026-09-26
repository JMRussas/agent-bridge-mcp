# agent-bridge-mcp

A communication bridge for agents and models: a durable mailbox with live
push, a directory of who is reachable, identity that comes from a credential
rather than a claim, and, on a machine whose source must be exposed, scoped
read and allowlisted exec over the trees it is given.

Nothing in `src/` is specific to any project. Roots, commands and log names
come from `config.json`; the two-machine game setup this was first built for
survives only as a worked example in [docs/examples/](docs/examples/).
[docs/ROADMAP.md](docs/ROADMAP.md) is the plan.

## Quick Reference

| What | Command |
|------|---------|
| First run | `.venv\Scripts\agent-bridge init` (writes `config.json`, generates the admin token, prints its registration commands) |
| Show / rotate the admin token | `.venv\Scripts\agent-bridge token` / `token --rotate` |
| Give an agent its credential | `.venv\Scripts\agent-bridge agent add agent-a --description "Claude Code in /path/to/repo-a" [--local]` (prints its registration for Claude Code and Codex) |
| List / show / remove agents | `agent-bridge agent list` / `agent show <name>` / `agent remove <name>` |
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
  auth.py       credential -> Principal (agent name or admin); which mailbox a caller may touch
  mailbox.py    durable message store + live fan-out
  files.py      scoped read/list/grep over the configured roots
  execute.py    allowlisted command runner
  logs.py       named log files under the roots, dated against the build beside them
  patterns.py   caller-supplied regexes, with a deadline
  config.py     config.json loader
  cli.py        init / token / agent / serve
tools/bridge.ps1  start/stop/status/firewall
docs/diagrams/    deployment, class and sequence diagrams (SVG); embedded in README
docs/examples/    the setup this was first built for, as a worked example
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

## Vocabulary

Four words, used precisely; [docs/identity.md](docs/identity.md) has the
reasoning and the alternatives that were rejected.

- **agent** — a *name* in the mailbox. A role: "the Claude in repo-a",
  "the Codex in repo-a", "the review bot". Anything can be behind it
  and the bridge neither knows nor cares. Not a machine, not a conversation.
- **credential** — proves an agent name. One per role, issued by the operator
  with `agent add`. It is the *only* source of identity: the sender of a
  message and the mailbox a request may touch follow from it, never from the
  request body.
- **admin** — this bridge's own credential (the single `token`). Its name is
  `self_name` and it may act as anyone. For the operator — scripts, local
  `curl` — **never for an agent**: an agent on the admin token sends as the
  bridge itself, reads every mailbox, and the whole boundary evaporates.
- **peer** — prose only: a remote machine. Not a thing in the code.

An **instance** (`agent-a#3f2a`) is one session of a role — the addressing is
reserved now, delivered in S1b. Instances share their role's credential and
trust; if two things must not read each other's mail, they are two roles.

## Connecting an agent

On the machine running the bridge, give the agent a credential — a role name
and one line on what it is — and paste what it prints where that agent runs:

```powershell
.venv\Scripts\agent-bridge agent add remote-agent --description "Claude Code on the other box"
.venv\Scripts\agent-bridge agent add agent-a      --description "Claude Code in /path/to/repo-a" --local
.venv\Scripts\agent-bridge agent add agent-b      --description "Codex CLI in /path/to/repo-a" --local
```

Each prints a `claude mcp add` line (run it *in the directory that agent works
in*; the default `local` scope keeps the token in `~/.claude.json` — never
`--scope project`, which commits it to `.mcp.json`) and a `codex mcp add` line
(the token goes in an environment variable, never on the command line).
`--local` prints loopback instead of the LAN address.

A request with `agent-a`'s token *is* `agent-a`: its messages carry that
sender whatever the body says, and `bridge_inbox`, `bridge_wait` and `/notify`
default to — and are confined to — its own mailbox. Two agents in the same
repo on the same box are two credentials; a reply to one is invisible to the
other. `bridge_agents` is the directory: every credentialed agent with its
description, so a remote agent can pick who to ask.

Check it first without Claude Code — `/api/health` needs no token and is the
fastest way to tell "firewall" apart from "wrong token":

```powershell
curl http://<bridge-host>:8791/api/health
```

**Host firewalls often block ICMP**, so `ping` can time out against a machine
that is perfectly reachable. Test with the health endpoint, never ping.

## Receiving messages without polling

The point of `/notify` is that a message *arrives* rather than being asked for.
In Claude Code, the `Monitor` tool consumes it directly:

```
Monitor(ws: {url: "ws://127.0.0.1:8791/notify?agent=<your-name>",
             protocols: ["bridge", "bearer.<token>"]},
        description: "bridge mail", timeout_ms: 1800000)
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
| `bridge_whoami` / `bridge_agents` / `bridge_roots` | what this machine is, who is reachable, what it exposes |
| `bridge_send` / `bridge_inbox` / `bridge_history` | the mailbox |
| `bridge_list` / `bridge_read` / `bridge_grep` | read-only source access |
| `bridge_commands` / `bridge_run` | allowlisted execution |
| `logs_list` / `logs_read` | named log files; registered only when `logs.names` is set |

Files are addressed as `root:relative/path`, e.g. `repo-a:src/main.py`.

## Logs are a separate surface, and why

`logs.names` in `config.json` lists log *filenames* (`app.log`, `diag.log`).
`logs_list` finds them under every root, including inside `bin/` and `dist/`
where `bridge_read` refuses to go, because reading a log is not trawling build
output. `logs.exe_names` lists the executables that write them; with those
set, every listing dates each log against the executable beside it and flags
build folders that lack one. That flag is the point: a binary built before the
code that writes the log produces none, and "no log" then reads as "the
subsystem never ran" when it means "old binary". With no `logs.names`, the two
tools are not registered at all.

## Security posture

This server reads source and runs commands, so the containment is the design,
not a wrapper around it:

1. **Paths are resolved before they are checked.** A caller-supplied path is
   made absolute and then required to sit under a configured root. Checking the
   string first and resolving after would pass both `..\..\Windows` and a
   junction pointing out of the tree.
2. **The exec allowlist is keyed by name, not by prefix.** A remote agent asks
   for `build`; it never composes a command line. Prefix matching is the
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
6. **No tool writes to disk.** The mailbox store is the only file the bridge
   touches. The one writing tool this ever had took an arbitrary path once,
   which made a read-only bridge able to overwrite any file the server's user
   could; a future tool that must write gets one configured directory and a
   suffix allowlist, nothing more.

7. **Identity comes from the credential, never from the request body.**
   `auth.Credentials` resolves a bearer token to a `Principal`: an agent's
   token names that agent (`agents` in `config.json`), the single `token` is
   the admin. `bridge_send(sender=...)` is honoured only for the admin; an
   agent's messages carry its own name. An agent may read, wait on or listen
   to its own mailbox (or an instance of it, `name#…`) only; the admin may
   name any, including `*`. Tools learn who called them from the Starlette
   request the streamable-HTTP transport attaches to the tool context,
   re-checked against the same header the middleware verified.
   `tests/test_identity.py` proves this through a real uvicorn session,
   because a unit test cannot see whether the SDK actually attached it.
8. **`/api/health` is the only unauthenticated route and says nothing about
   traffic** — `ok`, `self`, and the Host-allowlist diagnostics. Names, unread
   counts and roots are behind auth (`/api/agents`, `bridge_roots`).
9. **`mailbox.json` is on the deny list**, so a root that contains the bridge's
   own checkout cannot turn `bridge_read` into a way around the mailbox
   boundary.

Credentials live in `config.json` (gitignored). `agent-bridge init` generates
the admin token (32 random bytes as hex); `agent-bridge agent add <name>`
generates an agent's. `token --rotate` / `agent add --rotate` replace one;
then restart the bridge and re-register where it was used. Config loading
refuses an agent with a blank or placeholder token, a name that is this
machine's or `*` or contains `#`, and any two credentials that are equal.
**A placeholder admin token (`CHANGE_ME`) is never accepted as a credential**,
and **serving refuses to bind anything but loopback with no credential at
all** (no usable admin token *and* no agents) — a copied example cannot go
live open by accident.

The threat that matters most here is not the network; it is **prompt
injection through the content the bridge carries** — messages, source files,
and logs that may hold text typed by strangers. [docs/threat-model.md](docs/threat-model.md)
ranks the vectors and the controls, in the order they are being built.

## Gotchas

- **Line endings are LF, and the working copy must match, not just the
  commit.** `.gitattributes` normalises to LF on commit (CRLF only for
  `*.ps1`/`*.bat`/`*.cmd`), `.editorconfig` and `.vscode/settings.json` make
  editors write LF, and the repo's git config has `core.autocrlf=false`,
  `core.eol=lf`. The thing that kept breaking it was scripts: **Python's
  `write_text()` on Windows writes CRLF unless you pass `newline="\n"`**, and
  the next commit then warns for every file touched. `tests/test_repo_hygiene.py`
  fails on any tracked text file with the wrong ending, so it is caught where
  it is introduced.
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
  not the caller's own name. A peer asking for `http://10.0.0.5:8791`
  sends `Host: 10.0.0.5:8791`; the peer's own name never appears. What
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
