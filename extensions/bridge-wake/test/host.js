'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vscode = require('vscode');
exports.run = async function () {
  const ext = vscode.extensions.getExtension('agent-bridge-local.agent-bridge-wake');
  assert.ok(ext, 'Companion is installed in the extension development host');
  const api = await ext.activate();
  assert.equal(api.getStatus().autoWake, false);
  const integrations = await api.inspect();
  assert.ok(fs.existsSync(api.resolveCodex()), 'Installed Codex binary auto-discovery works');
  const report = { companionActivated: ext.isActive, status: api.getStatus(), integrations };
  if (process.env.BRIDGE_WAKE_HOST_REPORT) fs.writeFileSync(process.env.BRIDGE_WAKE_HOST_REPORT, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
};
