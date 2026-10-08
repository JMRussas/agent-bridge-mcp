'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { WakeQueue } = require('../src/bridge');

function setup(state = { jobs: {} }) {
  const calls = [];
  const client = {
    claim: async (...args) => { calls.push(['claim', ...args]); return { result: { acquired: true } }; },
    ack: async uid => { calls.push(['ack', uid]); }, close() {},
  };
  const journal = { state, save: async () => { calls.push(['save', JSON.parse(JSON.stringify(state))]); } };
  let running = 0, maximum = 0;
  const conversation = { run: async m => {
    running++; maximum = Math.max(maximum, running); calls.push(['turn', m.uid]);
    await new Promise(resolve => setTimeout(resolve, 5)); running--;
    return { threadId: 'same-thread', turnId: m.uid, output: 'ok' };
  }, close() {} };
  const queue = new WakeQueue({ client, journal, conversation, enabled: true });
  queue.on('failure', e => calls.push(['failure', e]));
  return { calls, client, journal, conversation, queue, maximum: () => maximum };
}
const message = uid => ({ uid, text: 'hello', sender: 'sender', ack_required: true });

test('duplicate pushes produce one turn, queued pushes serialize, result is durable before ack', async () => {
  const f = setup(); f.queue.receive(message('a')); f.queue.receive(message('a')); f.queue.receive(message('b'));
  await f.queue.chain;
  assert.equal(f.maximum(), 1);
  assert.deepEqual(f.calls.filter(c => c[0] === 'turn').map(c => c[1]), ['a', 'b']);
  const ack = f.calls.findIndex(c => c[0] === 'ack');
  assert.ok(f.calls.slice(0, ack).some(c => c[0] === 'save' && c[1].jobs.a.status === 'completed'));
});
test('disabled listener leaves messages pending until enabled', async () => {
  const f = setup(); f.queue.enable(false); f.queue.receive(message('a')); await f.queue.chain;
  assert.equal(f.calls.length, 0); f.queue.enable(true); await f.queue.chain;
  assert.equal(f.journal.state.jobs.a.status, 'acknowledged');
});
test('ack failure resumes handoff after restart without another model turn', async () => {
  const f = setup(); f.client.ack = async () => { throw new Error('offline'); };
  f.queue.receive(message('a')); await f.queue.chain;
  const g = setup(JSON.parse(JSON.stringify(f.journal.state))); g.queue.receive(message('a')); await g.queue.chain;
  assert.equal(g.calls.filter(c => c[0] === 'turn').length, 0);
  assert.equal(g.journal.state.jobs.a.status, 'acknowledged');
});
test('interrupted or failed turns remain uncertain and are never auto-retried', async () => {
  const f = setup(); f.conversation.run = async () => { throw new Error('lost response'); };
  f.queue.receive(message('a')); await f.queue.chain;
  assert.equal(f.journal.state.jobs.a.status, 'uncertain');
  const g = setup(f.journal.state); g.queue.receive(message('a')); await g.queue.chain;
  assert.equal(g.calls.length, 0);
  const h = setup({ jobs: { a: { status: 'running' } } }); h.queue.receive(message('a')); await h.queue.chain;
  assert.equal(h.calls.length, 0);
});
test('legacy messages and assignments claimed elsewhere never start a turn', async () => {
  const f = setup(); f.queue.receive({ ...message('a'), ack_required: false }); await f.queue.chain;
  f.client.claim = async () => ({ result: { acquired: false } }); f.queue.receive(message('b')); await f.queue.chain;
  assert.equal(f.calls.filter(c => c[0] === 'turn').length, 0);
});
test('failed durable write before launch never starts a turn', async () => {
  const f = setup(); f.journal.save = async () => { throw new Error('disk full'); };
  f.queue.receive(message('a')); await f.queue.chain;
  assert.equal(f.calls.filter(c => c[0] === 'turn').length, 0);
});

test('replayed push retries a failed acknowledgment without rerunning the completed turn', async () => {
  const f = setup(); let attempts = 0;
  f.client.ack = async () => { if (++attempts === 1) throw new Error('temporary disconnect'); };
  f.queue.receive(message('a')); await f.queue.chain;
  assert.equal(f.journal.state.jobs.a.status, 'completed');
  f.queue.receive(message('a')); await f.queue.chain;
  assert.equal(f.calls.filter(c => c[0] === 'turn').length, 1);
  assert.equal(f.journal.state.jobs.a.status, 'acknowledged');
});

test('an uncertain turn pauses subsequent queued assignments', async () => {
  const f = setup(); let closed = false;
  f.conversation.run = async () => { throw new Error('turn timed out'); };
  f.conversation.close = () => { closed = true; };
  f.queue.receive(message('a')); f.queue.receive(message('b')); await f.queue.chain;
  assert.equal(f.queue.enabled, false); assert.equal(closed, true);
  assert.equal(f.journal.state.jobs.a.status, 'uncertain');
  assert.equal(f.journal.state.jobs.b, undefined);
});
