import { AxiosError, AxiosHeaders } from 'axios';
import type { AxiosAdapter, AxiosRequestConfig, AxiosResponse, InternalAxiosRequestConfig } from 'axios';
import { managementRequest, managementTransaction } from '@/hermesTransport';
import { isRecord } from '@/utils/helpers';
import {
  decodeBase64, encodeBase64, isJsonValue, MAX_FILE_BYTES,
} from '@/hosted/management';
import type { ManagementBody, ManagementRequest, ManagementResponse } from '@/hosted/management';
import { clearConfigSnapshotRevisions, rememberConfigRevision } from './configRevision';

export interface ManagementClientConfig extends AxiosRequestConfig {
  /** Snapshot revision for a dependent config write, never an authorization header. */
  hermesExpectedRevision?: string;
}
let configRevision: string | undefined;
export function getHostedConfigRevision(): string | undefined { return configRevision; }
export function clearHostedConfigRevision(): void {
  configRevision = undefined;
  clearConfigSnapshotRevisions();
}

function nativePath(url: string): { path: string; query: Record<string, string> } {
  if (!url.startsWith('/') || url.startsWith('//') || /[\\#]/.test(url)) {
    throw new Error('Only CPA-relative management paths are allowed');
  }
  for (const char of url) {
    if (char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127) {
      throw new Error('Invalid management path');
    }
  }
  const [path, ...search] = url.split('?');
  const decoded = decodeURIComponent(path);
  if (decoded.includes('\\') || decoded.split('/').some((part) => part === '.' || part === '..')) {
    throw new Error('Invalid management path');
  }
  const native = /^\/v(?:0|1|8)\//.test(path) ? path : `/v8/management${path}`;
  const query: Record<string, string> = Object.create(null);
  for (const [key, value] of new URLSearchParams(search.join('?'))) {
    if (key in query) throw new Error('Duplicate management query parameter');
    query[key] = value;
  }
  return { path: native, query };
}

async function requestBody(data: unknown, contentType: string): Promise<ManagementBody> {
  if (data === undefined) return { kind: 'empty' };
  if (data instanceof FormData) {
    const fields: Record<string, string> = Object.create(null);
    const files: Extract<ManagementBody, { kind: 'multipart' }>['files'] = [];
    for (const [name, value] of data.entries()) {
      if (typeof value === 'string') {
        if (name in fields) throw new Error('Duplicate multipart text field');
        fields[name] = value;
      } else {
        if (value.size > MAX_FILE_BYTES) throw new Error('File exceeds the 10 MiB upload limit');
        if (files.length) throw new Error('Upload each file in its own bounded request');
        files.push({
          name, filename: value.name, content_type: value.type || 'application/octet-stream',
          base64: encodeBase64(new Uint8Array(await value.arrayBuffer())),
        });
      }
    }
    return { kind: 'multipart', fields, files };
  }
  if (data instanceof Blob) {
    if (data.size > MAX_FILE_BYTES) throw new Error('File exceeds the 10 MiB upload limit');
    return {
      kind: 'bytes', content_type: data.type || contentType,
      base64: encodeBase64(new Uint8Array(await data.arrayBuffer())),
    };
  }
  if (data instanceof ArrayBuffer || ArrayBuffer.isView(data)) {
    const bytes = data instanceof ArrayBuffer ? new Uint8Array(data) :
      new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    if (bytes.byteLength > MAX_FILE_BYTES) throw new Error('File exceeds the 10 MiB upload limit');
    return { kind: 'bytes', content_type: contentType, base64: encodeBase64(bytes) };
  }
  if (typeof data === 'string' && !/\bjson\b/i.test(contentType)) {
    return { kind: 'text', value: data, content_type: contentType };
  }
  const value: unknown = typeof data === 'string' ? JSON.parse(data) : data;
  if (!isJsonValue(value)) throw new Error('Management body is not JSON-representable');
  return { kind: 'json', value };
}

export async function toManagementRequest(
  config: ManagementClientConfig,
  expectedRevision = config.hermesExpectedRevision ?? configRevision
): Promise<ManagementRequest> {
  const { path, query } = nativePath(config.url ?? '');
  const method = (config.method ?? 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'POST' && method !== 'PUT' && method !== 'PATCH' && method !== 'DELETE') {
    throw new Error('Unsupported management method');
  }
  const params: unknown = config.params;
  const entries = params instanceof URLSearchParams ? params.entries() :
    isRecord(params) ? Object.entries(params) : [];
  if (params !== undefined && !(params instanceof URLSearchParams) && !isRecord(params)) {
    throw new Error('Invalid management query');
  }
  for (const [key, value] of entries) {
    if (value === null || value === undefined) continue;
    if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') {
      throw new Error('Management query values must be scalar');
    }
    query[key] = String(value);
  }
  const configWrite = method !== 'GET' && /^\/v8\/management\/config(?:\/|\.yaml$|$)/.test(path);
  if (configWrite && !expectedRevision) {
    throw new Error('Read the configuration before saving; its revision is required');
  }
  const responseType = config.responseType;
  if (responseType && !['json', 'text', 'blob', 'arraybuffer'].includes(responseType)) {
    throw new Error('Unsupported hosted response type');
  }
  const contentType = config.headers instanceof AxiosHeaders
    ? config.headers.getContentType()
    : Object.entries(config.headers ?? {}).find(([name]) => name.toLowerCase() === 'content-type')?.[1];
  return {
    method, path, ...(Object.keys(query).length ? { query } : {}),
    body: await requestBody(config.data, String(contentType || 'application/json')),
    response_kind: responseType === 'blob' || responseType === 'arraybuffer' ? 'bytes' :
      responseType === 'text' ? 'text' : 'json',
    ...(configWrite ? { expected_revision: expectedRevision } : {}),
  };
}

