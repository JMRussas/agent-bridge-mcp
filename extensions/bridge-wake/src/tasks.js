'use strict';
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { CodexRpc, CodexConversation } = require('./rpc');
const execute = promisify(execFile);

async function repositoryContext(cwd) {
  const git = async args => {
    try { return (await execute('git', args, { cwd, windowsHide: true, timeout: 10000 })).stdout.trim(); }
    catch { return null; }
  };
  const [branch, commit, changes] = await Promise.all([
    git(['branch', '--show-current']), git(['rev-parse', 'HEAD']), git(['status', '--porcelain']),
  ]);
  return { path: cwd, branch, commit, dirty: changes === null ? null : changes.length > 0 };
}

function handoff(message, repository) {
  let supplied;
  try { supplied = JSON.parse(message.text); } catch { /* Plain text is a valid task. */ }
  if (supplied?.kind === 'bridge-task/v1' && (typeof supplied.objective !== 'string' || !supplied.objective.trim())) {
    throw new Error('bridge-task/v1 requires a nonempty objective.');
  }
  const task = supplied?.kind === 'bridge-task/v1' ? supplied : { objective: message.text };
  return {
    kind: 'bridge-task/v1', taskId: message.uid, sender: message.sender,
    objective: task.objective, acceptanceCriteria: task.acceptanceCriteria ?? [],
    context: task.context ?? '', relevantFiles: task.relevantFiles ?? [],
    constraints: task.constraints ?? [], dependencies: task.dependencies ?? [],
    repository,
    permissions: { filesystem: 'read-only', mcp: false, edits: false, commits: false, pushes: false },
    returnAddress: { kind: 'bridge-work-claim', messageId: message.uid },
  };
}

// One process/thread per assignment. The queue owns serialization and recovery;
// an ambiguous running job must not reach run() again automatically.
class TaskConversations {
  constructor({ cwd, journal, executable, createRpc, getRepository = repositoryContext }) {
    Object.assign(this, { cwd, journal, getRepository });
    this.createRpc = createRpc || (() => new CodexRpc(typeof executable === 'function' ? executable() : executable, { cwd }));
  }
  async run(message) {
    const job = this.journal.state.jobs[message.uid];
    if (!job) throw new Error('Task must be journaled before launching a conversation.');
    job.handoff ||= handoff(message, await this.getRepository(this.cwd));
    await this.journal.save();
    const conversation = this.active = new CodexConversation(this.createRpc(), {
      cwd: this.cwd, threadId: job.threadId,
      saveThread: async threadId => { job.threadId = threadId; await this.journal.save(); },
    });
    try { return await conversation.run({ ...message, text: JSON.stringify(job.handoff) }); }
    finally { conversation.close(); if (this.active === conversation) this.active = null; }
  }
  close() { this.active?.close(); }
}
module.exports = { TaskConversations, handoff, repositoryContext };
