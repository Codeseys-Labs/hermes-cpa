import { useCallback, useEffect, useRef } from 'react';
import { isHosted } from '@/hosted/runtime';
import { apiKeysApi } from '@/services/api/apiKeys';
import { useAuthStore, useConfigStore } from '@/stores';

export const normalizeApiKeyList = (input: unknown): string[] => {
  if (!Array.isArray(input)) return [];
  const seen = new Set<string>();
  const keys: string[] = [];

  input.forEach((item) => {
    const record =
      item !== null && typeof item === 'object' && !Array.isArray(item)
        ? (item as Record<string, unknown>)
        : null;
    const value =
      typeof item === 'string'
        ? item
        : record
          ? (record['api-key'] ?? record['apiKey'] ?? record.key ?? record.Key)
          : '';
    const trimmed = String(value ?? '').trim();
    if (!trimmed || seen.has(trimmed)) return;
    seen.add(trimmed);
    keys.push(trimmed);
  });

  return keys;
};

interface ResolveModelDiscoveryKeysOptions {
  hosted: boolean;
  configApiKeys: unknown;
  listApiKeys: () => Promise<unknown>;
}

/**
 * Keys for the `/v1/models` probe. A hosted console discovers models over the host's
 * private inference channel, which replaces public access-key authorization, so it
 * neither needs nor reads any inference access key. The reference app prefers keys from
 * the cached config and falls back to the access-keys endpoint.
 */
export async function resolveModelDiscoveryKeys({
  hosted,
  configApiKeys,
  listApiKeys,
}: ResolveModelDiscoveryKeysOptions): Promise<string[]> {
  if (hosted) return [];

  const configKeys = normalizeApiKeyList(configApiKeys);
  if (configKeys.length) return configKeys;

  try {
    return normalizeApiKeyList(await listApiKeys());
  } catch {
    return [];
  }
}

/**
 * 解析用于 /models 探测的 API 密钥列表:
 * 优先取已缓存配置中的 api-keys,否则回退到 /api-keys 接口,结果按连接缓存。
 */
export function useApiKeysForModels() {
  const apiBase = useAuthStore((state) => state.apiBase);
  const configApiKeys = useConfigStore((state) => state.config?.apiKeys);
  const cacheRef = useRef<string[]>([]);

  useEffect(() => {
    cacheRef.current = [];
  }, [apiBase, configApiKeys]);

  return useCallback(
    async ({ force = false }: { force?: boolean } = {}) => {
      if (force) {
        cacheRef.current = [];
      }
      if (cacheRef.current.length) {
        return cacheRef.current;
      }

      const keys = await resolveModelDiscoveryKeys({
        hosted: isHosted(),
        configApiKeys,
        listApiKeys: () => apiKeysApi.list(),
      });
      if (keys.length) {
        cacheRef.current = keys;
      }
      return keys;
    },
    [configApiKeys]
  );
}
