# Identity and delivery

What an agent, a credential and an instance are; how a message finds its
recipient; and the choices that were considered and rejected. Written so a
reviewer who was not in the room can judge whether S1 is *right*, not only
whether it works. Roadmap stories are referenced by their IDs.

## The problem this solves

Before S1 the bridge had one shared token. It authorised *use* of the bridge
and named nobody: any caller could send as any `sender` and read any mailbox,
and `bridge_capabilities` said so in its hazards list. That was tolerable with
one Claude per machine and two machines. It is not tolerable with the actual
deployment, which has several agents on one machine — a Claude Code session
and a Codex session working in the same repository at the same time, possibly
with spawned workers — plus at least one more machine, plus scripts.

Two things had to become true:

1. The **sender** of a message is known, not claimed.
2. An agent can read **only its own** mail.

## Vocabulary

| Term | Meaning | In the code |
|---|---|---|
| **agent** | A *name* in the mailbox — a role such as `agent-a` ("the Claude in repo-a"), `agent-b`, `remote-agent`, `review-bot`. Anything can be behind it: a Claude session, a Codex session, a script, a bot, a human with `curl`. Not a machine, not a conversation. | the `agents` map in `config.json`; the `to`/`sender` of a message; `?agent=` |
| **credential** | Proves an agent name. One per role, issued by the operator with `agent add`. The only source of identity. | `auth.Credentials.agent_tokens` |
| **principal** | What a credential resolves to: a name plus whether it is the admin. | `auth.Principal` |
| **admin** | This bridge's own credential — the single `token`. Its name is `self_name`. May send as any name and read any mailbox, including the `*` wildcard listener. For the operator (scripts, local `curl`), never for an agent. | `Principal.admin`, `Credentials.admin_token` |
| **instance** | One session of a role: `agent-a#3f2a`. Self-declared, not separately authenticated; shares the role's credential and trust. Reserved in S1, delivered in S1b. | `Principal.may_read` accepts `name#…` |
| **mailbox** | The queue a name owns. Exists the moment anyone addresses the name. | `mailbox.Mailbox` |
| **peer** | Prose only: a remote machine. Not a concept in the code. | — |

These follow settled conventions in adjacent fields rather than inventing
new ones: *principal* is the security term (Java, Kerberos, Windows,
Kubernetes RBAC subjects); *agent* is A2A's and MCP Agent Mail's unit;
*mailbox* is the actor model's; the admin/agent split is Kubernetes'
User/ServiceAccount split and SPIFFE's "workload identity". MCP itself has no
notion of agent identity (host, client, server; OAuth bearer auth), so there
is nothing MCP-native to defer to.

## Why a credential is a role, not a machine or a conversation

**Not a machine.** The first draft called the config key `peers`, and a peer
is a machine. But the Claude and the Codex working in the same repository on
the same box must be two identities — a reply to one must be invisible to the
other — and they are on one machine. Location is metadata *about* an agent;
it goes in the description. The bridge cannot see where a caller runs and
must not pretend to.

**Not a conversation.** A credential is something the operator issues.
Conversations are created constantly, and workers are spawned by agents, not
by the operator. Per-conversation credentials would need either the operator
at the keyboard for every spawn or agents that can mint credentials — and an
agent that can mint a credential can name itself anything, which dissolves
the boundary the credential exists to draw. So the credential attaches to the
durable thing (the role) and a conversation is an *instance* of it.

**Therefore:** a spawned worker is another instance of its parent's role, with
its parent's authority. If a worker should have *different* authority, that
is a different role, and only the operator can create one. Isolation between
instances of one role is cooperative — either could declare the other's
instance id — and that is correct, because they are the same trust domain by
construction.

## How identity reaches a tool

Every route sits behind one middleware that resolves `Authorization: Bearer`
(or, on the WebSocket, the `bearer.<token>` subprotocol) through
`Credentials.identify()`. That proves the token. The tools then need to know
*who*.

The MCP SDK's streamable-HTTP transport attaches the Starlette request to the
tool's context (`request_context.request`). `caller()` re-reads the bearer
header from it and resolves it again — one extra `compare_digest` per call —
rather than stashing the principal in request state and threading it through
two frameworks. Whether the SDK actually attaches the request is not visible
from a unit test, so `tests/test_identity.py` runs uvicorn on a loopback port
and drives it with the SDK's own client.

**Considered for later (spike before S3):** the SDK has a native auth model —
a `TokenVerifier` returning an `AccessToken` with `client_id` and `scopes`,
and `get_access_token()` available in any tool. `client_id` = agent name and
`scopes` = S3's scopes would make identity and authorisation the SDK's
concept rather than ours. The risk is that the SDK's model is shaped around
OAuth 2.1 (issuer URL, `/.well-known/oauth-protected-resource`, a
`WWW-Authenticate` on 401 that may make a client start a discovery flow we
cannot serve). If Claude Code and Codex both behave with a static bearer
header against it, adopt it; if either tries to discover an authorisation
server, keep the middleware and record why.

