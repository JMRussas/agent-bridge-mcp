# Supervised wake-up

The bridge can deliver messages to a connected listener; it cannot generally
start an idle IDE/app conversation. SessionStart/inbox hooks may provide context
without making the harness wakeable. The supported wake path here is an opt-in
external worker that waits outside the model and launches a configured process.

Install the updated project to expose `agent-bridge-worker`, or use
`python -m agent_bridge.worker`. Use a role token in an environment variable.
Each bridge/role/worktree/harness combination needs a separate state directory.
The worker refuses admin credentials and mismatched existing state.

Example in PowerShell (supply your own executable, model, role token, and paths):

```powershell
$env:AGENT_BRIDGE_TOKEN = '<role-token>'
python -m agent_bridge.worker --url http://127.0.0.1:8791 `
  --state D:\BridgeWorkers\reviewer --cwd D:\Git\review-worktree `
  -- C:\path\to\codex.exe exec --sandbox read-only --model '<configured-model>' --json -
```

The installed Codex CLI help and official documentation establish stdin-driven
noninteractive execution:
https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex

Use an actual executable and explicit argv, not a command assembled from mailbox
text. The worker passes a JSON assignment on stdin with `shell=False`. The harness
inherits its operator-configured environment and permissions; the bridge does not
upgrade them. A configured resume invocation can target a known conversation;
this worker does not inject input into an existing IDE or discover model identity.

Send work with `ack_required=true`. The worker leaves legacy mail pending, uses
an exclusive local process lock and durable server claims, and stores message
state plus output in its state directory. Successful process output is flushed
before acknowledgment. Acknowledgment network failure retries the handoff without
relaunching. Nonzero exits can retry up to `--max-attempts` (default 3). Timeout,
interrupted launch, or output persistence failure remains uncertain and pending.
`--once` runs one inbox pass, suitable for an external scheduler/supervisor.

A process exit of zero demonstrates successful harness execution. It does not
prove implementation completion, tests passing, or acceptance. Record those
separately through the outcome protocol. Output artifact references have the
form `worker://<worker-id>/<message-uid>`; their local log is `<uid>.log` alongside
`worker.sqlite3`. These artifacts contain private conversation content and should
live outside exposed source roots. No background service is installed automatically.

## Starting the bridge itself

The worker above needs an available HTTP bridge. Local agents can register the
[stdio launcher](../README.md#on-demand-startup-for-local-agents) and call
`bridge_ensure_running` to start that service. `bridge_tools` and `bridge_call`
also ensure it is running before discovery or forwarding. The launcher does not
start a worker, deliver input into an idle IDE, or run a model turn by itself.
A remote HTTP client needs a launcher on the bridge host or an OS-managed service;
it cannot call a startup tool through a stopped HTTP endpoint.

## Recover an interrupted launch

1. Inspect the recorded claim/events and local worker state. Stop any old harness
   and descendants before deciding to retry; an expired timeout is not fencing.
2. With the operator credential, POST `/api/work-claims` using the message UUID,
   recorded worker ID, `action:"reset"`, and `data:{reason:"..."}`. This is an
   explicit decision to allow another attempt, retained in the event history.
3. Restart the same worker command with `--recover <message-uid>` before `--`.
   That resets only an uncertain local job. The original result/review trail remains.

Two workers with different state directories cannot launch the same running claim.
The local lock prevents concurrent use of one directory. Completed jobs are never
reexecuted solely because acknowledgment failed. Exactly-once external side effects
are not promised; explicit recovery must consider partial work from the old run.

## VS Code companion spike

[Agent Bridge Wake](../extensions/bridge-wake/README.md) adds a persistent
WebSocket listener, notifications, and optional automatic turns in a
new companion-owned Codex conversation per task. It reuses the bridge's durable claims and
explicit acknowledgments and keeps uncertain turns pending. A real two-push
smoke test confirmed separate threads with independent supplied context and
results recorded against the correct assignments. See the companion README for
the handoff format, output limits, and recovery behavior.
An isolated editor-host probe found no public API for submitting into the
installed Codex or Claude extension's existing chat. The companion is an
experimental alternative conversation, not an adapter for those open panels.

## Validation performed

Integration tests use real subprocesses and an isolated authenticated bridge.
They cover durable output, ack failure/restart, exclusive claims, known failures,
timeouts, output-storage failure, and Windows local locking. Installed Codex help
was inspected. The original worker validation did not run an authenticated model turn or wake an
idle IDE chat. The later companion smoke test above did run real Codex turns,
but did not wake an existing IDE chat panel.
