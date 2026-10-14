import { describe, expect, test } from 'bun:test';
import { createHashRouterWindow } from '../src/hermes/routerWindow';

interface HistoryCall {
  method: 'push' | 'replace' | 'go';
  args: unknown[];
}

/** The slice of a browser window the hosted document actually exposes to the router. */
function sourceWindow(hash: string) {
  const calls: HistoryCall[] = [];
  const source = {
    location: { hash },
    history: {
      state: { idx: 3 },
      pushState: (...args: unknown[]) => calls.push({ method: 'push', args }),
      replaceState: (...args: unknown[]) => calls.push({ method: 'replace', args }),
      go: (...args: unknown[]) => calls.push({ method: 'go', args }),
    },
    document: { marker: 'document' },
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  };
  return { facade: createHashRouterWindow(source as unknown as Window), calls, source };
}

describe('hosted router window', () => {
  test('exposes the fragment under a base that is never the data: document URL', () => {
    const { facade } = sourceWindow('#/logs?level=error');

    expect(facade.location.hash).toBe('#/logs?level=error');
    expect(facade.location.origin).toBe('https://hermes.invalid');
  });

  test('reads the hosted history state and document through the facade', () => {
    const { facade, source } = sourceWindow('#/');

    expect(facade.history.state).toEqual({ idx: 3 });
    expect(facade.document).toBe(source.document as unknown as Document);
  });

  test('rewrites every history URL to a bare fragment', () => {
    const { facade, calls } = sourceWindow('#/logs');

    facade.history.pushState({ key: 1 }, '', '#/quota');
    facade.history.replaceState({ key: 2 }, '', 'data:text/html;base64,AAAA#/system');
    facade.history.pushState({ key: 3 }, '', 'https://elsewhere.example/page');

    expect(calls).toEqual([
      { method: 'push', args: [{ key: 1 }, '', '#/quota'] },
      { method: 'replace', args: [{ key: 2 }, '', '#/system'] },
      { method: 'push', args: [{ key: 3 }, '', '#'] },
    ]);
  });

  test('keeps the current fragment when the router passes no URL', () => {
    const { facade, calls } = sourceWindow('#/auth-files');

    facade.history.replaceState({ idx: 0 }, '');

    expect(calls).toEqual([{ method: 'replace', args: [{ idx: 0 }, '', '#/auth-files'] }]);
  });

  test('delegates traversal to the real history', () => {
    const { facade, calls } = sourceWindow('#/');

    facade.history.go(-2);

    expect(calls).toEqual([{ method: 'go', args: [-2] }]);
  });
});
