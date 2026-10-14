/**
 * 认证状态管理
 * 从原项目 src/modules/login.js 和 src/core/connection.js 迁移
 */

import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import type { AuthState, LoginCredentials, ConnectionStatus } from '@/types';
import { STORAGE_KEY_AUTH } from '@/utils/constants';
import { obfuscatedStorage } from '@/services/storage/secureStorage';
import { apiClient } from '@/services/api/client';
import { LegacyBackendError, probeLegacyBackend } from '@/services/api/legacyBackendProbe';
import { useConfigStore } from './useConfigStore';
import { useModelsStore } from './useModelsStore';
import { useQuotaStore } from './useQuotaStore';
import { detectApiBaseFromLocation, normalizeApiBase } from '@/utils/connection';
import { isHosted } from '@/hosted/runtime';
import { clearHostedStorage, getUiStorage } from '@/hosted/storage';
import { getHostedTransport, lockHostedSession } from '@/hermesTransport';

interface AuthStoreState extends AuthState {
  connectionStatus: ConnectionStatus;

  // 操作
  login: (credentials: LoginCredentials) => Promise<void>;
  logout: () => void;
  checkAuth: () => Promise<boolean>;
  restoreSession: () => Promise<boolean>;
  updateServerVersion: (version: string | null, buildDate?: string | null) => void;
  updateServerPluginSupport: (supportsPlugin: boolean) => void;
}

let restoreSessionPromise: Promise<boolean> | null = null;
let hostedInitializationPromise: Promise<boolean> | null = null;

export const useAuthStore = create<AuthStoreState>()(
  persist(
    (set, get) => ({
      // 初始状态
      isAuthenticated: false,
      apiBase: '',
      managementKey: '',
      rememberPassword: false,
      serverVersion: null,
      serverBuildDate: null,
      supportsPlugin: false,
      connectionStatus: 'disconnected',

      // 恢复会话并自动登录
      restoreSession: () => {
        if (restoreSessionPromise) return restoreSessionPromise;
        if (isHosted()) {
          restoreSessionPromise = initializeHostedSession();
          return restoreSessionPromise;
        }

        restoreSessionPromise = (async () => {
          obfuscatedStorage.migratePlaintextKeys(['apiBase', 'apiUrl', 'managementKey']);

          const wasLoggedIn = getUiStorage('local').getItem('isLoggedIn') === 'true';
          const legacyBase =
            obfuscatedStorage.getItem<string>('apiBase') ||
            obfuscatedStorage.getItem<string>('apiUrl', { encrypt: true });
          const legacyKey = obfuscatedStorage.getItem<string>('managementKey');

          const { apiBase, managementKey, rememberPassword } = get();
          const resolvedBase = normalizeApiBase(
            apiBase || legacyBase || detectApiBaseFromLocation()
          );
          const resolvedKey = managementKey || legacyKey || '';
          const resolvedRememberPassword =
            rememberPassword || Boolean(managementKey) || Boolean(legacyKey);

          set({
            apiBase: resolvedBase,
            managementKey: resolvedKey,
            rememberPassword: resolvedRememberPassword,
          });
          apiClient.setConfig({ apiBase: resolvedBase, managementKey: resolvedKey });

          if (wasLoggedIn && resolvedBase && resolvedKey) {
            try {
              await get().login({
                apiBase: resolvedBase,
                managementKey: resolvedKey,
                rememberPassword: resolvedRememberPassword,
              });
              return true;
            } catch (error) {
              console.warn('Auto login failed:', error);
              return false;
            }
          }

          return false;
        })();

        return restoreSessionPromise;
      },

      // 登录
      login: async (credentials) => {
        if (isHosted()) {
          if (!(await initializeHostedSession())) throw new Error('Console is locked');
          return;
        }
        const apiBase = normalizeApiBase(credentials.apiBase);
        const managementKey = credentials.managementKey.trim();
        const rememberPassword = credentials.rememberPassword ?? get().rememberPassword ?? false;

        try {
          set({
            connectionStatus: 'connecting',
            serverVersion: null,
            serverBuildDate: null,
            supportsPlugin: false,
          });
          useConfigStore.getState().clearCache();
          useModelsStore.getState().clearCache();
          useQuotaStore.getState().clearQuotaCache();

          // 配置 API 客户端
          apiClient.setConfig({
            apiBase,
            managementKey,
          });

          // 测试连接 - 获取配置。只在 v8 路由不存在时诊断旧版后端。
          const revision = apiClient.getConnectionRevision();
          try {
            await useConfigStore.getState().fetchConfig(true);
          } catch (error) {
            if (
              (error as { status?: number })?.status === 404 &&
              revision === apiClient.getConnectionRevision() &&
              (await probeLegacyBackend(apiBase, managementKey)) &&
              revision === apiClient.getConnectionRevision()
            ) {
              throw new LegacyBackendError();
            }
            throw error;
          }

          // 登录成功
          set({
            isAuthenticated: true,
            apiBase,
            managementKey,
            rememberPassword,
            connectionStatus: 'connected',
          });
          if (rememberPassword) {
            getUiStorage('local').setItem('isLoggedIn', 'true');
          } else {
            getUiStorage('local').removeItem('isLoggedIn');
          }
        } catch (error: unknown) {
          set({ connectionStatus: 'error' });
          throw error;
        }
      },

      // 登出
      logout: () => {
        if (isHosted()) {
          if (getHostedTransport().unlocked) void lockHostedSession();
          clearHostedStorage();
        }
        restoreSessionPromise = null;
        hostedInitializationPromise = null;
        apiClient.setConfig({ apiBase: '', managementKey: '' });
        useConfigStore.getState().clearCache();
        useModelsStore.getState().clearCache();
        useQuotaStore.getState().clearQuotaCache();
        set({
          isAuthenticated: false,
          apiBase: '',
          managementKey: '',
          serverVersion: null,
          serverBuildDate: null,
          supportsPlugin: false,
          connectionStatus: 'disconnected',
        });
        getUiStorage('local').removeItem('isLoggedIn');
      },

      // 检查认证状态
      checkAuth: async () => {
        if (isHosted()) {
          if (!getHostedTransport().unlocked) return false;
          try {
            await useConfigStore.getState().fetchConfig();
            return getHostedTransport().unlocked && get().isAuthenticated;
          } catch {
            return false;
          }
        }
        const { managementKey, apiBase } = get();

        if (!managementKey || !apiBase) {
          return false;
        }

        try {
          // 重新配置客户端
          apiClient.setConfig({ apiBase, managementKey });
          set({ supportsPlugin: false });

          // 验证连接
          await useConfigStore.getState().fetchConfig();

          set({
            isAuthenticated: true,
            connectionStatus: 'connected',
          });

          return true;
        } catch {
          set({
            isAuthenticated: false,
            connectionStatus: 'error',
            supportsPlugin: false,
          });
          return false;
        }
      },

      // 更新服务器版本
      updateServerVersion: (version, buildDate) => {
        set({
          serverVersion: version || null,
          serverBuildDate: buildDate || null,
        });
      },

      updateServerPluginSupport: (supportsPlugin) => {
        set({ supportsPlugin });
      },
    }),
    {
      name: STORAGE_KEY_AUTH,
      storage: createJSONStorage(() => ({
        getItem: (name) => {
          const data = obfuscatedStorage.getItem<AuthStoreState>(name);
          return data ? JSON.stringify(data) : null;
        },
        setItem: (name, value) => {
          obfuscatedStorage.setItem(name, JSON.parse(value));
        },
        removeItem: (name) => {
          obfuscatedStorage.removeItem(name);
        },
      })),
      partialize: (state) => isHosted() ? {} : ({
        apiBase: state.apiBase,
        ...(state.rememberPassword ? { managementKey: state.managementKey } : {}),
        rememberPassword: state.rememberPassword,
        serverVersion: state.serverVersion,
        serverBuildDate: state.serverBuildDate,
      }),
    }
  )
);

