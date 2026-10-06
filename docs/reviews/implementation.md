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
