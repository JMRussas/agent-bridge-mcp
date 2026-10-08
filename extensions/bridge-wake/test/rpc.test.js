'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { CodexRpc, CodexConversation } = require('../src/rpc');

test('real stdio preserves a conversation across turns and handles early notifications', async () => {
  const { spawn } = require('node:child_process');
  const transport = new CodexRpc(process.execPath, {
    cwd: __dirname,
    spawnProcess: (exe, _args, options) => spawn(exe, [path.join(__dirname, 'fake-app-server.js')], options),
  });
  try {
    const saved = [];
    const conversation = new CodexConversation(transport, { cwd: __dirname, threadId: 'existing-thread', saveThread: async id => saved.push(id) });
    const first = await conversation.run({ uid: 'one', text: 'hello' });
    const second = await conversation.run({ uid: 'two', text: 'again' });
    assert.equal(first.threadId, 'existing-thread'); assert.equal(second.threadId, first.threadId);
    assert.notEqual(first.turnId, second.turnId); assert.equal(second.output, 'test answer');
    assert.deepEqual(saved, ['existing-thread']);
  } finally { transport.close(); }
});
