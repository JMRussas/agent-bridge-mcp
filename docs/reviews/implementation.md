# Reliability and evidence implementation

This implements the October 6 review in five separately reviewed commits.
No running service or machine-specific configuration is changed by these commits.

## Build versus adopt checkpoint

Source comparison against MCP Agent Mail's public README:
https://github.com/Dicklesworthstone/mcp_agent_mail

It offers acknowledgments, registration, searchable history, and advisory
reservations. Reservation conflicts are returned alongside grants, rather than
necessarily refusing acquisition. Windows Git-hook shims are documented, but
native-host installation, credential isolation, and compatibility with existing
REST clients have not been demonstrated here. Its mailbox would not replace the
bridge's source, logs, and command surfaces. Decision: retain the current bridge
and its existing protocol; avoid a speculative migration and extra service.
This is a bounded source-level compatibility assessment, not a hands-on trial.

## Step 1: retained evidence

SQLite stores history without automatic eviction. Existing JSON paths map to
sibling `.sqlite3` files and migrate transactionally once; originals are retained.
Pending count/text-byte limits reject sends. UUIDs and bridge identity persist.
Legacy read-state changes remain coalesced; new messages commit before fan-out.

Review: corrupt JSON and failed writes must fail explicitly; migration must
preserve IDs; database artifacts must not bypass per-role read boundaries.
Checks cover transactional send failure, one-time migration, stable identifiers,
pending pressure, restart, and existing HTTP/MCP behavior.

## Step 2: acknowledgment and provenance

`ack_required=true` on send prevents every legacy consumer from clearing the
message. Receivers may additionally opt into `ack_mode=true` on MCP inbox/wait,
or `?ack=explicit` on REST inbox/wait and WebSocket. `/notify?format=json` provides
structured frames. `bridge_ack(message_id)` / `POST /api/ack {message_id}` accept
UUIDs or legacy IDs and are recipient/operator-only and idempotent. REST history
now matches MCP history authorization. Server-stamped principal/admin/impersonation
fields distinguish operator messages; imported messages have unknown provenance.

Review fixed stale socket objects potentially overwriting acknowledgment state.
Tests cover reconnect replay, legacy consumer protection, restart, idempotence,
and cross-role denial. Delivery events mean socket writes, not model observation;
consumed events retain the legacy semantics. Acknowledgment means responsibility,
not completion, verification, or acceptance.

## Step 3: sessions, links, and outcomes

Role-owned sessions record harness/conversation and optional repository/worktree,
branch/commit, model configuration, and claimed source. Registration never attests
a model or liveness. Directory discovery exposes limited harness/model metadata;
full conversation context remains role-private. Sends may reference an authorized
session and include bounded, untrusted metadata.

Messages support many-to-many typed links to conversations, sessions, check-ins,
assignments, commits, artifacts, and other authorized messages. Each link records
actor, timestamp, and whether inferred. References do not fetch targets or grant
permission. Outcomes are append-only claims with explicit artifact/evidence refs.
Acceptance requires the authenticated assigner/operator and the currently verified
artifact; reopening or subsequent changes require fresh verification.

Review fixed metadata session spoofing and acceptance of stale or different
artifacts. Unit and REST tests cover the assignment-to-review flow, role boundaries,
restart persistence, malformed registration, and exact-artifact acceptance.

## Step 4: telemetry and reviewed learnings

Reports use only caller-visible messages. They define pending mail, overdue
ack-required mail (not proof of failure), send-to-ack/completion/acceptance times,
identical normalized reported blocker counts, reopenings, and event counts.
Inbox/wait responses record offers separately from socket writes and explicit
acknowledgments. Historical imports have no reconstructed delivery telemetry.

Learning proposals require retained supporting messages and evidence references;
contradicting messages are preserved. Reviews append evidence-backed decisions
without rewriting candidates. All source messages must be visible to the caller.
Current source outcomes are included; reopening source work after an accepted
review marks the learning needs_revision. Extraction is an explicit candidate
proposal API, not an unattended model invocation or automatic truth assessment.

