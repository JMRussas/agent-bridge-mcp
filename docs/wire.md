# Mailbox and evidence protocol 1.1

`bridge_capabilities` reports the API version, persistent bridge ID, tools,
registered REST routes, and acknowledgment options. `/api/whoami` gives the
same caller identity over REST. All `/api/*` routes except health require a
bearer role credential. The operator credential is for administration.

## Delivery and retained history

`bridge_send(to,text,sender="",thread="",ack_required=false,session_id="",meta=null)`
and `POST /api/send` accept the same named fields. Text is capped by
`max_message_bytes`; metadata is bounded at 16 KiB and is untrusted. Sender,
authenticated principal, admin status, and impersonation are stamped by the
server. Returns legacy `id`, UUID `uid`, and persistent `bridge_id`.

`ack_required=true` prevents legacy inbox/wait/socket consumers from clearing
mail. `bridge_inbox` and `bridge_wait` accept `ack_mode=true`; REST inbox/wait
accept `ack=explicit`. `peek` continues to work. Explicit delivery may repeat:
clients must tolerate duplicate UUIDs. A socket write is not model observation.

`bridge_ack(message_id)` and `POST /api/ack {message_id}` accept UUID or local
integer ID (MCP integer IDs are supplied as strings). Only recipient/operator
may acknowledge. The acknowledgment timestamp is durable and idempotent.
Acknowledgment means accepted responsibility, not completion or approval.

`bridge_history(agent,limit,thread)` / `GET /api/history` return retained traffic
visible to the caller, including consumed mail. REST history caps results at
1000. Inbox pages cap at 1000. Legacy socket replay caps at 1000 per connection;
larger pending queues must be drained with inbox/ack and reconnect.

Existing JSON paths migrate transactionally once into sibling `.sqlite3` files.
Original JSON remains unchanged and becomes historical backup, not live state.
Corruption or migration failure fails startup. History has no automatic purge.
`inbox_max` and `mailbox_max_bytes` bound global pending count/text bytes; they
reject new sends instead of evicting evidence. Disk failures reject sends.
SQLite database, WAL, SHM, and original mailbox files are private artifacts.
Run only one bridge service per database; clients may share that service.

## Sessions and references

| MCP | REST | Body / query |
| --- | --- | --- |
| `bridge_register_session(context)` | POST `/api/sessions` | `{context}` |
| `bridge_sessions()` | GET `/api/sessions` | none |
| `bridge_link(...)` | POST `/api/links` | `{message_id,relation,target_type,target_ref,inferred?}` |
| `bridge_links(target_type,target_ref)` | GET `/api/links` | target type/reference |
| `bridge_outcome(...)` | POST `/api/outcomes` | `{message_id,kind,artifact_ref?,evidence_refs?,details?}` |
| `bridge_evidence(message_id)` | GET `/api/evidence` | message ID |

Session context requires `harness` and `conversation_ref`. Optional strings:
`repository`, `worktree`, `branch`, `commit`, `configured_model`, `evidence_source`,
`wake_capability` (`none`, `context_only`, `supervised_worker`). Optional boolean:
`listener_connected`. Registration records actor/source/time; it never attests a
model or establishes liveness. Directory discovery exposes limited model/harness
metadata; conversation references and working context remain role-private.

Relations: `originated_in`, `replies_to`, `supports`, `contradicts`,
`followed_up_in`. Target types: `conversation`, `session`, `checkin`, `assignment`,
`commit`, `artifact`, `message`. Links are many-to-many references with actor,
time, and inference flag. External references are not fetched; linking confers
no permission to access their targets. Check-ins are external records referenced
by ID/URI, not a second conversation store in the bridge.

Outcomes: `blocked`, `completed`, `verified`, `accepted`, `reopened`.
Verification/acceptance require an exact artifact reference and evidence refs.
Acceptance belongs to the authenticated assigner/operator and must match the
current verified artifact. Reopening or recording subsequent work requires fresh
verification before acceptance. These are attributed claims; the bridge does
not run tests or independently verify referenced evidence.

## Telemetry and learnings

| MCP | REST | Body / query |
| --- | --- | --- |
| `bridge_telemetry(overdue_after_s=3600)` | GET `/api/telemetry` | optional threshold |
| `bridge_propose_learning(...)` | POST `/api/learnings` | `{text,message_ids,evidence_refs,contradicting_message_ids?}` |
| `bridge_learnings()` | GET `/api/learnings` | none |
| `bridge_review_learning(...)` | POST `/api/learning-reviews` | `{learning_id,status,evidence_refs,note?}` |

Metrics cover caller-visible messages only and include their definitions. Offers,
socket writes, acknowledgments, and outcomes are separate events. Imported
history has unknown original sender provenance and missing original event data;
we do not reconstruct observations from `read=true`. Reports currently scan
visible retained history; large deployments will need indexed aggregation.

Candidates require message evidence and external references. Reviews append
`accepted`, `rejected`, or `needs_revision` with reviewer and supporting refs.
All supporting and contradicting messages must be accessible to the caller.
Source outcomes accompany candidates; reopening after accepted review marks a
candidate for revision. No background model automatically turns claims into truth.

## Advisory ownership and supervised jobs

| MCP | REST | Body |
| --- | --- | --- |
| `bridge_acquire_lease(...)` | POST `/api/leases` | `{action:"acquire",session_id,root,worktree,paths,ttl_s?}` |
| `bridge_leases()` | GET `/api/leases` | none |
| `bridge_renew_lease(...)` | POST `/api/leases` | `{action:"renew",lease_id,ttl_s?}` |
| `bridge_release_lease(...)` | POST `/api/leases` | `{action:"release",lease_id}` |
| `bridge_work_claim(...)` | POST `/api/work-claims` | `{message_id,worker_id,action?,data?}` |

Leases require a role-owned session, configured root, relative worktree, and
literal file/directory paths (no globs/traversal). Paths resolve through symlinks
before conservative case-insensitive overlap checks. Conflicting acquisition
returns holders (REST 409). TTL is 1–86400 seconds; default 900. Owners/operator
may renew/release. Expired leases require reacquisition. Claims are shared
coordination metadata; release records remain stored. Leases do not fence writes.

Worker claims require ack-required messages and recipient authority. `start`
atomically acquires only unclaimed/failed work. Running/completed claims never
expire automatically. `launched` records the harness OS process ID, not proof of model observation.
`completed` requires a durable result reference; `failed`
records a known process failure. Completion is idempotent. Only the operator
may `reset` an interrupted claim with `data.reason`, after inspecting/stopping
the old process. Worker events describe harness operations, not verified task
completion or model identity. See [wake.md](wake.md).
