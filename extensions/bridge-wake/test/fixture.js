'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { spawn, spawnSync } = require('node:child_process');
const { randomUUID } = require('node:crypto');
const { once } = require('node:events');
const { BridgeClient } = require('../src/bridge');
const root = path.resolve(__dirname, '../../..');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(fn, timeout = 15000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) { if (await fn()) return; await delay(100); }
  throw new Error('Timed out waiting for test condition.');
}
async function withBridge(fn) {
  const listener = net.createServer(); listener.listen(0, '127.0.0.1'); await once(listener, 'listening');
  const port = listener.address().port; await new Promise(resolve => listener.close(resolve));
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-wake-test-'));
  const roleToken = randomUUID(), senderToken = randomUUID();
  const config = path.join(temp, 'config.json');
  fs.writeFileSync(config, JSON.stringify({ host: '127.0.0.1', port, self_name: 'wake-test-bridge',
    token: randomUUID(), agents: { 'wake-test': { token: roleToken }, 'wake-sender': { token: senderToken } },
    mailbox_store: path.join(temp, 'mailbox.sqlite3'),
  }));
  const python = process.env.BRIDGE_TEST_PYTHON || path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const log = fs.openSync(path.join(temp, 'server.log'), 'w');
  const child = spawn(python, ['-m', 'agent_bridge', 'serve', '--config', config], { cwd: root, stdio: ['ignore', log, log], windowsHide: true });
  const client = new BridgeClient(`http://127.0.0.1:${port}`, roleToken);
  const sender = new BridgeClient(`http://127.0.0.1:${port}`, senderToken);
  try {
    await waitFor(async () => { try { return (await client.request('/api/health')).ok; } catch { return false; } });
    await fn({ client, sender, temp, root, waitFor, delay });
  } finally {
    client.close(); sender.close();
    if (process.platform === 'win32') {
      spawnSync(path.join(process.env.SystemRoot, 'System32/taskkill.exe'), ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    } else child.kill('SIGKILL');
    if (child.exitCode === null) await once(child, 'exit');
    fs.closeSync(log);
    fs.rmSync(temp, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
}
module.exports = { withBridge, waitFor };
