import { expect, test } from 'bun:test';
import { apiClient } from '../src/services/api/client';
import { applyConfigPatch } from '../src/services/api/configPatch';
import { configFileApi } from '../src/services/api/configFile';
import { configRevisionArgs } from '../src/services/api/configRevision';
import { authFilesApi } from '../src/services/api/authFiles';
import { decodeBase64, encodeBase64, MAX_FILE_BYTES } from '../src/hosted/management';
import { toManagementRequest } from '../src/services/api/hostedAdapter';
import { getUiStorage } from '../src/hosted/storage';
import { copyText, openExternal, openPlugin, saveFile } from '../src/hosted/browser';
import type { ManagementRequest, ManagementResponse } from '../src/hosted/management';
import type * as AuthModule from '../src/stores/useAuthStore';

const revisionA = 'a'.repeat(64);
const revisionB = 'b'.repeat(64);

// A real MessageChannel is the RPC boundary. Only the unavailable CPA process and parent UI are fixtures.
test('hosted auth, native API, snapshot CAS, uploads and browser capabilities share the unlocked port', async () => {
  const names = ['window', 'document', 'localStorage', 'sessionStorage'] as const;
  const descriptors = new Map(names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const frame = new EventTarget();
  const channel = new MessageChannel();
  const sessionId = 'hosted-fixture-session-0001';
  const requests: { operation: string; payload: unknown }[] = [];
  let nonce = '';
  let storageReads = 0;
  let readySent = false;
  let unlock: () => void = () => { throw new Error('Handshake not started'); };
  let initialRead = true;
  let auth: typeof AuthModule | undefined;
  const locked = Promise.withResolvers<void>();
  const events: string[] = [];
  frame.addEventListener('server-version-update', () => events.push('version'));
  frame.addEventListener('server-plugin-support-update', () => events.push('plugin'));

  const parent = {
    postMessage(message: { client_nonce: string; type: string }) {
      expect(message.type).toBe('hermes:cpa:connect');
      nonce = message.client_nonce;
      const init = new Event('message');
      Object.defineProperties(init, {
        source: { value: parent }, ports: { value: [channel.port1] },
        data: { value: {
          type: 'hermes:cpa:init', protocol: 3, client_nonce: nonce,
          session_id: sessionId, unlocked: true,
        } },
      });
      queueMicrotask(() => frame.dispatchEvent(init));
    },
  };
  Object.defineProperties(frame, {
    parent: { value: parent }, location: { value: { host: 'fixture.invalid' } },
  });
  Object.defineProperty(globalThis, 'window', { configurable: true, value: frame });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: {
    querySelector: (selector: string) => selector === 'meta[name="hermes-cpa-hosted"]'
      ? { getAttribute: () => '3' } : null,
  } });
  for (const name of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(globalThis, name, { configurable: true, get() {
      storageReads += 1;
      throw new DOMException('Opaque document storage', 'SecurityError');
    } });
  }

  function native(request: ManagementRequest): ManagementResponse {
    if (request.path === '/v8/management/config.yaml') {
      if (request.method === 'GET') {
        return { status: 200, headers: { 'content-type': 'application/yaml' },
          revision: revisionA, body: { kind: 'text', value: 'server:\n  port: 8317\n' } };
      }
      expect(request.expected_revision).toBe(revisionA);
      expect(request.body).toEqual({
        kind: 'text', value: 'server:\n  port: 8318\n', content_type: 'application/yaml',
      });
    }
    if (request.path === '/v8/management/config' && request.method === 'GET') {
      const revision = initialRead ? revisionA : revisionB;
      initialRead = false;
      return { status: 200, revision,
        headers: { 'x-cpa-version': '8.0.21', 'x-cpa-support-plugin': 'true' },
        body: { kind: 'json', value: { access: { 'api-keys': ['generated-fixture-value'] } } } };
    }
    if (request.path === '/v0/resource/plugins/fixture/logo') {
      return { status: 200, headers: { 'content-type': 'image/png' },
        body: { kind: 'bytes', content_type: 'image/png', base64: encodeBase64(new Uint8Array([0, 255, 42])) } };
    }
    if (request.path === '/v8/management/credentials/download') {
      expect(request.query).toEqual({ name: 'fixture.json' });
      return { status: 200, headers: { 'content-disposition': 'attachment; filename="fixture.json"' },
        body: { kind: 'bytes', content_type: 'application/json',
          base64: encodeBase64(new TextEncoder().encode('{"fixture":true}')), filename: 'fixture.json' } };
    }
    if (request.path === '/v8/management/credentials' && request.method === 'POST') {
      expect(request.body.kind).toBe('multipart');
      if (request.body.kind !== 'multipart') throw new Error('Expected actual multipart bytes');
      expect(request.body.files).toHaveLength(1);
      const file = request.body.files[0];
      if (file.filename === 'limit.json') {
        const bytes = decodeBase64(file.base64);
        expect(bytes.byteLength).toBe(MAX_FILE_BYTES);
        expect(bytes[0]).toBe(65);
        expect(bytes[MAX_FILE_BYTES - 1]).toBe(65);
      }
      return file.filename === 'rejected.json'
        ? { status: 207, headers: {}, body: { kind: 'json', value: {
            status: 'partial', uploaded: 0, files: [], failed: [{ name: file.filename, error: 'native validation' }],
          } } }
        : { status: 200, headers: {}, body: { kind: 'json', value: { status: 'ok' } } };
    }
    if (request.path === '/v8/management/credentials/status') {
      return { status: 409, headers: { 'content-type': 'application/json' },
        body: { kind: 'json', value: { error: { code: 'conflict', message: 'Fixture conflict' } } } };
    }
    if (request.path === '/v8/management/credentials/fields') {
      expect(request.body).toEqual({ kind: 'json', value: { name: 'fixture.json', disabled: false } });
    }
    if (request.path === '/v8/management/credentials' && request.method === 'DELETE') {
      expect(request.body).toEqual({ kind: 'json', value: { names: ['fixture.json'] } });
    }
    if (request.path === '/v8/management/config/access/api-keys') {
      expect(request.expected_revision).toBe(revisionA);
    }
    return { status: 200, headers: {}, body: { kind: 'json', value: { status: 'ok' } } };
  }

  channel.port2.onmessage = ({ data }) => {
    if (data.type === 'hermes:cpa:hello') {
      unlock = () => {
        readySent = true;
        channel.port2.postMessage({ type: 'hermes:cpa:ready', protocol: 3,
          session_id: sessionId, client_nonce: nonce, unlocked: true });
      };
      return;
    }
    if (data.type === 'hermes:cpa:ack') return;
    expect(readySent).toBe(true);
    expect(data.type).toBe('hermes:cpa:request');
    expect(data.protocol).toBe(3);
    expect(data.session_id).toBe(sessionId);
    expect(data).not.toHaveProperty('lease_id');
    expect(data).not.toHaveProperty('document_id');
    requests.push({ operation: data.operation, payload: data.payload });
    if (data.operation === 'session.lock') locked.resolve();
    const result = data.operation === 'management.request' ? native(data.payload) :
      data.operation === 'management.transaction'
        ? { status: 200, headers: {}, revision: revisionB, body: { kind: 'json', value: { status: 'ok' } } }
        : null;
    channel.port2.postMessage({ type: 'hermes:cpa:response', protocol: 3,
      session_id: sessionId, id: data.id, ok: true, result });
  };
  channel.port2.start();

  try {
    // Import after installing throwing native-storage getters to exercise module-load hydration.
    auth = await import('../src/stores/useAuthStore');
    auth.useAuthStore.setState({ isAuthenticated: false });
    const initializing = auth.initializeHostedSession();
    // Wait for the real port's HELLO, not for an invented timeout-based auth state.
    await new Promise<void>((resolve) => {
      channel.port2.addEventListener('message', (event) => {
        if (event.data.type === 'hermes:cpa:hello') resolve();
      }, { once: true });
    });
    expect(auth.useAuthStore.getState().isAuthenticated).toBe(false);
    expect(requests).toHaveLength(0);
    unlock();
    expect(await initializing).toBe(true);
    expect(auth.useAuthStore.getState()).toMatchObject({
      isAuthenticated: true, apiBase: '', managementKey: '', rememberPassword: false,
    });
    expect(storageReads).toBe(0);
    expect(events).toContain('version');
    expect(events).toContain('plugin');

    const yaml = await configFileApi.fetchConfigSnapshot();
    expect(yaml.revision).toBe(revisionA);
    initialRead = true;
    const snapshot = await apiClient.get('/config');
    await apiClient.get('/config'); // A background read advances the global revision to B.
    expect(apiClient.getConfigRevision()).toBe(revisionB);
    await apiClient.put('/config/access/api-keys', ['changed-fixture'], ...configRevisionArgs(snapshot));
    await configFileApi.saveConfigYaml('server:\n  port: 8318\n', yaml.revision);
    const before = requests.length;
    await applyConfigPatch({ patch: { server: { port: 8318 } }, emptyMaps: [['plugins', 'configs']],
      deletions: [['routing', 'old']] }, apiClient.getConnectionRevision(), yaml.revision);
    expect(requests.slice(before)).toEqual([{ operation: 'management.transaction', payload: {
      expected_revision: revisionA,
      requests: [
        { method: 'PATCH', path: '/v8/management/config', body: { kind: 'json', value: { server: { port: 8318 } } }, response_kind: 'json', expected_revision: revisionA },
        { method: 'PUT', path: '/v8/management/config/plugins/configs', body: { kind: 'json', value: {} }, response_kind: 'json', expected_revision: revisionA },
        { method: 'DELETE', path: '/v8/management/config/routing/old', body: { kind: 'empty' }, response_kind: 'json', expected_revision: revisionA },
      ],
    } }]);

    await apiClient.patch('/credentials/fields', { name: 'fixture.json', disabled: false });
    await apiClient.delete('/credentials', { data: { names: ['fixture.json'] } });
    const conflict = await apiClient.patch('/credentials/status', {}).catch((error: unknown) => error);
    expect(conflict).toMatchObject({ status: 409 });
    await expect(apiClient.get('https://unapproved.invalid/')).rejects.toThrow('CPA-relative');
    const download = await apiClient.getRaw('/credentials/download?name=fixture.json', { responseType: 'blob' });
    expect(download.status).toBe(200);
    expect(download.headers['content-disposition']).toContain('fixture.json');
    expect(await download.data.text()).toBe('{"fixture":true}');
    const bytes = await apiClient.getRaw('/v0/resource/plugins/fixture/logo', { responseType: 'arraybuffer' });
    expect(Array.from(new Uint8Array(bytes.data))).toEqual([0, 255, 42]);

    const result = await authFilesApi.uploadFiles([
      new File([new Uint8Array(MAX_FILE_BYTES).fill(65)], 'limit.json', { type: 'application/json' }),
      new File(['{}'], 'rejected.json', { type: 'application/json' }),
      new File(['{}'], 'accepted.json', { type: 'application/json' }),
    ]);
    expect(result).toEqual({ status: 'partial', uploaded: 2, files: ['limit.json', 'accepted.json'],
      failed: [{ name: 'rejected.json', error: 'native validation' }] });
    await expect(toManagementRequest({ method: 'POST', url: '/credentials',
      data: new Blob([new Uint8Array(MAX_FILE_BYTES + 1)]) })).rejects.toThrow('10 MiB');
    const binary = await toManagementRequest({ method: 'POST', url: '/credentials',
      data: new Uint8Array([1, 2, 255]), headers: { 'Content-Type': 'application/octet-stream' } });
    expect(binary.body).toEqual({ kind: 'bytes', base64: 'AQL/', content_type: 'application/octet-stream' });

    await openExternal('https://example.invalid/fixture');
    await copyText('generated-fixture-value');
    await saveFile(new Blob(['fixture']), 'fixture.txt');
    await openPlugin('fixture-plugin', 0);
    expect(requests.slice(-4).map((entry) => entry.operation)).toEqual([
      'browser.open', 'browser.copy', 'file.save', 'plugin.open',
    ]);
    getUiStorage('local').setItem('ephemeral-provider', 'generated-fixture-value');
    getUiStorage('session').setItem('ephemeral-quota', 'fixture');
    auth.useAuthStore.getState().logout();
    await locked.promise;
    expect(auth.useAuthStore.getState().isAuthenticated).toBe(false);
    expect(getUiStorage('local').getItem('ephemeral-provider')).toBeNull();
    expect(getUiStorage('session').getItem('ephemeral-quota')).toBeNull();
    expect(storageReads).toBe(0);
    expect(requests.filter((entry) => entry.operation === 'session.lock')).toHaveLength(1);
    await expect(apiClient.post('/credentials/refresh', {})).rejects.toMatchObject({ kind: 'unavailable' });
  } finally {
    if (auth?.useAuthStore.getState().isAuthenticated) auth.useAuthStore.getState().logout();
    await Promise.resolve();
    channel.port1.close();
    channel.port2.close();
    for (const name of names) {
      const descriptor = descriptors.get(name);
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else Reflect.deleteProperty(globalThis, name);
    }
    apiClient.setConfig({ apiBase: '', managementKey: '' });
  }
}, 30_000);
