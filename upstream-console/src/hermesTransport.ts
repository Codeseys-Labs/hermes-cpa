import { isRecord } from '@/utils/helpers';
import { isHosted } from '@/hosted/runtime';
import { clearHostedStorage } from '@/hosted/storage';
import { MAX_WIRE_BYTES, parseManagementResponse } from '@/hosted/management';
import type { ManagementRequest, ManagementResponse } from '@/hosted/management';

export interface HostOperations {
  'management.request': ManagementRequest;
  'management.transaction': { requests: ManagementRequest[]; expected_revision: string };
  'browser.open': { url: string };
  'browser.copy': { text: string };
  'file.save': { filename: string; content_type: string; base64: string };
  'plugin.open': { plugin_id: string; menu_index: number };
  'session.lock': Record<string, never>;
}
export type HostFailureKind = 'unavailable' | 'consent-expired' | 'outcome-unknown';
export class HostRequestError extends Error {
  constructor(readonly kind: HostFailureKind, message: string) {
    super(message);
    this.name = 'HostRequestError';
  }
}

type Pending = {
  resolve: (value: unknown) => void;
  reject: (error: HostRequestError) => void;
  timer: ReturnType<typeof setTimeout>;
  effectful: boolean;
  bytes: number;
};
const OUTCOME_UNKNOWN = 'Outcome unknown; reconcile in the host without resubmitting';
const HOST_BUSY = 'The host was busy and did not forward the request; nothing changed. Try again';
const MAX_PENDING_BYTES = 64 * 1024 * 1024;

/** One document, one unlocked port, no reconnect or mutation replay. */
export class HostedTransport {
  readonly clientNonce = crypto.randomUUID();
  private sessionId: string | undefined;
  private port: MessagePort | undefined;
  private state: 'connecting' | 'unlocked' | 'closed' = 'connecting';
  private sequence = 0;
  private pending = new Map<string, Pending>();
  private pendingBytes = 0;
  private readyResolve!: () => void;
  private readyReject!: (error: HostRequestError) => void;
  readonly ready = new Promise<void>((resolve, reject) => {
    this.readyResolve = resolve;
    this.readyReject = reject;
  });
  private handshakeTimer: ReturnType<typeof setTimeout>;

  constructor(
    private readonly onClose: () => void,
    private readonly timeoutMs = 600_000,
    handshakeTimeoutMs = 10_000
  ) {
    // A caller may attach before awaiting ready. Keep an early close from becoming unhandled.
    void this.ready.catch(() => {});
    this.handshakeTimer = setTimeout(() => this.close(), handshakeTimeoutMs);
  }

  get unlocked(): boolean { return this.state === 'unlocked'; }

  /** Called only after the window listener verifies the exact parent and transferred port. */
  acceptInitialization(data: unknown, channel: MessagePort): boolean {
    if (this.state !== 'connecting' || this.port || !isRecord(data) ||
        data.type !== 'hermes:cpa:init' || data.protocol !== 3 ||
        data.client_nonce !== this.clientNonce || data.unlocked !== true ||
        typeof data.session_id !== 'string' || data.session_id.length < 16 ||
        data.session_id.length > 128) {
      channel.close();
      return false;
    }
    this.sessionId = data.session_id;
    this.port = channel;
    channel.onmessageerror = () => this.close();
    channel.onmessage = ({ data: message }: MessageEvent<unknown>) => {
      if (!isRecord(message) || message.protocol !== 3 || message.session_id !== this.sessionId) {
        return;
      }
      if (this.state === 'connecting') {
        if (message.type !== 'hermes:cpa:ready' || message.client_nonce !== this.clientNonce ||
            message.unlocked !== true) {
          this.close();
          return;
        }
        clearTimeout(this.handshakeTimer);
        this.state = 'unlocked';
        channel.postMessage({
          type: 'hermes:cpa:ack', protocol: 3,
          session_id: this.sessionId, client_nonce: this.clientNonce,
        });
        this.readyResolve();
        return;
      }
      if (message.type === 'hermes:cpa:locked') {
        this.close();
        return;
      }
      if (this.state !== 'unlocked' || message.type !== 'hermes:cpa:response' ||
          typeof message.id !== 'string') return;
      const item = this.pending.get(message.id);
      if (!item) return;
      let bytes: number;
      try { bytes = new TextEncoder().encode(JSON.stringify(message)).length; }
      catch { this.close(); return; }
      if (bytes > MAX_WIRE_BYTES) { this.close(); return; }
      clearTimeout(item.timer);
      this.pending.delete(message.id);
      this.pendingBytes -= item.bytes;
      if (message.ok === true && 'result' in message) {
        item.resolve(message.result);
      } else if (message.ok === false && isRecord(message.error)) {
        const code = message.error.code;
        const unknown = code === 'RECONCILE_REQUIRED' || code === 'OUTCOME_UNKNOWN';
        item.reject(new HostRequestError(
          unknown ? 'outcome-unknown' : code === 'CONSENT_EXPIRED' ? 'consent-expired' : 'unavailable',
          unknown ? OUTCOME_UNKNOWN : code === 'HOST_BUSY' ? HOST_BUSY : 'Host request was not completed'
        ));
        if (unknown) this.close();
      } else {
        item.reject(new HostRequestError(item.effectful ? 'outcome-unknown' : 'unavailable',
          item.effectful ? OUTCOME_UNKNOWN : 'Invalid host response'));
        this.close();
      }
    };
    channel.start();
    channel.postMessage({
      type: 'hermes:cpa:hello', protocol: 3,
      session_id: this.sessionId, client_nonce: this.clientNonce,
    });
    return true;
  }

