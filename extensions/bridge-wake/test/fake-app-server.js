'use strict';
const readline = require('node:readline');
const { randomUUID } = require('node:crypto');
const send = message => process.stdout.write(JSON.stringify(message) + '\n');
readline.createInterface({ input: process.stdin }).on('line', line => {
  const m = JSON.parse(line);
  if (!m.id) return;
  if (m.method === 'initialize') send({ id: m.id, result: {} });
  else if (m.method === 'config/read') send({ id: m.id, result: { config: { mcp_servers: { test: { command: 'unused' } } } } });
  else if (m.method === 'thread/start' || m.method === 'thread/resume') {
    if (m.params.config.mcp_servers.test.enabled !== false || m.params.sandbox !== 'read-only') {
      return send({ id: m.id, error: { message: 'Expected read-only and disabled MCP' } });
    }
    send({ id: m.id, result: { thread: { id: m.params.threadId || randomUUID() } } });
  } else if (m.method === 'turn/start') {
    const threadId = m.params.threadId, turnId = 'turn-' + m.id;
    const envelope = JSON.parse(m.params.input[0].text.split('\n').slice(1).join('\n'));
    let task;
    try { task = JSON.parse(envelope.text); } catch { /* Legacy transport test. */ }
    const output = task?.kind === 'bridge-task/v1' ? 'context:' + task.context : 'test answer';
    // Notifications may race ahead of the request response on a real stream.
    send({ method: 'item/completed', params: { threadId, turnId, item: { type: 'agentMessage', text: output } } });
    send({ method: 'turn/completed', params: { threadId, turn: { id: turnId, status: 'completed' } } });
    send({ id: m.id, result: { turn: { id: turnId } } });
  }
});
