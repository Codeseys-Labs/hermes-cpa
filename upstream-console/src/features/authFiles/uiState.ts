import { getUiStorage } from '@/hosted/storage';

export const AUTH_FILES_SORT_MODES = ['default', 'az', 'priority'] as const;
export const AUTH_FILES_STATUS_FILTER_MODES = [
  'all',
  'enabled',
  'disabled',
  'problem',
] as const;

export type AuthFilesSortMode = (typeof AUTH_FILES_SORT_MODES)[number];
export type AuthFilesStatusFilterMode = (typeof AUTH_FILES_STATUS_FILTER_MODES)[number];

export type AuthFilesUiState = {
  filter?: string;
  problemOnly?: boolean;
  disabledOnly?: boolean;
  statusFilterMode?: AuthFilesStatusFilterMode;
  compactMode?: boolean;
  search?: string;
  page?: number;
  pageSize?: number;
  regularPageSize?: number;
  compactPageSize?: number;
  sortMode?: AuthFilesSortMode;
};

const AUTH_FILES_UI_STATE_KEY = 'authFilesPage.uiState';
const AUTH_FILES_COMPACT_MODE_KEY = 'authFilesPage.compactMode';
const AUTH_FILES_SORT_MODE_SET = new Set<AuthFilesSortMode>(AUTH_FILES_SORT_MODES);
const AUTH_FILES_STATUS_FILTER_MODE_SET = new Set<AuthFilesStatusFilterMode>(
  AUTH_FILES_STATUS_FILTER_MODES
);

export const isAuthFilesSortMode = (value: unknown): value is AuthFilesSortMode =>
  typeof value === 'string' && AUTH_FILES_SORT_MODE_SET.has(value as AuthFilesSortMode);

export const isAuthFilesStatusFilterMode = (value: unknown): value is AuthFilesStatusFilterMode =>
  typeof value === 'string' &&
  AUTH_FILES_STATUS_FILTER_MODE_SET.has(value as AuthFilesStatusFilterMode);

const readAuthFilesUiStateFromStorage = (
  storage: Pick<Storage, 'getItem'> | null | undefined
): AuthFilesUiState | null => {
  if (!storage) return null;
  const raw = storage.getItem(AUTH_FILES_UI_STATE_KEY);
  if (!raw) return null;
  const parsed = JSON.parse(raw) as AuthFilesUiState;
  return parsed && typeof parsed === 'object' ? parsed : null;
};

export const readAuthFilesUiState = (): AuthFilesUiState | null => {
  try {
    return (
      readAuthFilesUiStateFromStorage(getUiStorage('local')) ??
      readAuthFilesUiStateFromStorage(getUiStorage('session'))
    );
  } catch {
    return null;
  }
};

export const writeAuthFilesUiState = (state: AuthFilesUiState) => {
  try {
    getUiStorage('local').setItem(AUTH_FILES_UI_STATE_KEY, JSON.stringify(state));
  } catch {
    // ignore
  }
  try {
    getUiStorage('session').removeItem(AUTH_FILES_UI_STATE_KEY);
  } catch {
    // ignore
  }
};

export const readPersistedAuthFilesCompactMode = (): boolean | null => {
  try {
    const raw = getUiStorage('local').getItem(AUTH_FILES_COMPACT_MODE_KEY);
    if (raw === null) return null;
    return JSON.parse(raw) === true;
  } catch {
    return null;
  }
};

export const writePersistedAuthFilesCompactMode = (compactMode: boolean) => {
  try {
    getUiStorage('local').setItem(AUTH_FILES_COMPACT_MODE_KEY, JSON.stringify(compactMode));
  } catch {
    // ignore
  }
};