function axiosResponse(result: ManagementResponse, config: InternalAxiosRequestConfig): AxiosResponse {
  const headers = new AxiosHeaders(result.headers);
  const revision = result.revision ?? headers.get('x-hermes-config-revision');
  if (typeof revision === 'string' && /^[a-f0-9]{64}$/i.test(revision)) {
    configRevision = revision;
    headers.set('x-hermes-config-revision', revision);
  }
  let data: unknown;
  switch (result.body.kind) {
    case 'json': data = result.body.value; break;
    case 'text': data = result.body.value; break;
    case 'bytes': {
      const bytes = decodeBase64(result.body.base64);
      headers.set('content-type', result.body.content_type);
      if (result.body.filename && !headers.has('content-disposition')) {
        headers.set('content-disposition',
          `attachment; filename*=UTF-8''${encodeURIComponent(result.body.filename)}`);
      }
      data = config.responseType === 'arraybuffer' ? bytes.buffer :
        config.responseType === 'text' ? new TextDecoder().decode(bytes) :
          new Blob([bytes], { type: result.body.content_type });
      break;
    }
  }
  rememberConfigRevision(data, revision);
  // Preserve the caller's requested binary representation even for native JSON errors.
  if (config.responseType === 'blob' && !(data instanceof Blob)) {
    data = new Blob([typeof data === 'string' ? data : JSON.stringify(data)], {
      type: String(headers.getContentType() || 'application/json'),
    });
  } else if (config.responseType === 'arraybuffer' && !(data instanceof ArrayBuffer)) {
    data = new TextEncoder().encode(typeof data === 'string' ? data : JSON.stringify(data)).buffer;
  }
  const response: AxiosResponse = { data, status: result.status, statusText: '', headers, config };
  const valid = config.validateStatus;
  if (valid !== null && (valid ? !valid(result.status) : result.status < 200 || result.status >= 300)) {
    throw new AxiosError(`Request failed with status code ${result.status}`,
      result.status >= 500 ? AxiosError.ERR_BAD_RESPONSE : AxiosError.ERR_BAD_REQUEST,
      config, undefined, response);
  }
  return response;
}

/** Axios keeps its public API and normalizers, but this adapter never opens a network connection. */
export const hostedAxiosAdapter: AxiosAdapter = async (config) => {
  const request = await toManagementRequest(config);
  if (config.signal?.aborted) throw new AxiosError('Request canceled', AxiosError.ERR_CANCELED, config);
  const result = await managementRequest(request);
  return axiosResponse(result, config);
};

export async function submitConfigTransaction(requests: ManagementRequest[], expectedRevision: string): Promise<void> {
  if (!requests.length) return;
  const result = await managementTransaction(requests, expectedRevision);
  axiosResponse(result, { headers: new AxiosHeaders() });
}
