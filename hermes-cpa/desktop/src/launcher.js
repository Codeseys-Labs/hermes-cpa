import { host, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, SandboxedFrame, ConfirmDialog } from '@hermes/plugin-sdk';
import React, { useEffect, useState } from 'react';
import { jsx } from 'react/jsx-runtime';
import { createCpaLauncher } from '../../console/launcher-view.js';

const ID = 'hermes-cpa';
const ROUTE = '/cpa';
const WORKSPACE_DOCK = { pane: 'workspace', pos: 'right' };

// One captured owner per launcher mount: console calls, resource presentation and revocation all use it, so a
// profile or connection switch can never deliver an old lock or grant to the newly selected backend.
function desktopSession(ctx) {
  return () => {
    if (typeof ctx.createPluginSession !== 'function') {
      throw new Error('This Hermes Desktop version cannot capture a plugin selection session');
    }
    const pinned = ctx.createPluginSession();
    if (!pinned || typeof pinned.rest !== 'function' || typeof pinned.close !== 'function' ||
        typeof pinned.onRetire !== 'function') {
      try { void pinned?.close?.(); } catch { /* the unusable session is already unreachable */ }
      throw new Error('Trusted Desktop selection session is unavailable');
    }
    const capabilities = {};
    if (typeof ctx.os?.openExternal === 'function') {
      capabilities.openExternal = async url => {
        if (!await ctx.os.openExternal(url)) throw new Error('External browser is unavailable');
      };
    }
    if (typeof ctx.os?.writeClipboard === 'function') {
      capabilities.copy = async text => {
        if (!await ctx.os.writeClipboard(text)) throw new Error('Clipboard is unavailable');
      };
    }
    if (typeof ctx.os?.saveFile === 'function') {
      capabilities.saveFile = async request => {
        const result = await ctx.os.saveFile({ suggestedFilename: request.suggestedFilename, bytes: request.bytes });
        if (!result || (result.status !== 'saved' && result.status !== 'cancelled')) throw new Error('Native file save did not complete');
        return result;
      };
    }
    if (typeof pinned.presentResource === 'function' && SandboxedFrame?.supportsPluginResources === true) {
      capabilities.presentNativeResource = request => pinned.presentResource(request);
    }
    return {
      backend: {
        get: (path, options = {}) => pinned.rest(path, options),
        post: (path, body, options = {}) => pinned.rest(path, { ...options, method: 'POST', body }),
      },
      capabilities,
      close: () => pinned.close(),
      onRetire: callback => pinned.onRetire(callback),
    };
  };
}

function selection() {
  return {
    connection: String(host.state.connectionId.get() ?? ''),
    profile: String(host.state.profile.get() ?? ''),
  };
}

function createDock({ render }) {
  let close = null;
  return {
    supported: () => typeof host.openWorkspace === 'function',
    open() {
      if (typeof host.openWorkspace !== 'function') return false;
      try {
        const closer = host.openWorkspace(ID, {
          title: 'CPA Console', dock: WORKSPACE_DOCK, minWidth: '30rem', render,
          onClose: () => { close = null; },
        });
        close = typeof closer === 'function' ? closer : null;
      } catch { return false; }
      return true;
    },
    dispose() { const closer = close; close = null; closer?.(); },
  };
}

function enterRoute({ dock, onUnavailable, win = window, navigate = path => host.navigate(path), delay = 80 }) {
  if (!dock.open()) { onUnavailable(); return () => {}; }
  win.history.back();
  const timer = setTimeout(() => { if (win.location.hash.slice(1).startsWith(ROUTE)) navigate('/'); }, delay);
  return () => clearTimeout(timer);
}
function DesktopConsole({ ctx }) {
  const [current, setCurrent] = useState(selection);
  useEffect(() => {
    const update = () => setCurrent(selection());
    const disposers = [host.state.connectionId.subscribe(update), host.state.profile.subscribe(update)];
    return () => disposers.forEach(dispose => dispose());
  }, []);
  return jsx(LauncherFor(ctx), { key: `${current.connection}:${current.profile}`, profile: current.profile });
}
// The compiled entry keeps dock and selection lifecycle local; the launcher implementation is shared.
const launchers = new WeakMap();
function LauncherFor(ctx) {
  let Launcher = launchers.get(ctx);
  if (!Launcher) {
    Launcher = createCpaLauncher({
      React,
      SandboxedFrame,
      ConfirmDialog,
      sessionFor: desktopSession(ctx),
    });
    launchers.set(ctx, Launcher);
  }
  return Launcher;
}

function RoutePage({ ctx, dock }) {
  const [docked, setDocked] = useState(dock.supported());
  useEffect(() => docked ? enterRoute({ dock, onUnavailable: () => setDocked(false) }) : undefined, [docked]);
  if (!docked) return jsx(DesktopConsole, { ctx });
  return jsx('div', { role: 'status', style: { flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }, children: 'Opening CPA Console beside the chat…' });
}

export default {
  id: ID,
  name: 'CLI Proxy API Console',
  register(ctx) {
    const dock = createDock({ render: () => jsx(DesktopConsole, { ctx }) });
    ctx.onDispose(dock.dispose);
    ctx.registerMany([
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 65, data: { codicon: 'server-process', label: 'CPA Console', path: ROUTE } },
      { id: 'route', area: ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(RoutePage, { ctx, dock }) },
      { id: 'open', area: PALETTE_AREA, data: { id: `${ID}.open`, label: 'Open CPA Console', keywords: ['cpa', 'cliproxyapi', 'proxy', 'console', 'management', 'models', 'providers'], run: () => { if (!dock.open()) host.navigate(ROUTE); } } },
    ]);
  },
};
