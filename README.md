# agent-bridge-mcp

An MCP server that lets coding agents — Claude Code, Codex, a script, on one
machine or several — talk to each other through a durable mailbox, and, more
usefully, lets an agent answer most of its own questions about another
machine's source without waiting for a reply.

Built for a two-machine split where the source for both halves of a system lives
on one box and the running service lives on another, so the contract between
them is never checked in one place.

## What it exposes

One process, one port, three surfaces:

- **`/mcp`** — streamable-HTTP MCP for Claude Code on another machine
- **`/notify`** — a WebSocket that pushes each message as one text frame, so a
  listening agent is *told* rather than having to poll
- **`/api/*`** — the same mailbox over plain REST, for an agent that is already
  mid-session and can only reach for `curl`

Fourteen tools across four groups: a mailbox, read-only source access, an
allowlisted command runner, and probes for the specific decode path this was
built to debug.

## Quick start

```powershell
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e .
.venv\Scripts\agent-bridge init             # writes config.json with the admin token
                                          # then add your roots to it
tools\bridge.ps1 start
tools\bridge.ps1 firewall                 # elevated shell, opens the port to LocalSubnet
```

Then one credential per agent — a role name and a line on what it is:

```powershell
.venv\Scripts\agent-bridge agent add sisyphus  --description "Claude Code on the other box"
.venv\Scripts\agent-bridge agent add rl-claude --description "Claude Code in this repo" --local
.venv\Scripts\agent-bridge agent add rl-codex  --description "Codex CLI in this repo"    --local
```

Each prints the `claude mcp add` / `codex mcp add` lines to paste where that
agent runs. The token *is* the identity: an agent sends as its own name and
reads only its own mailbox. The admin token from `init` is for you at a shell,
not for an agent.

`curl http://<host>:8791/api/health` needs no token and separates "firewall"
from "wrong token" in one step.

## Design notes

The mailbox is **durable first, pushed second**. An agent mid-turn cannot
service a socket, so every message is queued before it is offered to whatever
happens to be listening; a listener that is absent, slow or dead loses nothing
and reads the same message on its next turn. Push only ever improves latency —
it is never the system of record. Unread messages are also exempt from capacity
eviction, so an unanswered question cannot be aged out by unrelated chatter.

Containment is the design rather than a wrapper: paths are resolved *before*
they are range-checked against the roots, and the exec allowlist is keyed by
name so a caller never composes a command line.

See [CLAUDE.md](CLAUDE.md) for the full reference, the security posture, and the
gotchas that cost real time; [docs/identity.md](docs/identity.md) for what an
agent, a credential and an instance are and why; [docs/threat-model.md](docs/threat-model.md)
for prompt injection, which is the threat this design is actually about.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests\ -q
```

## License

MIT
