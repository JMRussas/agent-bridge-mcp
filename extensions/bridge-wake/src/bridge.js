'use strict';
const { EventEmitter } = require('node:events');
const { randomUUID } = require('node:crypto');
const WebSocket = require('ws');

class BridgeClient extends EventEmitter {
  constructor(origin, token) {
    super();
    const url = new URL(origin);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
      throw new Error('Bridge URL must be an HTTP(S) origin without credentials, path, or query.');
    }
    this.origin = url.origin; this.token = token; this.delay = 250;
  }
  async request(path, body) {
    const response = await fetch(this.origin + path, {
      method: body === undefined ? 'GET' : 'POST', redirect: 'error', signal: AbortSignal.timeout(10000),
      headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`Bridge returned HTTP ${response.status}; check service and role credential.`);
    return response.json();
  }
  async start() {
    this.identity = await this.request('/api/whoami');
    if (this.identity.admin || !this.identity.name || !this.identity.bridge_id) throw new Error('Companion requires a non-admin role credential.');
    this.stopped = false; this.connect();
    return this.identity;
  }
  connect() {
    if (this.stopped) return;
    const url = this.origin.replace(/^http/, 'ws') + '/notify?ack=explicit&format=json';
    const socket = this.socket = new WebSocket(url, ['bridge', `bearer.${this.token}`], { maxPayload: 256 * 1024, handshakeTimeout: 10000 });
    socket.on('open', () => { this.delay = 250; this.emit('status', 'connected'); });
    socket.on('message', data => {
      try {
        const m = JSON.parse(data.toString());
        if (typeof m.uid !== 'string' || typeof m.text !== 'string' || m.to !== this.identity.name) return;
        this.emit('message', m);
      } catch { this.emit('status', 'invalid frame ignored'); }
    });
    socket.on('error', () => this.emit('status', 'connection unavailable'));
    socket.on('close', () => {
      if (this.stopped) return;
      this.emit('status', 'reconnecting');
      this.retry = setTimeout(() => this.connect(), this.delay);
      this.delay = Math.min(this.delay * 2, 10000);
    });
  }
  close() { this.stopped = true; clearTimeout(this.retry); this.socket?.terminate(); }
  claim(uid, workerId, action, data = {}) {
    return this.request('/api/work-claims', { message_id: uid, worker_id: workerId, action, data });
  }
  ack(uid) { return this.request('/api/ack', { message_id: uid }); }
}

// One turn at a time. Persist before sending and before ack; ambiguous turns are
// never repeated on reconnect/restart. Server claims also exclude other workers.
class WakeQueue extends EventEmitter {
  constructor({ client, conversation, journal, enabled = false }) {
    super(); Object.assign(this, { client, conversation, journal, enabled });
    this.pending = new Map(); this.chain = Promise.resolve(); this.stopped = false;
    journal.state.workerId ||= randomUUID(); journal.state.jobs ||= {};
    for (const job of Object.values(journal.state.jobs)) {
      if (job.status === 'running') job.status = 'uncertain';
    }
  }
  receive(message) {
    if (this.stopped) return;
    const job = this.journal.state.jobs[message.uid];
    if (job?.status === 'acknowledged') return;
    if (this.pending.has(message.uid)) {
      if (this.enabled && job?.status === 'completed') this.drain(message);
      return;
    }
    this.pending.set(message.uid, message);
    this.emit('pending', message);
    if (this.enabled) this.drain(message);
  }
  enable(value) {
    this.enabled = value;
    if (value) for (const message of this.pending.values()) this.drain(message);
  }
  drain(message) {
    this.chain = this.chain.then(async () => {
      if (this.stopped || !this.enabled || !this.pending.has(message.uid)) return;
      try { await this.process(message); }
      catch (error) { this.emit('failure', { uid: message.uid, error }); }
    });
    return this.chain;
  }
  async process(message) {
    const { client, journal } = this;
    let job = journal.state.jobs[message.uid];
    if (!message.ack_required) {
      this.emit('failure', { uid: message.uid, error: new Error('Auto-wake requires ack_required=true; legacy message left pending.') }); return;
    }
    if (job?.status === 'uncertain' || job?.status === 'running') return;
    if (!job) {
      const claim = await client.claim(message.uid, journal.state.workerId, 'start');
      if (!claim.result.acquired) return;
      job = journal.state.jobs[message.uid] = { status: 'running' };
      await journal.save();
      try {
        const result = await this.conversation.run(message);
        Object.assign(job, result, { status: 'completed' });
        await journal.save();
        this.emit('completed', { uid: message.uid, ...result });
      } catch (error) {
        job.status = 'uncertain';
        this.enabled = false; // An ambiguous turn may still be running. Pause the queue.
        this.conversation.close();
        await journal.save(); throw error;
      }
    }
    if (job.status === 'completed') {
      await client.claim(message.uid, journal.state.workerId, 'completed', {
        artifact_ref: `bridge-wake://${job.threadId}/${job.turnId}`,
        task_id: message.uid, thread_id: job.threadId, turn_id: job.turnId,
        output: (job.output || '').slice(0, 2000), output_truncated: (job.output || '').length > 2000,
      });
      await client.ack(message.uid);
      job.status = 'acknowledged'; await journal.save();
      this.pending.delete(message.uid);
      this.emit('acknowledged', message.uid);
    }
  }
  close() { this.stopped = true; this.client.close(); this.conversation?.close(); }
}
module.exports = { BridgeClient, WakeQueue };
