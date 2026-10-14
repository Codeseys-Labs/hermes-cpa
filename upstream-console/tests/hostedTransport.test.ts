import { describe, expect, test } from 'bun:test';
import { HostedTransport, HostRequestError } from '../src/hermesTransport';

const sessionId = 'fixture-session-00000001';

function attach(transport: HostedTransport, onRequest: (message: Record<string, unknown>, port: MessagePort) => void) {
  const channel = new MessageChannel();
  channel.port2.onmessage = ({ data }) => {
    if (data.type === 'hermes:cpa:hello') {
      channel.port2.postMessage({
        type: 'hermes:cpa:ready', protocol: 3, session_id: sessionId,
        client_nonce: transport.clientNonce, unlocked: true,
      });
    } else if (data.type === 'hermes:cpa:request') onRequest(data, channel.port2);
  };
  channel.port2.start();
  expect(transport.acceptInitialization({
    type: 'hermes:cpa:init', protocol: 3, session_id: sessionId,
    client_nonce: transport.clientNonce, unlocked: true,
  }, channel.port1)).toBe(true);
  return channel.port2;
}

describe('privileged console MessageChannel v3', () => {
  test('requires a nonce-bound unlocked READY on the transferred port', async () => {
    let closes = 0;
    const transport = new HostedTransport(() => { closes += 1; });
    const invalid = new MessageChannel();
    expect(transport.acceptInitialization({
      type: 'hermes:cpa:init', protocol: 3, session_id: sessionId,
      client_nonce: 'another-document', unlocked: true,
    }, invalid.port1)).toBe(false);
    invalid.port2.close();
    expect(transport.unlocked).toBe(false);
    const channel = new MessageChannel();
    transport.acceptInitialization({
      type: 'hermes:cpa:init', protocol: 3, session_id: sessionId,
      client_nonce: transport.clientNonce, unlocked: true,
    }, channel.port1);
    expect(transport.unlocked).toBe(false);
    channel.port2.postMessage({
      type: 'hermes:cpa:ready', protocol: 3, session_id: sessionId,
      client_nonce: transport.clientNonce, unlocked: false,
    });
    await expect(transport.ready).rejects.toBeInstanceOf(HostRequestError);
    expect(transport.unlocked).toBe(false);
    expect(closes).toBe(1);
    channel.port2.close();
  });

  test('ignores stale document responses and preserves native response data', async () => {
    const transport = new HostedTransport(() => {});
    const seen: Record<string, unknown>[] = [];
    const peer = attach(transport, (message, port) => {
      seen.push(message);
      port.postMessage({
        type: 'hermes:cpa:response', protocol: 3, session_id: 'stale-session',
        id: message.id, ok: true, result: { secret: 'wrong-document' },
      });
      port.postMessage({
        type: 'hermes:cpa:response', protocol: 3, session_id: sessionId,
        id: message.id, ok: true,
        result: { status: 207, headers: { 'content-type': 'application/json' },
          body: { kind: 'json', value: { files: ['fixture.json'], failed: [] } } },
      });
    });
    try {
      await transport.ready;
      const response = await transport.request('management.request', {
        method: 'GET', path: '/v8/management/credentials', body: { kind: 'empty' },
        response_kind: 'json',
      });
      expect(response).toEqual({
        status: 207, headers: { 'content-type': 'application/json' },
        body: { kind: 'json', value: { files: ['fixture.json'], failed: [] } },
      });
      expect(seen).toHaveLength(1);
      expect(seen[0]).not.toHaveProperty('lease_id');
      expect(seen[0]).not.toHaveProperty('document_id');
    } finally { transport.close(); peer.close(); }
  });

  test('never replays an uncertain mutation after timeout', async () => {
    // Exercise real MessagePort delivery against the transport's platform deadline.
    // Mock timers alone cannot establish that the peer received the admitted request.
    let received = 0;
    const transport = new HostedTransport(() => {}, 30);
    const peer = attach(transport, () => { received += 1; });
    try {
      await transport.ready;
      await expect(transport.request('management.request', {
        method: 'POST', path: '/v8/management/credentials/refresh',
        body: { kind: 'json', value: { name: 'fixture.json' } }, response_kind: 'json',
      })).rejects.toMatchObject({ kind: 'outcome-unknown' });
      expect(received).toBe(1);
      await expect(transport.request('management.request', {
        method: 'POST', path: '/v8/management/credentials/refresh',
        body: { kind: 'json', value: { name: 'fixture.json' } }, response_kind: 'json',
      })).rejects.toMatchObject({ kind: 'unavailable' });
      expect(received).toBe(1);
    } finally { transport.close(); peer.close(); }
  });

  test('host revocation rejects pending writes as unknown and closes only this console', async () => {
    let closes = 0;
    const transport = new HostedTransport(() => { closes += 1; });
    const peer = attach(transport, (message, port) => {
      expect(message.operation).toBe('management.transaction');
      port.postMessage({ type: 'hermes:cpa:locked', protocol: 3, session_id: sessionId });
    });
    try {
      await transport.ready;
      const failure = await transport.request('management.transaction', {
        requests: [], expected_revision: 'a'.repeat(64),
      }).catch((error: unknown) => error);
      expect(failure).toMatchObject({ kind: 'outcome-unknown' });
      expect(closes).toBe(1);
      expect(transport.unlocked).toBe(false);
    } finally { transport.close(); peer.close(); }
  });

  test('a host busy refusal is a definite not-forwarded failure, distinct from an unknown outcome, and keeps the console open', async () => {
    let closes = 0;
    const transport = new HostedTransport(() => { closes += 1; });
    const peer = attach(transport, (message, port) => {
      port.postMessage({
        type: 'hermes:cpa:response', protocol: 3, session_id: sessionId, id: message.id, ok: false,
        error: { code: message.id === '1' ? 'HOST_BUSY' : 'OUTCOME_UNKNOWN' },
      });
    });
    const write = {
      method: 'POST', path: '/v8/management/credentials/refresh',
      body: { kind: 'json', value: { name: 'fixture.json' } }, response_kind: 'json',
    } as const;
    try {
      await transport.ready;
      const busy = await transport.request('management.request', write).catch((error: unknown) => error);
      expect(busy).toMatchObject({ kind: 'unavailable' });
      expect((busy as Error).message).toContain('did not forward');
      expect((busy as Error).message).not.toContain('Outcome unknown');
      expect(transport.unlocked).toBe(true);
      expect(closes).toBe(0);
      const unknown = await transport.request('management.request', write).catch((error: unknown) => error);
      expect(unknown).toMatchObject({ kind: 'outcome-unknown' });
      expect(transport.unlocked).toBe(false);
      expect(closes).toBe(1);
    } finally { transport.close(); peer.close(); }
  });
});
