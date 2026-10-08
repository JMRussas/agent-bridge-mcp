'use strict';
// Explicit, account-using test. Never part of npm test or CI.
const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const { TaskConversations } = require('../src/tasks');
const { WakeQueue } = require('../src/bridge');
const { withBridge } = require('./fixture');

async function run() {
  if (!process.env.BRIDGE_WAKE_CODEX) throw new Error('Set BRIDGE_WAKE_CODEX to an absolute Codex executable for this opt-in live test.');
  await withBridge(async ({ client, sender, root, waitFor, delay }) => {
    const state = { jobs: {} }, completed = [], failures = [];
    const journal = { state, save: async () => {} };
    const conversation = new TaskConversations({ cwd: root, journal, executable: process.env.BRIDGE_WAKE_CODEX });
    const queue = new WakeQueue({ client, conversation, enabled: true, journal: { state, save: async () => {} } });
    queue.on('completed', result => { completed.push(result); console.log(JSON.stringify({ phase: 'completed', ...result })); });
    queue.on('failure', result => { failures.push(result.error); console.error(result.error.message); });
    client.on('message', m => queue.receive(m));
    try {
      await client.start();
      const nonce = randomUUID().replaceAll('-', '').slice(0, 12);
      console.log('Sending first assignment to establish an isolated task conversation.');
      await sender.request('/api/send', { to: 'wake-test', text: JSON.stringify({ kind: 'bridge-task/v1', objective: 'Wake-up test. Reply exactly READY_ followed by the nonce in context. Do not use tools.', context: { nonce }, acceptanceCriteria: ['Exact marker response'] }), ack_required: true });
      await waitFor(() => completed.length === 1 || failures.length, 180000);
      if (failures.length) throw failures[0];
      assert.ok(completed[0].output.includes('READY_' + nonce), completed[0].output);
      await waitFor(() => Object.values(state.jobs).every(j => j.status === 'acknowledged'));
      await delay(1000); // No model turn or inbox polling during idle time.
      console.log('Sending second assignment to a fresh conversation with independent context.');
      await sender.request('/api/send', { to: 'wake-test', text: JSON.stringify({ kind: 'bridge-task/v1', objective: 'Wake-up test. Reply exactly SECOND_ followed by the nonce in context. If an earlier conversation nonce is visible, also print LEAK_ and that earlier nonce. Do not use tools.', context: { nonce: 'independent' } }), ack_required: true });
      await waitFor(() => completed.length === 2 || failures.length, 180000);
      if (failures.length) throw failures[0];
      assert.notEqual(completed[1].threadId, completed[0].threadId);
      assert.notEqual(completed[1].turnId, completed[0].turnId);
      assert.ok(completed[1].output.includes('SECOND_independent'), completed[1].output);
      assert.ok(!completed[1].output.includes(nonce), completed[1].output);
      await waitFor(() => Object.values(state.jobs).every(j => j.status === 'acknowledged'));
      assert.equal((await client.request('/api/inbox?peek=true')).count, 0);
      for (const result of completed) {
        const inspected = await sender.request('/api/evidence?message_id=' + encodeURIComponent(result.uid));
        assert.ok(JSON.stringify(inspected).includes(result.output), JSON.stringify(inspected));
        assert.equal(state.jobs[result.uid].threadId, result.threadId);
      }
      console.log(JSON.stringify({ passed: true, threads: completed.map(r => r.threadId), turns: completed.length, pending: 0, existingIdeChat: false }));
    } finally { queue.close(); }
  });
}
run().catch(error => { console.error(error); process.exitCode = 1; });
