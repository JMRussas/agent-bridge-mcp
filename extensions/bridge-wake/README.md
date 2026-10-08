# Agent Bridge Wake — VS Code feasibility spike

This companion keeps a bridge listener running outside model turns. It can
notify you of mail or automatically start a **new Codex conversation per task**.
It does **not** submit messages into the existing Codex or Claude chat panel.

An isolated VS Code Extension Development Host inspected Codex
`26.1002.51308` and Claude Code `2.1.292`. Both returned no exported integration
API. Their available commands did not expose submitting a message to a specified
conversation. Codex's “Add to Thread” adds editor context; it does not send a turn.
We did not use private webview RPC, simulated keystrokes, or private IDE sockets.

The supported experiment uses the public
[Codex app-server stdio protocol](https://learn.chatgpt.com/docs/app-server).
The installed CLI labels app-server experimental; this extension is a local
prototype, not a published or production-supported integration.

## Install and use

Install the locally built `agent-bridge-wake-0.1.0.vsix` with VS Code's
**Extensions: Install from VSIX** command, or press F5 using this repository's
“Agent Bridge Wake” debug configuration.

1. Open a single trusted repository folder.
2. Start the bridge normally or through its existing MCP launcher.
3. Set `agentBridgeWake.url` if it differs from `http://127.0.0.1:8791`.
4. Run **Agent Bridge: Set Role Token** with a dedicated role token. The token
   goes into VS Code SecretStorage, never repository settings. The companion
   refuses the admin credential. Register a new role with the bridge CLI and
   restart the bridge once if necessary.
5. The listener initially only reports pending mail. **Enable Companion
   Auto-Wake** opts that bridge/role/workspace into automatic turns; **Disable
   Companion Auto-Wake** stops future turns. Disabling does not cancel a turn
   already underway. **Disconnect Listener** also closes its Codex process.
6. Send an assignment to that role with `ack_required=true`. Read its response
   in **Agent Bridge: Show Wake Transcript** or inspect the assignment evidence
   through `bridge_evidence` / `GET /api/evidence?message_id=<uid>`. Legacy messages
   remain pending.

Use a dedicated role so this consumer does not compete with your interactive
chat's inbox. The conversation runs on the extension host's OS, in the open
repository, with local Codex authentication and its configured model. You can
set an absolute `agentBridgeWake.codexExecutable` path; otherwise the companion
uses the installed Codex extension's bundled binary when available. It requests
a read-only sandbox and disables configured MCP servers for this experiment.
It returns results through assignment evidence, without sending new inbox messages
or approving tool requests.

The listener reconnects without consuming mail. A persisted worker ID and
server-side claims prevent duplicate execution by competing consumers. Turns
are serialized. The response and thread/turn IDs are stored in workspace state
before the bridge is told the turn completed and the message is acknowledged.
The completion event includes the task UID, thread/turn IDs, and up to 2,000 UTF-16
code units of output, with an explicit truncation flag. Full output remains in
the local journal and transcript. Assignment participants can inspect that event.
A turn response is not proof that assigned code changes were verified or accepted.
Stored replies can contain private text; they live in VS Code workspace storage.
The output channel shows activity from the current extension session; the retained
workspace journal is used for recovery across restarts.

An interrupted turn is marked uncertain, pauses automatic wake-up, and closes
the companion Codex process. It is not automatically retried.
Inspect the transcript and bridge work claim before any recovery. This spike
has no reset/retry UI; do not delete its workspace state or reset claims until
an operator has checked for partial execution. A completed turn whose ack failed
can finish the acknowledgment after reconnect/re-enable without another turn.
The existing IDE conversations are neither selected nor altered.

## Task handoff

Each acknowledged-work message UID is one task and owns one conversation. Plain
text remains supported as the objective. For a richer handoff, send JSON as the
bridge message's `text`:

```json
{
  "kind": "bridge-task/v1",
  "objective": "Review the launcher startup locking",
  "acceptanceCriteria": ["Describe any race with a concrete trigger"],
  "context": "Retain the existing role authentication design.",
  "relevantFiles": ["src/agent_bridge/launcher.py"],
  "constraints": ["Read-only review; do not edit files"],
  "dependencies": []
}
```

The companion adds the authenticated sender, message UID, actual workspace path,
current branch/commit and dirty-state flag, enforced read-only permissions, and
an assignment evidence return address. Git fields are null if unavailable; a
commit alone does not capture uncommitted files. Include the necessary decisions,
excerpts, and dependencies in the supplied context. The full handoff is journaled
before launch, and the task's thread ID is saved before submitting its first turn.
Sender-supplied identity, repository, permissions, and return-address overrides
are ignored. A new message creates a new task even if its text repeats an earlier
objective; reconnecting delivery of the same UID does not.

The old listener-wide thread ID is no longer used for new tasks. Existing
completed jobs still finish acknowledgment without rerunning. Ambiguous running
jobs retain their task/thread mapping for inspection but are not automatically
resubmitted; there is no operator resume UI yet. Tasks currently run sequentially
with read-only access to the open workspace. Concurrent editing and isolated
worktrees are not implemented. Creating these app-server threads does not prove
they are visible or openable in the installed chat UI.

## Repeatable checks

From this directory, with Node 20+ and the repository's Python environment ready:

```text
npm ci --ignore-scripts
npm test
npm exec --yes --package=@vscode/vsce@4.0.0 -- vsce package --allow-missing-repository
```

The integration test starts its own authenticated bridge with temporary roles
and a temporary mailbox. It never sends to your live bridge. By default it uses
`../../.venv/Scripts/python.exe` on Windows or `../../.venv/bin/python` elsewhere;
set `BRIDGE_TEST_PYTHON` to override. Tests cover actual WebSocket push/reconnect,
queue serialization/deduplication, explicit ack, interrupted turns, persistence
failure, and app-server stdio framing. No model account is needed for these tests.

An account-using smoke test is deliberately separate:

```powershell
$env:BRIDGE_WAKE_CODEX = 'C:\absolute\path\to\codex.exe'
node test/live-smoke.js
```

It sends two isolated bridge assignments with different context, checks different
thread IDs and the expected answers, verifies the second answer does not include
the first task's nonce, and reads both returned results as the sender. It also
checks successful acknowledgments and an empty pending inbox. It uses your
existing Codex account and model.

For the editor-host check, launch VS Code with this directory as
`--extensionDevelopmentPath` and `test/host.js` as `--extensionTestsPath`, a
separate `--user-data-dir`, and this repository as the workspace. Optionally set
`BRIDGE_WAKE_HOST_REPORT` to an absolute JSON output path. This activates the
companion and inspects installed chat extensions without starting a model turn.

## Validation in this repository

On October 7, 2026:

- Thirteen automated companion tests passed on Windows and Linux, including actual
  bridge WebSocket delivery and app-server stdio framing. The existing Windows
  Python suite also passed all 204 tests.
- The companion activated successfully in an isolated, installed Windows VS Code
  host; automatic wake-up was off by default.
- The installed Codex and Claude extensions exposed no public existing-chat
  submission API in that host.
- Two real bridge tasks produced successful Codex turns in distinct threads,
  using separate context. Results were readable through each assignment's
  evidence, both messages were acknowledged, and the test inbox was empty.
- These results apply to companion-owned task conversations. Waking the already
  open Codex/Claude chat panel remains unproven and unsupported by this spike.
