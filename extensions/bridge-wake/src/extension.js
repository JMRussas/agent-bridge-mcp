'use strict';
const vscode = require('vscode');
const path = require('node:path');
const fs = require('node:fs');
const { createHash } = require('node:crypto');
const { BridgeClient, WakeQueue } = require('./bridge');
const { TaskConversations } = require('./tasks');

async function inspect() {
  const result = [];
  for (const id of ['openai.chatgpt', 'anthropic.claude-code']) {
    const extension = vscode.extensions.getExtension(id);
    if (!extension) { result.push({ id, installed: false }); continue; }
    let exported;
    try { exported = await extension.activate(); } catch { /* Report capability absence, not account details. */ }
    const commands = await vscode.commands.getCommands(true);
    result.push({ id, installed: true, version: extension.packageJSON.version, activationSucceeded: extension.isActive,
      exports: exported && typeof exported === 'object' ? Object.keys(exported) : [],
      commands: commands.filter(c => c.startsWith(id === 'openai.chatgpt' ? 'chatgpt.' : 'claude-vscode.')),
    });
  }
  return result;
}

function findCodex() {
  const configured = vscode.workspace.getConfiguration('agentBridgeWake').get('codexExecutable');
  if (configured) {
    if (!path.isAbsolute(configured) || !fs.existsSync(configured)) throw new Error('Configure an existing absolute Codex executable path.');
    return configured;
  }
  const ext = vscode.extensions.getExtension('openai.chatgpt');
  const platform = { win32: 'windows', darwin: 'macos', linux: 'linux' }[process.platform];
  const arch = { x64: 'x86_64', arm64: 'aarch64' }[process.arch] || process.arch;
  const candidate = ext && path.join(ext.extensionPath, 'bin', `${platform}-${arch}`, process.platform === 'win32' ? 'codex.exe' : 'codex');
  if (!candidate || !fs.existsSync(candidate)) throw new Error('Set agentBridgeWake.codexExecutable to your Codex executable.');
  return candidate;
}

function activate(context) {
  const output = vscode.window.createOutputChannel('Agent Bridge Wake');
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left);
  status.text = '$(mail) Bridge: disconnected'; status.command = 'agentBridgeWake.show'; status.show();
  let queue, connecting = false, activeScope;
  const log = text => output.appendLine(text);
  const folder = () => {
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before connecting the bridge.');
    const folders = vscode.workspace.workspaceFolders;
    if (folders?.length !== 1) throw new Error('Open a single repository folder for this spike.');
    return folders[0].uri.fsPath;
  };
  const tokenKey = () => 'bridge-role:' + vscode.workspace.getConfiguration('agentBridgeWake').get('url');
  async function connect() {
    if (queue || connecting) return;
    connecting = true;
    let client;
    try {
      const cwd = folder();
      const token = await context.secrets.get(tokenKey());
      if (!token) throw new Error('Run Agent Bridge: Set Role Token first.');
      client = new BridgeClient(vscode.workspace.getConfiguration('agentBridgeWake').get('url'), token);
      // Authenticate before accepting any frames, then bind state to bridge/role/workspace.
      const identity = await client.request('/api/whoami');
      if (identity.admin) throw new Error('Use a role credential, not the operator token.');
      activeScope = createHash('sha256').update(JSON.stringify([client.origin, identity.bridge_id, identity.name, cwd])).digest('hex');
      const scope = activeScope;
      const state = context.workspaceState.get('journal:' + scope, { jobs: {} });
      const journal = { state, save: () => context.workspaceState.update('journal:' + scope, state) };
      const conversations = new TaskConversations({ cwd, journal, executable: findCodex });
      queue = new WakeQueue({ client, conversation: conversations, journal,
        enabled: context.workspaceState.get('autoWake:' + activeScope, false) });
      const current = queue;
      queue.on('pending', message => {
        status.text = `$(mail) Bridge: ${current.pending.size} pending`;
        log(`Pending ${message.uid} from ${message.sender}; auto-wake ${current.enabled ? 'enabled' : 'disabled'}.`);
        if (!current.enabled) vscode.window.showInformationMessage('New agent bridge mail is pending.', 'Show').then(choice => { if (choice) output.show(); });
      });
      queue.on('completed', result => { log(`Thread ${result.threadId}, turn ${result.turnId}\n${result.output}`); });
      queue.on('acknowledged', uid => { log(`Acknowledged ${uid}`); status.text = `$(mail) Bridge: ${current.pending.size} pending`; });
      queue.on('failure', ({ uid, error }) => {
        log(`Pending ${uid}: ${error.message}`); status.text = '$(warning) Bridge: needs attention';
        if (!current.enabled) context.workspaceState.update('autoWake:' + scope, false).catch(() => log('Could not persist paused auto-wake state.'));
      });
      client.on('status', text => { log(`Bridge ${text}`); });
      client.on('message', message => current.receive(message));
      await journal.save();
      const connectedIdentity = await client.start();
      if (connectedIdentity.bridge_id !== identity.bridge_id || connectedIdentity.name !== identity.name) {
        throw new Error('Bridge identity changed while connecting; reconnect before waking an agent.');
      }
      await context.workspaceState.update('listenerEnabled', true);
      status.text = `$(mail) Bridge: ${identity.name}`;
      log(`Listening as ${identity.name}. Task-specific Codex conversations; existing IDE chats are not controlled.`);
    } catch (error) { client?.close(); queue?.close(); queue = undefined; throw error; }
    finally { connecting = false; }
  }
  const command = (name, fn) => context.subscriptions.push(vscode.commands.registerCommand(name, async () => {
    try { return await fn(); } catch (error) { log(error.message); vscode.window.showErrorMessage(error.message); }
  }));
  command('agentBridgeWake.setToken', async () => {
    folder();
    const token = await vscode.window.showInputBox({ password: true, prompt: 'Bridge role token (stored in VS Code SecretStorage)', ignoreFocusOut: true });
    if (token) { await context.secrets.store(tokenKey(), token); queue?.close(); queue = undefined; await connect(); }
  });
  command('agentBridgeWake.connect', connect);
  command('agentBridgeWake.disconnect', async () => {
    queue?.close(); queue = undefined; await context.workspaceState.update('listenerEnabled', false); status.text = '$(mail) Bridge: disconnected';
  });
  command('agentBridgeWake.enable', async () => {
    await connect();
    const choice = await vscode.window.showWarningMessage('Allow bridge assignments to start read-only Codex turns in a separate conversation for each task? This uses your Codex account. It cannot wake the existing Codex or Claude chat panel.', { modal: true }, 'Enable');
    if (choice === 'Enable') { await context.workspaceState.update('autoWake:' + activeScope, true); queue.enable(true); }
  });
  command('agentBridgeWake.disable', async () => {
    if (activeScope) await context.workspaceState.update('autoWake:' + activeScope, false);
    queue?.enable(false);
  });
  command('agentBridgeWake.show', () => output.show());
  command('agentBridgeWake.inspect', async () => { const result = await inspect(); log(JSON.stringify(result, null, 2)); output.show(); return result; });
  context.subscriptions.push(output, status, { dispose: () => queue?.close() });
  if (context.workspaceState.get('listenerEnabled', false) && vscode.workspace.isTrusted) connect().catch(e => log(e.message));
  return { inspect, resolveCodex: findCodex, getStatus: () => ({ connected: !!queue, autoWake: queue?.enabled ?? false }) };
}
module.exports = { activate, inspect };
