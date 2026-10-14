/**
 * 配置文件相关 API（/config.yaml）
 */

import { apiClient } from './client';
import { isHosted } from '@/hosted/runtime';

export interface ConfigFileSnapshot {
  content: string;
  revision?: string;
}

export const configFileApi = {
  async fetchConfigYaml(): Promise<string> {
    return (await configFileApi.fetchConfigSnapshot()).content;
  },

  async fetchConfigSnapshot(): Promise<ConfigFileSnapshot> {
    const response = await apiClient.getRaw('/config.yaml', {
      responseType: 'text',
      headers: { Accept: 'application/yaml, text/yaml, text/plain' },
    });
    const data: unknown = response.data;
    const content = typeof data === 'string' ? data : data == null ? '' : String(data);
    const revision: unknown = response.headers?.['x-hermes-config-revision'];
    if (isHosted() && (typeof revision !== 'string' || !/^[a-f0-9]{64}$/i.test(revision))) {
      throw new Error('Configuration response has no valid snapshot revision');
    }
    return { content, ...(typeof revision === 'string' ? { revision } : {}) };
  },

  async saveConfigYaml(content: string, expectedRevision?: string): Promise<void> {
    if (isHosted() && !expectedRevision) {
      throw new Error('Configuration snapshot revision is required before saving');
    }
    await apiClient.put('/config.yaml', content, {
      ...(expectedRevision ? { hermesExpectedRevision: expectedRevision } : {}),
      headers: {
        'Content-Type': 'application/yaml',
        Accept: 'application/json, text/plain, */*',
      },
    });
  },
};
