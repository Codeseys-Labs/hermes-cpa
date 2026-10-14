import { apiClient } from './client';
import { inheritConfigRevision } from './configRevision';

/** Capture before a config read and check immediately before its dependent write. */
export function guardConfigConnection(): () => void {
  const revision = apiClient.getConnectionRevision();
  return () => {
    if (revision !== apiClient.getConnectionRevision()) {
      throw new DOMException('The management connection changed.', 'AbortError');
    }
  };
}

/** Only an absent persisted config field is optional, not a missing API route. */
export const isMissingConfigValue = (error: unknown): boolean =>
  typeof error === 'object' &&
  error !== null &&
  'status' in error &&
  error.status === 404 &&
  'apiCode' in error &&
  error.apiCode === 'not_found';

export async function getConfigValue<T>(path: string, defaultValue: T): Promise<T> {
  try {
    return await apiClient.get<T>(path);
  } catch (error) {
    if (isMissingConfigValue(error)) {
      if (defaultValue !== null && typeof defaultValue === 'object' &&
          typeof error === 'object' && error !== null && 'data' in error) {
        inheritConfigRevision(error.data, defaultValue);
      }
      return defaultValue;
    }
    throw error;
  }
}
