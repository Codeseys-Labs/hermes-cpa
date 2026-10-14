import { afterEach, beforeEach, describe, expect, test } from 'bun:test';
import {
  readAuthFilesUiState,
  readPersistedAuthFilesCompactMode,
  writeAuthFilesUiState,
  writePersistedAuthFilesCompactMode,
} from '../src/features/authFiles/uiState';
import {
  readProvidersWorkbenchUiState,
  writeProvidersWorkbenchUiState,
} from '../src/features/providers/uiState';
import { clearHostedStorage } from '../src/hosted/storage';

const descriptors = ['document', 'localStorage', 'sessionStorage'] as const;
const originals = descriptors.map((name) => Object.getOwnPropertyDescriptor(globalThis, name));
let nativeStorageReads = 0;

/** An opaque-origin guest: the marker is present and native storage throws when evaluated. */
function hostDocument() {
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: { querySelector: () => ({ getAttribute: () => '3' }) },
  });
  for (const name of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      get() {
        nativeStorageReads++;
        throw new DOMException('The document is sandboxed', 'SecurityError');
      },
    });
  }
}

/** A normal browser tab with real, persistent storage. */
function referenceBrowser() {
  const stub = () => {
    const items = new Map<string, string>();
    return {
      items,
      storage: {
        getItem: (key: string) => items.get(key) ?? null,
        setItem: (key: string, value: string) => void items.set(key, value),
        removeItem: (key: string) => void items.delete(key),
      },
    };
  };
  const local = stub();
  const session = stub();
  Reflect.deleteProperty(globalThis, 'document');
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: local.storage });
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: session.storage,
  });
  return local.items;
}

beforeEach(() => {
  nativeStorageReads = 0;
  clearHostedStorage();
});

afterEach(() => {
  descriptors.forEach((name, index) => {
    const original = originals[index];
    if (original) Object.defineProperty(globalThis, name, original);
    else Reflect.deleteProperty(globalThis, name);
  });
});

describe('UI preferences in a hosted document', () => {
  test('provider workbench state survives within the document without native storage', () => {
    hostDocument();

    writeProvidersWorkbenchUiState({
      activeBrand: 'kimi',
      filtersByBrand: {
        kimi: { filter: 'k2', sortBy: 'name', sortDir: 'desc', selectedModels: ['kimi-k2'] },
      },
    });

    expect(readProvidersWorkbenchUiState()).toEqual({
      activeBrand: 'kimi',
      filtersByBrand: {
        kimi: { filter: 'k2', sortBy: 'name', sortDir: 'desc', selectedModels: ['kimi-k2'] },
      },
    });
    expect(nativeStorageReads).toBe(0);
  });

  test('credential list filters and compact mode are kept in memory only', () => {
    hostDocument();

    writeAuthFilesUiState({ search: 'codex', page: 2 });
    writePersistedAuthFilesCompactMode(true);

    expect(readAuthFilesUiState()).toEqual({ search: 'codex', page: 2 });
    expect(readPersistedAuthFilesCompactMode()).toBe(true);
    expect(nativeStorageReads).toBe(0);
  });

  test('locking clears what the guest remembered', () => {
    hostDocument();
    writeAuthFilesUiState({ search: 'private-account-name' });

    clearHostedStorage();

    expect(readAuthFilesUiState()).toBeNull();
  });
});

describe('UI preferences in the reference app', () => {
  test('are persisted in the browser storage', () => {
    const items = referenceBrowser();

    writePersistedAuthFilesCompactMode(true);
    writeAuthFilesUiState({ search: 'claude' });

    expect(items.get('authFilesPage.compactMode')).toBe('true');
    expect(JSON.parse(items.get('authFilesPage.uiState') ?? 'null')).toEqual({ search: 'claude' });
    expect(readAuthFilesUiState()).toEqual({ search: 'claude' });
  });
});
