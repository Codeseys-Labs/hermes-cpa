import { isRecord } from '@/utils/helpers';

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type ManagementBody =
  | { kind: 'empty' }
  | { kind: 'json'; value: JsonValue }
  | { kind: 'text'; value: string; content_type: string }
  | { kind: 'bytes'; base64: string; content_type: string }
  | {
      kind: 'multipart';
      fields: Record<string, string>;
      files: { name: string; filename: string; content_type: string; base64: string }[];
    };

export interface ManagementRequest {
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  path: string;
  query?: Record<string, string>;
  body: ManagementBody;
  response_kind: 'json' | 'text' | 'bytes';
  expected_revision?: string;
}

export interface ManagementResponse {
  status: number;
  headers: Record<string, string>;
  body:
    | { kind: 'json'; value: JsonValue }
    | { kind: 'text'; value: string }
    | { kind: 'bytes'; base64: string; content_type: string; filename?: string };
  revision?: string;
}

export const MAX_FILE_BYTES = 10 * 1024 * 1024;
export const MAX_RESPONSE_BYTES = 32 * 1024 * 1024;
export const MAX_WIRE_BYTES = 48 * 1024 * 1024;

export function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true;
  if (typeof value === 'number') return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  if (!isRecord(value)) return false;
  const prototype: unknown = Object.getPrototypeOf(value);
  return (prototype === null || prototype === Object.prototype) &&
    Object.values(value).every(isJsonValue);
}

/** Each chunk is a multiple of three bytes, so it encodes without padding and the pieces concatenate exactly;
 *  a 32 MiB export never materializes a second 32 MiB binary string. */
const BASE64_CHUNK_BYTES = 0x6000;

export function encodeBase64(bytes: Uint8Array): string {
  const chunks: string[] = [];
  for (let offset = 0; offset < bytes.length; offset += BASE64_CHUNK_BYTES) {
    chunks.push(btoa(String.fromCharCode(...bytes.subarray(offset, offset + BASE64_CHUNK_BYTES))));
  }
  return chunks.join('');
}

export function decodeBase64(value: string, limit = MAX_RESPONSE_BYTES): Uint8Array<ArrayBuffer> {
  const padding = value.endsWith('==') ? 2 : value.endsWith('=') ? 1 : 0;
  const firstPadding = value.indexOf('=');
  if (value.length > Math.ceil(limit / 3) * 4 || value.length % 4 !== 0 ||
      /[^A-Za-z0-9+/=]/.test(value) ||
      (firstPadding !== -1 && firstPadding !== value.length - padding)) {
    throw new Error('Invalid or oversized binary response');
  }
  const binary = atob(value);
  if (binary.length > limit) throw new Error('Oversized binary response');
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

/** Validate the envelope here; individual services normalize the native JSON body. */
export function parseManagementResponse(value: unknown): ManagementResponse {
  if (!isRecord(value) || !Number.isInteger(value.status) ||
      typeof value.status !== 'number' || value.status < 100 || value.status > 599 ||
      !isRecord(value.headers) || !isRecord(value.body)) {
    throw new Error('Invalid management response');
  }
  const headers: Record<string, string> = {};
  for (const [key, entry] of Object.entries(value.headers)) {
    if (typeof entry !== 'string') throw new Error('Invalid management response header');
    headers[key.toLowerCase()] = entry;
  }
  const body = value.body;
  let parsedBody: ManagementResponse['body'];
  switch (body.kind) {
    case 'json':
      if (!isJsonValue(body.value)) throw new Error('Invalid JSON response');
      if (new TextEncoder().encode(JSON.stringify(body.value)).length > MAX_RESPONSE_BYTES) {
        throw new Error('Oversized JSON response');
      }
      parsedBody = { kind: 'json', value: body.value };
      break;
    case 'text':
      if (typeof body.value !== 'string' ||
          new TextEncoder().encode(body.value).length > MAX_RESPONSE_BYTES) {
        throw new Error('Invalid or oversized text response');
      }
      parsedBody = { kind: 'text', value: body.value };
      break;
    case 'bytes':
      if (typeof body.base64 !== 'string' || typeof body.content_type !== 'string' ||
          (body.filename !== undefined && typeof body.filename !== 'string')) {
        throw new Error('Invalid binary response');
      }
      if (body.base64.length > Math.ceil(MAX_RESPONSE_BYTES / 3) * 4) {
        throw new Error('Oversized binary response');
      }
      parsedBody = {
        kind: 'bytes', base64: body.base64, content_type: body.content_type,
        ...(typeof body.filename === 'string' ? { filename: body.filename } : {}),
      };
      break;
    default:
      throw new Error('Invalid management response body kind');
  }
  if (value.revision !== undefined &&
      (typeof value.revision !== 'string' || !/^[a-f0-9]{64}$/i.test(value.revision))) {
    throw new Error('Invalid configuration revision');
  }
  return {
    status: value.status, headers, body: parsedBody,
    ...(typeof value.revision === 'string' ? { revision: value.revision } : {}),
  };
}
