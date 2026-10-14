const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const entry = fs.readFileSync(path.join(__dirname, '../../dashboard/src/launcher.js'), 'utf8')
  .replace("import { createCpaLauncher } from '../../console/launcher-view.js';", 'const { createCpaLauncher } = globalThis.__CPA_LAUNCHER_VIEW__;');

let loads = 0;
async function loadEntry() {
  loads += 1;
  return import(`data:text/javascript;base64,${Buffer.from(`${entry}\n// load ${loads}`).toString('base64')}`);
}

test('dashboard launcher registers against the host SDK and pins one profile session for every trusted operation', async () => {
  const originalWindow = global.window;
  const originalView = global.__CPA_LAUNCHER_VIEW__;
  let registration;
  let launcherOptions;
  const calls = [];
  const pinned = {
    fetchJSON: async (url, init) => {
      calls.push({ url, init });
      return { data: { ok: true } };
    },
    presentResource: async request => {
      calls.push({ presentResource: request });
      return { url: 'http://p-grant.localhost/v0/resource/plugins/demo/index.html', close: async () => { calls.push({ close: true }); } };
    },
    close: async () => { calls.push({ sessionClosed: true }); },
  };
  const React = {
    createElement: (type, props) => ({ type, props }),
  };
  global.window = {
    __HERMES_PLUGIN_SDK__: {
      React,
      components: {},
      getManagementProfile: () => 'original-home',
      createPluginSession: (name, profile) => {
        calls.push({ createSession: { name, profile } });
        return pinned;
      },
    },
    __HERMES_PLUGINS__: {
      register: (name, component) => { registration = { name, component }; },
    },
  };
  global.__CPA_LAUNCHER_VIEW__ = {
    createCpaLauncher: options => {
      launcherOptions = options;
      return function Launcher() { return null; };
    },
  };
  try {
    await loadEntry();
    assert.equal(registration.name, 'hermes-cpa');
    const element = registration.component();
    assert.equal(element.props.profile, 'original-home');
    const hostSession = launcherOptions.sessionFor(element.props.profile);
    assert.deepEqual(calls.shift(), { createSession: { name: 'hermes-cpa', profile: 'original-home' } });
    await hostSession.backend.get('/console/asset?profile=original-home');
    await hostSession.backend.post('/console/lock?profile=original-home', { document_id: 'doc', lease_id: 'private' });
    await hostSession.capabilities.presentNativeResource({ documentId: 'doc', grantId: 'grant', entryPath: '/v0/resource/plugins/demo/index.html', expiresAt: 1 });
    await hostSession.close();
    assert.deepEqual(calls, [
      { url: '/console/asset?profile=original-home', init: { signal: undefined } },
      { url: '/console/lock?profile=original-home', init: { method: 'POST', body: JSON.stringify({ document_id: 'doc', lease_id: 'private' }), headers: { 'content-type': 'application/json' }, signal: undefined } },
      { presentResource: { documentId: 'doc', grantId: 'grant', entryPath: '/v0/resource/plugins/demo/index.html', expiresAt: 1 } },
      { sessionClosed: true },
    ]);
  } finally {
    global.window = originalWindow;
    global.__CPA_LAUNCHER_VIEW__ = originalView;
  }
});

test('dashboard session opens no page asynchronously, bounds calls with a signal, and preserves the host busy refusal', async () => {
  const originalWindow = global.window;
  const originalView = global.__CPA_LAUNCHER_VIEW__;
  let launcherOptions;
  const opened = [];
  const busy = Object.assign(new Error('Too Many Requests'), {
    name: 'ApiError', status: 429, body: JSON.stringify({ error: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: false }),
  });
  const signals = [];
  const pinned = {
    fetchJSON: async (url, init) => {
      signals.push(init.signal);
      if (url.startsWith('/console/execute')) throw busy;
      return { data: {} };
    },
    presentResource: async () => ({ url: 'http://p-grant.localhost/', close: async () => {} }),
    close: async () => {},
  };
  global.window = {
    open: (...args) => { opened.push(args); return null; },
    __HERMES_PLUGIN_SDK__: {
      React: { createElement: (type, props) => ({ type, props }) },
      components: {},
      getManagementProfile: () => 'original-home',
      createPluginSession: () => pinned,
    },
    __HERMES_PLUGINS__: { register: () => {} },
  };
  global.__CPA_LAUNCHER_VIEW__ = {
    createCpaLauncher: options => { launcherOptions = options; return function Launcher() { return null; }; },
  };
  try {
    await loadEntry();
    const hostSession = launcherOptions.sessionFor('original-home');
    assert.equal('openExternal' in hostSession.capabilities, false,
      'an asynchronous window.open has no user activation and noopener hides a blocked popup');
    assert.equal(launcherOptions.dashboard, true, 'the shared view supplies the trusted click-to-open link');
    assert.deepEqual(opened, []);

    await hostSession.backend.post('/console/lock?profile=original-home', { lease_id: 'private' }, { timeoutMs: 9000 });
    assert.ok(signals[0] instanceof AbortSignal, 'a bounded revocation carries a timeout signal');
    const caller = new AbortController();
    await hostSession.backend.get('/console/asset?profile=original-home', { signal: caller.signal, timeoutMs: 10000 });
    assert.equal(signals[1], caller.signal, 'a caller signal is never replaced');
    await hostSession.backend.post('/console/result?profile=original-home', { operation_id: 'operation-1' });
    assert.equal(signals[2], undefined, 'a call without a timeout carries no signal, exactly as before');

    const refusal = await hostSession.backend.post('/console/execute?profile=original-home', {}).catch(error => error);
    assert.equal(refusal, busy, 'the ApiError status and body reach the bridge unchanged');
    assert.equal(refusal.status, 429);
    assert.deepEqual(JSON.parse(refusal.body), { error: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: false });
  } finally {
    global.window = originalWindow;
    global.__CPA_LAUNCHER_VIEW__ = originalView;
  }
});
