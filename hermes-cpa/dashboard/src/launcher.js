import { createCpaLauncher } from '../../console/launcher-view.js';

const pluginName = 'hermes-cpa';
const sdk = window.__HERMES_PLUGIN_SDK__;
const registry = window.__HERMES_PLUGINS__;

function unavailable(message) {
  return () => sdk.React.createElement('p', { role: 'alert' }, message);
}

if (!sdk || !registry || typeof registry.register !== 'function' || typeof sdk.getManagementProfile !== 'function' ||
    typeof sdk.createPluginSession !== 'function') {
  if (registry?.register && sdk?.React) registry.register(pluginName, unavailable('This Hermes Dashboard does not support the trusted CPA launcher.'));
} else {
  const { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter, Button } = sdk.components;
  const h = sdk.React.createElement;
  function ConfirmDialog({ open, onClose, onConfirm, title, description, confirmLabel, children }) {
    return h(Dialog, { open, onOpenChange: next => { if (!next) onClose(); } },
      h(DialogContent, { style: { maxHeight: '85dvh', overflow: 'auto' } },
        h(DialogHeader, null, h(DialogTitle, null, title), h(DialogDescription, null, description)),
        children ? h('div', { style: { padding: '12px 16px' } }, children) : null,
        h(DialogFooter, null,
          h(Button, { type: 'button', outlined: true, onClick: onClose }, 'Cancel'),
          h(Button, { type: 'button', onClick: onConfirm }, confirmLabel),
        ),
      ),
    );
  }
  const sessionFor = profile => {
    const pinned = sdk.createPluginSession(pluginName, profile);
    if (!pinned || typeof pinned.fetchJSON !== 'function' || typeof pinned.presentResource !== 'function' ||
        typeof pinned.close !== 'function') {
      throw new Error('Trusted Dashboard selection session is unavailable');
    }
    // No openExternal here: an asynchronous window.open has no user activation left and `noopener` hides a
    // blocked popup. The shared launcher view supplies a trusted click-to-open link instead.
    const capabilities = {
      saveFile: async ({ suggestedFilename, bytes }, contentType) => {
        const url = URL.createObjectURL(new Blob([bytes], { type: contentType || 'application/octet-stream' }));
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = suggestedFilename;
        anchor.rel = 'noopener';
        anchor.style.display = 'none';
        document.body.appendChild(anchor);
        anchor.click();
        setTimeout(() => { URL.revokeObjectURL(url); anchor.remove(); }, 1000);
        return { status: 'saved' };
      },
      presentNativeResource: request => pinned.presentResource(request),
    };
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      capabilities.copy = text => navigator.clipboard.writeText(text);
    }
    const signalFor = options => options.signal ?? (Number.isFinite(options.timeoutMs) &&
      typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function'
      ? AbortSignal.timeout(options.timeoutMs) : undefined);
    return {
      backend: {
        get: (path, options = {}) => pinned.fetchJSON(path, { signal: signalFor(options) }),
        post: (path, body, options = {}) => pinned.fetchJSON(path, {
          method: 'POST', body: JSON.stringify(body), headers: { 'content-type': 'application/json' }, signal: signalFor(options),
        }),
      },
      capabilities,
      close: () => pinned.close(),
    };
  };
  const Launcher = createCpaLauncher({
    React: sdk.React,
    ConfirmDialog,
    sessionFor,
    dashboard: true,
  });
  registry.register(pluginName, function DashboardCpaLauncher() {
    return sdk.React.createElement(Launcher, { profile: sdk.getManagementProfile() });
  });
}
