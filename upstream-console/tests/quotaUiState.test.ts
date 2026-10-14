/**
 * Session-scoped quota page preferences.
 *
 * The merge-on-write case is the one that matters: the tab strip and the sort
 * control each write a single field and know nothing about the other, so a
 * whole-object write would make changing a tab silently reset the sort.
 */

import { afterAll, afterEach, beforeEach, describe, expect, test } from 'bun:test';
import { readQuotaUiState, writeQuotaUiState } from '@/features/quota/uiState';

const KEY = 'quotaPage.uiState';

/** Test files share one process — leaving fake globals behind would leak. */
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');

/** bun's global has no sessionStorage; a Map-backed stub is enough here. */
function installSessionStorage(): Storage {
  const store = new Map<string, string>();
  const storage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: (index: number) => [...store.keys()][index] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage });
  return storage;
}

let storage: Storage;

beforeEach(() => {
  storage = installSessionStorage();
});

afterEach(() => {
  Reflect.deleteProperty(globalThis, 'document');
});

afterAll(() => {
  if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage);
  else Reflect.deleteProperty(globalThis, 'sessionStorage');
  if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument);
});

describe('quota ui state', () => {
  test('round-trips both preferences', () => {
    writeQuotaUiState({ tab: 'codex', sortMode: 'soonest' });
    expect(readQuotaUiState()).toEqual({ tab: 'codex', sortMode: 'soonest' });
  });

  test('retains the Devin tab across visits', () => {
    writeQuotaUiState({ tab: 'devin', sortMode: 'soonest' });
    expect(readQuotaUiState()).toEqual({ tab: 'devin', sortMode: 'soonest' });
  });

  test('writing one preference preserves the other', () => {
    writeQuotaUiState({ sortMode: 'soonest' });
    writeQuotaUiState({ tab: 'kimi' });

    expect(readQuotaUiState()).toEqual({ tab: 'kimi', sortMode: 'soonest' });
  });

  test('rejects values that are not part of the current contract', () => {
    storage.setItem(KEY, JSON.stringify({ tab: 'not-a-tab', sortMode: 'by-vibes' }));
    expect(readQuotaUiState()).toEqual({ tab: undefined, sortMode: undefined });
  });

  test('survives absent, malformed, and non-object payloads', () => {
    expect(readQuotaUiState()).toBeNull();

    storage.setItem(KEY, '{not json');
    expect(readQuotaUiState()).toBeNull();

    storage.setItem(KEY, '"a string"');
    expect(readQuotaUiState()).toBeNull();
  });

  test('a hosted document keeps preferences in memory and never touches native storage', () => {
    Object.defineProperty(globalThis, 'document', {
      configurable: true,
      value: { querySelector: () => ({ getAttribute: () => '3' }) },
    });
    Object.defineProperty(globalThis, 'sessionStorage', {
      configurable: true,
      get() {
        throw new DOMException('opaque origin', 'SecurityError');
      },
    });

    writeQuotaUiState({ tab: 'claude', sortMode: 'soonest' });

    expect(readQuotaUiState()).toEqual({ tab: 'claude', sortMode: 'soonest' });
  });
});
