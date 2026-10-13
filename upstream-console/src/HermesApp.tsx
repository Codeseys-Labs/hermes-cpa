// Restricted build: feature-owned upstream dashboard/config components are
// pruned at source; auth, storage, provider and raw YAML modules are not imported.
import { createRoot } from 'react-dom/client';
import { useEffect } from 'react';
import '@/i18n/hermesRead';
import { HashRouter } from 'react-router-dom';
import '@/styles/global.scss';
import { MainLayout } from '@/hermes/HermesMainLayout';
import { initializePreferences } from '@/hermes/preferences';

// data: has no hierarchical URL base. This base is never fetched.
const routerWindow = {
  get location() { return new URL('https://hermes.invalid/' + window.location.hash); },
  history: {
    get state() { return window.history.state; },
    replaceState(state: unknown, unused: string, url?: string | URL | null) {
      const hash = url == null ? window.location.hash : String(url).split('#')[1] ?? '';
      window.history.replaceState(state, unused, '#' + hash.replace(/^#/, ''));
    },
    pushState(state: unknown, unused: string, url?: string | URL | null) {
      const hash = url == null ? window.location.hash : String(url).split('#')[1] ?? '';
      window.history.pushState(state, unused, '#' + hash.replace(/^#/, ''));
    },
    go(delta: number) { window.history.go(delta); },
  },
  document: window.document,
  addEventListener: window.addEventListener.bind(window),
  removeEventListener: window.removeEventListener.bind(window),
} as unknown as Window;
function HermesApp() {
  useEffect(initializePreferences, []);
  return <HashRouter window={routerWindow}><MainLayout /></HashRouter>;
}
createRoot(document.getElementById('root')!).render(<HermesApp />);