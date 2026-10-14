import axios from 'axios';
import { normalizeApiBase } from '@/utils/connection';
import { isHosted } from '@/hosted/runtime';
import { managementRequest } from '@/hermesTransport';

/** Diagnostic only: never use v0 responses as application data or a login fallback. */
export class LegacyBackendError extends Error {
  constructor() {
    super('The backend supports v0 management but not v8 management');
    this.name = 'LegacyBackendError';
  }
}

export async function probeLegacyBackend(apiBase: string, managementKey: string): Promise<boolean> {
  if (isHosted()) {
    try {
      const response = await managementRequest({
        method: 'GET', path: '/v0/management/config',
        body: { kind: 'empty' }, response_kind: 'json',
      });
      return response.status >= 200 && response.status < 300 &&
        response.body.kind === 'json' && response.body.value !== null &&
        typeof response.body.value === 'object' && !Array.isArray(response.body.value);
    } catch {
      return false;
    }
  }
  const base = normalizeApiBase(apiBase);
  if (!base || !managementKey) return false;

  try {
    const response = await axios.get<unknown>(`${base}/v0/management/config`, {
      headers: { Authorization: `Bearer ${managementKey}` },
      timeout: 3000,
      // This request must not affect the v8 client's auth or version events.
      maxRedirects: 0,
    });
    return (
      response.data !== null && typeof response.data === 'object' && !Array.isArray(response.data)
    );
  } catch {
    // Includes 401, 403, network errors and missing v0 routes: none proves an old backend.
    return false;
  }
}
