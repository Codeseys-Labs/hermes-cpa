/**
 * Axios API 客户端
 * 替代原项目 src/core/api-client.js
 */

import axios, { AxiosInstance, AxiosResponse } from 'axios';
import type { ApiClientConfig, ApiError } from '@/types';
import {
  BUILD_DATE_HEADER_KEYS,
  CPA_BUILD_DATE_HEADER_KEYS,
  CPA_SUPPORT_PLUGIN_HEADER_KEYS,
  CPA_VERSION_HEADER_KEYS,
  REQUEST_TIMEOUT_MS,
  VERSION_HEADER_KEYS,
} from '@/utils/constants';
import { computeApiUrl } from '@/utils/connection';
import { parseApiErrorResponse } from './apiError';
import { isHosted } from '@/hosted/runtime';
import { HostRequestError, lockHostedSession } from '@/hermesTransport';
import { clearHostedConfigRevision, getHostedConfigRevision, hostedAxiosAdapter } from './hostedAdapter';
import type { ManagementClientConfig } from './hostedAdapter';

class ApiClient {
  private instance: AxiosInstance;
  private apiBase: string = '';
  private managementKey: string = '';
  private connectionRevision = 0;

  constructor() {
    this.instance = axios.create({
      timeout: REQUEST_TIMEOUT_MS,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    this.setupInterceptors();
  }

  /**
   * 设置 API 配置
   */
  setConfig(config: ApiClientConfig): void {
    const hosted = isHosted();
    const apiBase = hosted ? '' : computeApiUrl(config.apiBase);
    const managementKey = hosted ? '' : config.managementKey;
    if (hosted || apiBase !== this.apiBase || managementKey !== this.managementKey) {
      this.connectionRevision += 1;
      clearHostedConfigRevision();
    }
    this.apiBase = apiBase;
    this.managementKey = managementKey;

    if (config.timeout) {
      this.instance.defaults.timeout = config.timeout;
    } else {
      this.instance.defaults.timeout = REQUEST_TIMEOUT_MS;
    }
  }

  /** Guards read/modify/write operations across connection changes, including ABA switches. */
  getConnectionRevision(): number {
    return this.connectionRevision;
  }

  getConfigRevision(): string | undefined {
    return getHostedConfigRevision();
  }

  private readHeader(headers: Record<string, unknown> | undefined, keys: string[]): string | null {
    if (!headers) return null;

    const normalizeValue = (value: unknown): string | null => {
      if (value === undefined || value === null) return null;
      if (Array.isArray(value)) {
        const first = value.find(
          (entry) => entry !== undefined && entry !== null && String(entry).trim()
        );
        return first !== undefined ? String(first) : null;
      }
      const text = String(value);
      return text ? text : null;
    };

    const headerGetter = (headers as { get?: (name: string) => unknown }).get;
    if (typeof headerGetter === 'function') {
      for (const key of keys) {
        const match = normalizeValue(headerGetter.call(headers, key));
        if (match) return match;
      }
    }

    const entries =
      typeof (headers as { entries?: () => Iterable<[string, unknown]> }).entries === 'function'
        ? Array.from((headers as { entries: () => Iterable<[string, unknown]> }).entries())
        : Object.entries(headers);

    const normalized = Object.fromEntries(
      entries.map(([key, value]) => [String(key).toLowerCase(), value])
    );
    for (const key of keys) {
      const match = normalizeValue(normalized[key.toLowerCase()]);
      if (match) return match;
    }
    return null;
  }

  private readBooleanHeader(
    headers: Record<string, unknown> | undefined,
    keys: string[]
  ): boolean | null {
    const value = this.readHeader(headers, keys);
    if (value === null) return null;

    const normalized = value.trim().toLowerCase();
    if (['1', 'true', 'yes', 'on'].includes(normalized)) return true;
    if (['0', 'false', 'no', 'off'].includes(normalized)) return false;
    return null;
  }

  /**
   * 设置请求/响应拦截器
   */
  private setupInterceptors(): void {
    // 请求拦截器
    this.instance.interceptors.request.use(
      (config) => {
        if (isHosted()) {
          // A caller-supplied Axios adapter or URL must never bypass the hosted channel.
          config.adapter = hostedAxiosAdapter;
          config.baseURL = '';
          config.headers.delete('Authorization');
          config.headers.delete('Cookie');
          config.headers.delete('Host');
          return config;
        }
        // 设置 baseURL
        config.baseURL = this.apiBase;

        // 添加认证头
        if (this.managementKey) {
          config.headers.Authorization = `Bearer ${this.managementKey}`;
        }

        return config;
      },
      (error) => Promise.reject(this.handleError(error))
    );

    // 响应拦截器
    this.instance.interceptors.response.use(
      (response) => {
        const headers = response.headers as Record<string, string | undefined>;
        const cpaVersion = this.readHeader(headers, CPA_VERSION_HEADER_KEYS);
        const cpaBuildDate = this.readHeader(headers, CPA_BUILD_DATE_HEADER_KEYS);
        const version = cpaVersion || this.readHeader(headers, VERSION_HEADER_KEYS);
        const buildDate = cpaBuildDate || this.readHeader(headers, BUILD_DATE_HEADER_KEYS);
        const supportsPlugin = this.readBooleanHeader(headers, CPA_SUPPORT_PLUGIN_HEADER_KEYS);

        // 触发版本更新事件（后续通过 store 处理）
        if (version || buildDate) {
          window.dispatchEvent(
            new CustomEvent('server-version-update', {
              detail: { version: version || null, buildDate: buildDate || null },
            })
          );
        }
        if (supportsPlugin !== null) {
          window.dispatchEvent(
            new CustomEvent('server-plugin-support-update', {
              detail: { supportsPlugin },
            })
          );
        }

        return response;
      },
      (error) => Promise.reject(this.handleError(error))
    );
  }

  /**
   * 错误处理
   */
  private handleError(error: unknown): ApiError {
    if (error instanceof HostRequestError) return error;
    if (axios.isAxiosError(error)) {
      if (isHosted() && error.code === 'ERR_CANCELED' &&
          (error.config?.method?.toUpperCase() !== 'GET' ||
           error.config?.url?.split('?')[0].endsWith('/oauth/auth-url'))) {
        // Aborting the UI promise is not proof that the owner rejected an admitted effect.
        void lockHostedSession();
        return new HostRequestError('outcome-unknown',
          'Outcome unknown; reconcile in the host without resubmitting');
      }
      const responseData: unknown = error.response?.data;
      const parsedError = parseApiErrorResponse(responseData, error.message);
      const apiError = new Error(parsedError.message) as ApiError;
      apiError.name = 'ApiError';
      apiError.status = error.response?.status;
      apiError.code = error.code;
      apiError.apiCode = parsedError.apiCode;
      apiError.details = responseData;
      apiError.data = responseData;

      // 401 未授权 - 触发登出事件
      if (error.response?.status === 401) {
        window.dispatchEvent(new Event('unauthorized'));
      }

      return apiError;
    }

    const fallbackMessage =
      error instanceof Error
        ? error.message
        : typeof error === 'string'
          ? error
          : 'Unknown error occurred';
    const fallback = new Error(fallbackMessage) as ApiError;
    fallback.name = 'ApiError';
    return fallback;
  }

  /**
   * GET 请求
   */
  async get<T = unknown>(url: string, config?: ManagementClientConfig): Promise<T> {
    const response = await this.instance.get<T>(url, config);
    return response.data;
  }

  /**
   * POST 请求
   */
  async post<T = unknown>(url: string, data?: unknown, config?: ManagementClientConfig): Promise<T> {
    const response = await this.instance.post<T>(url, data, config);
    return response.data;
  }

  /**
   * PUT 请求
   */
  async put<T = unknown>(url: string, data?: unknown, config?: ManagementClientConfig): Promise<T> {
    const requestConfig: ManagementClientConfig = {
      ...config, hermesExpectedRevision: config?.hermesExpectedRevision ?? getHostedConfigRevision(),
    };
    const response = await this.instance.put<T>(url, data, requestConfig);
    return response.data;
  }

  /**
   * PATCH 请求
   */
  async patch<T = unknown>(url: string, data?: unknown, config?: ManagementClientConfig): Promise<T> {
    const requestConfig: ManagementClientConfig = {
      ...config, hermesExpectedRevision: config?.hermesExpectedRevision ?? getHostedConfigRevision(),
    };
    const response = await this.instance.patch<T>(url, data, requestConfig);
    return response.data;
  }

  /**
   * DELETE 请求
   */
  async delete<T = unknown>(url: string, config?: ManagementClientConfig): Promise<T> {
    const requestConfig: ManagementClientConfig = {
      ...config, hermesExpectedRevision: config?.hermesExpectedRevision ?? getHostedConfigRevision(),
    };
    const response = await this.instance.delete<T>(url, requestConfig);
    return response.data;
  }

  /**
   * 获取原始响应（用于下载等场景）
   */
  async getRaw(url: string, config?: ManagementClientConfig): Promise<AxiosResponse> {
    return this.instance.get(url, config);
  }

  /**
   * 发送 FormData
   */
  async postForm<T = unknown>(
    url: string,
    formData: FormData,
    config?: ManagementClientConfig
  ): Promise<T> {
    const response = await this.instance.post<T>(url, formData, {
      ...config,
      headers: {
        ...(config?.headers || {}),
        'Content-Type': 'multipart/form-data',
      },
    });
    return response.data;
  }
}

// 导出单例
export const apiClient = new ApiClient();
