import { afterEach, expect, test } from 'bun:test';
import { clearHostedStorage, getUiStorage } from '../src/hosted/storage';
import { isHosted } from '../src/hosted/runtime';

const names = ['document', 'localStorage', 'sessionStorage'] as const;
const original = new Map(names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
afterEach(() => {
  clearHostedStorage();
  for (const name of names) {
    const descriptor = original.get(name);
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else Reflect.deleteProperty(globalThis, name);
  }
});

test('hosted detection and both memory stores never evaluate opaque-origin storage getters', () => {
  Object.defineProperty(globalThis, 'document', { configurable: true, value: {
    querySelector: () => ({ getAttribute: () => '3' }),
  } });
  for (const name of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(globalThis, name, { configurable: true, get() {
      throw new Error('Native storage getter must not be touched');
    } });
  }
  expect(isHosted()).toBe(true);
  const local = getUiStorage('local');
  const session = getUiStorage('session');
  local.setItem('provider', 'generated-fixture');
  session.setItem('filter', 'all');
  expect(getUiStorage('local')).toBe(local);
  expect(local.length).toBe(1);
  expect(local.key(0)).toBe('provider');
  expect(session.getItem('provider')).toBeNull();
  clearHostedStorage();
  expect(local.getItem('provider')).toBeNull();
  expect(session.length).toBe(0);
});

test('reference mode resolves real storage lazily and retains native failures', () => {
  Object.defineProperty(globalThis, 'document', { configurable: true, value: {
    querySelector: () => null,
  } });
  let accesses = 0;
  const native = { getItem: () => 'existing-reference-preference' };
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, get() {
    accesses += 1;
    return native;
  } });
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, get() {
    throw new DOMException('Storage denied', 'SecurityError');
  } });
  expect(isHosted()).toBe(false);
  expect(accesses).toBe(0);
  expect(getUiStorage('local')).toBe(native);
  expect(accesses).toBe(1);
  expect(() => getUiStorage('session')).toThrow('Storage denied');
});