  async request<K extends keyof HostOperations>(operation: K, payload: HostOperations[K]): Promise<unknown> {
    await this.ready;
    const channel = this.port;
    if (this.state !== 'unlocked' || !channel) {
      throw new HostRequestError('unavailable', 'Console is locked');
    }
    const id = String(++this.sequence);
    const message = {
      type: 'hermes:cpa:request', protocol: 3, session_id: this.sessionId, id, operation, payload,
    };
    const bytes = new TextEncoder().encode(JSON.stringify(message)).length;
    if (bytes > MAX_WIRE_BYTES || this.pending.size >= 32 ||
        this.pendingBytes + bytes > MAX_PENDING_BYTES) {
      throw new HostRequestError('unavailable', 'Host request exceeds the transport capacity');
    }
    const effectful = operation !== 'management.request' ||
      ('method' in payload && (payload.method !== 'GET' || payload.path.endsWith('/oauth/auth-url')));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        this.pendingBytes -= bytes;
        reject(new HostRequestError(effectful ? 'outcome-unknown' : 'unavailable',
          effectful ? OUTCOME_UNKNOWN : 'Host request timed out'));
        this.close();
      }, this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer, effectful, bytes });
      this.pendingBytes += bytes;
      try { channel.postMessage(message); }
      catch { this.close(); }
    });
  }

  close(): void {
    if (this.state === 'closed') return;
    this.state = 'closed';
    clearTimeout(this.handshakeTimer);
    this.port?.close();
    this.port = undefined;
    this.readyReject(new HostRequestError('unavailable', 'Host unavailable or console locked'));
    for (const item of this.pending.values()) {
      clearTimeout(item.timer);
      item.reject(new HostRequestError(item.effectful ? 'outcome-unknown' : 'unavailable',
        item.effectful ? OUTCOME_UNKNOWN : 'Console is locked'));
    }
    this.pending.clear();
    this.pendingBytes = 0;
    this.onClose();
  }
}

let transport: HostedTransport | undefined;
export function getHostedTransport(): HostedTransport {
  if (!isHosted() || typeof window === 'undefined' || window.parent === window) {
    throw new HostRequestError('unavailable', 'Trusted console host is required');
  }
  if (!transport) {
    const host = window.parent;
    const listener = (event: MessageEvent<unknown>) => {
      if (event.source !== host || event.ports.length !== 1) return;
      transport?.acceptInitialization(event.data, event.ports[0]);
    };
    transport = new HostedTransport(() => {
      window.removeEventListener('message', listener);
      clearHostedStorage();
      window.dispatchEvent(new Event('hermes-console-locked'));
    });
    window.addEventListener('message', listener);
    window.addEventListener('pagehide', () => transport?.close(), { once: true });
    host.postMessage({
      type: 'hermes:cpa:connect', protocol: 3, client_nonce: transport.clientNonce,
    }, '*');
  }
  return transport;
}

export async function managementRequest(payload: ManagementRequest): Promise<ManagementResponse> {
  const channel = getHostedTransport();
  const value = await channel.request('management.request', payload);
  try {
    return parseManagementResponse(value);
  } catch {
    channel.close();
    const effectful = payload.method !== 'GET' || payload.path.endsWith('/oauth/auth-url');
    throw new HostRequestError(effectful ? 'outcome-unknown' : 'unavailable',
      effectful ? OUTCOME_UNKNOWN : 'Invalid management response');
  }
}

export async function managementTransaction(
  requests: ManagementRequest[], expectedRevision: string
): Promise<ManagementResponse> {
  const channel = getHostedTransport();
  const value = await channel.request('management.transaction', {
    requests, expected_revision: expectedRevision,
  });
  try {
    return parseManagementResponse(value);
  } catch {
    channel.close();
    throw new HostRequestError('outcome-unknown', OUTCOME_UNKNOWN);
  }
}

/** Revoke local access immediately, even if the host lock acknowledgement is lost. */
export async function lockHostedSession(): Promise<void> {
  const channel = getHostedTransport();
  try {
    const pending = channel.request('session.lock', {}).catch(() => undefined);
    // request() posts after its ready microtask, before this continuation closes the port.
    await Promise.resolve();
    channel.close();
    await pending;
  } catch {
    channel.close();
  }
}
