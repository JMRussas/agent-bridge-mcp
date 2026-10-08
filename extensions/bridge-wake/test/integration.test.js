'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { WakeQueue } = require('../src/bridge');
const { withBridge } = require('./fixture');

test('real bridge push wakes once, explicit ack removes pending mail, reconnect does not replay work', async () => {
  await withBridge(async ({ client, sender, waitFor, delay }) => {
    const turns = [];
    const state = { jobs: {} };
    const queue = new WakeQueue({ client, enabled: true, journal: { state, save: async () => {} },
      conversation: { run: async m => { turns.push(m.uid); return { threadId: 'thread', turnId: m.uid, output: 'ok' }; }, close() {} } });
    const errors = []; queue.on('failure', e => errors.push(e.error.message));
    client.on('message', m => queue.receive(m)); await client.start();
    // Send immediately: connect-time backlog recovery must also work.
    await sender.request('/api/send', { to: 'wake-test', text: 'test push', ack_required: true });
    await waitFor(() => Object.values(state.jobs).some(j => j.status === 'acknowledged'));
    assert.equal((await client.request('/api/inbox?peek=true')).count, 0);
    client.socket.terminate(); await delay(700);
    assert.equal(turns.length, 1); assert.deepEqual(errors, []);
    queue.close();
  });
});