## Delivery semantics

In legacy mode a mailbox is consumed by its reader: `bridge_inbox` marks read, and a frame
written to a `/notify` socket under the addressee's own name is consumed. Ack-required messages remain pending until explicit recipient acknowledgment;
see [wire.md](wire.md). With
one session per role that is the obvious behaviour. With several instances of
one role — three `agent-a` terminals — the question "who gets a message to
`agent-a`?" has three standard answers, and the design supports all three
rather than choosing one:

| Address | Semantics | Messaging name | Use |
|---|---|---|---|
| `agent-a` | first live instance to read it consumes it | work queue / competing consumers | "someone in repo-a, answer this" — what the bridge does today |
| `agent-a#3f2a` | only that instance | direct / reply-to | replying to the session that asked |
| `repo-a` (a group, S1c) | one copy per member mailbox | pub/sub / fan-out | "I changed the contract, everyone" |

A message's `sender` becomes the full address when the sender declared an
instance, so a reply naturally goes back to the asker; `thread` remains the
correlation id.

**Work-queue is the default because it is the cheapest.** Fan-out to *n*
instances costs *n* sessions reading, reasoning about, and possibly all
answering the same message — *n* times the context tokens and duplicated
work. The sender who wants that pays for it explicitly by addressing a group.
The one open fork: a *notice* (as opposed to a question) sent to a bare name
reaches one instance and not the others. That may eventually want
`deliver: one | all` on `bridge_send`; it is deliberately not decided in S1.

**What S1 does and does not deliver.** S1 makes the credential the identity
and reserves instance addressing in `Principal.may_read` so S1b needs no auth
change. S1b (instances) must additionally handle: declaring an instance on
`/notify` and on tools; per-instance subscriber queues; expiring instance
names that have no live subscriber and no unread mail; and unread mail to a
dead instance, which today would never evict (eviction drops *read* messages
only) and must be reassignable to the bare name or expirable. S1c (groups)
must batch persistence: a post is written before it returns, the store is
rewritten whole, and a five-member group is five rewrites unless batched.
S1b replaces P3's "per-session read receipts", which was the same problem
with a worse answer — receipts give every session every message and put the
"was this handled" question on nobody.

## The directory

A remote agent's real question is "who is here and who should I ask?".
`bridge_agents` answers it in one list: every credentialed agent with its
one-line description and what waits for it; the admin, labelled as the
operator; and any name that only ever appeared in traffic (the admin sending
as `script`, or mail to a name nobody holds), flagged `credentialed: false`
so a typo in `to` is visible instead of a silent mailbox. This replaces
`bridge_peers`, which listed traffic only, and merges the "who is configured"
and "who has mail" views that would otherwise need two names. Descriptions
are capped at one line (200 characters) so a directory of many agents stays
a few hundred tokens in the caller's context.

`/api/health` — the only unauthenticated route — no longer includes this
list, nor roots. It reports what a caller needs to tell a firewall problem
from a token problem from a Host-allowlist problem, and nothing about
traffic (S7).

## Wiring an agent

The mechanism that ties an identity to a *context* rather than a machine is
per-directory registration:

- **Claude Code:** `claude mcp add` run *in the directory the agent works
  in*, default (`local`) scope, which stores the server and its token in
  `~/.claude.json` keyed by directory. Never `--scope project`, which writes
  `.mcp.json` into the repository and commits the token.
- **Codex CLI:** `codex mcp add <name> --url <url> --bearer-token-env-var
  <VAR>`; the token is read from the environment, never placed on the command
  line. Project scope is `.codex/config.toml` in a trusted project.
- **Anything else:** an HTTP client with a bearer header against `/api/*`.

`agent add` prints all three, and `--local` prints loopback rather than the
LAN address for an agent on the bridge's own machine.

## Topology and scale

The mailbox is not per-host: every agent, on any machine, can use one
bridge's mailbox. What *is* per-host is source and exec — reading a machine's
files needs a bridge on that machine. So the shape that scales is **one hub
for mail, and a bridge per machine only where that machine's source or
commands must be exposed**. Past a handful of machines the pairwise token
exchange is the wall, and the answer is L1 (Tailscale identity), not a
distributed mailbox. The other walls, in the order they arrive: the JSON
store's whole-file rewrite per post (P3, SQLite); `BaseHTTPMiddleware` under
SSE load (G4). Nothing in S1 moves any of them closer.

## Decisions recorded

- Credential = role. Instances share it; different authority means a
  different role. (This document.)
- Config key `agents`, not `peers`; "peer" is prose for a remote machine.
- Identity is re-derived from the request in each tool call, not stashed.
- An agent's claimed `sender` is silently replaced with its own name, not
  refused, and the response reports the sender used: a session that calls
  itself `agent-a-worker` should not be blocked.
- Work-queue delivery is the default for a bare name; fan-out is explicit.
- `/api/health` says nothing about traffic.
