import { isHosted } from './runtime';

function memoryStorage(): Storage {
  const items = new Map<string, string>();
  return {
    get length() { return items.size; },
    clear: () => items.clear(),
    getItem: (key) => items.get(String(key)) ?? null,
    key: (index) => Array.from(items.keys())[index] ?? null,
    removeItem: (key) => { items.delete(String(key)); },
    setItem: (key, value) => { items.set(String(key), String(value)); },
  };
}

const memory = { local: memoryStorage(), session: memoryStorage() };

/** Never evaluate a native storage getter in an opaque hosted document. */
export function getUiStorage(kind: 'local' | 'session'): Storage {
  if (isHosted()) return memory[kind];
  return kind === 'local' ? localStorage : sessionStorage;
}

export function clearHostedStorage(): void {
  memory.local.clear();
  memory.session.clear();
}
