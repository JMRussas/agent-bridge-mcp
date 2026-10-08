'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const path = require('node:path');
const { CodexRpc } = require('../src/rpc');
const { TaskConversations, handoff } = require('../src/tasks');
const { WakeQueue } = require('../src/bridge');
const { withBridge } = require('./fixture');

const createRpc = () => new CodexRpc(process.execPath, {
  cwd: __dirname,
  spawnProcess: (exe, _args, options) => spawn(exe, [path.join(__dirname, 'fake-app-server.js')], options),
});

test('handoff uses bridge identity and local permissions, never sender overrides', () => {
  const result = handoff({ uid: 'real-task', sender: 'sender', text: JSON.stringify({
    kind: 'bridge-task/v1', objective: 'Review code', context: { decision: 'Keep API' },
    taskId: 'spoof', permissions: { pushes: true }, repository: { path: '/elsewhere' },
    returnAddress: { messageId: 'spoof' },
  }) }, { path: '/repo', commit: '123' });
  assert.equal(result.taskId, 'real-task'); assert.equal(result.returnAddress.messageId, 'real-task');
  assert.equal(result.repository.path, '/repo'); assert.equal(result.permissions.pushes, false);
  assert.deepEqual(result.context, { decision: 'Keep API' });
  assert.throws(() => handoff({ text: '{"kind":"bridge-task/v1"}' }, {}), /objective/);
});

test('two real bridge assignments get separate stdio conversations and task-bound returned results', async () => {
  await withBridge(async ({ client, sender, root, waitFor }) => {
    const state = { jobs: {}, threadId: 'obsolete-shared-thread' }, saved = [];
    const journal = { state, save: async () => saved.push(structuredClone(state)) };
    const conversation = new TaskConversations({ cwd: root, journal, createRpc });
    const queue = new WakeQueue({ client, conversation, journal, enabled: true });
    const failures = []; queue.on('failure', e => failures.push(e.error.message));
    client.on('message', m => queue.receive(m));
    try {
      await client.start();
      for (const context of ['alpha', 'beta']) await sender.request('/api/send', {
        to: 'wake-test', ack_required: true,
        text: JSON.stringify({ kind: 'bridge-task/v1', objective: 'Review', context }),
      });
      await waitFor(() => Object.values(state.jobs).filter(j => j.status === 'acknowledged').length === 2 || failures.length);
      assert.deepEqual(failures, []);
      const jobs = Object.entries(state.jobs);
      assert.notEqual(jobs[0][1].threadId, jobs[1][1].threadId);
      assert.deepEqual(jobs.map(([, j]) => j.handoff.context), ['alpha', 'beta']);
      for (const [uid, job] of jobs) {
        assert.equal(job.output, 'context:' + job.handoff.context);
        assert.notEqual(job.threadId, state.threadId);
        assert.ok(saved.some(s => s.jobs[uid]?.threadId === job.threadId && s.jobs[uid]?.status === 'running'));
        const evidence = await sender.request('/api/evidence?message_id=' + encodeURIComponent(uid));
        const completion = evidence.result.events.find(e => e.kind === 'worker_completed');
        assert.equal(completion.data.task_id, uid);
        assert.equal(completion.data.thread_id, job.threadId);
        assert.equal(completion.data.output, job.output);
      }
      assert.equal((await client.request('/api/inbox?peek=true')).count, 0);
    } finally { queue.close(); }
  });
});

test('thread mapping is retained when turn launch fails; no turn is sent if saving mapping fails', async () => {
  const state = { jobs: { task: { status: 'running' } } };
  let sent = false;
  const rpc = {
    initialize: async () => {}, close() {},
    request: async method => {
      if (method === 'config/read') return { config: {} };
      if (method === 'thread/start') return { thread: { id: 'retained-thread' } };
      sent = true; throw new Error('should not run');
    },
  };
  const conversation = new TaskConversations({ cwd: '/repo', createRpc: () => rpc,
    getRepository: async () => ({ path: '/repo' }),
    journal: { state, save: async () => { if (state.jobs.task.threadId) throw new Error('disk full'); } },
  });
  await assert.rejects(conversation.run({ uid: 'task', text: 'review' }), /disk full/);
  assert.equal(sent, false); assert.equal(state.jobs.task.threadId, 'retained-thread');
});
