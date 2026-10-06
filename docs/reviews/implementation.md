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
