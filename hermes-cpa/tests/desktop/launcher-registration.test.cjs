const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// The Desktop entry imports the host SDK, React and the shared view. Replace those four imports with fixtures
// so the real session adapter in desktop/src/launcher.js is what runs.
const source = fs.readFileSync(path.join(__dirname, '../../desktop/src/launcher.js'), 'utf8')
  .replace(/^import \{ host, [^}]+\} from '@hermes\/plugin-sdk';$/m,
    'const { host, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, SandboxedFrame, ConfirmDialog } = globalThis.__HERMES_SDK__;')
  .replace("import React, { useEffect, useState } from 'react';",
    'const React = globalThis.__REACT__; const { useEffect, useState } = React;')
  .replace("import { jsx } from 'react/jsx-runtime';", 'const jsx = globalThis.__JSX__;')
  .replace("import { createCpaLauncher } from '../../console/launcher-view.js';",
    'const { createCpaLauncher } = globalThis.__CPA_LAUNCHER_VIEW__;');
assert.equal(/^import /m.test(source), false, 'every import of the Desktop entry must be replaced by a fixture');

let launcherOptions;
Object.assign(globalThis, {
  __HERMES_SDK__: {
    host: { state: {
      connectionId: { get: () => 'connection-a', subscribe: () => () => {} },
      profile: { get: () => 'work', subscribe: () => () => {} },
    } },
    ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'sidebar', PALETTE_AREA: 'palette',
    SandboxedFrame: { supportsVerifiedDocument: true, supportsPluginResources: true },
    ConfirmDialog: function ConfirmDialog() { return null; },
  },
  __REACT__: { useState: initial => [typeof initial === 'function' ? initial() : initial, () => {}], useEffect: () => {} },
  __JSX__: (type, props) => ({ type, props }),
  __CPA_LAUNCHER_VIEW__: {
    createCpaLauncher: options => { launcherOptions = options; return function Launcher() { return null; }; },
  },
});

const loadDesktop = () => import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

/** Registers the plugin against `ctx` and walks the route down to the options the shared view receives. */
async function sessionFactory(ctx) {
  const desktop = await loadDesktop();
  const registered = [];
  ctx.onDispose = () => {};
  ctx.registerMany = items => registered.push(...items);
  desktop.default.register(ctx);
  const page = registered.find(item => item.id === 'route').render();
  const consoleElement = page.type(page.props);
  const launcher = consoleElement.type(consoleElement.props);
  assert.equal(launcher.props.profile, 'work');
  return launcherOptions.sessionFor;
}

test('desktop launcher consumes one captured selection session and never the ambient selection-bound REST door', async () => {
  const calls = [];
  const retire = [];
  let created = 0;
  const pinned = {
    rest: async (requestPath, options) => { calls.push({ rest: requestPath, options }); return { data: {} }; },
    presentResource: async request => {
      calls.push({ presentResource: request });
      return { url: 'hermes-plugin-resource://p-grant/v0/resource/plugins/demo/index.html', close: async () => {} };
    },
    onRetire: callback => { retire.push(callback); return () => calls.push('unsubscribed'); },
    close: async () => { calls.push('closed'); },
  };
  const ctx = {
    createPluginSession: (...args) => { created += 1; assert.deepEqual(args, [], 'the host captures the selection itself'); return pinned; },
    rest: () => assert.fail('the ambient selection-bound door can follow a later selection'),
    os: {
      openExternal: async url => { calls.push({ openExternal: url }); return true; },
      writeClipboard: async () => true,
      saveFile: async request => ({ status: 'saved', size: request.bytes.byteLength }),
      presentPluginResource: () => assert.fail('resource presentation belongs to the captured session'),
    },
  };
  const sessionFor = await sessionFactory(ctx);
  const session = sessionFor('work');
  assert.equal(created, 1);

  await session.backend.get('/console/asset?profile=work', { timeoutMs: 10000 });
  await session.backend.post('/console/lock?profile=work', { lease_id: 'private' }, { timeoutMs: 9000 });
  assert.deepEqual(calls.splice(0), [
    { rest: '/console/asset?profile=work', options: { timeoutMs: 10000 } },
    { rest: '/console/lock?profile=work', options: { timeoutMs: 9000, method: 'POST', body: { lease_id: 'private' } } },
  ]);

  const request = { documentId: 'doc', grantId: 'grant', entryPath: '/v0/resource/plugins/demo/index.html', expiresAt: 1_700_000_000.25 };
  const presentation = await session.capabilities.presentNativeResource(request);
  assert.match(presentation.url, /^hermes-plugin-resource:\/\//);
  assert.deepEqual(calls.splice(0), [{ presentResource: request }], 'the fractional owner expiry reaches the host untouched');

  const callback = () => Promise.resolve();
  const unsubscribe = session.onRetire(callback);
  assert.deepEqual(retire, [callback], 'revocation before deselection is registered with the captured session');
  unsubscribe();

  await session.capabilities.openExternal('https://example.test/oauth?state=abc&x=1');
  assert.deepEqual(calls.splice(0, 1), ['unsubscribed']);
  assert.deepEqual(calls.splice(0), [{ openExternal: 'https://example.test/oauth?state=abc&x=1' }], 'Desktop keeps the native external open');

  const exportBytes = new ArrayBuffer(32 * 1024 * 1024);
  assert.deepEqual(await session.capabilities.saveFile({ suggestedFilename: 'export.bin', bytes: exportBytes }), { status: 'saved', size: exportBytes.byteLength });

  await session.close();
  assert.deepEqual(calls, ['closed']);
});

test('desktop launcher fails closed without a usable captured selection session', async () => {
  const sessionFor = await sessionFactory({ os: {} });
  assert.throws(() => sessionFor('work'), /cannot capture a plugin selection session/);

  let closed = 0;
  const unusable = await sessionFactory({
    createPluginSession: () => ({ rest: async () => ({}), close: async () => { closed += 1; } }),
    os: {},
  });
  assert.throws(() => unusable('work'), /selection session is unavailable/);
  assert.equal(closed, 1, 'a session that cannot revoke before deselection is closed immediately and never used');

  const withoutResources = await sessionFactory({
    createPluginSession: () => ({ rest: async () => ({}), onRetire: () => () => {}, close: async () => {} }),
    os: {},
  });
  assert.equal('presentNativeResource' in withoutResources('work').capabilities, false);
});