/** Only a real, explicitly unlocked host channel can authenticate the embedded console. */
export async function initializeHostedSession(): Promise<boolean> {
  if (!isHosted()) return useAuthStore.getState().restoreSession();
  if (hostedInitializationPromise) return hostedInitializationPromise;
  const channel = getHostedTransport();
  hostedInitializationPromise = (async () => {
    try {
      await channel.ready;
      if (!channel.unlocked) return false;
      if (useAuthStore.getState().isAuthenticated) return true;
      useAuthStore.setState({ connectionStatus: 'connecting' });
      apiClient.setConfig({ apiBase: '', managementKey: '' });
      useConfigStore.getState().clearCache();
      useModelsStore.getState().clearCache();
      useQuotaStore.getState().clearQuotaCache();
      await useConfigStore.getState().fetchConfig(true);
      if (!channel.unlocked) return false;
      useAuthStore.setState({
        isAuthenticated: true, apiBase: '', managementKey: '', rememberPassword: false,
        connectionStatus: 'connected',
      });
      return true;
    } catch {
      useAuthStore.getState().logout();
      return false;
    }
  })();
  return hostedInitializationPromise;
}

// 监听全局未授权事件
if (typeof window !== 'undefined') {
  window.addEventListener('hermes-console-locked', () => {
    if (isHosted()) useAuthStore.getState().logout();
  });
  window.addEventListener('unauthorized', () => {
    useAuthStore.getState().logout();
  });

  window.addEventListener('server-version-update', ((e: CustomEvent) => {
    const detail = e.detail || {};
    useAuthStore.getState().updateServerVersion(detail.version || null, detail.buildDate || null);
  }) as EventListener);

  window.addEventListener('server-plugin-support-update', ((e: CustomEvent) => {
    useAuthStore.getState().updateServerPluginSupport(e.detail?.supportsPlugin === true);
  }) as EventListener);
}