Review fixed annotation shadowing and stale learning acceptance. Tests cover
metric definitions, privacy, persistence, contradictory evidence, revalidation,
nonfinite thresholds, and REST proposal/review flow.


## Step 5: advisory ownership and supervised wake-up

Leases resolve configured-root/worktree paths and aliases before conservative
case-insensitive overlap checks. Acquisition is transactional and rejects conflicts
with named holders. Owners/operator can renew/release; expired claims require
reacquisition. Shared ownership metadata is visible to authenticated roles; no
filesystem fencing or automatic merge acceptance is introduced.

The opt-in worker long-polls outside the model, uses explicit argv/stdin, persists
results before ack, and binds its state to bridge/role/worktree/harness. Server-side
claims prevent competing workers; a cross-platform local lock protects one state
directory. Process launch, known failure, retained result, and acknowledgment are
separate evidence. Interrupted/timeout/output-storage failures are uncertain;
operator inspection/reset and explicit local recovery are required. Known process
failures have a bounded retry count. Neither process exit nor registration attests
a model or grants approval.

Final review fixes: Windows locked-byte reads; delayed legacy read overwriting
another connection's ack; concurrent ack timestamp races; failed output persistence
causing reexecution; missing reverse link lookup; source/grep access to database and
configuration backup artifacts; larger reports blocking the event loop; mismatched
worker state; idempotent completion handoffs. Runtime result events retain immutable
successful completion rather than recording each ack retry as a new completion.

Validation uses Windows Python and real subprocesses against an isolated bridge,
plus real MCP transport identity tests. Installed Codex CLI help and official
OpenAI noninteractive-execution documentation were inspected using OpenAI Docs.
No authenticated model turn, idle IDE injection, running-service restart, or live
mailbox migration was performed. Deployment is an explicit operational step using
these committed changes; existing machine-specific config and backups are untouched.

The cross-step review also added strict send validation, indexed UUID/legacy-ID
lookup, bounded report history batches, and a real-MCP slow-report responsiveness
check. Socket event data distinguishes recipient listeners from wildcard observers.

Final validation: 194 tests passed, 3 skipped (ripgrep unavailable on the Windows
PATH and Windows symlink creation unavailable). Python compilation, worker CLI
help, and diff whitespace checks passed. Each stage was reviewed, corrected, and
committed separately; no external publication or production rollout is included.


## October 7 follow-up: repeatable tests and local launcher

The test environment now pins Python in `.python-version` and dependencies in
`uv.lock`. Every test has exactly one purpose marker; the Windows runner supports
purpose and name filters, discovers ripgrep, and exercises directory aliases with
a junction when symlink creation lacks permission.

A separate stdio MCP launcher provides `bridge_status`, `bridge_ensure_running`,
`bridge_tools`, and `bridge_call`. It starts the local HTTP bridge on demand and
forwards with an existing role credential. It refuses unhealthy services and
occupied ports, serializes competing starts, and preserves upstream tool errors.
It never automatically retries an upstream call with uncertain effects.

Initial follow-up validation: 204 Windows tests passed with no skips, including
real subprocess startup, concurrent reuse, crash recovery, stdio discovery while
the bridge is down, and role isolation through the HTTP transport. A read-only
live smoke check reused the existing bridge and discovered its 29 tools through
the launcher. Windows and WSL Codex registrations were updated locally; those
machine-specific settings and credentials are outside this repository.
The October 6 totals above remain the record for that earlier milestone.


Pre-push platform validation on October 7: the full locked suite passed on
Windows through `tools/test.ps1` (204 passed) and on Linux/WSL with Python 3.12.11
(203 passed, one expected Windows-only environment-name test skipped). The Linux
venv was isolated from the existing Windows installation. Both runs exercised
real launcher subprocesses and HTTP/stdio MCP. The Windows runner now rejects a
missing `.EXE` in `PATHEXT`, which otherwise lets PowerShell launch `uv` without
waiting for its result. Whitespace and staged dependency-lock checks passed.
