import { describe, expect, test } from 'bun:test';
import { resolveModelDiscoveryKeys } from '../src/hooks/useApiKeysForModels';

const failIfListed = () => {
  throw new Error('access keys must not be read');
};

describe('model discovery keys', () => {
  test('a hosted console never reads or needs an inference access key', async () => {
    const keys = await resolveModelDiscoveryKeys({
      hosted: true,
      configApiKeys: ['sk-from-config'],
      listApiKeys: failIfListed,
    });

    expect(keys).toEqual([]);
  });

  test('the reference app prefers de-duplicated keys from the cached config', async () => {
    const keys = await resolveModelDiscoveryKeys({
      hosted: false,
      configApiKeys: [' sk-a ', 'sk-a', { 'api-key': 'sk-b' }, '', null],
      listApiKeys: failIfListed,
    });

    expect(keys).toEqual(['sk-a', 'sk-b']);
  });

  test('the reference app falls back to the access-keys endpoint', async () => {
    const keys = await resolveModelDiscoveryKeys({
      hosted: false,
      configApiKeys: undefined,
      listApiKeys: async () => ['sk-listed'],
    });

    expect(keys).toEqual(['sk-listed']);
  });

  test('a failing access-keys endpoint yields no key instead of failing discovery', async () => {
    const keys = await resolveModelDiscoveryKeys({
      hosted: false,
      configApiKeys: [],
      listApiKeys: async () => {
        throw new Error('401');
      },
    });

    expect(keys).toEqual([]);
  });
});
